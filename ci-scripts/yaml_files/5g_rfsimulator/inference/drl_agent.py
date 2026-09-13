"""
drl_agent.py — DRL Actor-Critic Agent for Local PRB Allocation

架構（2026-07-09 起：MLP → GRU）：
  - Actor Network : GRU + MLP head + Masked Softmax → PRB 分配比例
  - Critic Network: GRU + MLP head → 狀態價值估計 V(s)
  - 演算法        : 離線 Advantage Actor-Critic (A2C)，序列化版本
                    從 MongoDB 讀取「時間連續的經驗序列」進行批次更新

  為什麼從純 MLP 改成 GRU：原本的 state 是無記憶的單步快照，即使加了
  dl_buffer_info（見下方），也只看得到「當下」，看不出趨勢（例如某個 UE 的
  buffer 是在成長還是萎縮）。GRU 讓 policy 自己學會維護記憶，不需要手動設計
  delta 特徵或疊幀。推論時 Actor 的隱藏狀態跨 ZMQ 呼叫持久化（見
  DRLAgent._actor_hidden／reset_hidden()）；訓練時每個序列一律從零初始化的
  隱藏狀態開始（見 train_on_batch()）。詳見 DRL_DESIGN.md。

State Space (固定長度向量，不足補零)：
  [norm_bsr_0, norm_cqi_0, norm_buf_0, norm_bsr_1, norm_cqi_1, norm_buf_1, ..., active_ratio, fairness_bias]
  長度 = MAX_UE_COUNT * 3 + 2 = 50

  norm_buf（dl_buffer_info，真實 RLC 佇列位元組數）：norm_bsr（Δtbs）與
  norm_cqi（dl_mcs1）在 UE 沒有排隊資料時會同時凍結在舊值（OAI 排程器直接
  跳過無資料的 UE，見 gNB_scheduler_dlsch.c），無法區分「無資料可傳」與
  「有資料但通道差/PRB 不足」。norm_buf 不受「是否被排程」影響。

  fairness_bias（Stage 2 起改版，取代舊版 2026-07-09 的 prb_quota_ratio）：
  由獨立的 Global xApp process（global_xapp.py）每隔數秒讀取全部 12 個節點
  最近的 MongoDB 經驗，算出「本節點吞吐量相對全域平均的落差」並廣播回來
  （低於全域平均 → bias > 1，代表被犧牲、可以更積極）。這是純粹的 state
  輸入特徵，不做任何硬性 PRB 裁切——資源池大小仍完全由 C 層「Backhaul-aware
  動態 PRB 預算」機制（gNB_scheduler_dlsch.c）獨立決定，兩者不會疊加節流。
  全部 12 個節點（relay/access 皆同）都接收同一套機制，無特殊分支。收到
  訊號前預設中性值 1.0。正規化：原始 bias 落在 [0.5, 2.0]，寫入 state 前線性
  映射到 [0, 1]（見 encode_state()）。

Action Space：
  各 UE 的 PRB 分配比例 [0, 1]，總和為 1.0
  非活躍 UE slot 的比例透過 mask 強制為 0。

訓練方式：
  InferenceServer 的背景執行緒每 TRAIN_INTERVAL_S 秒呼叫 train_on_batch()，
  從 MongoDB 取得「時間連續的經驗序列」（training_pipeline.py 的
  fetch_sequences()）進行梯度更新，不再是打散的獨立經驗。
"""

from __future__ import annotations

import logging
import math
import os
from pathlib import Path
from typing import Optional

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

from reward_calculator import JFI_MIN, REWARD_MODE

# =============================================================================
# 超參數
# =============================================================================

MAX_UE_COUNT: int = 16
STATE_DIM: int = MAX_UE_COUNT * 3 + 2   # [bsr, cqi, buf] × N + active_ratio + fairness_bias

# fairness_bias 正規化區間（Global xApp 廣播的原始值域），見 encode_state()
FAIRNESS_BIAS_MIN: float = 0.5
FAIRNESS_BIAS_MAX: float = 2.0

MAX_BSR: float = 1_000_000.0            # DL delta-TBS 正規化上限 (bytes/100ms, ≈80 Mbps)
                                         # C xApp rate limiter 每 10 個 MAC callback 才送一次 ZMQ，
                                         # 測量窗口為 100ms，故上限 = 100,000 × 10 = 1,000,000
MAX_BUF_INFO: float = 2_000_000.0       # dl_buffer_info（RLC 佇列位元組數）正規化上限
                                         # 現場實測（2026-07-09，Scenario R）：node3/5 最大值
                                         # 約 2,147,000，node4 約 124,000，上限抓 2,000,000 讓多數
                                         # 觀測值落在有解析度的區間，跟 reward_calculator.py 既有
                                         # 的 MAX_BSR=2,000,000 常數同量級，非巧合
GAMMA: float = 0.95                      # 折扣因子

