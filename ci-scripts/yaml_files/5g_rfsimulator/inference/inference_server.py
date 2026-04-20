"""
inference_server.py — Local xApp Python 推論伺服器（Phase 4 DRL 版本）

架構角色：
  - 監聽 ZMQ REP socket，接收 C xApp 每 10ms 送來的 UE 狀態
  - 執行 PRB 分配推論，並在 5ms 內回傳結果
  - 計算複合獎勵函數，建構完整的 (s, a, r, s') 強化學習經驗
  - 非同步批次寫入 MongoDB，供訓練執行緒讀取
  - 背景執行緒定期從 MongoDB 讀取經驗，執行 Actor-Critic 離線訓練

推論策略（漸進切換）：
  Phase 3 啟發式 (BSR 比例加權) ← 冷啟動 or DRL 尚未收集足夠資料時
         ↓  收集 MIN_TRAIN_EXPERIENCES 筆資料後首次訓練
  Phase 4 DRL Actor Network     ← 訓練完成後自動切換
  (任一步若推論超時仍 Fallback 至啟發式，確保系統穩定)

ZMQ 協定：
  接收 (C → Python):
    {"node_id": XXXX, "ues": [{"rnti": X, "bsr": Y, "wb_cqi": Z}, ...]}
  回傳 (Python → C):
    {"allocations": [{"rnti": X, "prb_abs": N}, ...]}
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np
import pymongo
import zmq

from drl_agent import DRLAgent, MIN_TRAIN_EXPERIENCES, MAX_UE_COUNT
from reward_calculator import compute_reward, compute_reward_breakdown

# =============================================================================
# 全域常數
# =============================================================================

TOTAL_PRB_COUNT: int = 106          # 必須與 C xApp 的 TOTAL_PRB_COUNT 一致
MIN_PRB_PER_UE: int = 2             # BSR 啟發式：每個 UE 的最小 PRB 保底
ZMQ_POLL_MS: int = 100              # ZMQ poller 超時，允許優雅退出
MONGO_FLUSH_INTERVAL: float = 1.0   # 批次寫入 MongoDB 的間隔（秒）
INFERENCE_WARN_MS: float = 4.0      # 推論延遲警告閾值（ms）
TRAIN_INTERVAL_S: float = 60.0      # DRL 背景訓練間隔（秒）
TRAIN_FETCH_LIMIT: int = 2000       # 每次從 MongoDB 讀取的最多筆數


# =============================================================================
# InferenceServer
# =============================================================================

class InferenceServer:
    """
    ZeroMQ REP 推論伺服器，整合 DRL Actor-Critic 閉環控制。

    每個 IAB Node 有一個獨立實例，監聽專屬 IPC socket，
    執行 PRB 分配推論並將完整的 RL 經驗寫入 MongoDB。

    RL 經驗建構流程（S, A, R, S' 四元組）：
      step t：接收 S_t，若 t>0 則以 S_t 作為 S'_{t-1} 計算獎勵 R_{t-1}
              → 完成上一筆經驗寫入 MongoDB
              → 對 S_t 執行推論，得到 A_t
              → 儲存 (S_t, A_t) 等待下一步
    """

    def __init__(
        self,
        node_id: int,
        zmq_endpoint: str,
        mongo_uri: str = "mongodb://localhost:27017",
        mongo_db: str = "iab_xapp",
        total_prb: int = TOTAL_PRB_COUNT,
        model_dir: str = "/app/models",
    ) -> None:
        self.node_id = node_id
        self.zmq_endpoint = zmq_endpoint
        self.total_prb = total_prb
        self._running = False

        # ZMQ
        self._zmq_ctx: Optional[zmq.Context] = None
        self._zmq_sock: Optional[zmq.Socket] = None

        # MongoDB
        self._mongo_uri = mongo_uri
        self._mongo_db = mongo_db
        self._mongo_col: Optional[pymongo.collection.Collection] = None
        self._mongo_client: Optional[pymongo.MongoClient] = None

        # 批次寫入緩衝區（主迴圈寫入，背景執行緒讀取）
        self._write_buffer: list[dict[str, Any]] = []
        self._write_lock = threading.Lock()
        self._flush_thread: Optional[threading.Thread] = None
        self._train_thread: Optional[threading.Thread] = None

        # DRL Agent（每個節點獨立實例）
        self._agent = DRLAgent(
            node_id=node_id,
            model_dir=model_dir,
            total_prb=total_prb,
        )

        # RL 狀態轉移暫存（用於計算 R(S_{t-1}, A_{t-1}, S_t)）
        self._prev_ues: Optional[list[dict]] = None
        self._prev_allocations: Optional[list[dict]] = None
        self._prev_state_vec: Optional[np.ndarray] = None
        self._prev_mask_vec: Optional[np.ndarray] = None
        self._prev_action_ratios: Optional[np.ndarray] = None

        # 統計計數器
        self._total_inferences: int = 0
        self._drl_inferences: int = 0
        self._heuristic_inferences: int = 0

        # 設定日誌格式
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
        self._zmq_sock.setsockopt(zmq.LINGER, 0)
        self._zmq_sock.bind(self.zmq_endpoint)
        self._log.info("ZMQ REP socket 已綁定至 %s", self.zmq_endpoint)

    def _init_mongo(self) -> None:
        """建立 MongoDB 連線；失敗時降級為不持久化模式。"""
        try:
            self._mongo_client = pymongo.MongoClient(
                self._mongo_uri, serverSelectionTimeoutMS=3000
            )
            self._mongo_client.server_info()
            db = self._mongo_client[self._mongo_db]
            self._mongo_col = db[f"node{self.node_id}_experiences"]
            # 建立查詢索引：時間戳（批次訓練讀取用）
            self._mongo_col.create_index("timestamp")
            # 建立複合索引：reward 欄位存在性（篩選完整 RL 經驗用）
            self._mongo_col.create_index("reward")
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

    def _queue_experience(self, doc: dict[str, Any]) -> None:
        """將一筆文件放入寫入緩衝區（執行緒安全）。"""
        with self._write_lock:
            self._write_buffer.append(doc)

    # -------------------------------------------------------------------------
    # DRL 背景訓練（背景執行緒）
    # -------------------------------------------------------------------------

    def _train_worker(self) -> None:
        """
        背景訓練執行緒：每 TRAIN_INTERVAL_S 秒從 MongoDB 讀取經驗並觸發訓練。

        只讀取含有完整 RL 欄位（reward, next_state_vec）的文件，
        跳過早期只有 state/action 的 Phase 3 文件。
        """
        # 等待一個完整訓練間隔後才開始，讓系統先累積足夠經驗
        time.sleep(TRAIN_INTERVAL_S)

        while self._running:
            try:
                self._run_training_round()
            except Exception as exc:
                self._log.warning("訓練執行緒例外: %s", exc)
            time.sleep(TRAIN_INTERVAL_S)

    def _run_training_round(self) -> None:
        """執行一輪訓練：讀取 MongoDB → 訓練 → 儲存模型。"""
        if self._mongo_col is None:
            return

        # 先強制寫入緩衝區，確保最新資料可被讀到
        self._flush_to_mongo()

        # 查詢含完整 RL 欄位的文件
        try:
            cursor = (
                self._mongo_col
                .find(
                    {"reward": {"$exists": True}, "next_state_vec": {"$exists": True}},
                    projection={
                        "state_vec": 1, "mask_vec": 1, "action_ratios": 1,
                        "reward": 1, "next_state_vec": 1, "next_mask_vec": 1,
                        "_id": 0,
                    },
                )
                .sort("timestamp", pymongo.DESCENDING)
                .limit(TRAIN_FETCH_LIMIT)
            )
            experiences = list(cursor)
        except pymongo.errors.PyMongoError as exc:
            self._log.warning("讀取訓練資料失敗: %s", exc)
            return

        n = len(experiences)
        self._log.info("讀取到 %d 筆 RL 經驗，準備訓練", n)

        if n < MIN_TRAIN_EXPERIENCES:
            self._log.info(
                "經驗數量不足 (需 %d 筆)，等待更多資料累積...",
                MIN_TRAIN_EXPERIENCES,
            )
            return

        metrics = self._agent.train_on_batch(experiences)
        if metrics:
            self._agent.save()
            self._log.info(
                "訓練完成 | DRL 推論模式: %s | "
                "DRL/啟發式 = %d/%d",
                self._agent.is_trained,
                self._drl_inferences,
                self._heuristic_inferences,
            )

    # -------------------------------------------------------------------------
    # 推論邏輯
    # -------------------------------------------------------------------------

    def _infer_heuristic(self, ues: list[dict]) -> tuple[list[dict], np.ndarray]:
        """
        Phase 3 BSR 比例加權啟發式推論（冷啟動 Fallback）。

        PRB 分配策略：
          1. 以 BSR 反映各 UE 的排隊資料量作為基礎權重。
          2. 以 CQI 反映通道品質進行輕微加成（最高 +20%）。
          3. 每個 UE 保底 MIN_PRB_PER_UE 個 PRB，防止低 BSR UE 餓死。
          4. 捨入後將剩餘 PRB 補給分數最高的 UE。

        Returns:
            (allocations, action_ratios)
        """
        n = len(ues)
        if n == 0:
            return [], np.zeros(MAX_UE_COUNT, dtype=np.float32)

        scores = np.array(
            [
                max(float(ue.get("bsr", 0)), 1.0)
                * (1.0 + float(ue.get("wb_cqi", 7)) / 15.0 * 0.2)
                for ue in ues
            ],
            dtype=np.float32,
        )

        reserved = MIN_PRB_PER_UE * n
        available = max(self.total_prb - reserved, 0)
        ratios = scores / scores.sum()
        prb_extra = (ratios * available).astype(np.int32)

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

        # 建立 action_ratios 向量
        action_ratios = np.zeros(MAX_UE_COUNT, dtype=np.float32)
        for i in range(n):
            action_ratios[i] = allocations[i]["prb_abs"] / self.total_prb

        return allocations, action_ratios

    def _infer(self, ues: list[dict]) -> tuple[list[dict], np.ndarray]:
        """
        執行推論並回傳 (allocations, action_ratios)。

        策略：
          - DRL 訓練完成 → 使用 DRL Actor Network
          - 尚未完成首次訓練 → 使用 BSR 啟發式（同時積累訓練資料）
        """
        use_drl = self._agent.is_trained

        if use_drl:
            try:
                allocations, action_ratios = self._agent.infer(ues)
                self._drl_inferences += 1
                return allocations, action_ratios
            except Exception as exc:
                self._log.warning("DRL 推論失敗: %s，退回啟發式", exc)

        # BSR 啟發式（Phase 3 或 Fallback）
        allocations, action_ratios = self._infer_heuristic(ues)
        self._heuristic_inferences += 1
        return allocations, action_ratios

    def _build_rl_experience(
        self,
        prev_ues: list[dict],
        prev_allocations: list[dict],
        prev_state_vec: np.ndarray,
        prev_mask_vec: np.ndarray,
        prev_action_ratios: np.ndarray,
        curr_ues: list[dict],
    ) -> dict[str, Any]:
        """
        建構一筆完整的 RL 經驗文件 (S, A, R, S')。

        reward 以上一步的 (state, action) 與當前 state 計算，
        實現 one-step TD 結構。
        """
        # 計算獎勵：R(S_{t-1}, A_{t-1}, S_t)
        reward = compute_reward(prev_ues, prev_allocations, self.total_prb)

        # 編碼當前狀態 S_t（作為 S' ）
        next_state_vec, next_mask_vec = self._agent.encode_state(curr_ues)

        # 獎勵細節（用於 MongoDB 監控）
        breakdown = compute_reward_breakdown(
            prev_ues, prev_allocations, self.total_prb
        )

        doc: dict[str, Any] = {
            "node_id":       self.node_id,
            "timestamp":     datetime.now(timezone.utc),
            # 狀態（原始格式，供人工分析）
            "state":         prev_ues,
            "action":        prev_allocations,
            # RL 訓練所需的向量格式
            "state_vec":     prev_state_vec.tolist(),
            "mask_vec":      prev_mask_vec.tolist(),
            "action_ratios": prev_action_ratios.tolist(),
            "reward":        reward,
            "next_state_vec": next_state_vec.tolist(),
            "next_mask_vec":  next_mask_vec.tolist(),
            # 獎勵分解（監控用）
            "r_throughput":  breakdown["r_throughput"],
            "r_fairness":    breakdown["r_fairness"],
            "r_delay":       breakdown["r_delay"],
            # 推論模式（供事後分析）
            "used_drl":      self._agent.is_trained,
        }
        return doc

    # -------------------------------------------------------------------------
    # 主迴圈
    # -------------------------------------------------------------------------

    def run(self) -> None:
        """啟動推論伺服器，阻塞直到 KeyboardInterrupt。"""
        self._init_zmq()
        self._init_mongo()

        # 嘗試載入預存模型（讓重啟後不從頭訓練）
        self._agent.load()

        self._running = True

        # 背景執行緒 1：MongoDB 批次寫入
        self._flush_thread = threading.Thread(
            target=self._flush_worker,
            daemon=True,
            name=f"mongo-flush-node{self.node_id}",
        )
        self._flush_thread.start()

        # 背景執行緒 2：DRL 離線訓練
        self._train_thread = threading.Thread(
            target=self._train_worker,
            daemon=True,
            name=f"drl-train-node{self.node_id}",
        )
        self._train_thread.start()

        self._log.info(
            "Node %d 推論伺服器啟動 | 初始模式: %s | 等待 C xApp 請求...",
            self.node_id,
            "DRL" if self._agent.is_trained else "BSR 啟發式（收集資料中）",
        )

        assert self._zmq_sock is not None
        poller = zmq.Poller()
        poller.register(self._zmq_sock, zmq.POLLIN)

        try:
            while self._running:
                events = dict(poller.poll(ZMQ_POLL_MS))
                if self._zmq_sock not in events:
                    continue

                # ── 接收 C xApp 請求 ──────────────────────────────────────
                raw: str = self._zmq_sock.recv_string()
                t_recv = time.perf_counter()

                try:
                    payload: dict[str, Any] = json.loads(raw)
                    ues: list[dict[str, Any]] = payload.get("ues", [])

                    # ── 狀態 debug log（每 500 次）────────────────────────
                    if self._total_inferences % 500 == 0 and ues:
                        ue_summary = " ".join(
                            f"rnti={u.get('rnti',0)} bsr={u.get('bsr',0)} cqi={u.get('wb_cqi',0)}"
                            for u in ues
                        )
                        self._log.info("STATE[%d] %s", self._total_inferences, ue_summary)

                    # ── 計算上一步的獎勵並寫入 MongoDB ────────────────────
                    if (
                        self._prev_ues is not None
                        and self._prev_allocations is not None
                        and len(ues) > 0
                    ):
                        exp_doc = self._build_rl_experience(
                            prev_ues=self._prev_ues,
                            prev_allocations=self._prev_allocations,
                            prev_state_vec=self._prev_state_vec,
                            prev_mask_vec=self._prev_mask_vec,
                            prev_action_ratios=self._prev_action_ratios,
                            curr_ues=ues,
                        )
                        self._queue_experience(exp_doc)

                    # ── 執行推論 ──────────────────────────────────────────
                    allocations, action_ratios = self._infer(ues)
                    self._total_inferences += 1

                    # ── 暫存本步狀態（下一步計算獎勵用）─────────────────
                    if ues and allocations:
                        self._prev_ues = ues
                        self._prev_allocations = allocations
                        self._prev_state_vec, self._prev_mask_vec = (
                            self._agent.encode_state(ues)
                        )
                        self._prev_action_ratios = action_ratios
                    else:
                        # 無活躍 UE 時清除暫存，避免跨不同 UE 組合計算獎勵
                        self._prev_ues = None
                        self._prev_allocations = None

                    # ── 回傳結果（必須在 5ms 內完成）────────────────────
                    response = json.dumps(
                        {"allocations": allocations}, separators=(",", ":")
                    )
                    self._zmq_sock.send_string(response)

                    # ── 延遲監控 ──────────────────────────────────────────
                    elapsed_ms = (time.perf_counter() - t_recv) * 1000
                    if elapsed_ms > INFERENCE_WARN_MS:
                        self._log.warning(
                            "推論延遲 %.2fms 接近 5ms 上限 | "
                            "DRL=%s | UE 數=%d",
                            elapsed_ms,
                            self._agent.is_trained,
                            len(ues),
                        )

                    # ── 定期列印統計 (每 1000 次推論) ────────────────────
                    if self._total_inferences % 1000 == 0:
                        self._log.info(
                            "推論統計 | 總計=%d | DRL=%d | 啟發式=%d",
                            self._total_inferences,
                            self._drl_inferences,
                            self._heuristic_inferences,
                        )

                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                    self._log.error("請求解析失敗: %s，回傳空分配", exc)
                    # 即使失敗也必須回傳 reply，否則 REQ socket 會卡住
                    self._zmq_sock.send_string('{"allocations":[]}')

        except KeyboardInterrupt:
            self._log.info("收到中斷訊號，正在關閉...")
        finally:
            self._shutdown()

    def stop(self) -> None:
        """從外部執行緒請求停止主迴圈。"""
        self._running = False

    def _shutdown(self) -> None:
        """依序關閉所有資源。"""
        self._running = False

        # 最後一次強制寫入緩衝區
        self._flush_to_mongo()

        # 關閉前儲存模型（保留訓練進度）
        if self._agent.is_trained:
            try:
                self._agent.save()
            except Exception as exc:
                self._log.warning("關閉時模型儲存失敗: %s", exc)

        if self._zmq_sock is not None:
            self._zmq_sock.close()
        if self._zmq_ctx is not None:
            self._zmq_ctx.term()
        if self._mongo_client is not None:
            self._mongo_client.close()

        self._log.info(
            "Node %d 推論伺服器已關閉 | "
            "總推論次數=%d | DRL=%d | 啟發式=%d",
            self.node_id,
            self._total_inferences,
            self._drl_inferences,
            self._heuristic_inferences,
        )
