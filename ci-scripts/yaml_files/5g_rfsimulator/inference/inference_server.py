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
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pymongo
import zmq

from drl_agent import DRLAgent, MAX_UE_COUNT
from reward_calculator import compute_reward, compute_reward_breakdown
from training_pipeline import run_training_round

# =============================================================================
# 全域常數
# =============================================================================

TOTAL_PRB_COUNT: int = 106          # 必須與 C xApp 的 TOTAL_PRB_COUNT 一致
MIN_PRB_PER_UE: int = 5             # BSR 啟發式：每個 UE 的最小 PRB 保底（與 DRL 路徑一致，防止 MAC 層 SIGSEGV）
ZMQ_POLL_MS: int = 100              # ZMQ poller 超時，允許優雅退出
MONGO_FLUSH_INTERVAL: float = 1.0   # 批次寫入 MongoDB 的間隔（秒）
INFERENCE_WARN_MS: float = 4.0      # 推論延遲警告閾值（ms）
TRAIN_INTERVAL_S: float = 60.0      # DRL 背景訓練間隔（秒）
TRAIN_FETCH_LIMIT: int = 2000       # 每次從 MongoDB 讀取的最多筆數
TRAIN_EPOCHS_PER_ROUND: int = 10    # 每輪訓練的梯度更新次數
EXPLORE_PROB: float = 0.30          # 啟發式階段 Dirichlet 隨機探索的比例
RELOAD_POLL_INTERVAL_S: float = 30.0  # 檢查磁碟 checkpoint 是否被 FL ClientApp 更新的輪詢間隔


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
        self._reload_thread: Optional[threading.Thread] = None

        # DRL Agent（每個節點獨立實例）
        self._agent = DRLAgent(
            node_id=node_id,
            model_dir=model_dir,
            total_prb=total_prb,
        )

        # 模型鎖：保護 agent 的 forward pass（ZMQ 主迴圈）、梯度更新
        # （_train_worker）、與磁碟熱重載（_reload_worker，Phase 5 FL ClientApp
        # 是獨立 subprocess，跟本進程只透過磁碟 checkpoint 同步，不共用記憶體）
        # 三方對同一組 actor/critic 權重的存取。刻意不把 MongoDB I/O 包進鎖裡，
        # 確保鎖持有時間維持在毫秒級，不影響 ZMQ 的 5ms 回應預算。
        self._model_lock = threading.Lock()
        self._last_ckpt_mtime: float = 0.0

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

        # Phase 5: Global xApp 整合（IAB 回傳配額軟性約束）。
        # Node1/2（relay）將自己的 PRB 分配透過 ZMQ PUB 廣播出去；
        # Node3/4/5（access）SUB 一個由獨立 bridge process（見
        # global_xapp_bridge.py）彙整 Node1/2 分配後算出的配額，
        # 以 effective_prb = min(106, quota) 限制自己實際可用的 PRB。
        self._is_relay: bool = node_id in (1, 2)
        self._alloc_pub_sock: Optional[zmq.Socket] = None   # Node1/2: publish allocs
        self._quota_sub_sock: Optional[zmq.Socket] = None   # Node3/4/5: receive quota
        self._prb_quota: int = total_prb                    # default: no restriction
        self._quota_lock = threading.Lock()
        self._quota_thread: Optional[threading.Thread] = None

        # 訓練保護：記錄上一輪訓練時的 MongoDB 筆數，無新資料則跳過
        self._last_train_mongo_count: int = 0

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

        # Phase 5: relay nodes publish allocations; access nodes receive quota
        if self._is_relay:
            alloc_port = 5560 + self.node_id  # Node1→5561, Node2→5562
            self._alloc_pub_sock = self._zmq_ctx.socket(zmq.PUB)
            self._alloc_pub_sock.setsockopt(zmq.LINGER, 0)
            self._alloc_pub_sock.setsockopt(zmq.SNDHWM, 5)
            self._alloc_pub_sock.bind(f"tcp://127.0.0.1:{alloc_port}")
            self._log.info("[Global] Alloc PUB bound: tcp://127.0.0.1:%d", alloc_port)
        else:
            self._quota_sub_sock = self._zmq_ctx.socket(zmq.SUB)
            self._quota_sub_sock.setsockopt(zmq.LINGER, 0)
            self._quota_sub_sock.setsockopt(zmq.RCVTIMEO, 500)
            self._quota_sub_sock.setsockopt_string(zmq.SUBSCRIBE, f"node{self.node_id}")
            self._quota_sub_sock.connect("tcp://127.0.0.1:5560")
            self._log.info("[Global] Quota SUB connected: topic=node%d", self.node_id)

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
    # Phase 5: Global xApp quota 接收執行緒
    # -------------------------------------------------------------------------

    def _quota_sub_worker(self) -> None:
        """背景執行緒：接收 Global xApp bridge 算出的 PRB 配額（僅 access nodes）。"""
        assert self._quota_sub_sock is not None
        self._log.info("[Global] Quota receive thread started (Node%d)", self.node_id)
        while self._running:
            try:
                msg = self._quota_sub_sock.recv_string()
                # Format: "node{id} {json}" e.g. "node3 {"quota":40}"
                parts = msg.split(" ", 1)
                if len(parts) == 2:
                    data = json.loads(parts[1])
                    quota = int(data.get("quota", self.total_prb))
                    quota = max(5, min(self.total_prb, quota))
                    with self._quota_lock:
                        old = self._prb_quota
                        self._prb_quota = quota
                    if quota != old:
                        self._log.info("[Global] PRB quota updated: %d → %d", old, quota)
            except zmq.Again:
                pass  # timeout, keep looping
            except (json.JSONDecodeError, ValueError):
                pass
            except Exception as exc:
                if self._running:
                    self._log.debug("[Global] Quota thread error: %s", exc)

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
        """執行一輪訓練：讀取 MongoDB → 訓練 → 儲存模型（訓練迴圈委派給 training_pipeline，
        與 Phase 5 FL ClientApp 共用同一份邏輯，見 training_pipeline.py）。"""
        if self._mongo_col is None:
            return

        # 先強制寫入緩衝區，確保最新資料可被讀到
        self._flush_to_mongo()

        # 若 MongoDB 筆數與上一輪相同（ZMQ 停擺，無新資料），跳過訓練
        # 防止在 stale 資料上反覆訓練導致 entropy collapse
        try:
            current_count = self._mongo_col.count_documents({})
        except Exception:
            current_count = self._last_train_mongo_count
        if current_count <= self._last_train_mongo_count and self._last_train_mongo_count > 0:
            self._log.info("MongoDB 無新資料（%d 筆），跳過本輪訓練", current_count)
            return
        self._last_train_mongo_count = current_count

        # 訓練/評估委派給共用函式；lock 只包住實際碰觸權重的段落（MongoDB
        # 讀取在鎖外進行），確保不影響 ZMQ 主迴圈的 5ms 回應預算。
        metrics = run_training_round(
            self._agent,
            self._mongo_col,
            epochs=TRAIN_EPOCHS_PER_ROUND,
            fetch_limit=TRAIN_FETCH_LIMIT,
            log=self._log,
            lock=self._model_lock,
        )
        if not metrics:
            return

        with self._model_lock:
            self._agent.save()
        self._touch_ckpt_mtime()

        # overfitting 指標：test_actor_loss 比 train_actor_loss 高超過 0.3 時警告
        t_aloss = metrics.get("test_actor_loss", 0.0)
        tr_aloss = metrics.get("actor_loss", 0.0)
        overfit_flag = ""
        if "test_actor_loss" in metrics and (t_aloss - tr_aloss) > 0.3:
            overfit_flag = " ⚠ OVERFIT"

        self._log.info(
            "訓練完成 %d epochs | step=%d "
            "train[actor=%.4f critic=%.4f entropy=%.4f reward=%.4f] "
            "test[actor=%.4f critic=%.4f entropy=%.4f reward=%.4f] "
            "train_n=%d test_n=%d%s | DRL/啟發式=%d/%d",
            TRAIN_EPOCHS_PER_ROUND,
            metrics.get("train_step", 0),
            tr_aloss,
            metrics.get("critic_loss", 0),
            metrics.get("entropy", 0),
            metrics.get("mean_reward", 0),
            t_aloss,
            metrics.get("test_critic_loss", 0),
            metrics.get("test_entropy", 0),
            metrics.get("test_mean_reward", 0),
            metrics.get("n_train", 0),
            metrics.get("n_test", 0),
            overfit_flag,
            self._drl_inferences,
            self._heuristic_inferences,
        )

    # -------------------------------------------------------------------------
    # 磁碟 checkpoint 熱重載（背景執行緒，Phase 5：接收 FL ClientApp 聚合後權重）
    # -------------------------------------------------------------------------

    def _ckpt_path(self) -> Path:
        return self._agent.model_dir / f"model_node{self.node_id}.pt"

    def _touch_ckpt_mtime(self) -> None:
        """記錄目前 checkpoint 的 mtime，避免 _reload_worker 重複載入本進程自己剛寫的檔案。"""
        try:
            self._last_ckpt_mtime = self._ckpt_path().stat().st_mtime
        except FileNotFoundError:
            pass

    def _reload_worker(self) -> None:
        """背景執行緒：每 RELOAD_POLL_INTERVAL_S 秒檢查 checkpoint 是否被外部
        process（Phase 5 的 flower-supernode ClientApp subprocess）更新過。"""
        while self._running:
            time.sleep(RELOAD_POLL_INTERVAL_S)
            self._maybe_reload_checkpoint()

    def _maybe_reload_checkpoint(self) -> None:
        try:
            mtime = self._ckpt_path().stat().st_mtime
        except FileNotFoundError:
            return
        if mtime <= self._last_ckpt_mtime:
            return
        with self._model_lock:
            if self._agent.load():
                self._last_ckpt_mtime = mtime
                self._log.info(
                    "已從磁碟熱重載模型 (mtime=%.0f)，可能來自 Phase 5 FL 聚合", mtime
                )

    # -------------------------------------------------------------------------
    # 推論邏輯
    # -------------------------------------------------------------------------

    def _infer_heuristic(
        self, ues: list[dict], explore: bool = False
    ) -> tuple[list[dict], np.ndarray]:
        """
        Phase 3 BSR 比例加權啟發式推論（冷啟動 Fallback）。

        explore=True 時使用 Dirichlet 隨機分配，產生多樣化訓練資料：
          - 與等量分配相比，不同的 action 會導致不同的 reward（fairness 差異）
          - 讓 DRL 學到「公平分配 ≻ 不公平分配」，打破全 0 梯度瓶頸
        """
        n = len(ues)
        if n == 0:
            return [], np.zeros(MAX_UE_COUNT, dtype=np.float32)

        if explore:
            # Dirichlet(α<1) 傾向產生稀疏/不均勻的分配，增加 reward 方差
            alpha = np.ones(n, dtype=np.float32) * 0.7
            ratios = np.random.dirichlet(alpha).astype(np.float32)
            prb_floats = ratios * self.total_prb
            prb_ints = np.maximum(np.floor(prb_floats).astype(np.int32), MIN_PRB_PER_UE)
            diff = self.total_prb - int(prb_ints.sum())
            if diff > 0:
                prb_ints[int(np.argmax(ratios))] += diff
            elif diff < 0:
                # 從最大的逐一減去（保底 MIN_PRB_PER_UE）
                for idx in np.argsort(prb_ints)[::-1]:
                    if diff >= 0:
                        break
                    can_remove = int(prb_ints[idx]) - MIN_PRB_PER_UE
                    remove = min(can_remove, -diff)
                    prb_ints[idx] -= remove
                    diff += remove
        else:
            scores = np.array(
                [
                    max(float(ue.get("bsr", 0)), 1.0)
                    * (1.0 + float(ue.get("wb_cqi", 0)) / 28.0 * 0.2)
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
            prb_ints = np.array(
                [MIN_PRB_PER_UE + prb_extra[i] for i in range(n)], dtype=np.int32
            )

        allocations = [
            {"rnti": int(ues[i]["rnti"]), "prb_abs": int(prb_ints[i])}
            for i in range(n)
        ]

        action_ratios = np.zeros(MAX_UE_COUNT, dtype=np.float32)
        for i in range(n):
            action_ratios[i] = allocations[i]["prb_abs"] / self.total_prb

        return allocations, action_ratios

    def _infer(self, ues: list[dict]) -> tuple[list[dict], np.ndarray]:
        """
        執行推論並回傳 (allocations, action_ratios)。

        策略：
          - DRL 訓練完成 → 使用 DRL Actor Network
          - 尚未完成首次訓練 → BSR 啟發式 + EXPLORE_PROB 機率的 Dirichlet 探索
        """
        use_drl = self._agent.is_trained

        if use_drl:
            try:
                with self._model_lock:
                    allocations, action_ratios = self._agent.infer(ues)
                self._drl_inferences += 1
                return allocations, action_ratios
            except Exception as exc:
                self._log.warning("DRL 推論失敗: %s，退回啟發式", exc)

        # 啟發式階段：以 EXPLORE_PROB 比例注入 Dirichlet 隨機探索
        explore = (not use_drl) and (np.random.rand() < EXPLORE_PROB)
        allocations, action_ratios = self._infer_heuristic(ues, explore=explore)
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
        # 計算獎勵：R(A_{t-1}, S_t)
        # 使用 curr_ues（S_t）而非 prev_ues（S_{t-1}）：
        # S_t.delta_tbs 反映的是 A_{t-1} 排程後的 DL 吞吐量，
        # 才是 A_{t-1} 真正造成的結果。
        reward = compute_reward(curr_ues, prev_allocations, self.total_prb)

        # 編碼當前狀態 S_t（作為 S' ）
        next_state_vec, next_mask_vec = self._agent.encode_state(curr_ues)

        # 獎勵細節（用於 MongoDB 監控）
        breakdown = compute_reward_breakdown(
            curr_ues, prev_allocations, self.total_prb
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
        self._touch_ckpt_mtime()  # 避免 _reload_worker 把剛載入的檔案當成外部更新重複載入

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

        # 背景執行緒 3：磁碟 checkpoint 熱重載（Phase 5，接收 FL ClientApp 的聚合結果）
        self._reload_thread = threading.Thread(
            target=self._reload_worker,
            daemon=True,
            name=f"ckpt-reload-node{self.node_id}",
        )
        self._reload_thread.start()

        # 背景執行緒 4：Global xApp 配額接收（access nodes only）
        if not self._is_relay and self._quota_sub_sock is not None:
            self._quota_thread = threading.Thread(
                target=self._quota_sub_worker,
                daemon=True,
                name=f"quota-sub-node{self.node_id}",
            )
            self._quota_thread.start()

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
                            f"rnti={u.get('rnti',0)} delta_tbs={u.get('bsr',0)} mcs={u.get('wb_cqi',0)}"
                            for u in ues
                        )
                        self._log.info("STATE[%d] %s", self._total_inferences, ue_summary)

                    # ── 計算上一步的獎勵並寫入 MongoDB ────────────────────
                    # 跳過 xApp 重連後 delta_tbs 全為 0 的空白 state，
                    # 避免 reward≈0 的無效 experience 污染訓練資料。
                    _total_delta = sum(u.get("bsr", 0) for u in ues)
                    if (
                        self._prev_ues is not None
                        and self._prev_allocations is not None
                        and len(ues) > 0
                        and _total_delta > 0
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

                    # ── Phase 5a: 回傳限制配額（access nodes only）────────
                    if not self._is_relay and allocations:
                        with self._quota_lock:
                            quota = self._prb_quota
                        if quota < self.total_prb:
                            total_alloc = sum(a["prb_abs"] for a in allocations)
                            if total_alloc > quota and total_alloc > 0:
                                n_alloc = len(allocations)
                                remaining = quota
                                capped = []
                                for idx, a in enumerate(allocations):
                                    if idx == n_alloc - 1:
                                        prb = max(1, remaining)
                                    else:
                                        prb = max(1, int(a["prb_abs"] * quota / total_alloc))
                                        remaining -= prb
                                    capped.append({"rnti": a["rnti"], "prb_abs": prb})
                                allocations = capped
                                # Recompute action_ratios to reflect actual execution
                                action_ratios = np.array(
                                    [a["prb_abs"] / self.total_prb for a in allocations]
                                    + [0.0] * (MAX_UE_COUNT - len(allocations)),
                                    dtype=np.float32,
                                )

                    # ── Phase 5b: 發布分配給 Global xApp bridge（relay nodes only）
                    if self._is_relay and self._alloc_pub_sock is not None:
                        try:
                            pub_msg = json.dumps(
                                {"node_id": self.node_id, "allocations": allocations},
                                separators=(",", ":"),
                            )
                            self._alloc_pub_sock.send_string(pub_msg, zmq.NOBLOCK)
                        except Exception:
                            pass

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
                    try:
                        self._zmq_sock.send_string('{"allocations":[]}')
                    except Exception:
                        pass
                except Exception as exc:
                    # 捕捉所有非預期例外（含 ZMQError、推論崩潰等）
                    # 必須嘗試送 reply，否則 REP socket 卡在「必須先 send」狀態
                    self._log.error("推論迴圈非預期例外: %s，嘗試送空分配並重置 socket", exc)
                    try:
                        self._zmq_sock.send_string('{"allocations":[]}')
                    except Exception:
                        pass
                    # 重建 ZMQ socket，避免 REP 卡住
                    try:
                        poller.unregister(self._zmq_sock)
                        self._zmq_sock.close(linger=0)
                        self._zmq_sock = self._zmq_ctx.socket(zmq.REP)
                        self._zmq_sock.bind(self.zmq_endpoint)
                        poller.register(self._zmq_sock, zmq.POLLIN)
                        self._log.info("ZMQ REP socket 已重建")
                    except Exception as reset_exc:
                        self._log.error("ZMQ socket 重建失敗: %s", reset_exc)

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
                with self._model_lock:
                    self._agent.save()
            except Exception as exc:
                self._log.warning("關閉時模型儲存失敗: %s", exc)

        if self._alloc_pub_sock is not None:
            self._alloc_pub_sock.close()
        if self._quota_sub_sock is not None:
            self._quota_sub_sock.close()
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