LR_ACTOR: float = 1e-4
LR_CRITIC: float = 3e-4
HIDDEN_DIM: int = 128
MIN_TRAIN_EXPERIENCES: int = 200         # 觸發第一次訓練所需的最少「原始經驗」數
                                         # （在序列切窗之前的門檻，training_pipeline.py 用）

# 序列化訓練參數（2026-07-09 取代舊版 TRAIN_BATCH_SIZE，GRU 需要時間連續的
# 序列而不是打散的獨立經驗，見 training_pipeline.py 的 fetch_sequences()）：
TRAIN_SEQ_LEN: int = 32          # 每個訓練序列的步數，~3.2 秒涵蓋範圍（100ms cadence）
                                  # 遠小於一個流量相位的典型長度（~600 步，見
                                  # traffic_scenario.py 預設 phase_duration=60s），
                                  # 確保序列不會跨越相位邊界內部
TRAIN_SEQ_COUNT: int = 16        # 每次梯度更新用幾個序列（16×32=512 筆原始經驗）
                                  # 刻意比舊版 TRAIN_BATCH_SIZE=128 大：序列內部樣本
                                  # 時間相關（不像舊版打散抽樣是獨立的），需要更多
                                  # 原始經驗才能得到同樣品質的梯度估計，這是
                                  # BPTT-based RL 的標準做法
MIN_TRAIN_SEQUENCES: int = TRAIN_SEQ_COUNT   # 訓練門檻：候選序列池至少要有一個 batch 的量

# Entropy 正則化係數：entropy_coeff = max(ENTROPY_COEFF_MIN, ENTROPY_COEFF_INIT × ENTROPY_DECAY_RATE ^ step)
# 修正記錄：舊版下限誤寫成跟初始值相同的 0.01，導致 max(0.01, 0.01×0.997^t) 對任何 t>0
# 恆等於 0.01，衰減公式形同死碼。下限改為遠小於初始值的 0.001，衰減才會真的生效。
ENTROPY_COEFF_INIT: float = 0.01
ENTROPY_DECAY_RATE: float = 0.997
ENTROPY_COEFF_MIN: float = 0.001

# Dirichlet 策略集中度 K：α = probs × K，K 越大越確定性、越小探索性越強。
# 修正記錄：舊版是寫死常數 K=5，訓練前後不變，代表即使 policy 已收斂，infer() 實際
# 下發的分配仍帶有恆定雜訊，不會隨訓練減少。改為隨 self._train_steps 指數退火，
# 從 DIRICHLET_K_MIN 逐漸升高至 DIRICHLET_K_MAX（見 DRLAgent._current_concentration()），
# 讓推論階段的隨機性隨訓練收斂真正變小，同時仍保留隨機策略架構（不改成完全確定性輸出，
# 維持系統持續探索、持續學習的能力）。
DIRICHLET_K_MIN: float = 5.0             # 訓練初期集中度（與舊版固定值相同，不改變早期探索行為）
DIRICHLET_K_MAX: float = 50.0            # 訓練後期集中度上限，大幅降低採樣雜訊
DIRICHLET_K_ANNEAL_TAU: float = 5000.0   # 退火時間常數（訓練步數），越大退火越慢

# Lagrangian 限制式的乘子 λ：reward = R_tp + λ·(JFI_raw - JFI_MIN)（見
# reward_calculator.py）。λ 不是手動設的常數，訓練迴圈裡用下面的簡單規則
# 自動調整：JFI 低於門檻時 λ 變大加重懲罰，達標時 λ 趨近 0 全力衝 throughput。
LAMBDA_INIT: float = 0.0     # 初始假設限制式已滿足，不額外懲罰
LAMBDA_LR:   float = 0.02    # λ 每次 mini-batch 更新的步長
LAMBDA_MAX:  float = 10.0    # 安全上限，避免 JFI 持續低於門檻時 λ 無界成長、
                              # 最終讓 fairness 項完全壓過 throughput 項


# =============================================================================
# 神經網路定義（GRU-based，2026-07-09）
# =============================================================================

