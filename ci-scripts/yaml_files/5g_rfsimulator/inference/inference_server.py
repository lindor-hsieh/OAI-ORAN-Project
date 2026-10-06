"""
inference_server.py — Local xApp Python 推論伺服器（Phase 4 DRL 版本）

架構角色：
  - 監聽 ZMQ REP socket，接收 C xApp 每 10ms 送來的 UE 狀態
  - 執行 PRB 分配推論，並在 5ms 內回傳結果
  - 計算複合獎勵函數，建構完整的 (s, a, r, s') 強化學習經驗
  - 非同步批次寫入 MongoDB，供訓練執行緒讀取
  - 背景執行緒定期從 MongoDB 讀取經驗，執行 Actor-Critic 離線訓練
  - Stage 2 起：訂閱 Global xApp（`global_xapp.py`）的全域公平性廣播
    （ZMQ SUB，`tcp://127.0.0.1:5560`），把 `fairness_bias` 編碼進 state
    vector 第 49 維，純軟性 state 特徵，不做任何硬性 PRB 裁切（見
    `_fairness_sub_worker()`／`_current_fairness_bias()`）。模型週期性由
    Global rApp（Flower FedAvg）聚合後的權重熱重載，見 `_train_worker()`
    對 checkpoint mtime 的偵測邏輯。

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
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pymongo
import zmq

from drl_agent import DRLAgent, MAX_BUF_INFO, MAX_UE_COUNT
from reward_calculator import REWARD_MODE, compute_lagrangian_reward, compute_reward_breakdown
from training_pipeline import TRAIN_FETCH_LIMIT, run_training_round

# =============================================================================
# 全域常數
# =============================================================================

TOTAL_PRB_COUNT: int = 106          # 必須與 C xApp 的 TOTAL_PRB_COUNT 一致
MIN_PRB_PER_UE: int = 5             # BSR 啟發式：每個 UE 的最小 PRB 保底（與 DRL 路徑一致，防止 MAC 層 SIGSEGV）
ZMQ_POLL_MS: int = 100              # ZMQ poller 超時，允許優雅退出
MONGO_FLUSH_INTERVAL: float = 1.0   # 批次寫入 MongoDB 的間隔（秒）
INFERENCE_WARN_MS: float = 4.0      # 推論延遲警告閾值（ms）
TRAIN_INTERVAL_S: float = 60.0      # DRL 背景訓練間隔（秒）
# 2026-09-27：DRL_TRAIN_ENABLED=0 → 不啟動背景訓練（凍結模型做量測；FL 服務也不要帶起）。預設 1。
TRAIN_ENABLED: bool = os.getenv("DRL_TRAIN_ENABLED", "1").strip() not in ("0", "false", "False")
TRAIN_EPOCHS_PER_ROUND: int = int(os.getenv("TRAIN_EPOCHS_PER_ROUND", "10"))
TRAIN_THREAD_NICE: int = int(os.getenv("TRAIN_THREAD_NICE", "15"))   # 背景訓練執行緒的 nice 值（2026-10-03）    # 每輪訓練的梯度更新次數（v3 設 30）
EXPLORE_PROB: float = 0.30          # 啟發式階段 Dirichlet 隨機探索的比例
RELOAD_POLL_INTERVAL_S: float = 30.0  # 檢查磁碟 checkpoint 是否被 FL ClientApp 更新的輪詢間隔

# 2026-09-18 新增：_prev_ues 新鮮度門檻（秒）。正常穩態下連續兩次 ZMQ 請求間隔
# 是有效控制週期 100ms（見 CLAUDE.md 第 7 節），這裡抓 2.0s（20 倍餘裕）純粹是
# 為了跟「FlexRIC/DU 崩潰後完整重啟」這種數量級（分鐘）的中斷做區隔，不會誤觸
# 發到任何正常的排程抖動或 GC pause。訓練管線（training_pipeline.py 的
# docstring）原本就把「資料新鮮度判斷（staleness guard）」列為要留給呼叫端
# （也就是這裡）處理的責任，但先前一直沒有真的實作——DU 崩潰重啟後 RNTI 會
# 重新分配，若沒有這道防線，_build_rl_experience() 會用崩潰前的 (state, action)
# 搭配崩潰後完全不相關的一組新 UE 計算 reward，reward_calculator.py 的
# alloc_map.get(rnti, 1) 找不到匹配的舊 RNTI 時會悄悄假設 PRB=1，產生一筆嫁接
# 兩個不相關時間點的假經驗，混進訓練資料。
STALE_PREV_UES_THRESHOLD_S: float = 5.0   # 控制週期是 ~1 秒（不是 100ms），2.0 只有 2 倍餘裕、會誤丟偶發的 >2 秒間隔；改 5.0

# 2026-09-29 新增：PF-shadow 模式（Local DRL v2 離線 BC 預訓練用，見 LOCAL_DRL_V2_DESIGN.md §1.3）。
# XAPP_MODE=shadow 時：完全不做 DRL 推論/訓練，只用來偷看 state 並記錄 PF 真實行為當監督標籤；
# 回傳給 C xApp 的一律是「解除上限」控制，讓 MAC 排程器維持純 PF 行為不受干擾。
XAPP_MODE: str = os.getenv("XAPP_MODE", "active").strip().lower()
# 資料要跟 T/TH 雙軌框架的場景家族對應分開存（CLAUDE.md 第 3/8 節），由啟動腳本依當次跑的場景指定。
PF_SHADOW_SCENARIO_TAG: str = os.getenv("PF_SHADOW_SCENARIO_TAG", "unknown").strip()
# 2026-09-30 新增：XAPP_MODE=rule（試驗用固定規則，不是 DRL）。資料記錄同 shadow（存進 node{N}_pf_shadow，
# 多一個 rule_caps 欄位），但回傳規則上限：同節點 ≥2 個活躍 UE 時，MCS ≤ RULE_BAD_MCS 且比同節點最好的 UE
# 低至少 RULE_MCS_GAP 的 UE 限在 RULE_CAP 檔，其餘不設上限。用來驗證混合通道場景下「限制壞通道 UE」的增益
# （/home/lindor/pf16_run_20260930/upper_bound/ 的離線模型預測約 +11%）。
RULE_BAD_MCS: int = int(os.getenv("RULE_BAD_MCS", "5"))
RULE_MCS_GAP: int = int(os.getenv("RULE_MCS_GAP", "3"))
RULE_CAP: float = float(os.getenv("RULE_CAP", "0.3"))
# 2026-10-01：頻域上限在這個平台無效（每 UE 每 TDD 週期約 5.3 次傳輸的 ACK 限制，壞 UE 少用的 RB 好 UE 用不到，
# 見 HISTORY.md 續四十六），改試時域遮罩。RULE_KIND=cap（舊）或 mask：壞 UE 的 slot_mask = RULE_MASK（其餘全開、不設上限）。
# 容器內 /tmp/rule_mask（十六進位字串）存在時覆寫 RULE_MASK，每 5 秒重讀一次——同一次重啟內可換遮罩樣式。
RULE_KIND: str = os.getenv("RULE_KIND", "cap").strip().lower()
RULE_PERSIST_S: int = int(os.getenv("RULE_PERSIST_S", "10"))
RULE_MASK: int = int(os.getenv("RULE_MASK", "0x9292"), 16)
RULE_MASK_FILE = "/tmp/rule_mask"
FORCE_NODES_FILE = "/tmp/force_nodes"   # RULE_KIND=force：要強制遮壞 UE 的節點（機制對照用）
FORCE_OBSERVE_S: int = int(os.getenv("FORCE_OBSERVE_S", "8"))
FORCE_SKIP_S: int = int(os.getenv("FORCE_SKIP_S", "25"))   # 10 不夠：前一相位通道好時壞 UE 的 MCS 要 20 秒以上才降下來（Node5 實測）
FORCE_MCS_GAP: float = float(os.getenv("FORCE_MCS_GAP", "2"))
# 動作持續（action repeat，2026-10-04 v3.3）：DRL 每 ACTION_HOLD 個控制週期（每週期 1 秒牆鐘）才重新抽一次動作，期間沿用同一組遮罩；
# 每一秒仍各寫一筆經驗（hold_id／hold_pos 標記），訓練讀取時由 training_pipeline.merge_action_holds() 併成一筆
# 「決策」經驗（state＝決策當下、reward＝K 秒平均、next_state＝下一個決策當下）。1＝每秒重選（v3.2 以前）。
ACTION_HOLD: int = max(1, int(os.getenv("DRL_ACTION_HOLD", "1")))
RULE_MASK_REFRESH_S: float = float(os.getenv("RULE_MASK_REFRESH_S", "5.0"))
RULE_NODES: set = {int(x) for x in os.getenv("RULE_NODES", "").split(",") if x.strip()}   # 空＝全部節點
RULE_ACCESS_PHAT_MAX: float = float(os.getenv("RULE_ACCESS_PHAT_MAX", "0"))   # 重讀 /tmp/rule_mask 的間隔（2026-10-03 動作持續驗證設 0.5）
# 每個節點最多同時遮幾個 UE（2026-10-01）：預設 1（access 節點 2-UE 配置的原行為）；relay 直連 UE 試驗（場景 PB）
# 要同時遮 relay 下兩個壞通道 UE，設 2。永遠不遮當下 MCS 最高的子節點（relay 的子節點 MT 恆為 MCS 28，不會被遮）。
RULE_MAX_MASKED: int = int(os.getenv("RULE_MAX_MASKED", "1"))
# 2026-10-02：訓練暫停開關（學習曲線的定期實測用）。主機 /tmp 掛進容器，主機上 touch 這個檔＝12 個節點同時：
# 不跑訓練回合、不寫 RL 經驗（量測用測試 seed，不能進訓練資料）；推論照常（用當下模型＝凍結評估）。刪檔即恢復。
DRL_PAUSE_FILE: str = os.getenv("DRL_PAUSE_FILE", "/tmp/drl_pause")
# 2026-10-02：RULE_KIND=dyn（動態規則，HS 等動態場景用；只用可觀測 state，不看場景標籤）。
# 介入條件＝同節點有「好通道且積壓」的 UE（MCS ≥ RULE_DYN_GOOD_MCS、RLC 佇列 ≥ RULE_DYN_STARVE_BUF）
# ＋「壞通道」的 UE（MCS ≤ RULE_DYN_BAD_MCS）持續 RULE_DYN_ON_S 秒 → 遮壞 UE（永不遮當下 MCS 最高者，relay 的 MT 因此不會被遮）；
# 被遮期間壞 UE 的 MCS 會回升，用遲滯：MCS > RULE_DYN_RELEASE_MCS、或好 UE 不再積壓、或壞 UE 沒流量，持續 RULE_DYN_OFF_S 秒才解除。
RULE_DYN_GOOD_MCS: int = int(os.getenv("RULE_DYN_GOOD_MCS", "20"))
RULE_DYN_BAD_MCS: int = int(os.getenv("RULE_DYN_BAD_MCS", "9"))
RULE_DYN_RELEASE_MCS: int = int(os.getenv("RULE_DYN_RELEASE_MCS", "14"))
RULE_DYN_STARVE_BUF: float = float(os.getenv("RULE_DYN_STARVE_BUF", "100000"))
RULE_DYN_ON_S: int = int(os.getenv("RULE_DYN_ON_S", "3"))
RULE_DYN_OFF_S: int = int(os.getenv("RULE_DYN_OFF_S", "5"))
# 2026-10-03：access 層的好 UE（L22）MCS 只有約 8（壞 UE L24 約 3），GOOD_MCS=20 永遠不成立。RULE_DYN_GAP>0 時另要求壞 UE 的 MCS
# 比「積壓的好 UE」低至少 GAP（只看積壓、MCS≥GOOD_MCS 且與節點最高 MCS 差 ≤2 者）；預設 0＝原行為。access 試驗設 GOOD_MCS=6、GAP=4（場景 PA 實測）。
RULE_DYN_GAP: int = int(os.getenv("RULE_DYN_GAP", "0"))
SHADOW_LIKE_MODES = ("shadow", "rule")

# 2026-09-30 新增：relational state 特徵 p̂／ĉ（LOCAL_DRL_V2_DESIGN.md §4.2）。
# 靜態拓樸（CLAUDE.md 第 1 節）：relay=Node1~4（parent 是 Donor，Donor 沒有 MT/rApp → relay 的 p̂ 恆中性）；
# access=Node5~12。（UE17 已於 2026-09-30 移除；原本是直連 Node4 的 UE，從來就不是 child 節點。）
PARENT_OF: dict[int, int] = {5: 1, 6: 1, 7: 2, 8: 2, 9: 3, 10: 3, 11: 4, 12: 4}
CHILDREN_OF: dict[int, tuple[int, ...]] = {1: (5, 6), 2: (7, 8), 3: (9, 10), 4: (11, 12)}
# 取得方式：每個 Local rApp 約每秒把自己的 (bh_ratio, RLC 佇列總和) upsert 到共用 collection
# `node_status`，同一個背景執行緒讀 parent/children 的最新值算好 p̂/ĉ 快取起來——推論路徑只讀快取，
# 不碰 MongoDB（直接在推論裡查 MongoDB 會吃掉 xApp 的 5ms 逾時預算）。
REL_POLL_S: float = 1.0          # 跟控制週期（~1 秒）同量級
REL_STALE_S: float = 10.0        # 對方超過這麼久沒更新（容器掛了/重啟中）→ 視為沒有資料，退回中性值
REL_TREND_EWMA: float = 0.3      # parent bh_ratio 的平滑係數；趨勢 = 最新值 − 平滑值（單步差分太吵）
REL_TREND_GAIN: float = 5.0      # （2026-10-01 前的 p̂＝parent bh_ratio 趨勢用；p̂ 改義後不再使用）
# 2026-10-01：p̂ 改為 parent DU 的 RLC 佇列總和（上游壅塞程度）＝ log1p(parent total_buf) / log1p(MAX_BUF_INFO × REL_PARENT_CHILDREN)。
# backhaul 預算停用（SYSTEM_SPEC D3）後 bh_ratio 恆為 1，舊的 p̂ 恆為 0.5、不帶資訊。relay 的 parent 是 Donor（沒有 rApp）→ p̂＝0。
REL_PARENT_CHILDREN: int = 4     # relay DU 的子節點數（2 個 access MT＋2 個 relay 直連 UE），作正規化上限


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
        self.xapp_mode = XAPP_MODE   # "active"（預設）或 "shadow"（PF-shadow 資料收集）

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
        # 三方對同一組 actor/critic 權重的存取。刻意不把 MongoDB I/O 包進鎖裡。
        # ZMQ 主迴圈這一側單次持有仍是毫秒級；_train_worker 這一側自 2026-07-09
        # GRU 改版起，training_pipeline.run_training_round() 把鎖拆成「每個
        # epoch 各自取得/釋放」（而非整個 epochs 迴圈共用一個鎖），因為 GRU
        # 的 BPTT 一次 epoch 就要數百毫秒，整段鎖住會讓 infer() 卡到數秒、
        # 遠超 ZMQ 的 5ms 回應預算（詳見 training_pipeline.py 的說明）。
        self._model_lock = threading.Lock()
        self._shadow_agent: Optional[DRLAgent] = None   # 影子模型（背景訓練用，見 _run_training_round）
        self._last_ckpt_mtime: float = 0.0
        self._prev_behavior_logp: Optional[float] = None   # 上一步動作的行為策略 logπ（DRL 推論才有）
        self._bh_ratio: float = 1.0   # 最近一次 E2 回報的可用 PRB 比例，見 run() 的 payload 解析

        # RL 狀態轉移暫存（用於計算 R(S_{t-1}, A_{t-1}, S_t)）
        self._prev_ues: Optional[list[dict]] = None
        self._prev_allocations: Optional[list[dict]] = None
        self._prev_state_vec: Optional[np.ndarray] = None
        self._prev_mask_vec: Optional[np.ndarray] = None
        self._prev_action_ratios: Optional[np.ndarray] = None
        self._prev_ts: Optional[float] = None   # 見 STALE_PREV_UES_THRESHOLD_S 說明
        self._prev_macro: Optional[int] = None  # 上一步的宏動作（v2.1，存進經驗的 macro_action）
        self._prev_factored: Optional[dict] = None  # 上一步的兩段式動作（v3.1，存進經驗的 factored_action）
        # 動作持續（ACTION_HOLD>1）：目前沿用中的動作與剩餘秒數；_prev_hold＝上一步動作的 (hold_id, hold_pos)
        self._hold_left: int = 0
        self._hold_seq: int = 0
        self._hold_cur: Optional[dict] = None
        self._prev_hold: Optional[tuple[str, int]] = None
        # 上一次回傳中被遮罩（slot_mask≠0xFFFF）的 RNTI（v2.1 state 的 was_masked 特徵；active／rule／shadow 都維護）
        self._masked_rntis: set = set()

        # 統計計數器
        self._total_inferences: int = 0
        self._drl_inferences: int = 0
        self._heuristic_inferences: int = 0

        # Stage 2 起：Global xApp 全域公平性廣播（取代舊版 relay→access ZMQ
        # 配額裁切機制——舊機制跟 C 層「Backhaul-aware 動態 PRB 預算」機制
        # 做的是同一件事、會雙重節流，已移除）。全部 12 個節點對稱地當
        # Global xApp（獨立 process，見 global_xapp.py）的 SUB client，收到
        # 的是純軟性 state 特徵（fairness_bias），不做任何硬性 PRB 裁切。
        self._fairness_sub_sock: Optional[zmq.Socket] = None
        self._fairness_bias: float = 1.0                    # 中性值，收到廣播前預設
        self._fairness_lock = threading.Lock()
        self._fairness_thread: Optional[threading.Thread] = None

        # Relational state 特徵 p̂／ĉ（見 PARENT_OF/CHILDREN_OF 說明）：主迴圈寫 _my_status、
        # 背景執行緒 _relational_worker 發佈自己的狀態並讀 parent/children 算 p̂/ĉ，推論只讀快取。
        self._rel_lock = threading.Lock()
        self._my_status: Optional[tuple[float, float]] = None   # (bh_ratio, 本節點 RLC 佇列總和 bytes)
        self._p_hat: float = 0.0                                 # 無 parent/資料過期＝無上游壅塞
        self._c_hat: float = 0.0                                 # 中性值（無 children/資料過期）
        self._parent_bh_ewma: Optional[float] = None
        self._rel_thread: Optional[threading.Thread] = None

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

        # Stage 2 起：全部 12 個節點對稱地訂閱 Global xApp 的全域公平性廣播
        # （global_xapp.py 綁定 tcp://127.0.0.1:5560，見該檔頭說明）。
        self._fairness_sub_sock = self._zmq_ctx.socket(zmq.SUB)
        self._fairness_sub_sock.setsockopt(zmq.LINGER, 0)
        self._fairness_sub_sock.setsockopt(zmq.RCVTIMEO, 500)
        self._fairness_sub_sock.setsockopt_string(zmq.SUBSCRIBE, f"node{self.node_id}")
        self._fairness_sub_sock.connect("tcp://127.0.0.1:5560")
        self._log.info("[Global xApp] Fairness SUB connected: topic=node%d", self.node_id)

    def _init_mongo(self) -> None:
        """建立 MongoDB 連線；失敗時降級為不持久化模式。"""
        try:
            self._mongo_client = pymongo.MongoClient(
                self._mongo_uri, serverSelectionTimeoutMS=3000
            )
            self._mongo_client.server_info()
            db = self._mongo_client[self._mongo_db]
            if self.xapp_mode in SHADOW_LIKE_MODES:
                # PF-shadow 資料跟一般 RL 經驗分開存（不同 schema：沒有 reward/action，
                # 是 (state, PF 實際份額標籤) 配對，見 LOCAL_DRL_V2_DESIGN.md §1.3/§1.5）。
                coll_name = f"node{self.node_id}_pf_shadow"
                self._mongo_col = db[coll_name]
                self._mongo_col.create_index("timestamp")
                self._mongo_col.create_index("scenario_tag")
            else:
                coll_name = f"node{self.node_id}_experiences"
                self._mongo_col = db[coll_name]
                # 建立查詢索引：時間戳（批次訓練讀取用）
                self._mongo_col.create_index("timestamp")
                # 建立複合索引：reward 欄位存在性（篩選完整 RL 經驗用）
                self._mongo_col.create_index("reward")
            self._log.info(
                "MongoDB 已連線: %s/%s/%s",
                self._mongo_uri, self._mongo_db, coll_name,
            )
        except pymongo.errors.PyMongoError as exc:
            self._log.warning("MongoDB 連線失敗 (%s)，資料將不會持久化", exc)
            self._mongo_col = None

    # -------------------------------------------------------------------------
    # Stage 2: Global xApp 全域公平性廣播接收執行緒
    # -------------------------------------------------------------------------

    def _fairness_sub_worker(self) -> None:
        """背景執行緒：接收 Global xApp 算出的全域公平性偏差（全部 12 節點對稱）。"""
        assert self._fairness_sub_sock is not None
        self._log.info("[Global xApp] Fairness receive thread started (Node%d)", self.node_id)
        while self._running:
            try:
                msg = self._fairness_sub_sock.recv_string()
                # Format: "node{id} {json}" e.g. "node3 {"fairness_bias":1.4}"
                parts = msg.split(" ", 1)
                if len(parts) == 2:
                    data = json.loads(parts[1])
                    bias = float(data.get("fairness_bias", 1.0))
                    bias = max(0.5, min(2.0, bias))
                    with self._fairness_lock:
                        old = self._fairness_bias
                        self._fairness_bias = bias
                    if abs(bias - old) > 1e-6:
                        self._log.debug("[Global xApp] fairness_bias updated: %.3f → %.3f", old, bias)
            except zmq.Again:
                pass  # timeout, keep looping
            except (json.JSONDecodeError, ValueError):
                pass
            except Exception as exc:
                if self._running:
                    self._log.debug("[Global xApp] Fairness thread error: %s", exc)

    def _current_fairness_bias(self) -> float:
        """
        回傳目前有效的全域公平性偏差，作為 encode_state()/infer() 的
        state 特徵（見 drl_agent.py 的 fairness_bias 參數）。

        全部 12 個節點（relay/access 皆同）對稱地依 _fairness_sub_worker()
        收到的最新值回傳，純軟性輸入，不做任何硬性 PRB 裁切。
        """
        with self._fairness_lock:
            return self._fairness_bias

    # -------------------------------------------------------------------------
    # Relational state 特徵 p̂／ĉ（2026-09-30，LOCAL_DRL_V2_DESIGN.md §4.2）
    # -------------------------------------------------------------------------

    def _current_relational(self) -> tuple[float, float]:
        """回傳快取中的 (p̂, ĉ)，供 encode_state()/infer() 使用；只讀記憶體，不碰 MongoDB。"""
        with self._rel_lock:
            return self._p_hat, self._c_hat

    def _encode_current_state(
        self, ues: list[dict], fairness_bias: Optional[float] = None,
    ) -> tuple[np.ndarray, np.ndarray]:
        """用目前的 fairness_bias／bh_ratio／p̂／ĉ 編碼 state；所有呼叫點共用，確保線上與 shadow 同一套特徵。"""
        p_hat, c_hat = self._current_relational()
        return self._agent.encode_state(
            ues,
            fairness_bias=self._current_fairness_bias() if fairness_bias is None else fairness_bias,
            bh_ratio=self._bh_ratio,
            parent_trend=p_hat,
            children_demand=c_hat,
            prev_masked=self._masked_rntis,
        )

    def _relational_worker(self) -> None:
        """
        背景執行緒（active/shadow 兩種模式都跑）：每 REL_POLL_S 秒
          1. upsert 自己的 (bh_ratio, 佇列總和, 時間戳) 到 node_status
          2. 讀 parent/children 的最新狀態，算 p̂（parent DU 的佇列總和＝上游壅塞）與 ĉ（children 佇列總和）
        資料超過 REL_STALE_S 沒更新就退回中性值，避免拿死掉容器的舊數字當輸入。
        """
        if self._mongo_client is None:
            self._log.warning("[Relational] 無 MongoDB 連線，p̂/ĉ 固定為中性值")
            return
        coll = self._mongo_client[self._mongo_db]["node_status"]
        parent = PARENT_OF.get(self.node_id)
        children = CHILDREN_OF.get(self.node_id, ())
        peer_ids = ([parent] if parent is not None else []) + list(children)
        self._log.info("[Relational] 啟動：parent=%s children=%s", parent, children)

        while self._running:
            time.sleep(REL_POLL_S)
            try:
                with self._rel_lock:
                    my = self._my_status
                now = time.time()   # 12 個 inference 容器都在 PC1，共用同一個系統時鐘
                if my is not None:
                    coll.update_one(
                        {"_id": self.node_id},
                        {"$set": {"bh_ratio": my[0], "total_buf": my[1], "ts": now}},
                        upsert=True,
                    )
                if not peer_ids:
                    continue
                docs = {d["_id"]: d for d in coll.find({"_id": {"$in": peer_ids}})}

                def fresh(nid: int) -> Optional[dict]:
                    d = docs.get(nid)
                    return d if d is not None and now - float(d.get("ts", 0.0)) <= REL_STALE_S else None

                p_hat = 0.0
                pd = fresh(parent) if parent is not None else None
                if pd is not None:
                    p_hat = float(np.clip(np.log1p(max(float(pd.get("total_buf", 0.0)), 0.0))
                                          / np.log1p(MAX_BUF_INFO * REL_PARENT_CHILDREN), 0.0, 1.0))

                c_hat = 0.0
                if children:
                    bufs = [float(cd.get("total_buf", 0.0)) for cd in (fresh(k) for k in children) if cd]
                    if bufs:
                        c_hat = float(np.clip(
                            np.log1p(sum(bufs)) / np.log1p(MAX_BUF_INFO * len(children)), 0.0, 1.0))

                with self._rel_lock:
                    self._p_hat, self._c_hat = p_hat, c_hat
            except pymongo.errors.PyMongoError as exc:
                self._log.debug("[Relational] MongoDB 錯誤: %s", exc)
            except Exception as exc:
                self._log.warning("[Relational] 非預期錯誤: %s", exc)

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
        if "reward" in doc and os.path.exists(DRL_PAUSE_FILE):
            return   # 暫停期間（定期實測）不寫 RL 經驗
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
        # 等待一個完整訓練間隔後才開始，讓系統先累積足夠經驗。再依 node_id 錯開（12 個節點均分一個間隔，
        # 每個節點差 ~5 秒），避免 12 個 process 同一秒開始訓練、搶同一組 4 個核心而拉長推論延遲。
        if not TRAIN_ENABLED:
            self._log.info("DRL_TRAIN_ENABLED=0：背景訓練停用（模型凍結，只做推論）")
            return
        # 2026-10-03：訓練執行緒調低優先權（只影響這條執行緒），12 個節點的推論與訓練共用 cpuset 12-15，讓推論先拿到 CPU
        try:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), TRAIN_THREAD_NICE)
            self._log.info("訓練執行緒 nice=%d", TRAIN_THREAD_NICE)
        except (OSError, AttributeError) as exc:
            self._log.warning("無法調整訓練執行緒優先權：%s", exc)
        time.sleep(TRAIN_INTERVAL_S + ((self.node_id - 1) % 12) * (TRAIN_INTERVAL_S / 12.0))

        while self._running:
            if os.path.exists(DRL_PAUSE_FILE):
                time.sleep(5.0)
                continue
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

        # 訓練前先套用 FL 可能剛寫入的聚合權重（不等 _reload_worker 的 30 秒輪詢），
        # 讓這一輪的微調從最新的全域權重出發，而不是從過期的記憶體權重出發。
        self._maybe_reload_checkpoint()

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
        # 2026-10-03 影子模型訓練：舊版每次梯度更新都持有 _model_lock，推論在訓練期間被擋住（冒煙測試：有訓練時 ZMQ 逾時
        # 175 次／15 分鐘，暫停訓練時 81 次，延遲尖峰到 35～240 ms）。改成在複本上訓練、全程不持鎖，只在開始複製與結束換回
        # 權重時短暫持鎖（毫秒級）。訓練期間若 FL 熱重載了磁碟權重（_last_ckpt_mtime 變了），本輪結果作廢、保留 FL 權重。
        with self._model_lock:
            snapshot = self._agent.export_state()
            mtime_before = self._last_ckpt_mtime
        if self._shadow_agent is None:
            self._shadow_agent = DRLAgent(node_id=self.node_id, model_dir=f"/tmp/shadow_node{self.node_id}")
        self._shadow_agent.import_state(snapshot)
        metrics = run_training_round(
            self._shadow_agent,
            self._mongo_col,
            epochs=TRAIN_EPOCHS_PER_ROUND,
            fetch_limit=TRAIN_FETCH_LIMIT,
            log=self._log,
            lock=None,
        )
        if not metrics:
            return
        with self._model_lock:
            if self._last_ckpt_mtime != mtime_before:
                self._log.warning("訓練期間權重已被熱重載（FL），本輪本地訓練結果作廢")
                return
            self._agent.import_state(self._shadow_agent.export_state())

        self._save_unless_superseded()

        # overfitting 指標：test_actor_loss 比 train_actor_loss 高超過 0.3 時警告
        t_aloss = metrics.get("test_actor_loss", 0.0)
        tr_aloss = metrics.get("actor_loss", 0.0)
        overfit_flag = ""
        if "test_actor_loss" in metrics and (t_aloss - tr_aloss) > 0.3:
            overfit_flag = " ⚠ OVERFIT"

        # train_n/test_n 欄位名稱依 MODEL_ARCH 而異（mlp: n_train_exp/n_test_exp；
        # gru: n_train_seq/n_test_seq），iab/check_convergence.py 的 LOG_LINE_RE
        # 只認 "train_n=.../test_n=..." 這個輸出格式，跟哪個 arch 無關，所以這裡
        # 統一取其中有值的那組，不依 arch 分支。
        train_n = metrics.get("n_train_exp", metrics.get("n_train_seq", 0))
        test_n = metrics.get("n_test_exp", metrics.get("n_test_seq", 0))
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
            train_n,
            test_n,
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

    def _save_unless_superseded(self) -> bool:
        """
        存檔前先確認磁碟 checkpoint 沒有被外部（FL 伺服器／ClientApp）更新過。

        修正寫入競態（2026-09-26）：舊版每輪訓練後無條件把記憶體權重存回檔案，而 FL 寫入聚合權重後
        要等 _reload_worker（30 秒輪詢）才會被載入；若本地訓練（每 60 秒一次）剛好在這個空窗存檔，
        FL 的全域權重就被過期的記憶體權重覆蓋，而且 mtime 被刷新後 reload 永遠不會再載入——聚合
        效果被悄悄吃掉。現在存檔與檢查在同一個鎖內：磁碟檔比我們最後一次存/載的 mtime 新，代表
        FL 已更新，改成「採用磁碟上的 FL 權重、放棄覆寫」；否則才存檔。

        Returns: True=已存檔；False=偵測到外部更新、改為載入磁碟權重（本輪本地訓練結果被 FL 權重取代）。
        """
        with self._model_lock:
            try:
                disk_mtime = self._ckpt_path().stat().st_mtime
            except FileNotFoundError:
                disk_mtime = 0.0
            if disk_mtime > self._last_ckpt_mtime:
                old_mtime = self._last_ckpt_mtime
                if self._agent.load():
                    self._last_ckpt_mtime = disk_mtime
                self._log.warning(
                    "存檔前偵測到磁碟 checkpoint 已被外部（FL）更新 (mtime=%.0f > %.0f)，"
                    "改為載入 FL 權重、不覆寫", disk_mtime, old_mtime,
                )
                return False
            self._agent.save()
        self._touch_ckpt_mtime()
        return True

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

    def _infer(
        self, ues: list[dict], feat_snap: tuple[float, float, float],
    ) -> tuple[list[dict], np.ndarray, Optional[float]]:
        """
        執行推論並回傳 (allocations, action_ratios, behavior_logp)。behavior_logp 只有 DRL 推論才有（啟發式為 None）。

        策略：
          - DRL 訓練完成 → 使用 DRL Actor Network
          - 尚未完成首次訓練 → BSR 啟發式 + EXPLORE_PROB 機率的 Dirichlet 探索
        """
        use_drl = self._agent.is_trained

        if use_drl:
            try:
                fairness_bias, p_hat, c_hat = feat_snap   # 呼叫端一次取好的快照，跟之後存進經驗的 state 同一份
                with self._model_lock:
                    allocations, action_ratios, behavior_logp = self._agent.infer(
                        ues, fairness_bias=fairness_bias, bh_ratio=self._bh_ratio,
                        parent_trend=p_hat, children_demand=c_hat, prev_masked=self._masked_rntis)
                self._drl_inferences += 1
                return allocations, action_ratios, behavior_logp
            except Exception as exc:
                if self._agent.arch == "mlp":
                    # Local DRL v2：失敗就退回 PF（全部不設上限），不用 BSR 啟發式——啟發式會設上限，
                    # 且動作格式（份額）跟 v2 的檔位不同，混進經驗會污染訓練資料
                    self._log.warning("DRL 推論失敗: %s，退回 PF（不設上限）", exc)
                    allocations, tiers = self._agent.pf_action(ues)
                    self._heuristic_inferences += 1
                    return allocations, tiers, None
                self._log.warning("DRL 推論失敗: %s，退回啟發式", exc)

        # 啟發式階段：以 EXPLORE_PROB 比例注入 Dirichlet 隨機探索
        explore = (not use_drl) and (np.random.rand() < EXPLORE_PROB)
        allocations, action_ratios = self._infer_heuristic(ues, explore=explore)
        self._heuristic_inferences += 1
        return allocations, action_ratios, None

    def _build_rl_experience(
        self,
        prev_ues: list[dict],
        prev_allocations: list[dict],
        prev_state_vec: np.ndarray,
        prev_mask_vec: np.ndarray,
        prev_action_ratios: np.ndarray,
        curr_ues: list[dict],
        prev_behavior_logp: Optional[float] = None,
        prev_macro: Optional[int] = None,
        prev_factored: Optional[dict] = None,
        prev_hold: Optional[tuple[str, int]] = None,
    ) -> dict[str, Any]:
        """
        建構一筆完整的 RL 經驗文件 (S, A, R, S')。

        reward 以上一步的 (state, action) 與當前 state 計算，
        實現 one-step TD 結構。
        """
        # 計算獎勵：R(A_{t-1}, S_t)。
        # 使用 curr_ues（S_t）而非 prev_ues（S_{t-1}）：
        # S_t.delta_tbs 反映的是 A_{t-1} 排程後的 DL 吞吐量，
        # 才是 A_{t-1} 真正造成的結果。
        # REWARD_MODE=throughput_only（五階段路線圖 Stage 2~4 用陽春版）時改用
        # 純 throughput 的 compute_reward_breakdown()，不含 JFI 限制式；
        # 預設 lagrangian 維持現行行為（見 reward_calculator.py）。
        # alpha_fair（v3）：這裡先存純吞吐量當佔位，訓練讀取時由 training_pipeline 依子樹 α-fair 效用重算
        if REWARD_MODE in ("throughput_only", "alpha_fair"):
            result = compute_reward_breakdown(
                curr_ues, prev_allocations,
                total_prb=self.total_prb,
            )
        else:
            result = compute_lagrangian_reward(
                curr_ues, prev_allocations,
                lambda_val=self._agent.lambda_,
                total_prb=self.total_prb,
            )
        reward = result["reward"]
        is_idle = result["r_throughput"] < 1e-9

        # 編碼當前狀態 S_t（作為 S' ）
        next_state_vec, next_mask_vec = self._encode_current_state(curr_ues)

        doc: dict[str, Any] = {
            "node_id":       self.node_id,
            "timestamp":     datetime.now(timezone.utc),
            # 狀態（原始格式，供人工分析）
            "state":         prev_ues,
            "action":        prev_allocations,
            # RL 訓練所需的向量格式
            "state_vec":     prev_state_vec.tolist(),
            "mask_vec":      prev_mask_vec.tolist(),
            "reward":        reward,
            "next_state_vec": next_state_vec.tolist(),
            "next_mask_vec":  next_mask_vec.tolist(),
            # 獎勵分解（監控用；jfi_raw 供 DRLAgent.train_on_batch() 更新 lambda 用）
            "r_throughput":  result["r_throughput"],
            "jfi_raw":       result["jfi_raw"],
            "r_fairness":    result["r_fairness"],
            "r_delay":       result["r_delay"],
            # 閒置轉換標記（r_throughput≈0，2026-07-09 起閒置轉換也會寫入
            # MongoDB，讓 GRU 訓練資料分佈與推論時實際遇到的分佈一致；
            # training_pipeline.py 用這個欄位排除閒置樣本不計入 JFI 平均）
            "is_idle":       is_idle,
            # 推論模式（供事後分析）
            "used_drl":      self._agent.is_trained,
        }
        # 動作：MLP（Local DRL v2）存每 UE 檔位 index（-1=非活躍）；GRU 舊分支存 Dirichlet 份額
        if self._agent.arch == "mlp":
            doc["action_tiers"] = prev_action_ratios.astype(int).tolist()
        else:
            doc["action_ratios"] = prev_action_ratios.tolist()
        # "lambda_applied" 只在 REWARD_MODE=lagrangian 時存在（見
        # compute_lagrangian_reward()）；compute_reward_breakdown()（throughput_only）
        # 不會回傳這個 key。刻意用「這個 key 是否存在」而非「值是否為 0」當作
        # 事後判斷這筆經驗用的是哪種 reward 模式的依據，所以這裡不能塞一個
        # 預設值進去，只在 result 裡真的有這個 key 時才寫入文件。
        if "lambda_applied" in result:
            doc["lambda_applied"] = result["lambda_applied"]
        # 行為策略 log π(a|s)：只有 DRL 推論產生的動作才有（啟發式階段沒有 → 不寫，訓練時該筆只用於 Critic）
        if prev_behavior_logp is not None:
            doc["behavior_logp"] = float(prev_behavior_logp)
        if prev_macro is not None:
            doc["macro_action"] = int(prev_macro)   # v2.1 宏動作（DRL_ACTION_SPACE=macro）
        if prev_factored is not None:
            doc["factored_action"] = prev_factored   # v3.1 兩段式動作（DRL_ACTION_SPACE=factored）
        if prev_hold is not None:
            doc["hold_id"], doc["hold_pos"] = prev_hold   # v3.3 動作持續：同一決策的第幾秒（0＝決策當下）
        return doc

    # -------------------------------------------------------------------------
    # PF-shadow 模式（2026-09-29 新增，見 LOCAL_DRL_V2_DESIGN.md §1.3/§1.5）
    # -------------------------------------------------------------------------

    def _rule_decide(self, ues: list[dict]) -> tuple[list[int], list[int]]:
        """XAPP_MODE=rule 的試驗規則（場景 P 用，不是 DRL）：回傳每個 UE 的 (prb_abs, slot_mask)。shadow 模式一律全開。"""
        caps = [self.total_prb] * len(ues)
        masks = [0xFFFF] * len(ues)
        if self.xapp_mode == "rule" and RULE_KIND == "force":
            return caps, self._rule_force(ues)
        if RULE_NODES and self.node_id not in RULE_NODES:   # 只讓指定節點套用規則（2026-10-04：分離 relay／access 的貢獻）
            return caps, masks
        # 「低 p̂ access 控制」（2026-10-04）：access 節點只在上游 parent 佇列（p̂，可觀測代理）低於門檻時才套用規則；
        # p̂ 低≈上游未壅塞，不是熱點外的完美篩選，判別結果另行以場景真值統計。0＝不設。
        if RULE_ACCESS_PHAT_MAX > 0 and self.node_id >= 5 and self._current_relational()[0] > RULE_ACCESS_PHAT_MAX:
            return caps, masks
        if self.xapp_mode == "rule" and RULE_KIND == "dyn" and len(ues) >= 2:
            return caps, self._rule_dyn(ues)
        if self.xapp_mode == "rule" and len(ues) >= 2:
            mcs = [int(ue.get("wb_cqi", 0)) for ue in ues]
            best = max(mcs)
            # 2026-10-01：沒有流量的 UE（bsr=0）回報的 MCS 是 0，會被誤判成壞 UE 黏住（輕負載節點在流量開始前就被遮），
            # 只考慮本視窗真的有送資料、MCS>0 的 UE。
            active = [int(ue.get("bsr", 0)) > 0 and m > 0 for ue, m in zip(ues, mcs)]
            best = max([m for m, a in zip(mcs, active) if a], default=0)
            is_bad = [a and m <= RULE_BAD_MCS and best - m >= RULE_MCS_GAP for m, a in zip(mcs, active)]
            # 2026-10-01：流量剛開始時 OAI 鏈路調適從低 MCS 往上爬（輕負載 UE 前幾秒 MCS 1~5，之後 28），會被誤判成壞 UE。
            # 要求連續 RULE_PERSIST_S 秒（每秒一次請求）都符合條件才算壞 UE。
            streak = self.__dict__.setdefault("_rule_bad_streak", {})
            for ue, bad in zip(ues, is_bad):
                r = int(ue["rnti"])
                streak[r] = streak.get(r, 0) + 1 if bad else 0
            is_bad = [streak[int(ue["rnti"])] >= RULE_PERSIST_S for ue in ues]
            # 黏住（2026-10-01）：遮罩後壞 UE 被排程得少、MCS 會回升到 5~7，條件失效又解除遮罩，來回震盪
            # （時域試驗第一輪約 25% 時間沒遮到）。一旦判定為壞 UE 就在這次執行期間一直套用。
            # 2026-10-01 修正：只加不刪會把好 UE 也加進去（壞 UE 被遮後 MCS 回升到 8~9，好 UE MCS 短暫 ≤5 時
            # 條件反轉），兩個 UE 都被遮。改成：本節點目前在線的 UE 已有被遮者就不再新增；新增時排除當下 MCS
            # 最高的 UE → 每個節點永遠只遮一個 UE（試驗規則，只給場景 P 的 2-UE 配置用）。
            if RULE_KIND == "mask":
                sticky = self.__dict__.setdefault("_rule_bad_rntis", set())
                rntis = [int(ue["rnti"]) for ue in ues]
                n_masked = sum(r in sticky for r in rntis)
                if n_masked < RULE_MAX_MASKED:
                    top = rntis[max(range(len(mcs)), key=lambda i: (active[i], mcs[i]))]
                    cand = sorted((m, r) for r, m, bad in zip(rntis, mcs, is_bad)
                                  if bad and r != top and r not in sticky)
                    for _, r in cand[:RULE_MAX_MASKED - n_masked]:
                        sticky.add(r)
                is_bad = [r in sticky for r in rntis]
            if RULE_KIND == "mask":
                rm = self._rule_mask()
                masks = [rm if b else 0xFFFF for b in is_bad]
            else:
                caps = [max(1, round(RULE_CAP * self.total_prb)) if b else self.total_prb
                        for b in is_bad]
        return caps, masks

    def _rule_dyn(self, ues: list[dict]) -> list[int]:
        """RULE_KIND=dyn：依可觀測 state 動態遮罩（見 RULE_DYN_* 註解）。回傳每個 UE 的 slot_mask。"""
        st = self.__dict__.setdefault("_dyn", {"on": {}, "off": {}, "masked": set()})
        rntis = [int(ue["rnti"]) for ue in ues]
        mcs = [int(ue.get("wb_cqi", 0)) for ue in ues]
        buf = [max(float(ue.get("dl_buffer_info", 0)), 0.0) for ue in ues]
        active = [(int(ue.get("bsr", 0)) > 0 or b > 0) and m > 0 for ue, m, b in zip(ues, mcs, buf)]
        top_m = max((m for a, m in zip(active, mcs) if a), default=0)
        # GAP>0 時只認「通道最好（與最高 MCS 差 ≤2）」的積壓 UE：relay 的細胞邊緣 UE 積壓時 MCS 也可到 7，不能當成被餓的好 UE
        starv_mcs = [m for a, m, b in zip(active, mcs, buf) if a and m >= RULE_DYN_GOOD_MCS and b >= RULE_DYN_STARVE_BUF
                     and (RULE_DYN_GAP <= 0 or m >= top_m - 2)]
        starved = bool(starv_mcs)
        gap_ok = (lambda m: m <= max(starv_mcs) - RULE_DYN_GAP) if starved else (lambda m: False)
        top = rntis[max(range(len(ues)), key=lambda i: (active[i], mcs[i]))]
        st["masked"] &= set(rntis)
        for r, a, m in zip(rntis, active, mcs):
            if r in st["masked"]:
                release = (not starved) or (not a) or m > RULE_DYN_RELEASE_MCS or r == top
                st["off"][r] = st["off"].get(r, 0) + 1 if release else 0
                if st["off"][r] >= RULE_DYN_OFF_S:
                    st["masked"].discard(r); st["on"][r] = 0
            else:
                cond = starved and a and m <= RULE_DYN_BAD_MCS and gap_ok(m) and r != top
                st["on"][r] = st["on"].get(r, 0) + 1 if cond else 0
                if st["on"][r] >= RULE_DYN_ON_S and len(st["masked"] & set(rntis)) < RULE_MAX_MASKED:
                    st["masked"].add(r); st["off"][r] = 0
        rm = self._rule_mask()
        return [rm if r in st["masked"] else 0xFFFF for r in rntis]

    def _rule_force(self, ues: list[dict]) -> list[int]:
        """RULE_KIND=force（2026-10-04 機制對照，不是策略）：主機 /tmp/force_nodes（逗號列表，每秒重讀）列出的節點
        遮「壞 UE」＝前 FORCE_OBSERVE_S 秒平均 MCS 較低的活躍 UE（選定後整段黏住）；節點不在列表時全開並重置。
        要遮哪個節點由外部依場景真值指定，只用來驗證局部遮罩在完整拓樸下的效果。"""
        st = self.__dict__.setdefault("_force", {"mcs": {}, "masked": None, "ts": -1e9, "nodes": set()})
        now = time.monotonic()
        if now - st["ts"] > 1.0:
            st["ts"] = now
            try:
                with open(FORCE_NODES_FILE) as f:
                    st["nodes"] = {int(x) for x in f.read().replace("\n", ",").split(",") if x.strip()}
            except (OSError, ValueError):
                st["nodes"] = set()
        masks = [0xFFFF] * len(ues)
        if self.node_id not in st["nodes"]:
            if st["masked"] is not None or st["mcs"]:
                self._log.info("[force] node%d 解除強制遮罩", self.node_id)
            st["mcs"], st["masked"], st["n_seen"] = {}, None, 0
            return masks
        rntis = [int(ue["rnti"]) for ue in ues]
        if self.node_id <= 4:
            # relay（2026-10-05 修正）：relay 有兩個子節點 MT（is_iab_child=1）＋兩個直連 UE。舊版「遮 MCS 最高者以外」會遮到其中一個 MT
            # （v3 實測相位 0 下游 43.5→32.4），兩個 MT 都在線時差距條件又永遠不成立。改為直接遮全部直連 UE（is_iab_child=0），
            # 由外部依場景真值只在「relay UE 在邊緣」的相位把 relay 列進 /tmp/force_nodes。
            if st["masked"] is None:
                st["masked"] = True
                self._log.info("[force] node%d 遮全部直連 UE", self.node_id)
            rm = self._rule_mask()
            return [rm if not int(ue.get("is_iab_child", 0)) else 0xFFFF for ue in ues]
        if st["masked"] is None:
            # 2026-10-05 修正：相位剛開始時鏈路調適還在爬升，前 5 秒平均會把好 UE 誤判成壞 UE（v2 實測 5 個相位錯 2 個）。
            # 改為略過進入列表後的前 FORCE_SKIP_S 秒、再觀察 FORCE_OBSERVE_S 秒，且最低者須比次低者低 ≥ FORCE_MCS_GAP 才選定（否則繼續觀察）。
            st["n_seen"] = st.get("n_seen", 0) + 1
            if st["n_seen"] > FORCE_SKIP_S:
                for ue, r in zip(ues, rntis):
                    m = int(ue.get("wb_cqi", 0))
                    if int(ue.get("bsr", 0)) > 0 and m > 0:
                        st["mcs"].setdefault(r, []).append(m)
            ready = {r: v[-FORCE_OBSERVE_S:] for r, v in st["mcs"].items() if r in rntis and len(v) >= FORCE_OBSERVE_S}
            avg = {r: sum(v) / len(v) for r, v in ready.items()}
            srt = sorted(avg.values())
            gap = (srt[1] - srt[0]) if len(srt) >= 2 else 0
            if len(ready) >= 2 and gap >= FORCE_MCS_GAP:
                st["masked"] = frozenset([min(avg, key=avg.get)])   # access：只遮 MCS 最低者（relay 在上方另外處理）
                self._log.info("[force] node%d 遮 rnti=%s（平均 MCS %s）", self.node_id, sorted(st["masked"]),
                            {r: round(a, 1) for r, a in avg.items()})
        if st["masked"] is not None:
            rm = self._rule_mask()
            masks = [rm if r in st["masked"] else 0xFFFF for r in rntis]
        return masks

    def _rule_mask(self) -> int:
        """時域規則的遮罩：/tmp/rule_mask 存在時用它（每 5 秒重讀），否則用 RULE_MASK。"""
        now = time.monotonic()
        if now - getattr(self, "_rule_mask_ts", -1e9) > RULE_MASK_REFRESH_S:
            self._rule_mask_ts = now
            try:
                with open(RULE_MASK_FILE) as f:
                    self._rule_mask_val = int(f.read().strip(), 16) & 0xFFFF
            except (OSError, ValueError):
                self._rule_mask_val = RULE_MASK
        return self._rule_mask_val

    def _handle_shadow_request(self, ues: list[dict], t_recv: float,
                               rule_caps: Optional[list[int]] = None,
                               rule_masks: Optional[list[int]] = None) -> None:
        """
        記錄一筆 PF-shadow 資料，供 Critic 離線預訓練（pretrain_critic.py，LOCAL_DRL_V2_DESIGN.md §1）。

        每一步都存（含閒置步：線上經驗也包含 reward=0 的閒置轉換，V(s) 要學到同樣的分佈）：
          - state_vec/mask_vec：跟線上 Actor 完全相同的 _encode_current_state()（含 relational 特徵）
          - ues：原始 UE 狀態（含 bsr）。reward 要用「下一步」的 bsr 算（同線上 _build_rl_experience()
            的 R(A_{t-1}, S_t) 定義），由預訓練腳本把相鄰兩筆配對後以 compute_reward_breakdown() 算出
          - pf_actual_rbs／pf_rb_share：PF 這一週期實際分給各 UE 的 RB 數與份額，供分析 PF 行為用
            （不再當 Actor 的 BC 標籤——上限動作空間裡 PF 的動作就是全部不設上限，見 §1.5）

        fairness_bias 用中性值 1.0：shadow 模式不啟動 _fairness_sub_worker，且 Global xApp 讀的是
        node{N}_experiences、shadow 期間沒有資料可算；這一維在預訓練資料裡恆為中性。
        """
        if not ues:
            return

        state_vec, mask_vec = self._encode_current_state(ues, fairness_bias=1.0)

        raw_rbs = [max(float(ue.get("pf_actual_rbs", 0)), 0.0) for ue in ues]
        total_rbs = sum(raw_rbs)

        doc: dict[str, Any] = {
            "node_id":      self.node_id,
            "timestamp":    datetime.now(timezone.utc),
            "t_mono":       t_recv,          # 單調時鐘，預訓練配對相鄰兩筆時判斷是否中斷
            "scenario_tag": PF_SHADOW_SCENARIO_TAG,
            "state_vec":    state_vec.tolist(),
            "mask_vec":     mask_vec.tolist(),
            "ues":          ues,
            "pf_actual_rbs": raw_rbs,
            "pf_rb_share":  [r / total_rbs for r in raw_rbs] if total_rbs > 0 else None,
        }
        if rule_caps is not None:
            doc["rule_caps"] = rule_caps     # 本步回傳的 prb_abs（XAPP_MODE=rule）；pf_actual_rbs 是上一個視窗的實際分配
        if rule_masks is not None:
            doc["rule_masks"] = rule_masks   # 本步回傳的 slot_mask（RULE_KIND=mask）
            # v2.1 行為複製標籤：規則遮了幾個 UE → 宏動作（0＝不遮、2＝遮 1 個（0x1111）、3＝遮 2 個）
            k = sum(1 for m in rule_masks if int(m) != 0xFFFF)
            doc["rule_macro"] = 0 if k == 0 else (2 if k == 1 else 3)
            doc["rule_masked_idx"] = [i for i, m in enumerate(rule_masks) if int(m) != 0xFFFF]
        self._queue_experience(doc)

        if self._total_inferences % 500 == 0:
            self._log.info(
                "PF-SHADOW[%d] scenario=%s n_ue=%d total_rbs=%.0f p̂=%.3f ĉ=%.3f",
                self._total_inferences, PF_SHADOW_SCENARIO_TAG, len(ues), total_rbs,
                *self._current_relational(),
            )

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

        # 背景執行緒：relational state 特徵 p̂/ĉ（active/shadow 兩種模式都需要，state 定義要一致）
        self._rel_thread = threading.Thread(
            target=self._relational_worker,
            daemon=True,
            name=f"relational-node{self.node_id}",
        )
        self._rel_thread.start()

        # 背景執行緒 2/3/4（DRL 訓練、checkpoint 熱重載、全域公平性廣播接收）：
        # shadow 模式完全不做 DRL 推論/訓練，這三個執行緒沒有存在意義，略過啟動。
        if self.xapp_mode not in SHADOW_LIKE_MODES:
            self._train_thread = threading.Thread(
                target=self._train_worker,
                daemon=True,
                name=f"drl-train-node{self.node_id}",
            )
            self._train_thread.start()

            self._reload_thread = threading.Thread(
                target=self._reload_worker,
                daemon=True,
                name=f"ckpt-reload-node{self.node_id}",
            )
            self._reload_thread.start()

            if self._fairness_sub_sock is not None:
                self._fairness_thread = threading.Thread(
                    target=self._fairness_sub_worker,
                    daemon=True,
                    name=f"fairness-sub-node{self.node_id}",
                )
                self._fairness_thread.start()

        self._log.info(
            "Node %d 推論伺服器啟動 | 模式: %s | 等待 C xApp 請求...",
            self.node_id,
            f"PF-shadow（{PF_SHADOW_SCENARIO_TAG}）" if self.xapp_mode == "shadow"
            else f"固定規則上限（{PF_SHADOW_SCENARIO_TAG}，MCS≤{RULE_BAD_MCS} 且落差≥{RULE_MCS_GAP} → {RULE_CAP}）"
            if self.xapp_mode == "rule"
            else ("DRL" if self._agent.is_trained else "BSR 啟發式（收集資料中）"),
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
                    # 子節點類型（2026-10-01）：relay 節點 E2 回報的子節點依附著順序排列，access MT 一定先附著
                    # （relay 直連 UE 由 start_relay_ues.sh 在 13/13 E2 之後才啟動），前 len(CHILDREN_OF) 個即 MT。
                    n_iab = len(CHILDREN_OF.get(self.node_id, ()))
                    for i_ue, u_ in enumerate(ues):
                        u_["is_iab_child"] = 1 if i_ue < n_iab else 0
                    # Backhaul-aware 可用 PRB 比例（E2 回報，xApp 帶入 JSON 的 bh_ratio）；缺欄位/非法值→1.0（池子全開）
                    try:
                        bh = float(payload.get("bh_ratio", 1.0))
                        self._bh_ratio = min(1.0, max(0.0, bh)) if bh == bh else 1.0   # bh==bh 排除 NaN
                    except (TypeError, ValueError):
                        self._bh_ratio = 1.0

                    # 發佈給 relational 背景執行緒（parent/children 節點會讀這兩個值算它們的 p̂/ĉ）
                    total_buf = sum(max(float(u.get("dl_buffer_info", 0)), 0.0) for u in ues)
                    with self._rel_lock:
                        self._my_status = (self._bh_ratio, total_buf)

                    # ── PF-shadow 模式：完全獨立的極簡分支，處理完直接 continue ──
                    # 不做 DRL 推論/訓練/prev_* 狀態追蹤，只偷看 state 存起來，
                    # 回傳「解除上限」讓 MAC 排程器維持純 PF 行為。見
                    # LOCAL_DRL_V2_DESIGN.md §1.3。
                    if self.xapp_mode in SHADOW_LIKE_MODES:
                        caps, masks = self._rule_decide(ues)
                        self._handle_shadow_request(ues, t_recv,
                                                    rule_caps=caps if self.xapp_mode == "rule" else None,
                                                    rule_masks=masks if self.xapp_mode == "rule" else None)
                        self._masked_rntis = {int(ue["rnti"]) for ue, m in zip(ues, masks) if int(m) != 0xFFFF}
                        response = json.dumps(
                            {
                                "allocations": [
                                    {"rnti": int(ue["rnti"]), "prb_abs": int(c), "slot_mask": int(m)}
                                    for ue, c, m in zip(ues, caps, masks)
                                ]
                            },
                            separators=(",", ":"),
                        )
                        self._zmq_sock.send_string(response)
                        self._total_inferences += 1
                        continue

                    # ── 狀態 debug log（每 500 次）────────────────────────
                    if self._total_inferences % 500 == 0 and ues:
                        ue_summary = " ".join(
                            f"rnti={u.get('rnti',0)} delta_tbs={u.get('bsr',0)} mcs={u.get('wb_cqi',0)}"
                            for u in ues
                        )
                        self._log.info("STATE[%d] bh_ratio=%.3f %s", self._total_inferences, self._bh_ratio, ue_summary)

                    # ── 計算上一步的獎勵並寫入 MongoDB ────────────────────
                    # 閒置轉換（delta_tbs 全為 0，例如 P_IDLE 造成的無流量
                    # 期間）也要寫入，讓 GRU 訓練資料的序列分佈跟推論時
                    # _actor_hidden 實際會連續經歷的分佈一致（見
                    # DRL_DESIGN.md／snuggly-weaving-twilight 計畫）；
                    # 只在 UE 完全消失（xApp 重連後的空白 state）時才跳過，
                    # 這種情況下 prev/curr 不是同一組 UE，reward 沒有意義。
                    #
                    # 新鮮度檢查（STALE_PREV_UES_THRESHOLD_S，見上方常數說明）：
                    # 即使 ues 非空，若跟上一筆請求的時間差超過門檻，代表中間
                    # 發生過中斷（典型情境：FlexRIC/DU 崩潰後完整重啟，RNTI
                    # 重新分配），prev_ues 描述的是一組已經不存在的 UE，不能
                    # 拿來跟 curr_ues 配對計算 reward——即使兩邊剛好有 UE
                    # 數量相同也一樣，RNTI 對不上就是對不上。
                    is_stale = (
                        self._prev_ts is not None
                        and (t_recv - self._prev_ts) > STALE_PREV_UES_THRESHOLD_S
                    )
                    if is_stale:
                        self._log.warning(
                            "偵測到 %.1fs 的請求中斷（> %.1fs 門檻），視為連線中斷後重新開始，"
                            "捨棄上一筆暫存狀態，不計算這一步的 reward",
                            t_recv - self._prev_ts, STALE_PREV_UES_THRESHOLD_S,
                        )

                    if (
                        not is_stale
                        and self._prev_ues is not None
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
                            prev_behavior_logp=self._prev_behavior_logp,
                            prev_macro=self._prev_macro,
                            prev_factored=self._prev_factored,
                            prev_hold=self._prev_hold,
                        )
                        self._queue_experience(exp_doc)

                    # ── 執行推論 ──────────────────────────────────────────
                    feat_snap = (self._current_fairness_bias(), *self._current_relational())
                    rnti_set = frozenset(int(u.get("rnti", 0)) for u in ues)
                    hc = self._hold_cur
                    if ACTION_HOLD > 1 and self._hold_left > 0 and hc is not None and hc["rntis"] == rnti_set:
                        # 動作持續中：沿用決策當下的遮罩，不重新抽樣（behavior_logp 只記在決策那一步）
                        allocations = [dict(a) for a in hc["allocs"]]
                        action_ratios, behavior_logp = hc["ratios"], None
                        self._hold_left -= 1
                        hc["pos"] += 1
                        cur_hold, cur_macro, cur_factored = (hc["id"], hc["pos"]), hc["macro"], hc["factored"]
                    else:
                        allocations, action_ratios, behavior_logp = self._infer(ues, feat_snap)
                        cur_macro, cur_factored = self._agent.last_macro, self._agent.last_factored
                        cur_hold = None
                        self._hold_left, self._hold_cur = 0, None
                        if ACTION_HOLD > 1 and behavior_logp is not None and allocations:
                            self._hold_seq += 1
                            hid = f"{self.node_id}-{int(time.time())}-{self._hold_seq}"
                            self._hold_cur = {"id": hid, "pos": 0, "rntis": rnti_set, "ratios": action_ratios,
                                              "allocs": [dict(a) for a in allocations],
                                              "macro": cur_macro, "factored": cur_factored}
                            self._hold_left = ACTION_HOLD - 1
                            cur_hold = (hid, 0)
                    self._total_inferences += 1

                    # ── 暫存本步狀態（下一步計算獎勵用）─────────────────
                    if ues and allocations:
                        self._prev_ues = ues
                        self._prev_allocations = allocations
                        # 用推論當下同一份特徵快照編碼（不能重新讀快取：背景執行緒可能已更新
                        # fairness_bias/p̂/ĉ，存下的 state 就不是策略實際看到的 state，PPO 比例會錯）
                        self._prev_state_vec, self._prev_mask_vec = self._agent.encode_state(
                            ues, fairness_bias=feat_snap[0], bh_ratio=self._bh_ratio,
                            parent_trend=feat_snap[1], children_demand=feat_snap[2],
                            prev_masked=self._masked_rntis,
                        )
                        self._prev_action_ratios = action_ratios
                        self._prev_macro = cur_macro
                        self._prev_factored = cur_factored
                        self._prev_hold = cur_hold
                        self._prev_behavior_logp = behavior_logp
                        self._prev_ts = t_recv
                    else:
                        # 無活躍 UE 時清除暫存，避免跨不同 UE 組合計算獎勵。
                        # 這是真正的 UE 斷線（而非流量閒置——閒置時 ues 仍
                        # 非空，C xApp 持續送請求），才重置 GRU 隱藏狀態，
                        # 避免舊 UE 組合的記憶污染下一組完全不同的 UE。
                        self._prev_ues = None
                        self._prev_allocations = None
                        self._prev_ts = None
                        self._agent.reset_hidden()

                    # ── 回傳結果（必須在 5ms 內完成）────────────────────
                    response = json.dumps(
                        {"allocations": allocations}, separators=(",", ":")
                    )
                    self._zmq_sock.send_string(response)
                    # 下一步 state 的 was_masked（必須在本步 state 都編碼完之後才更新）
                    self._masked_rntis = {int(a["rnti"]) for a in (allocations or [])
                                          if int(a.get("slot_mask", 0xFFFF)) != 0xFFFF}

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

        if self._fairness_sub_sock is not None:
            self._fairness_sub_sock.close()
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
