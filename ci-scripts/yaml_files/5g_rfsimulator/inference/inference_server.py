"""
inference_server.py — Local xApp Python 推論伺服器（共用核心邏輯）

架構角色：
  - 監聽 ZMQ REP socket，接收 C xApp 每 10ms 送來的 UE 狀態
  - 執行 PRB 分配推論，並在 5ms 內回傳結果
  - 非同步批次寫入 MongoDB，為聯邦學習準備歷史資料集

ZMQ 協定：
  接收 (C → Python):
    {"node_id": XXXX, "ues": [{"rnti": X, "bsr": Y, "wb_cqi": Z}, ...]}
  回傳 (Python → C):
    {"allocations": [{"rnti": X, "prb_abs": N}, ...]}

演進路徑：
  Phase 3 (當前)：BSR 比例加權啟發式推論
  Phase 4        ：替換 _infer() 為 DRL Actor Network
  Phase 5        ：新增 Flower Client FL 訓練迴圈
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pymongo
import zmq

# =============================================================================
# 全域常數
# =============================================================================

TOTAL_PRB_COUNT: int = 106   # 必須與 C xApp 的 TOTAL_PRB_COUNT 一致
MIN_PRB_PER_UE: int = 2      # 每個 UE 的最小 PRB 保底，防止餓死
ZMQ_POLL_MS: int = 100       # poller 超時，允許 server 優雅退出
MONGO_FLUSH_INTERVAL: float = 1.0   # 批次寫入 MongoDB 的間隔（秒）
INFERENCE_WARN_MS: float = 4.0      # 推論延遲警告閾值（ms）


# =============================================================================
# InferenceServer
# =============================================================================

class InferenceServer:
    """
    ZeroMQ REP 推論伺服器。

    每個 IAB Node 有一個獨立實例，監聽專屬 IPC socket，
    執行 PRB 分配推論並將 (state, action) 寫入 MongoDB。
    """

    def __init__(
        self,
        node_id: int,
        zmq_endpoint: str,
        mongo_uri: str = "mongodb://localhost:27017",
        mongo_db: str = "iab_xapp",
        total_prb: int = TOTAL_PRB_COUNT,
    ) -> None:
        self.node_id = node_id
        self.zmq_endpoint = zmq_endpoint
        self.total_prb = total_prb
        self._running = False

        # ZMQ
        self._zmq_ctx: zmq.Context | None = None
        self._zmq_sock: zmq.Socket | None = None

        # MongoDB
        self._mongo_uri = mongo_uri
        self._mongo_db = mongo_db
        self._mongo_col: pymongo.collection.Collection | None = None
        self._mongo_client: pymongo.MongoClient | None = None

        # 批次寫入緩衝區（主迴圈寫入，背景執行緒讀取）
        self._write_buffer: list[dict[str, Any]] = []
        self._write_lock = threading.Lock()
        self._flush_thread: threading.Thread | None = None

        # 設定日誌格式，前綴標示節點編號方便辨識
        logging.basicConfig(
            level=logging.INFO,
            format=f"[Node{node_id} PY] %(asctime)s %(levelname)s %(message)s",
            datefmt="%H:%M:%S",
        )
        self._log = logging.getLogger(f"node{node_id}")

    # -------------------------------------------------------------------------
    # 初始化
    # -------------------------------------------------------------------------

    def _init_zmq(self) -> None:
        """建立並綁定 ZMQ REP socket。"""
        self._zmq_ctx = zmq.Context()
        self._zmq_sock = self._zmq_ctx.socket(zmq.REP)
        # LINGER=0：關閉時立即丟棄未送訊息，避免程式結束時卡住
        self._zmq_sock.setsockopt(zmq.LINGER, 0)
        self._zmq_sock.bind(self.zmq_endpoint)
        self._log.info("ZMQ REP socket 已綁定至 %s", self.zmq_endpoint)

    def _init_mongo(self) -> None:
        """建立 MongoDB 連線；失敗時降級為不持久化模式。"""
        try:
            self._mongo_client = pymongo.MongoClient(
                self._mongo_uri, serverSelectionTimeoutMS=3000
            )
            # 觸發實際連線以驗證可達性
            self._mongo_client.server_info()
            db = self._mongo_client[self._mongo_db]
            self._mongo_col = db[f"node{self.node_id}_experiences"]
            # 為後續 FL 查詢建立索引
            self._mongo_col.create_index("timestamp")
            self._log.info(
                "MongoDB 已連線: %s/%s/node%d_experiences",
                self._mongo_uri, self._mongo_db, self.node_id,
            )
        except pymongo.errors.PyMongoError as exc:
            self._log.warning("MongoDB 連線失敗 (%s)，資料將不會持久化", exc)
            self._mongo_col = None

    # -------------------------------------------------------------------------
    # MongoDB 批次寫入（背景執行緒）
    # -------------------------------------------------------------------------

    def _flush_worker(self) -> None:
        """背景執行緒：定期將緩衝區資料批次寫入 MongoDB。"""
        while self._running:
            time.sleep(MONGO_FLUSH_INTERVAL)
            self._flush_to_mongo()

    def _flush_to_mongo(self) -> None:
        """將緩衝區中的所有文件一次性寫入 MongoDB。"""
        if self._mongo_col is None:
            return
        with self._write_lock:
            if not self._write_buffer:
                return
            batch = self._write_buffer[:]
            self._write_buffer.clear()
        try:
            self._mongo_col.insert_many(batch, ordered=False)
            self._log.debug("MongoDB 批次寫入 %d 筆", len(batch))
        except pymongo.errors.PyMongoError as exc:
            self._log.warning("MongoDB 批次寫入失敗: %s", exc)

    def _queue_experience(
        self,
        state: list[dict[str, Any]],
        action: list[dict[str, Any]],
    ) -> None:
        """
        將一筆 (state, action) 放入寫入緩衝區。

        Schema：
          node_id   : int       — 節點識別
          timestamp : datetime  — UTC 時間戳
          state     : list      — [{"rnti", "bsr", "wb_cqi"}, ...]
          action    : list      — [{"rnti", "prb_abs"}, ...]
        """
        doc: dict[str, Any] = {
            "node_id":   self.node_id,
            "timestamp": datetime.now(timezone.utc),
            "state":     state,
            "action":    action,
        }
        with self._write_lock:
            self._write_buffer.append(doc)

    # -------------------------------------------------------------------------
    # 推論邏輯
    # -------------------------------------------------------------------------

    def _infer(self, ues: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        PRB 分配推論。

        【Phase 3 — BSR 比例加權啟發式】
          1. 以 BSR 反映各 UE 的排隊資料量作為基礎權重。
          2. 以 CQI 反映通道品質進行輕微加成（最高 +20%），
             讓通道較好的 UE 能更有效率地利用分配到的 PRB。
          3. 每個 UE 保底 MIN_PRB_PER_UE 個 PRB，防止低 BSR UE 餓死。
          4. 捨入後將剩餘 PRB 補給分數最高的 UE，確保總量不超過 TOTAL_PRB。

        【Phase 4 替換點】
          將本函式替換為 DRL Actor Network 推論：
            state_vec = self._build_state_tensor(ues)
            with torch.no_grad():
                action_vec = self.actor(state_vec)
            return self._decode_action(action_vec, ues)

        參數：
          ues : list of {"rnti": int, "bsr": int, "wb_cqi": int}

        回傳：
          list of {"rnti": int, "prb_abs": int}
        """
        n = len(ues)
        if n == 0:
            return []

        # ── 計算加權分數 ───────────────────────────────────────────────────
        # BSR 保底為 1.0 避免全零時除法錯誤
        # wb_cqi 範圍 [0, 15]，加成幅度最高 20%
        scores = np.array(
            [
                max(float(ue.get("bsr", 0)), 1.0)
                * (1.0 + float(ue.get("wb_cqi", 7)) / 15.0 * 0.2)
                for ue in ues
            ],
            dtype=np.float32,
        )

        # ── PRB 分配 ──────────────────────────────────────────────────────
        # 保留每個 UE 的最小配額後，剩餘 PRB 依比例分配
        reserved = MIN_PRB_PER_UE * n
        available = max(self.total_prb - reserved, 0)
        ratios = scores / scores.sum()
        prb_extra = (ratios * available).astype(np.int32)

        # 修正捨入誤差：多餘 PRB 補給分數最高的 UE
        remainder = int(available - prb_extra.sum())
        if remainder > 0:
            prb_extra[int(np.argmax(ratios))] += remainder

        allocations = [
            {
                "rnti":    int(ues[i]["rnti"]),
                "prb_abs": int(MIN_PRB_PER_UE + prb_extra[i]),
            }
            for i in range(n)
        ]
        return allocations

    # -------------------------------------------------------------------------
    # 主迴圈
    # -------------------------------------------------------------------------

    def run(self) -> None:
        """啟動推論伺服器，阻塞直到收到 KeyboardInterrupt 或呼叫 stop()。"""
        self._init_zmq()
        self._init_mongo()

        self._running = True
        self._flush_thread = threading.Thread(
            target=self._flush_worker,
            daemon=True,
            name=f"mongo-flush-node{self.node_id}",
        )
        self._flush_thread.start()

        self._log.info(
            "Node %d 推論伺服器啟動，等待 C xApp 請求...", self.node_id
        )

        assert self._zmq_sock is not None
        poller = zmq.Poller()
        poller.register(self._zmq_sock, zmq.POLLIN)

        try:
            while self._running:
                # poll 帶超時，確保 _running=False 時能離開迴圈
                events = dict(poller.poll(ZMQ_POLL_MS))
                if self._zmq_sock not in events:
                    continue

                # ── 接收請求 ──────────────────────────────────────────────
                raw: str = self._zmq_sock.recv_string()
                t_recv = time.perf_counter()

                try:
                    payload: dict[str, Any] = json.loads(raw)
                    ues: list[dict[str, Any]] = payload.get("ues", [])

                    # ── 執行推論 ──────────────────────────────────────────
                    allocations = self._infer(ues)

                    # ── 回傳結果（必須在 5ms 內完成）────────────────────
                    response = json.dumps(
                        {"allocations": allocations}, separators=(",", ":")
                    )
                    self._zmq_sock.send_string(response)

                    # ── 延遲監控 ──────────────────────────────────────────
                    elapsed_ms = (time.perf_counter() - t_recv) * 1000
                    if elapsed_ms > INFERENCE_WARN_MS:
                        self._log.warning(
                            "推論延遲 %.2fms 接近 5ms 上限，請檢查負載", elapsed_ms
                        )

                    # ── 非同步寫入 MongoDB ────────────────────────────────
                    if ues and allocations:
                        self._queue_experience(ues, allocations)

                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                    self._log.error("請求解析失敗: %s，回傳空分配", exc)
                    # 即使解析失敗也必須回傳 reply，否則 REQ socket 會卡住
                    self._zmq_sock.send_string('{"allocations":[]}')

        except KeyboardInterrupt:
            self._log.info("收到中斷訊號，正在關閉...")
        finally:
            self._shutdown()

    def stop(self) -> None:
        """從外部執行緒請求停止主迴圈。"""
        self._running = False

    def _shutdown(self) -> None:
        """釋放所有資源。"""
        self._running = False
        # 最後一次強制寫入，避免遺失緩衝中的資料
        self._flush_to_mongo()
        if self._zmq_sock is not None:
            self._zmq_sock.close()
        if self._zmq_ctx is not None:
            self._zmq_ctx.term()
        if self._mongo_client is not None:
            self._mongo_client.close()
        self._log.info("Node %d 推論伺服器已關閉", self.node_id)