class ActorNetwork(nn.Module):
    """
    Policy Network：state 序列 → GRU 隱藏狀態 → PRB 分配 logits → Masked Softmax。

    非活躍 UE slot 在 softmax 前被設為 -∞，確保輸出比例為 0。
    """

    def __init__(
        self,
        state_dim: int = STATE_DIM,
        max_ues: int = MAX_UE_COUNT,
    ) -> None:
        super().__init__()
        self.gru = nn.GRU(state_dim, HIDDEN_DIM, num_layers=1, batch_first=True)
        self.head = nn.Sequential(
            nn.Linear(HIDDEN_DIM, HIDDEN_DIM),
            nn.ReLU(),
            nn.Linear(HIDDEN_DIM, max_ues),
        )

    def forward(
        self,
        state: torch.Tensor,             # (batch, seq_len, state_dim)
        mask: torch.Tensor,              # (batch, seq_len, max_ues)  True = 活躍 UE
        hidden: Optional[torch.Tensor] = None,   # (1, batch, HIDDEN_DIM)
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """回傳 (各 UE 的 PRB 分配比例 (batch, seq_len, max_ues), 新的隱藏狀態)。"""
        gru_out, new_hidden = self.gru(state, hidden)   # gru_out: (batch, seq_len, HIDDEN_DIM)
        logits = self.head(gru_out)                      # (batch, seq_len, max_ues)
        logits = logits.masked_fill(~mask, -1e9)          # 遮蔽非活躍 slot
        probs = F.softmax(logits, dim=-1)
        return probs, new_hidden


class CriticNetwork(nn.Module):
    """
    Value Network：state 序列 → GRU 隱藏狀態 → V(s)。

    只在訓練時使用（train_on_batch()/evaluate_on_batch()），每個序列一律從
    零初始化的隱藏狀態開始。跟 ActorNetwork 不同，Critic 不需要跨 infer()
    呼叫持久化隱藏狀態——infer() 從不呼叫 Critic。
    """

    def __init__(self, state_dim: int = STATE_DIM) -> None:
        super().__init__()
        self.gru = nn.GRU(state_dim, HIDDEN_DIM, num_layers=1, batch_first=True)
        self.head = nn.Sequential(
            nn.Linear(HIDDEN_DIM, 64),
            nn.ReLU(),
            nn.Linear(64, 1),
        )

    def forward(
        self,
        state: torch.Tensor,     # (batch, seq_len, state_dim)
        hidden: Optional[torch.Tensor] = None,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """回傳 (狀態價值估計 (batch, seq_len), 新的隱藏狀態)。"""
        gru_out, new_hidden = self.gru(state, hidden)
        value = self.head(gru_out).squeeze(-1)   # (batch, seq_len)
        return value, new_hidden


# =============================================================================
# DRLAgent
# =============================================================================

class DRLAgent:
    """
    PRB 分配的 Actor-Critic DRL Agent（GRU-based，序列化 Offline A2C）。

    典型使用流程：
        agent = DRLAgent(node_id=1)
        agent.load()                     # 嘗試載入預存權重

        # 每 ~100ms 推論一次（由 InferenceServer 呼叫），Actor 隱藏狀態
        # 跨呼叫持久化
        allocations = agent.infer(ues, fairness_bias)

        # 每 TRAIN_INTERVAL_S 秒訓練一次（由背景執行緒呼叫），用時間連續的
        # 經驗序列，訓練時隱藏狀態一律歸零重新開始
        metrics = agent.train_on_batch(sequences)
        agent.save()
    """

    def __init__(
        self,
        node_id: int,
        model_dir: str = "/app/models",
        total_prb: int = 106,
        device: Optional[str] = None,
    ) -> None:
        self.node_id = node_id
        self.model_dir = Path(model_dir)
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self.total_prb = total_prb
        self.device = torch.device(
            device if device else ("cuda" if torch.cuda.is_available() else "cpu")
        )

        self.actor = ActorNetwork().to(self.device)
        self.critic = CriticNetwork().to(self.device)
        self.actor_opt = optim.Adam(self.actor.parameters(), lr=LR_ACTOR)
        self.critic_opt = optim.Adam(self.critic.parameters(), lr=LR_CRITIC)

        # _is_trained=False 時 InferenceServer 退回 BSR 啟發式
        self._is_trained: bool = False
        self._train_steps: int = 0

        # Lagrangian 限制式的乘子，見 LAMBDA_INIT/LAMBDA_LR/LAMBDA_MAX 說明
        self._lambda: float = LAMBDA_INIT

        # 推論時 Actor 的隱藏狀態，跨 infer() 呼叫持久化（見 reset_hidden()）。
        # 不存進 checkpoint——見 save()/load() 的說明。
        self._actor_hidden: Optional[torch.Tensor] = None

        self._log = logging.getLogger(f"drl_agent_node{node_id}")

    @property
    def lambda_(self) -> float:
        """目前的 Lagrangian 乘子，供 inference_server.py 計算 reward 時讀取。"""
        return self._lambda

    def reset_hidden(self) -> None:
        """
        重置推論時 Actor 的隱藏狀態。

        呼叫時機（見 DRL_DESIGN.md 完整說明）：
          - 真正的 UE 斷線（ues 變空），不是流量閒置——流量閒置的緩降/持平/
            恢復軌跡正是要 GRU 捕捉的訊號，不應該被重置抹掉
          - load() 內部（換權重後，舊隱藏狀態是用舊網路產生的，對新網路是
            未定義輸入）
        """
        self._actor_hidden = None

    # -------------------------------------------------------------------------
    # Dirichlet 集中度退火
    # -------------------------------------------------------------------------

    def _current_concentration(self) -> float:
        """
        Dirichlet 集中度 K 隨 self._train_steps 指數退火，從 DIRICHLET_K_MIN
        逐漸趨近 DIRICHLET_K_MAX，讓推論階段的採樣雜訊隨訓練收斂真正變小。
        """
        progress = 1.0 - math.exp(-self._train_steps / DIRICHLET_K_ANNEAL_TAU)
        return DIRICHLET_K_MIN + (DIRICHLET_K_MAX - DIRICHLET_K_MIN) * progress

    # -------------------------------------------------------------------------
    # 狀態編碼
    # -------------------------------------------------------------------------

    def encode_state(
        self,
        ues: list[dict],
        fairness_bias: float = 1.0,
    ) -> tuple[np.ndarray, np.ndarray]:
        """
        將 UE 列表編碼為固定長度的 numpy 向量。

        Args:
            ues           : UE 狀態列表
            fairness_bias : Global xApp 廣播的全域公平性偏差，原始值域
                            [FAIRNESS_BIAS_MIN, FAIRNESS_BIAS_MAX]，尚未收到
                            廣播或無資料時傳中性值 1.0（預設值）。

        回傳：
            state_vec : (STATE_DIM,)  float32
            mask_vec  : (MAX_UE_COUNT,) bool，True = 活躍 UE
        """
        n = min(len(ues), MAX_UE_COUNT)
        state_vec = np.zeros(STATE_DIM, dtype=np.float32)
        mask_vec = np.zeros(MAX_UE_COUNT, dtype=bool)

        for i, ue in enumerate(ues[:n]):
            bsr = float(ue.get("bsr", 0))          # delta_dl_aggr_tbs (bytes)
            mcs = float(ue.get("wb_cqi", 0))       # dl_mcs1 (0-28, mapped to wb_cqi key)
            buf = float(ue.get("dl_buffer_info", 0))  # 真實 RLC 佇列位元組數，不受排程與否影響
            # Log 正規化 delta TBS → [0, 1]
            state_vec[i * 3]     = np.log1p(bsr) / np.log1p(MAX_BSR)
            # 正規化 MCS → [0, 1]（MCS=0 合法，反映低通道品質）
            state_vec[i * 3 + 1] = mcs / 28.0
            # Log 正規化 buffer occupancy → [0, 1]（同樣用 log，數值跨數量級）
            state_vec[i * 3 + 2] = np.log1p(buf) / np.log1p(MAX_BUF_INFO)
            mask_vec[i] = True

        # 活躍 UE 比例作為全域 context 特徵
        state_vec[MAX_UE_COUNT * 3] = n / MAX_UE_COUNT
        # Global xApp 廣播的全域公平性偏差，線性映射 [BIAS_MIN,BIAS_MAX] → [0,1]
        clipped_bias = np.clip(fairness_bias, FAIRNESS_BIAS_MIN, FAIRNESS_BIAS_MAX)
        state_vec[MAX_UE_COUNT * 3 + 1] = float(
            (clipped_bias - FAIRNESS_BIAS_MIN) / (FAIRNESS_BIAS_MAX - FAIRNESS_BIAS_MIN)
        )

        return state_vec, mask_vec

    def _to_tensors(
        self,
        state_vec: np.ndarray,
        mask_vec: np.ndarray,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """將 numpy 向量轉換為 (batch=1, seq_len=1, *) 的 tensor，供 infer() 單步推論用。"""
        state_t = torch.tensor(
            state_vec, dtype=torch.float32, device=self.device
        ).unsqueeze(0).unsqueeze(0)    # (1, 1, state_dim)
        mask_t = torch.tensor(
            mask_vec, dtype=torch.bool, device=self.device
        ).unsqueeze(0).unsqueeze(0)    # (1, 1, max_ues)
        return state_t, mask_t

    # -------------------------------------------------------------------------
    # 推論
    # -------------------------------------------------------------------------

    def infer(
        self,
        ues: list[dict],
        fairness_bias: float = 1.0,
    ) -> tuple[list[dict], np.ndarray]:
        """
        執行 DRL Actor 推論，回傳 PRB 分配結果與比例向量。

        Args:
            ues           : [{"rnti": int, "bsr": int, "wb_cqi": int}, ...]
            fairness_bias : Global xApp 廣播的全域公平性偏差，見 encode_state()

        Returns:
            allocations   : [{"rnti": int, "prb_abs": int}, ...]
            action_ratios : (MAX_UE_COUNT,) float32，用於 MongoDB 儲存
        """
        if not ues:
            return [], np.zeros(MAX_UE_COUNT, dtype=np.float32)

        n = min(len(ues), MAX_UE_COUNT)
        state_vec, mask_vec = self.encode_state(ues, fairness_bias)
        state_t, mask_t = self._to_tensors(state_vec, mask_vec)

        self.actor.eval()
        with torch.no_grad():
            probs_seq, new_hidden = self.actor(state_t, mask_t, self._actor_hidden)
            self._actor_hidden = new_hidden.detach()   # 跨呼叫持久化，見 reset_hidden()
            probs = probs_seq[0, 0]                    # 攤平回 (MAX_UE_COUNT,)，下方邏輯不變

            # Dirichlet 隨機策略：從 Dirichlet(α = probs[:n] × K) 採樣
            # 確保 action_ratios ≠ actor probs，訓練時 log π(a|s) 梯度有效
            # K 隨訓練步數退火（見 _current_concentration()），訓練越久取樣越集中
            alpha = torch.clamp(probs[:n] * self._current_concentration(), min=1e-3)
            dist = torch.distributions.Dirichlet(alpha)
            active_ratios = dist.sample().cpu().numpy()     # (n,)，加總恰好為 1

        # 轉換為整數 PRB，修正捨入誤差
        prb_floats = active_ratios * self.total_prb
        prb_ints = prb_floats.astype(np.int32)
        remainder = int(self.total_prb - prb_ints.sum())
        if remainder > 0:
            fracs = prb_floats - prb_ints
            top_idx = int(np.argmax(fracs))
            prb_ints[top_idx] += remainder

        # 確保每個活躍 UE 至少分配 5 個 PRB（防止極端分配觸發 MAC 層 SIGSEGV）
        MIN_PRB = 5
        for i in range(n):
            if prb_ints[i] < MIN_PRB:
                prb_ints[i] = MIN_PRB
        # 修正因保底導致總和超過 total_prb：從最大的逐一扣除
        overflow = int(prb_ints.sum()) - self.total_prb
        if overflow > 0:
            for idx in np.argsort(prb_ints)[::-1]:
                can_remove = prb_ints[idx] - MIN_PRB
                remove = min(can_remove, overflow)
                prb_ints[idx] -= remove
                overflow -= remove
                if overflow <= 0:
                    break

        allocations = [
            {"rnti": int(ues[i]["rnti"]), "prb_abs": int(prb_ints[i])}
            for i in range(n)
        ]

        # 儲存 Dirichlet 採樣值（四捨五入前），供訓練時計算 log π(a|s)
        action_ratios = np.zeros(MAX_UE_COUNT, dtype=np.float32)
        action_ratios[:n] = active_ratios

        return allocations, action_ratios

    # -------------------------------------------------------------------------
    # 離線訓練（序列化版本）
    # -------------------------------------------------------------------------

    @staticmethod
    def _filter_valid_sequences(sequences: list[list[dict]]) -> list[list[dict]]:
        """
        防禦性過濾：排除內含 state_vec/next_state_vec 維度不對的殘留舊資料的
        序列（例如 STATE_DIM 曾經變更過，清空重來時若沒清乾淨就會混進舊維度
        的文件，混進 np.array() 建構會直接拋 inhomogeneous shape 例外炸掉整個
        訓練執行緒）。整個序列只要有一筆不合格就整段丟棄（維度錯誤的那筆
        之後的連續性也不再可信）。
        """
        valid = []
        for seq in sequences:
            if all(
                len(e.get("state_vec", [])) == STATE_DIM
                and len(e.get("next_state_vec", [])) == STATE_DIM
                for e in seq
            ):
                valid.append(seq)
        return valid

    def _build_sequence_tensors(
        self, sequences: list[list[dict]]
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        """
        把 n_seq 個長度 L 的經驗序列，組成 Critic 用的延伸狀態序列（長度 L+1：
        L 個 state_vec + 最後一筆的 next_state_vec）與對應的 rewards/actions。

        善用連續性保證（fetch_sequences() 已確保每個序列內部連續）：
        seq[j]["next_state_vec"] == seq[j+1]["state_vec"]，所以延伸序列裡
        「中間」的狀態不用重複塞兩次，只要最後補一筆 next_state_vec 即可。

        Returns:
            ext_states : (n_seq, L+1, STATE_DIM)
            ext_masks  : (n_seq, L+1, MAX_UE_COUNT)
            rewards    : (n_seq, L)
            actions    : (n_seq, L, MAX_UE_COUNT)
        """
        ext_states_list = []
        ext_masks_list = []
        rewards_list = []
        actions_list = []
        for seq in sequences:
            states = [e["state_vec"] for e in seq] + [seq[-1]["next_state_vec"]]
            masks = [e["mask_vec"] for e in seq] + [seq[-1]["next_mask_vec"]]
            ext_states_list.append(states)
            ext_masks_list.append(masks)
            rewards_list.append([e["reward"] for e in seq])
            actions_list.append([e["action_ratios"] for e in seq])

        ext_states = torch.tensor(np.array(ext_states_list, dtype=np.float32), device=self.device)
        ext_masks  = torch.tensor(np.array(ext_masks_list,  dtype=bool),       device=self.device)
        rewards    = torch.tensor(np.array(rewards_list,    dtype=np.float32), device=self.device)
        actions    = torch.tensor(np.array(actions_list,    dtype=np.float32), device=self.device)
        return ext_states, ext_masks, rewards, actions

    def _dirichlet_log_probs_and_entropy(
        self,
        probs_seq: torch.Tensor,   # (n_seq, L, MAX_UE_COUNT)
        masks_seq: torch.Tensor,   # (n_seq, L, MAX_UE_COUNT)
        actions_seq: torch.Tensor, # (n_seq, L, MAX_UE_COUNT)
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """
        對攤平後的每個 (序列, 時間步) 樣本分別計算 Dirichlet log π(a|s) 與
        entropy（每個樣本的活躍 UE 數量不同，需逐一處理，跟舊版單步邏輯相同，
        只是這裡多一層序列維度要先攤平）。

        Returns:
            log_probs_t : (n_seq*L,)
            entropy_t   : (n_seq*L,)
        """
        n_seq, seq_len = probs_seq.shape[0], probs_seq.shape[1]
        log_probs_list: list[torch.Tensor] = []
        entropy_list: list[torch.Tensor] = []
        for i in range(n_seq):
            for j in range(seq_len):
                n_ij = int(masks_seq[i, j].sum().item())
                if n_ij == 0:
                    log_probs_list.append(torch.tensor(0.0, device=self.device))
                    entropy_list.append(torch.tensor(0.0, device=self.device))
                    continue

                # 用「目前」的退火後 K 重新計算 log_prob，而非該筆經驗被採樣當下的 K
                # （replay buffer 可能橫跨數千步、K 已經改變）。這是 Offline A2C 既有
                # 的 off-policy 近似之一，不做嚴格的重要性採樣校正；經驗本身的 staleness
                # （actor 權重也已經改變）已經是同等級的近似，多這一項不改變近似的性質。
                alpha_ij = torch.clamp(
                    probs_seq[i, j, :n_ij] * self._current_concentration(), min=1e-3
                )
                dist_ij = torch.distributions.Dirichlet(alpha_ij)

                a_ij = actions_seq[i, j, :n_ij]
                a_sum = a_ij.sum()
                if a_sum < 1e-8:
                    log_probs_list.append(torch.tensor(0.0, device=self.device))
                    entropy_list.append(dist_ij.entropy())
                    continue
                a_ij = torch.clamp(a_ij / a_sum, min=1e-6)
                a_ij = a_ij / a_ij.sum()

                log_probs_list.append(dist_ij.log_prob(a_ij))
                entropy_list.append(dist_ij.entropy())

        return torch.stack(log_probs_list), torch.stack(entropy_list)

    def train_on_batch(self, sequences: list[list[dict]]) -> dict:
        """
        從 MongoDB 取得的「時間連續經驗序列」進行 Actor-Critic 離線更新。

        Args:
            sequences : list of sequences，每個序列是長度 TRAIN_SEQ_LEN 的
                        經驗 dict 列表，按時間順序排列，保證內部時間連續
                        （由 training_pipeline.fetch_sequences() 保證，
                        DRLAgent 本身不重新驗證連續性，只驗證維度）。

        Experience document schema（序列裡每個元素）：
          {
            "state_vec"     : list[float],  長度 STATE_DIM
            "mask_vec"      : list[bool],   長度 MAX_UE_COUNT
            "action_ratios" : list[float],  長度 MAX_UE_COUNT
            "reward"        : float,
            "next_state_vec": list[float],  長度 STATE_DIM
            "next_mask_vec" : list[bool],   長度 MAX_UE_COUNT
            "jfi_raw"       : float,        (可能缺失，見 λ 更新的過濾邏輯)
            "r_throughput"  : float,        (可能缺失，用於排除閒置樣本的 λ 平均)
          }

        Returns:
            metrics : dict with training statistics
        """
        sequences = self._filter_valid_sequences(sequences)
        if len(sequences) < TRAIN_SEQ_COUNT:
            self._log.info(
                "序列數量不足 (有 %d 筆，需 %d 筆)，跳過訓練",
                len(sequences), TRAIN_SEQ_COUNT,
            )
            return {}

        idxs = np.random.choice(len(sequences), TRAIN_SEQ_COUNT, replace=False)
        batch = [sequences[i] for i in idxs]
        seq_len = len(batch[0])   # 所有序列長度應相同（TRAIN_SEQ_LEN），取第一筆

        # ── Lagrangian 乘子 λ 更新 ──────────────────────────────────────────
        # 攤平這個 batch 裡所有 (序列, 時間步) 樣本的 jfi_raw 求平均。
        # 閒置轉換（r_throughput 接近 0）現在也會被寫進 MongoDB（見
        # inference_server.py），但閒置狀態下 jfi_raw 恆為 0、不代表真的
        # 不公平，必須排除在 λ 平均之外，否則會錯誤地把 λ 推高。
        flat_experiences = [e for seq in batch for e in seq]
        jfi_vals = [
            e["jfi_raw"] for e in flat_experiences
            if e.get("jfi_raw") is not None and e.get("r_throughput", 0.0) > 1e-9
        ]
        batch_jfi_mean: Optional[float] = None
        if jfi_vals:
            batch_jfi_mean = float(np.mean(jfi_vals))
            # REWARD_MODE=throughput_only（陽春版，見 CLAUDE.md 五階段路線圖
            # Stage 2~4）時跳過 λ 更新，self._lambda 恆為 LAMBDA_INIT（0.0）
            # ——等同沒有 Lagrangian 限制式，reward 只剩 inference_server.py
            # 那邊改用的 compute_reward_breakdown() 純 throughput 分量。
            # batch_jfi_mean 本身仍照算，維持監控用的 log/metrics 不受影響。
            if REWARD_MODE != "throughput_only":
                self._lambda = max(0.0, min(
                    LAMBDA_MAX,
                    self._lambda + LAMBDA_LR * (JFI_MIN - batch_jfi_mean),
                ))

        # ── 建立 Tensor（延伸序列，見 _build_sequence_tensors 說明）────────
        ext_states, ext_masks, rewards, actions = self._build_sequence_tensors(batch)

        # ── Critic 更新（單次 forward 涵蓋 L+1 步，位移取得 current/next）──
        self.critic.train()
        value_seq, _ = self.critic(ext_states)          # (n_seq, L+1)，hidden=None 零初始化
        current_values = value_seq[:, :seq_len]          # V(s_t)，梯度啟用
        next_values = value_seq[:, 1:].detach()           # V(s'_t)，明確截斷梯度（bootstrap target）
        targets = rewards + GAMMA * next_values
        advantages = (targets - current_values).detach()
        if advantages.std() > 1e-8:
            advantages = (advantages - advantages.mean()) / (advantages.std() + 1e-8)

        critic_loss = F.mse_loss(current_values, targets)

        self.critic_opt.zero_grad()
        critic_loss.backward()
        torch.nn.utils.clip_grad_norm_(self.critic.parameters(), 1.0)
        self.critic_opt.step()

        # ── Actor 更新 (Dirichlet Policy Gradient) ────────────────────────
        self.actor.train()

        # 只需要 L 個實際造訪過的 state（不評估 π(a|s')，維持現有 1-step
        # actor-critic 設計），重用 ext_states 的前 L 步，不用另外建 tensor
        probs_seq, _ = self.actor(
            ext_states[:, :seq_len, :], ext_masks[:, :seq_len, :]
        )   # (n_seq, L, MAX_UE_COUNT)，hidden=None 零初始化

        log_probs_t, entropy_t = self._dirichlet_log_probs_and_entropy(
            probs_seq, ext_masks[:, :seq_len, :], actions
        )   # 攤平成 (n_seq*L,)

        advantages_flat = advantages.reshape(-1)

        actor_loss = -(advantages_flat * log_probs_t).mean()
        entropy_coeff = max(
            ENTROPY_COEFF_MIN, ENTROPY_COEFF_INIT * (ENTROPY_DECAY_RATE ** self._train_steps)
        )

        # entropy 緊急保護：entropy < -5 時大幅拉高 entropy 係數，阻止繼續崩潰
        current_entropy = float(entropy_t.mean())
        if current_entropy < -5.0:
            entropy_coeff = max(entropy_coeff, 0.1 * abs(current_entropy) / 5.0)

        actor_loss = actor_loss - entropy_coeff * entropy_t.mean()

        self.actor_opt.zero_grad()
        actor_loss.backward()
        torch.nn.utils.clip_grad_norm_(self.actor.parameters(), 1.0)
        self.actor_opt.step()

        self._train_steps += 1
        self._is_trained = True

        metrics = {
            "train_step":  self._train_steps,
            "actor_loss":  float(actor_loss.item()),
            "critic_loss": float(critic_loss.item()),
            "entropy":     float(entropy_t.mean().item()),
            "mean_reward": float(rewards.mean().item()),
            "mean_adv":    float(advantages.mean().item()),
            "lambda":      self._lambda,
            "batch_jfi_mean": batch_jfi_mean,
            "n_sequences": len(batch),
            "seq_len": seq_len,
        }
        self._log.info(
            "[訓練] step=%d actor_loss=%.4f critic_loss=%.4f "
            "entropy=%.4f mean_reward=%.4f lambda=%.4f batch_jfi=%s "
            "n_seq=%d seq_len=%d",
            self._train_steps,
            metrics["actor_loss"],
            metrics["critic_loss"],
            metrics["entropy"],
            metrics["mean_reward"],
            self._lambda,
            f"{batch_jfi_mean:.4f}" if batch_jfi_mean is not None else "N/A",
            len(batch), seq_len,
        )
        return metrics

    def evaluate_on_batch(self, sequences: list[list[dict]]) -> dict:
        """
        在測試集（序列）上計算 loss，不更新梯度（用於偵測 overfitting）。

        Returns:
            {"test_actor_loss", "test_critic_loss", "test_entropy", "test_mean_reward"}
            或 {} 若資料不足。
        """
        sequences = self._filter_valid_sequences(sequences)
        if len(sequences) < TRAIN_SEQ_COUNT:
            return {}

        idxs = np.random.choice(len(sequences), TRAIN_SEQ_COUNT, replace=False)
        batch = [sequences[i] for i in idxs]
        seq_len = len(batch[0])

        ext_states, ext_masks, rewards, actions = self._build_sequence_tensors(batch)

        self.actor.eval()
        self.critic.eval()
        with torch.no_grad():
            value_seq, _ = self.critic(ext_states)
            current_values = value_seq[:, :seq_len]
            next_values = value_seq[:, 1:]
            targets = rewards + GAMMA * next_values
            advantages = targets - current_values
            if advantages.std() > 1e-8:
                advantages = (advantages - advantages.mean()) / (advantages.std() + 1e-8)

            test_critic_loss = F.mse_loss(current_values, targets)

            probs_seq, _ = self.actor(
                ext_states[:, :seq_len, :], ext_masks[:, :seq_len, :]
            )
            log_probs_t, entropy_t = self._dirichlet_log_probs_and_entropy(
                probs_seq, ext_masks[:, :seq_len, :], actions
            )
            advantages_flat = advantages.reshape(-1)
            test_actor_loss = -(advantages_flat * log_probs_t).mean()

        return {
            "test_actor_loss":  float(test_actor_loss.item()),
            "test_critic_loss": float(test_critic_loss.item()),
            "test_entropy":     float(entropy_t.mean().item()),
            "test_mean_reward": float(rewards.mean().item()),
        }

    @property
    def is_trained(self) -> bool:
        """是否已完成至少一次訓練，可切換至 DRL 推論模式。"""
        return self._is_trained

    # -------------------------------------------------------------------------
    # 模型持久化
    # -------------------------------------------------------------------------

    def save(self) -> None:
        """
        將 Actor + Critic 的權重與優化器狀態原子性存至磁碟。

        先寫入同目錄下的臨時檔，再用 os.replace()（同檔案系統上為原子操作）
        覆蓋正式檔名，確保任何讀者（本進程的近即時執行緒、或跨進程的
        FL ClientApp/InferenceServer 熱重載執行緒）永遠只會讀到完整的舊檔
        或完整的新檔，不會讀到寫一半的損毀檔案。

        不存 self._actor_hidden：推論時的隱藏狀態是「輸入歷史 + 特定權重」
        共同決定的，跟訓練時序列一律歸零重新開始是不同概念，混進 checkpoint
        會是概念上的錯誤（見 reset_hidden()／load() 的說明）。
        """
        path = self.model_dir / f"model_node{self.node_id}.pt"
        tmp_path = path.with_suffix(f".pt.tmp.{os.getpid()}")
        torch.save(
            {
                "actor":       self.actor.state_dict(),
                "critic":      self.critic.state_dict(),
                "actor_opt":   self.actor_opt.state_dict(),
                "critic_opt":  self.critic_opt.state_dict(),
                "train_steps": self._train_steps,
                "lambda":      self._lambda,
            },
            tmp_path,
        )
        os.replace(tmp_path, path)
        self._log.info("模型已儲存至 %s (訓練步數: %d)", path, self._train_steps)

    def load(self) -> bool:
        """
        嘗試從磁碟載入預存權重。

        載入成功後一律呼叫 reset_hidden()：隱藏狀態是舊網路權重產生的，
        對新載入的權重是未曾訓練過要處理的輸入，不重置會讓推論吃到一個
        語意不明的隱藏狀態。

        Returns:
            True 表示成功載入，False 表示找不到檔案或載入失敗（從隨機初始化開始）。
        """
        path = self.model_dir / f"model_node{self.node_id}.pt"
        if not path.exists():
            self._log.info("未找到預存模型 (%s)，從隨機初始化開始", path)
            return False
        try:
            ckpt = torch.load(path, map_location=self.device)
            self.actor.load_state_dict(ckpt["actor"])
            self.critic.load_state_dict(ckpt["critic"])
            self.actor_opt.load_state_dict(ckpt["actor_opt"])
            self.critic_opt.load_state_dict(ckpt["critic_opt"])
            self._train_steps = ckpt.get("train_steps", 0)
            self._is_trained  = self._train_steps > 0
            # 舊 checkpoint（Lagrangian 上線前存的）沒有 lambda 欄位，優雅降級為初始值
            self._lambda = ckpt.get("lambda", LAMBDA_INIT)
            self.reset_hidden()
            self._log.info(
                "模型已從 %s 載入 (訓練步數: %d, lambda=%.4f)",
                path, self._train_steps, self._lambda,
            )
            return True
        except Exception as exc:
            self._log.warning("模型載入失敗: %s，從隨機初始化開始", exc)
            return False
