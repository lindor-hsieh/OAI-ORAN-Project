"""
drl_agent.py — DRL Actor-Critic Agent for Local PRB Allocation

架構（2026-09-18 起：MODEL_ARCH 開關，MLP／GRU 並存）：
  - `MODEL_ARCH` 環境變數（"mlp" | "gru"，預設 "mlp"）決定 Actor/Critic 用哪一組
    網路，比照 reward_calculator.py 的 REWARD_MODE 既有模式，切換時不需要改程式碼。
  - **為什麼加這個開關**：CLAUDE.md 五階段路線圖的「最基礎 DRL」（Stage 2~4）原意
    是不含 GRU 的陽春模型，但 2026-07-09 曾經把 Actor/Critic 從 MLP 全面改成 GRU，
    兩個月後才補上的路線圖文字沒有把這件事考慮進去，導致 Stage 2/3 已完成的結果
    其實是用錯誤架構跑的。2026-09-18 討論後決定：GRU 不整個拔掉（未來改良版或其他
    研究仍可能用到，重寫成本高），改成跟 REWARD_MODE 一樣的環境變數開關，兩套架構
    並存，Stage 2~4 預設 `MODEL_ARCH=mlp`。
  - `MODEL_ARCH=mlp`：ActorNetworkMLP/CriticNetworkMLP，無記憶、單步 state 快照，
    訓練用 i.i.d. 隨機抽樣的獨立經驗（見 training_pipeline.py 的 fetch_experiences()）。
  - `MODEL_ARCH=gru`（2026-07-09 導入，原因見下）：ActorNetworkGRU/CriticNetworkGRU +
    MLP head，序列化版本，從 MongoDB 讀取「時間連續的經驗序列」進行批次更新。
    原始的 state 是無記憶的單步快照，即使加了 dl_buffer_info（見下方），也只看得到
    「當下」，看不出趨勢（例如某個 UE 的 buffer 是在成長還是萎縮）。GRU 讓 policy
    自己學會維護記憶，不需要手動設計 delta 特徵或疊幀。推論時 Actor 的隱藏狀態跨
    ZMQ 呼叫持久化（見 DRLAgent._actor_hidden／reset_hidden()）；訓練時每個序列
    一律從零初始化的隱藏狀態開始（見 train_on_batch_gru()）。詳見 DRL_DESIGN.md。

State Space (固定長度向量，不足補零)：
  [norm_bsr_0, norm_cqi_0, norm_buf_0, ..., active_ratio, fairness_bias, bh_ratio]
  長度 = MAX_UE_COUNT * 3 + 3 = 51

  bh_ratio（2026-09-26 加入，第 51 維）：Backhaul-aware 動態 PRB 預算的可用比例 ∈ [0,1]（DU 排程器實際可用 PRB
  池 = 106 × 此值），由 E2SM-MAC 回報（mac_ind_msg_t.backhaul_prb_ratio）經 xApp 帶進來。動作是「每 UE PRB
  上限」，池子大小決定上限是否綁得住，DRL 必須看到它。收到前預設 1.0（池子全開）。

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
  InferenceServer 的背景執行緒每 TRAIN_INTERVAL_S 秒呼叫 train_on_batch()。
  MODEL_ARCH=mlp 時從 MongoDB 取得「打散的獨立經驗」（training_pipeline.py 的
  fetch_experiences()）做 i.i.d. mini-batch 更新；MODEL_ARCH=gru 時取得「時間
  連續的經驗序列」（fetch_sequences()）做序列化更新。train_on_batch()／
  evaluate_on_batch() 是依 self.arch 分派的公開介面，呼叫端（training_pipeline.py／
  inference_server.py／client_app.py）不需要知道底層是哪個分支。
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

# 每個 process 只用 1 個 torch 執行緒（環境變數 TORCH_NUM_THREADS 可覆寫）。12 個 inference 容器 + 12 個 FL ClientApp
# 都釘在同一組 4 個核心（cpuset 12-15），torch 預設用 8 個執行緒會嚴重超額訂閱，讓訓練持有模型鎖的時間
# 與推論延遲暴增（5ms 逾時 → 退回 PF）。這個模型很小（2 層 MLP），單執行緒就夠。
torch.set_num_threads(max(1, int(os.environ.get("TORCH_NUM_THREADS", "1"))))

from reward_calculator import JFI_MIN, REWARD_MODE

# =============================================================================
# 架構開關
# =============================================================================

# "mlp"（預設，Stage 2~4「最基礎 DRL」用）｜"gru"（保留給未來改良版/其他研究，
# 見上方模組 docstring 的決策說明）。比照 reward_calculator.py 的 REWARD_MODE
# 讀取方式，docker-compose-iab-server.yaml 對應加上
# `MODEL_ARCH: "${MODEL_ARCH:-mlp}"`。
MODEL_ARCH: str = os.environ.get("MODEL_ARCH", "mlp").strip().lower()
if MODEL_ARCH not in ("mlp", "gru"):
    raise ValueError(f"未知的 MODEL_ARCH={MODEL_ARCH!r}，必須是 'mlp' 或 'gru'")

# =============================================================================
# 超參數
# =============================================================================

MAX_UE_COUNT: int = 16
STATE_DIM: int = MAX_UE_COUNT * 3 + 3   # [bsr, cqi, buf] × N + active_ratio + fairness_bias + bh_ratio

# fairness_bias 正規化區間（Global xApp 廣播的原始值域），見 encode_state()
FAIRNESS_BIAS_MIN: float = 0.5
FAIRNESS_BIAS_MAX: float = 2.0

MAX_BSR: float = 2_000_000.0            # DL delta-TBS 正規化上限 (bytes/1 秒視窗)：xApp 的 E2 回報週期是 100ms、每 10 次回報才送一次 ZMQ →
                                         # 每筆狀態累積 ~1 秒（實測 MongoDB 文件間隔 1.00 秒）；S=0.4 下單 UE 峰值 ~2MB/視窗，實測 max ~1.46M
MAX_BUF_INFO: float = 2_000_000.0       # dl_buffer_info（RLC 佇列位元組數）正規化上限
                                         # 現場實測（2026-07-09，Scenario R）：node3/5 最大值
                                         # 約 2,147,000，node4 約 124,000，上限抓 2,000,000 讓多數
                                         # 觀測值落在有解析度的區間，跟 reward_calculator.py 既有
                                         # 的 MAX_BSR=2,000,000 常數同量級，非巧合
GAMMA: float = 0.95                      # 折扣因子（僅 GRU 分支使用）

# ── MLP 分支的信用分配設計（2026-09-26 重設計，方案 A；見 DRL_DESIGN.md 附註）──────────────
# 動機：獎勵（Δtbs）的變動主要來自「外生的流量需求」而不是動作；不壅塞時每個 UE 的需求都被滿足，
# 獎勵與 PRB 怎麼分無關。舊版對每個批次做 advantage z-score，會把「純雜訊」放大成單位尺度，
# Actor 變成隨機遊走（一直無法收斂的原因之一）。改為：
#   1. γ = 0（contextual bandit）：動作幾乎不影響下一步的流量，折扣只增加變異。
#   2. advantage = clip((r − V(s)) / running_std(r), ±ADV_CLIP)：不做每批 z-score，用「獎勵的滾動標準差」
#      當固定尺度；V(s) 由 Critic 從 state（含佇列、MCS、上一窗 Δtbs）預測，殘差才是動作＋雜訊。
#   3. 只在「壅塞」樣本更新 Actor：某個活躍 UE 的 RLC 佇列（dl_buffer_info）≥ CONTENDED_BUF_BYTES
#      （決策當下 s_t 或結果 s_{t+1}），代表需求超過供給、PRB 怎麼分才有差別。非壅塞樣本只訓練 Critic。
GAMMA_MLP: float = float(os.getenv("DRL_GAMMA_MLP", "0.0"))
ADV_CLIP: float = 3.0
REWARD_STD_EMA: float = 0.1              # 獎勵滾動標準差的 EMA 權重（新批次 10%）
REWARD_STD_FLOOR: float = 1e-3           # 滾動標準差下限，避免除以 ~0
# 壅塞判斷門檻（RLC 佇列位元組數，單一 UE）。控制視窗是 ~1 秒，UE 穩態需求約 2~10 sim Mbps ≈ 100~500 kB/視窗（牆鐘，S=0.4）。
# 訓練日誌的 contended 比例應約 35~45%（壅塞相位佔 ~45% 時間，另有少量正常相位誤判）；偏離很多時以環境變數調整。
CONTENDED_BUF_BYTES: float = float(os.getenv("CONTENDED_BUF_BYTES", "100000"))   # 2026-09-26 依真實 1 秒視窗資料校準：門檻 100k 時正常相位誤判 13.7%、壅塞相位命中 65%（50k：24%/74%，200k：8.7%/54%）
# 從 BSR 啟發式切換到 DRL 推論所需的最少訓練步數。舊版只要 train_steps>0（第一輪 10 步）就切換，此時 policy
# 近乎隨機初始化，比啟發式還差、會汙染早期資料。預設 100 步（≈10 輪 ≈ 10 分鐘，FL 客戶端訓練也計入）。
MIN_DRL_TRAIN_STEPS: int = int(os.getenv("DRL_MIN_TRAIN_STEPS", "100"))
# 離策略校正（PPO 式比例裁剪，2026-09-26）：回放緩衝區涵蓋多個策略版本，用一般策略梯度訓練舊資料會有偏差。
# 推論時把當時的 log π(a|s) 存進經驗（behavior_logp），訓練時比例 ρ=exp(logπ_新−logπ_舊)，目標 min(ρA, clip(ρ,1±ε)A)。
# 沒有 behavior_logp 的經驗（BSR 啟發式階段、舊資料）只訓練 Critic，不更新 Actor。
PPO_CLIP_EPS: float = float(os.getenv("DRL_PPO_CLIP_EPS", "0.2"))
MIN_CONTENDED_SAMPLES: int = 8           # 一個 batch 內壅塞樣本少於這個數就跳過本步 Actor 更新（只更新 Critic）

# Actor 學習率：預設 3e-4（環境變數 DRL_LR_ACTOR）。控制週期 1 秒 → 每節點每小時只有 ~3600 筆經驗、6 小時 ~21600 筆、
# 每分鐘 10 步梯度更新（6 小時 ~3600 步）；合成實驗顯示 1e-4 要 ~1200 步才收斂、3e-4 只要 300~600 步，真實資料更吵，1e-4 有訓練不足風險。
LR_ACTOR: float = float(os.getenv("DRL_LR_ACTOR", "3e-4"))

# 動作 → 每 UE PRB 上限的對應方式（2026-09-27）。
#   "split"（舊）：Dirichlet 樣本 s（總和 1）直接當份額，上限 = s×106，兩個 UE 就各被切成約一半——
#                  「不截斷（= 原本的 PF）」不是可表達的動作，均分也會讓每個 UE 每個 slot 都被截斷（非 work-conserving）。
#   "relative"（新，預設）：上限 = min(1, n_active × s)。均分（s=1/n）→ 所有 UE 上限 1.0 = 不截斷 = 原本的 PF，
#                  策略只在「偏離均分」時才限制份額低於平均的 UE；PF 是策略空間內可達的恆等點，學不到東西時退化成 PF，
#                  而不是比 PF 差。C 端 xApp 對每個 UE 獨立換算比例（>1 夾成 1、不檢查總和），不需要改 C 程式。
CAP_MODE: str = os.getenv("DRL_CAP_MODE", "relative").strip().lower()

# 2026-09-27：推論時是否用確定性輸出（policy 機率本身，不做 Dirichlet 採樣）。
# 動機：Dirichlet 採樣噪音＋relative 上限的夾值（min(1, n·s)）在夾到 1 的那一側被浪費、
# 在夾到下限的那一側直接扣掉吞吐量，是非對稱的——噪音本身就會拉低平均送達量，
# 與 policy 是否收斂無關。訓練仍應保留隨機策略（探索、log π(a|s) 梯度都需要），
# 只在凍結模型做評估/量測時開這個開關，不影響訓練邏輯。
DETERMINISTIC: bool = os.getenv("DRL_DETERMINISTIC", "0").strip() not in ("0", "false", "False")
LR_CRITIC: float = 3e-4
HIDDEN_DIM: int = 128
MIN_TRAIN_EXPERIENCES: int = 200         # 觸發第一次訓練所需的最少「原始經驗」數
                                         # （MLP：i.i.d. 抽樣的門檻本身；GRU：序列
                                         # 切窗之前的門檻，training_pipeline.py 用）

# MLP（MODEL_ARCH=mlp）訓練參數：打散抽樣獨立經驗，不要求時間連續性。
TRAIN_BATCH_SIZE: int = 128      # 每次梯度更新的 mini-batch 大小（i.i.d. 抽樣）

# 序列化訓練參數（僅 MODEL_ARCH=gru 使用，2026-07-09 導入，GRU 需要時間連續的
# 序列而不是打散的獨立經驗，見 training_pipeline.py 的 fetch_sequences()）：
TRAIN_SEQ_LEN: int = 32          # 每個訓練序列的步數，~3.2 秒涵蓋範圍（100ms cadence）
                                  # 遠小於一個流量相位的典型長度（~600 步，見
                                  # traffic_scenario.py 預設 phase_duration=60s），
                                  # 確保序列不會跨越相位邊界內部
TRAIN_SEQ_COUNT: int = 16        # 每次梯度更新用幾個序列（16×32=512 筆原始經驗）
                                  # 刻意比 TRAIN_BATCH_SIZE=128 大：序列內部樣本
                                  # 時間相關（不像 i.i.d. 打散抽樣是獨立的），需要更多
                                  # 原始經驗才能得到同樣品質的梯度估計，這是
                                  # BPTT-based RL 的標準做法
MIN_TRAIN_SEQUENCES: int = TRAIN_SEQ_COUNT   # 訓練門檻（GRU only）：候選序列池至少要有一個 batch 的量

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
# 神經網路定義 — MLP（MODEL_ARCH=mlp，2026-09-18 起 Stage 2~4 預設架構）
# =============================================================================

class ActorNetworkMLP(nn.Module):
    """
    Policy Network：單步 state → PRB 分配 logits → Masked Softmax。

    無記憶、無隱藏狀態，每次呼叫只看當下這一筆 state。
    非活躍 UE slot 在 softmax 前被設為 -∞，確保輸出比例為 0。
    """

    def __init__(
        self,
        state_dim: int = STATE_DIM,
        max_ues: int = MAX_UE_COUNT,
    ) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(state_dim, HIDDEN_DIM),
            nn.ReLU(),
            nn.Linear(HIDDEN_DIM, HIDDEN_DIM),
            nn.ReLU(),
            nn.Linear(HIDDEN_DIM, max_ues),
        )

    def forward(
        self,
        state: torch.Tensor,   # (batch, state_dim)
        mask: torch.Tensor,    # (batch, max_ues)  True = 活躍 UE
    ) -> torch.Tensor:
        """回傳各 UE 的 PRB 分配比例，形狀 (batch, max_ues)。"""
        logits = self.net(state)                   # (batch, max_ues)
        logits = logits.masked_fill(~mask, -1e9)    # 遮蔽非活躍 slot
        return F.softmax(logits, dim=-1)            # (batch, max_ues)


class CriticNetworkMLP(nn.Module):
    """Value Network：單步 state → scalar V(s)。無記憶、無隱藏狀態。"""

    def __init__(self, state_dim: int = STATE_DIM) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(state_dim, HIDDEN_DIM),
            nn.ReLU(),
            nn.Linear(HIDDEN_DIM, 64),
            nn.ReLU(),
            nn.Linear(64, 1),
        )

    def forward(self, state: torch.Tensor) -> torch.Tensor:
        """回傳狀態價值估計，形狀 (batch,)。"""
        return self.net(state).squeeze(-1)


# =============================================================================
# 神經網路定義 — GRU（MODEL_ARCH=gru，2026-07-09 導入，保留供未來切換）
# =============================================================================

class ActorNetworkGRU(nn.Module):
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


class CriticNetworkGRU(nn.Module):
    """
    Value Network：state 序列 → GRU 隱藏狀態 → V(s)。

    只在訓練時使用（train_on_batch_gru()/evaluate_on_batch_gru()），每個序列
    一律從零初始化的隱藏狀態開始。跟 ActorNetworkGRU 不同，Critic 不需要跨
    infer() 呼叫持久化隱藏狀態——infer() 從不呼叫 Critic。
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
    PRB 分配的 Actor-Critic DRL Agent。架構由 MODEL_ARCH 決定（見上方模組
    docstring）：self.arch == "mlp" 或 "gru"，兩者的 infer()/train_on_batch()/
    evaluate_on_batch() 對外簽章一致，呼叫端（training_pipeline.py／
    inference_server.py／client_app.py）不需要 if-branch。

    典型使用流程：
        agent = DRLAgent(node_id=1)
        agent.load()                     # 嘗試載入預存權重（arch 不符會拒絕載入）

        # 每 ~100ms 推論一次（由 InferenceServer 呼叫）。arch=="gru" 時 Actor
        # 隱藏狀態跨呼叫持久化；arch=="mlp" 時無隱藏狀態，每次獨立。
        allocations = agent.infer(ues, fairness_bias)

        # 每 TRAIN_INTERVAL_S 秒訓練一次（由背景執行緒呼叫）。arch=="mlp" 時
        # 傳入打散的獨立經驗 list[dict]；arch=="gru" 時傳入時間連續的經驗
        # 序列 list[list[dict]]（training_pipeline.py 依 agent.arch 決定要
        # fetch 哪一種）。
        metrics = agent.train_on_batch(experiences_or_sequences)
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
        self.arch = MODEL_ARCH

        if self.arch == "gru":
            self.actor = ActorNetworkGRU().to(self.device)
            self.critic = CriticNetworkGRU().to(self.device)
        else:
            self.actor = ActorNetworkMLP().to(self.device)
            self.critic = CriticNetworkMLP().to(self.device)
        self.actor_opt = optim.Adam(self.actor.parameters(), lr=LR_ACTOR)
        self.critic_opt = optim.Adam(self.critic.parameters(), lr=LR_CRITIC)

        # _is_trained=False 時 InferenceServer 退回 BSR 啟發式
        self._is_trained: bool = False
        self._train_steps: int = 0

        # Lagrangian 限制式的乘子，見 LAMBDA_INIT/LAMBDA_LR/LAMBDA_MAX 說明
        self._lambda: float = LAMBDA_INIT

        # 獎勵的滾動標準差（MLP 分支 advantage 的固定尺度，見 GAMMA_MLP 說明）；None=尚未初始化。
        # 存進 checkpoint（見 save()/load()），重啟後不必重新估。
        self._reward_std: Optional[float] = None

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
        重置推論時 Actor 的隱藏狀態。arch=="mlp" 時本來就沒有隱藏狀態，呼叫
        此函式是 no-op（呼叫端不需要依 arch 分支，直接呼叫即可）。

        arch=="gru" 時的呼叫時機（見 DRL_DESIGN.md 完整說明）：
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
        bh_ratio: float = 1.0,
    ) -> tuple[np.ndarray, np.ndarray]:
        """
        將 UE 列表編碼為固定長度的 numpy 向量。

        Args:
            ues           : UE 狀態列表
            fairness_bias : Global xApp 廣播的全域公平性偏差，原始值域
                            [FAIRNESS_BIAS_MIN, FAIRNESS_BIAS_MAX]，尚未收到
                            廣播或無資料時傳中性值 1.0（預設值）。
            bh_ratio      : Backhaul-aware 可用 PRB 比例 ∈ [0,1]（E2 回報），預設 1.0。

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

        state_vec[MAX_UE_COUNT * 3 + 2] = float(np.clip(bh_ratio, 0.0, 1.0))

        return state_vec, mask_vec

    def _to_tensors(
        self,
        state_vec: np.ndarray,
        mask_vec: np.ndarray,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """
        將 numpy 向量轉換為 tensor，供 infer() 單步推論用。

        arch=="gru" 時多包一層 seq_len 維度 (1, 1, *)；arch=="mlp" 時是單純
        的 (1, *) flat batch。
        """
        state_t = torch.tensor(state_vec, dtype=torch.float32, device=self.device).unsqueeze(0)
        mask_t = torch.tensor(mask_vec, dtype=torch.bool, device=self.device).unsqueeze(0)
        if self.arch == "gru":
            state_t = state_t.unsqueeze(1)   # (1, 1, state_dim)
            mask_t = mask_t.unsqueeze(1)     # (1, 1, max_ues)
        return state_t, mask_t

    # -------------------------------------------------------------------------
    # 推論
    # -------------------------------------------------------------------------

    def infer(
        self,
        ues: list[dict],
        fairness_bias: float = 1.0,
        bh_ratio: float = 1.0,
    ) -> tuple[list[dict], np.ndarray, Optional[float]]:
        """
        執行 DRL Actor 推論，回傳 (PRB 分配結果, 比例向量, 行為策略 log π(a|s))。

        Args:
            ues           : [{"rnti": int, "bsr": int, "wb_cqi": int}, ...]
            fairness_bias : Global xApp 廣播的全域公平性偏差，見 encode_state()

        Returns:
            allocations   : [{"rnti": int, "prb_abs": int}, ...]
            action_ratios : (MAX_UE_COUNT,) float32，用於 MongoDB 儲存
            behavior_logp : 這次採樣動作在當時策略下的 log π(a|s)（存進經驗供 PPO 比例裁剪；n<2 時為 0）
        """
        if not ues:
            return [], np.zeros(MAX_UE_COUNT, dtype=np.float32), None

        n = min(len(ues), MAX_UE_COUNT)
        state_vec, mask_vec = self.encode_state(ues, fairness_bias, bh_ratio)
        state_t, mask_t = self._to_tensors(state_vec, mask_vec)

        self.actor.eval()
        with torch.no_grad():
            if self.arch == "gru":
                probs_seq, new_hidden = self.actor(state_t, mask_t, self._actor_hidden)
                self._actor_hidden = new_hidden.detach()   # 跨呼叫持久化，見 reset_hidden()
                probs = probs_seq[0, 0]                    # 攤平回 (MAX_UE_COUNT,)
            else:
                probs = self.actor(state_t, mask_t)[0]      # (MAX_UE_COUNT,)，無隱藏狀態

            if DETERMINISTIC:
                # 確定性輸出：直接用 policy 的機率當份額（= Dirichlet 期望值），不採樣、無噪音。
                # 沒有採樣就沒有「行為策略」可言，behavior_logp 留 None（PPO 訓練端本來就會跳過缺
                # behavior_logp 的經驗，只訓練 Critic；凍結評估時 TRAIN_ENABLED=0，這條路徑不影響訓練）。
                p_c = torch.clamp(probs[:n], min=1e-6)
                active_ratios = (p_c / p_c.sum()).cpu().numpy()
                behavior_logp = None
            else:
                # Dirichlet 隨機策略：從 Dirichlet(α = probs[:n] × K) 採樣
                # 確保 action_ratios ≠ actor probs，訓練時 log π(a|s) 梯度有效
                # K 隨訓練步數退火（見 _current_concentration()），訓練越久取樣越集中
                alpha = torch.clamp(probs[:n] * self._current_concentration(), min=1e-3)
                dist = torch.distributions.Dirichlet(alpha)
                sample_t = dist.sample()
                active_ratios = sample_t.cpu().numpy()          # (n,)，加總恰好為 1
                # 行為策略的 log π_舊(a|s)：與訓練端 _dirichlet_log_probs_and_entropy_flat() 相同的夾值＋重新正規化，
                # 才能讓新舊 logπ 的比例只反映「策略差異」而不是夾值差異
                a_c = torch.clamp(sample_t, min=1e-6)
                behavior_logp = float(dist.log_prob(a_c / a_c.sum()).item()) if n >= 2 else 0.0

        if CAP_MODE == "relative" and n >= 2:
            # 上限 = min(1, n·s)：均分 = 不截斷（PF）；只限制份額低於平均的 UE。每 UE 至少 MIN_PRB。
            MIN_PRB_REL = 5
            caps = np.minimum(1.0, n * active_ratios)
            prb_ints = np.clip(np.rint(caps * self.total_prb), MIN_PRB_REL, self.total_prb).astype(np.int32)
            allocations = [
                {"rnti": int(ues[i]["rnti"]), "prb_abs": int(prb_ints[i])}
                for i in range(n)
            ]
            action_ratios = np.zeros(MAX_UE_COUNT, dtype=np.float32)
            action_ratios[:n] = active_ratios              # 訓練用的動作仍是 Dirichlet 樣本 s（PPO 的 logπ 不變）
            return allocations, action_ratios, behavior_logp

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

        return allocations, action_ratios, behavior_logp

    # -------------------------------------------------------------------------
    # 離線訓練 — 公開介面（依 self.arch 分派給 MLP 或 GRU 分支）
    # -------------------------------------------------------------------------

    def train_on_batch(self, data) -> dict:
        """
        依 self.arch 分派：
          - arch=="mlp"：data 是打散的獨立經驗 list[dict]（見 train_on_batch_mlp()）
          - arch=="gru"：data 是時間連續的經驗序列 list[list[dict]]（見 train_on_batch_gru()）
        呼叫端（training_pipeline.py）依 agent.arch 準備對應形狀的資料，這裡只負責分派。
        """
        if self.arch == "gru":
            return self.train_on_batch_gru(data)
        return self.train_on_batch_mlp(data)

    def evaluate_on_batch(self, data) -> dict:
        """依 self.arch 分派，資料形狀規則同 train_on_batch()。"""
        if self.arch == "gru":
            return self.evaluate_on_batch_gru(data)
        return self.evaluate_on_batch_mlp(data)

    # -------------------------------------------------------------------------
    # 離線訓練 — MLP 分支（打散抽樣獨立經驗，i.i.d.）
    # -------------------------------------------------------------------------

    @staticmethod
    def _filter_valid_experiences(experiences: list[dict]) -> list[dict]:
        """
        防禦性過濾：排除 state_vec/next_state_vec 維度不對的殘留舊資料（例如
        STATE_DIM 曾經變更過，或切換 MODEL_ARCH 前沒清乾淨 MongoDB，混進
        np.array() 建構會直接拋 inhomogeneous shape 例外炸掉整個訓練執行緒）。
        """
        return [
            e for e in experiences
            if len(e.get("state_vec", [])) == STATE_DIM
            and len(e.get("next_state_vec", [])) == STATE_DIM
        ]

    def _build_experience_tensors(
        self, batch: list[dict]
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        """把打散的獨立經驗批次組成 flat tensor，(batch, *) 無 seq_len 維度。"""
        states = torch.tensor(
            np.array([e["state_vec"] for e in batch], dtype=np.float32), device=self.device)
        masks = torch.tensor(
            np.array([e["mask_vec"] for e in batch], dtype=bool), device=self.device)
        actions = torch.tensor(
            np.array([e["action_ratios"] for e in batch], dtype=np.float32), device=self.device)
        rewards = torch.tensor(
            np.array([e["reward"] for e in batch], dtype=np.float32), device=self.device)
        next_states = torch.tensor(
            np.array([e["next_state_vec"] for e in batch], dtype=np.float32), device=self.device)
        next_masks = torch.tensor(
            np.array([e["next_mask_vec"] for e in batch], dtype=bool), device=self.device)
        return states, masks, actions, rewards, next_states, next_masks

    @staticmethod
    def _max_queue_bytes(states: torch.Tensor, masks: torch.Tensor) -> torch.Tensor:
        """
        由 state 向量還原每個樣本「活躍 UE 中最大的 RLC 佇列位元組數」，形狀 (batch,)。
        state 的第 3i+2 維是 log1p(buf)/log1p(MAX_BUF_INFO)，反轉即得 bytes；非活躍 slot 以 mask 排除。
        """
        norm_buf = states[:, 2:MAX_UE_COUNT * 3:3]                       # (batch, MAX_UE_COUNT)
        buf_bytes = torch.expm1(norm_buf * math.log1p(MAX_BUF_INFO))
        buf_bytes = buf_bytes.masked_fill(~masks, 0.0)
        return buf_bytes.max(dim=1).values

    def _contended_mask(
        self,
        states: torch.Tensor, masks: torch.Tensor,
        next_states: torch.Tensor, next_masks: torch.Tensor,
    ) -> torch.Tensor:
        """
        壅塞樣本遮罩 (batch,)：決策當下或結果狀態任一活躍 UE 的 RLC 佇列 ≥ CONTENDED_BUF_BYTES，
        **且決策當下至少有 2 個活躍 UE**——只有 1 個 UE 時 Dirichlet 只有一維、log_prob 恆為 0、梯度為 0，
        沒有任何分配決策可學，不能算成「可更新 Actor 的樣本」。
        """
        q = torch.maximum(self._max_queue_bytes(states, masks),
                          self._max_queue_bytes(next_states, next_masks))
        return (q >= CONTENDED_BUF_BYTES) & (masks.sum(dim=1) >= 2)

    def count_contended(self, experiences: list[dict]) -> int:
        """
        數一批經驗中「可更新 Actor 的壅塞樣本」有幾筆（同 _contended_mask 的定義）。供 FL 客戶端回報
        num-examples 用：FedAvg 應該用「對 Actor 有貢獻的樣本數」加權，沒壅塞的節點 Actor 沒被更新，
        不該用全部樣本數（緩衝區滿了之後每個節點都約 10000 筆）把有學到的節點稀釋掉。
        """
        exps = self._filter_valid_experiences(experiences)
        if not exps:
            return 0
        states, masks, _, _, next_states, next_masks = self._build_experience_tensors(exps)
        _, has_old = self._behavior_logp_tensors(exps)
        return int((self._contended_mask(states, masks, next_states, next_masks) & has_old).sum().item())

    def _advantages_mlp(
        self, rewards: torch.Tensor, current_values: torch.Tensor, next_values: torch.Tensor,
        update_std: bool,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """
        MLP 分支的 advantage：clip((target − V(s)) / running_std(r), ±ADV_CLIP)，不做每批 z-score。
        Returns: (advantages, targets)。update_std=True（訓練）時才更新獎勵滾動標準差。
        """
        targets = rewards + GAMMA_MLP * next_values
        if update_std:
            batch_std = float(rewards.std().item()) if rewards.numel() > 1 else 0.0
            if self._reward_std is None:
                self._reward_std = batch_std
            else:
                self._reward_std = (1.0 - REWARD_STD_EMA) * self._reward_std + REWARD_STD_EMA * batch_std
        scale = max(self._reward_std if self._reward_std is not None else 0.0, REWARD_STD_FLOOR)
        adv = torch.clamp((targets - current_values).detach() / scale, -ADV_CLIP, ADV_CLIP)
        return adv, targets

    def _behavior_logp_tensors(self, batch: list[dict]) -> tuple[torch.Tensor, torch.Tensor]:
        """取出批次的行為策略 log π_舊(a|s)（(batch,)）與「是否有這個欄位」遮罩。缺欄位者填 0、遮罩 False。"""
        vals = [e.get("behavior_logp") for e in batch]
        has = torch.tensor([v is not None for v in vals], dtype=torch.bool, device=self.device)
        old = torch.tensor([float(v) if v is not None else 0.0 for v in vals], dtype=torch.float32, device=self.device)
        return old, has

    @staticmethod
    def _ppo_actor_loss(
        log_new: torch.Tensor, log_old: torch.Tensor, adv: torch.Tensor, mask: torch.Tensor,
    ) -> tuple[torch.Tensor, float]:
        """PPO 裁剪目標（只在 mask 內）：-mean(min(ρA, clip(ρ,1-ε,1+ε)A))。回傳 (loss, 被裁剪的樣本比例)。"""
        ratio = torch.exp(torch.clamp(log_new - log_old, -20.0, 20.0))[mask]
        a = adv[mask]
        surr = torch.minimum(ratio * a, torch.clamp(ratio, 1.0 - PPO_CLIP_EPS, 1.0 + PPO_CLIP_EPS) * a)
        clipped = ((ratio < 1.0 - PPO_CLIP_EPS) | (ratio > 1.0 + PPO_CLIP_EPS)).float().mean().item()
        return -surr.mean(), float(clipped)

    def _dirichlet_log_probs_and_entropy_flat(
        self,
        probs: torch.Tensor,     # (batch, MAX_UE_COUNT)
        masks: torch.Tensor,     # (batch, MAX_UE_COUNT)
        actions: torch.Tensor,   # (batch, MAX_UE_COUNT)
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """對每個樣本分別計算 Dirichlet log π(a|s) 與 entropy（無序列維度）。"""
        log_probs_list: list[torch.Tensor] = []
        entropy_list: list[torch.Tensor] = []
        for i in range(probs.shape[0]):
            n_i = int(masks[i].sum().item())
            if n_i == 0:
                log_probs_list.append(torch.tensor(0.0, device=self.device))
                entropy_list.append(torch.tensor(0.0, device=self.device))
                continue

            alpha_i = torch.clamp(probs[i, :n_i] * self._current_concentration(), min=1e-3)
            dist_i = torch.distributions.Dirichlet(alpha_i)

            a_i = actions[i, :n_i]
            a_sum = a_i.sum()
            if a_sum < 1e-8:
                log_probs_list.append(torch.tensor(0.0, device=self.device))
                entropy_list.append(dist_i.entropy())
                continue
            a_i = torch.clamp(a_i / a_sum, min=1e-6)
            a_i = a_i / a_i.sum()

            log_probs_list.append(dist_i.log_prob(a_i))
            entropy_list.append(dist_i.entropy())

        return torch.stack(log_probs_list), torch.stack(entropy_list)

    def train_on_batch_mlp(self, experiences: list[dict]) -> dict:
        """
        從 MongoDB 取得的打散獨立經驗批次進行 Actor-Critic 離線更新（i.i.d.，
        無時間連續性要求）。

        Experience document schema：同 train_on_batch_gru()，唯獨這裡的
        experiences 是攤平的 list[dict]，不是 list[list[dict]]。

        Returns:
            metrics : dict with training statistics
        """
        experiences = self._filter_valid_experiences(experiences)
        if len(experiences) < TRAIN_BATCH_SIZE:
            self._log.info(
                "經驗數量不足 (有 %d 筆，需 %d 筆)，跳過訓練",
                len(experiences), TRAIN_BATCH_SIZE,
            )
            return {}

        idxs = np.random.choice(len(experiences), TRAIN_BATCH_SIZE, replace=False)
        batch = [experiences[i] for i in idxs]

        # ── Lagrangian 乘子 λ 更新（同 GRU 分支邏輯，見 train_on_batch_gru()）──
        jfi_vals = [
            e["jfi_raw"] for e in batch
            if e.get("jfi_raw") is not None and e.get("r_throughput", 0.0) > 1e-9
        ]
        batch_jfi_mean: Optional[float] = None
        if jfi_vals:
            batch_jfi_mean = float(np.mean(jfi_vals))
            if REWARD_MODE != "throughput_only":
                self._lambda = max(0.0, min(
                    LAMBDA_MAX,
                    self._lambda + LAMBDA_LR * (JFI_MIN - batch_jfi_mean),
                ))

        states, masks, actions, rewards, next_states, next_masks = self._build_experience_tensors(batch)

        # ── Critic 更新（γ=GAMMA_MLP，預設 0 → 回歸 V(s)≈E[r|s]；全部樣本都訓練）─────────────
        self.critic.train()
        current_values = self.critic(states)
        with torch.no_grad():
            next_values = self.critic(next_states)
        advantages, targets = self._advantages_mlp(rewards, current_values, next_values, update_std=True)

        critic_loss = F.mse_loss(current_values, targets)

        self.critic_opt.zero_grad()
        critic_loss.backward()
        torch.nn.utils.clip_grad_norm_(self.critic.parameters(), 1.0)
        self.critic_opt.step()

        # ── Actor 更新（Dirichlet Policy Gradient，只用「壅塞」樣本）────────────────────
        contended = self._contended_mask(states, masks, next_states, next_masks)
        old_logp, has_old = self._behavior_logp_tensors(batch)
        actor_mask = contended & has_old     # 只有「壅塞」且「有行為策略 logπ」的樣本能更新 Actor
        n_contended = int(contended.sum().item())
        n_actor = int(actor_mask.sum().item())
        contended_frac = n_contended / len(batch)

        actor_loss_val = 0.0
        entropy_val = 0.0
        actor_updated = False
        clip_frac = 0.0
        if n_actor >= MIN_CONTENDED_SAMPLES:
            self.actor.train()
            probs = self.actor(states, masks)
            log_probs_t, entropy_t = self._dirichlet_log_probs_and_entropy_flat(probs, masks, actions)

            actor_loss, clip_frac = self._ppo_actor_loss(log_probs_t, old_logp, advantages, actor_mask)
            entropy_coeff = max(
                ENTROPY_COEFF_MIN, ENTROPY_COEFF_INIT * (ENTROPY_DECAY_RATE ** self._train_steps)
            )
            current_entropy = float(entropy_t[actor_mask].mean().detach())
            if current_entropy < -5.0:
                entropy_coeff = max(entropy_coeff, 0.1 * abs(current_entropy) / 5.0)

            actor_loss = actor_loss - entropy_coeff * entropy_t[actor_mask].mean()

            self.actor_opt.zero_grad()
            actor_loss.backward()
            torch.nn.utils.clip_grad_norm_(self.actor.parameters(), 1.0)
            self.actor_opt.step()
            actor_updated = True
            actor_loss_val = float(actor_loss.item())
            entropy_val = current_entropy

        self._train_steps += 1
        self._is_trained = True

        metrics = {
            "train_step":  self._train_steps,
            "actor_loss":  actor_loss_val,
            "critic_loss": float(critic_loss.item()),
            "entropy":     entropy_val,
            "mean_reward": float(rewards.mean().item()),
            "mean_adv":    float(advantages.mean().item()),
            "lambda":      self._lambda,
            "batch_jfi_mean": batch_jfi_mean,
            "n_experiences": len(batch),
            "contended_frac": contended_frac,
            "n_actor_samples": n_actor,
            "ppo_clip_frac": clip_frac,
            "actor_updated": actor_updated,
            "reward_std":  float(self._reward_std) if self._reward_std is not None else 0.0,
        }
        self._log.info(
            "[訓練][MLP] step=%d actor_loss=%.4f critic_loss=%.5f entropy=%.4f mean_reward=%.4f "
            "contended=%.0f%%(%s,n_actor=%d,clip=%.0f%%) reward_std=%.4f lambda=%.4f batch_jfi=%s n=%d",
            self._train_steps,
            metrics["actor_loss"], metrics["critic_loss"], metrics["entropy"],
            metrics["mean_reward"], 100.0 * contended_frac,
            "Actor更新" if actor_updated else "Actor跳過", n_actor, 100.0 * clip_frac,
            metrics["reward_std"], self._lambda,
            f"{batch_jfi_mean:.4f}" if batch_jfi_mean is not None else "N/A",
            len(batch),
        )
        return metrics

    def evaluate_on_batch_mlp(self, experiences: list[dict]) -> dict:
        """在測試集（打散經驗）上計算 loss，不更新梯度。回傳同 evaluate_on_batch_gru()。"""
        experiences = self._filter_valid_experiences(experiences)
        if len(experiences) < TRAIN_BATCH_SIZE:
            return {}

        idxs = np.random.choice(len(experiences), TRAIN_BATCH_SIZE, replace=False)
        batch = [experiences[i] for i in idxs]
        states, masks, actions, rewards, next_states, next_masks = self._build_experience_tensors(batch)

        self.actor.eval()
        self.critic.eval()
        with torch.no_grad():
            current_values = self.critic(states)
            next_values = self.critic(next_states)
            advantages, targets = self._advantages_mlp(rewards, current_values, next_values, update_std=False)

            test_critic_loss = F.mse_loss(current_values, targets)

            probs = self.actor(states, masks)
            log_probs_t, entropy_t = self._dirichlet_log_probs_and_entropy_flat(probs, masks, actions)
            contended = self._contended_mask(states, masks, next_states, next_masks)
            old_logp, has_old = self._behavior_logp_tensors(batch)
            am = contended & has_old
            if int(am.sum().item()) > 0:
                test_actor_loss, _ = self._ppo_actor_loss(log_probs_t, old_logp, advantages, am)
                test_entropy = entropy_t[am].mean()
            else:
                test_actor_loss = torch.tensor(0.0)
                test_entropy = entropy_t.mean()

        return {
            "test_actor_loss":  float(test_actor_loss.item()),
            "test_critic_loss": float(test_critic_loss.item()),
            "test_entropy":     float(test_entropy.item()),
            "test_mean_reward": float(rewards.mean().item()),
        }

    # -------------------------------------------------------------------------
    # 離線訓練 — GRU 分支（序列化，2026-07-09 導入，保留供未來切換）
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

    def train_on_batch_gru(self, sequences: list[list[dict]]) -> dict:
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
            "[訓練][GRU] step=%d actor_loss=%.4f critic_loss=%.4f "
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

    def evaluate_on_batch_gru(self, sequences: list[list[dict]]) -> dict:
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
        """是否已訓練足夠步數（>= MIN_DRL_TRAIN_STEPS），可從 BSR 啟發式切換至 DRL 推論模式。"""
        return self._is_trained and self._train_steps >= MIN_DRL_TRAIN_STEPS

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
                "arch":        self.arch,
                "actor":       self.actor.state_dict(),
                "critic":      self.critic.state_dict(),
                "actor_opt":   self.actor_opt.state_dict(),
                "critic_opt":  self.critic_opt.state_dict(),
                "train_steps": self._train_steps,
                "lambda":      self._lambda,
                "reward_std":  self._reward_std,
            },
            tmp_path,
        )
        os.replace(tmp_path, path)
        self._log.info("模型已儲存至 %s (arch=%s, 訓練步數: %d)", path, self.arch, self._train_steps)

    def load(self) -> bool:
        """
        嘗試從磁碟載入預存權重。

        載入成功後一律呼叫 reset_hidden()：隱藏狀態是舊網路權重產生的，
        對新載入的權重是未曾訓練過要處理的輸入，不重置會讓推論吃到一個
        語意不明的隱藏狀態。

        arch 不符時明確拒絕載入（而不是讓 load_state_dict() 在 key 不匹配時
        丟泛用例外）：MLP／GRU 的 state_dict key 不相容，切換 MODEL_ARCH 後
        應該清空 checkpoint 重新開始，若沒清乾淨，這裡會攔下來、印警告、
        維持隨機初始化，不會讓程式帶著錯誤架構的殘留權重跑。

        Returns:
            True 表示成功載入，False 表示找不到檔案、架構不符、或載入失敗
            （從隨機初始化開始）。
        """
        path = self.model_dir / f"model_node{self.node_id}.pt"
        if not path.exists():
            self._log.info("未找到預存模型 (%s)，從隨機初始化開始", path)
            return False
        try:
            ckpt = torch.load(path, map_location=self.device)
            ckpt_arch = ckpt.get("arch", "gru")   # 舊版（MODEL_ARCH 開關上線前）一律是 GRU
            if ckpt_arch != self.arch:
                self._log.warning(
                    "checkpoint 架構 (%s) 與目前 MODEL_ARCH (%s) 不符，拒絕載入、"
                    "從隨機初始化開始——切換 MODEL_ARCH 前應先清空 checkpoint",
                    ckpt_arch, self.arch,
                )
                return False
            self.actor.load_state_dict(ckpt["actor"])
            self.critic.load_state_dict(ckpt["critic"])
            self.actor_opt.load_state_dict(ckpt["actor_opt"])
            self.critic_opt.load_state_dict(ckpt["critic_opt"])
            self._train_steps = ckpt.get("train_steps", 0)
            self._is_trained  = self._train_steps > 0
            # 舊 checkpoint（Lagrangian 上線前存的）沒有 lambda 欄位，優雅降級為初始值
            self._lambda = ckpt.get("lambda", LAMBDA_INIT)
            self._reward_std = ckpt.get("reward_std", None)   # 舊 checkpoint 沒有此欄位 → 重新估
            self.reset_hidden()
            self._log.info(
                "模型已從 %s 載入 (arch=%s, 訓練步數: %d, lambda=%.4f)",
                path, self.arch, self._train_steps, self._lambda,
            )
            return True
        except Exception as exc:
            self._log.warning("模型載入失敗: %s，從隨機初始化開始", exc)
            return False
