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

Local DRL v2（2026-09-30，MODEL_ARCH=mlp，設計見 LOCAL_DRL_V2_DESIGN.md）：
  - 動作：每個活躍 UE 各自從 CAP_TIERS 5 檔選一個 PRB 上限（1.0 檔 = 不設上限 = PF），
    Actor 是全部 UE 共用的小 MLP 頭（build_per_ue_inputs()），初始化≈PF（residual 零修正初始化）。
  - reward 維持純吞吐量（REWARD_MODE=throughput_only），Critic 用 PF-shadow 資料離線預訓練
    （pretrain_critic()），不另設反事實 reward 模型（state-only 項會被 V(s) 完整吸收，數學上等價）。
  - GRU 分支（MODEL_ARCH=gru）維持舊版 Dirichlet 連續份額，未改。

State Space (固定長度向量，不足補零)：
  [norm_bsr_0, norm_cqi_0, norm_buf_0, ..., active_ratio, fairness_bias, bh_ratio,
   parent_trend(p̂), children_demand(ĉ)]
  長度 = MAX_UE_COUNT * 4 + 5 = 69（2026-10-01 起：每 UE 多一維「是否為 IAB 子節點（MT）」；p̂ 改為 parent DU 的佇列，見 encode_state()）

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
  MLP（v2）：每 UE 檔位 index ∈ {0..N_TIERS-1}，非活躍 UE 記為 -1（MongoDB 欄位 action_tiers）。
  GRU（舊版）：各 UE 的 PRB 分配比例 [0, 1]，總和為 1.0，非活躍 slot 經 mask 為 0（欄位 action_ratios）。

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
import itertools
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
UE_FEAT_DIM: int = 5                      # 每 UE：norm_bsr, norm_mcs, norm_buf, is_iab_child（relay 的子節點 MT＝1）, was_masked（上一步被遮罩＝1，2026-10-02 v2.1）
NODE_CTX_DIM: int = 5                     # 節點層級：active_ratio, fairness_bias, bh_ratio, parent_congestion(p̂), children_demand(ĉ)
# Local DRL v2（2026-09-30，LOCAL_DRL_V2_DESIGN.md §4.2）：51 → 53 維，多 p̂／ĉ 兩個 relational 特徵
STATE_DIM: int = MAX_UE_COUNT * UE_FEAT_DIM + NODE_CTX_DIM

# ── Local DRL v2 離散動作空間（MODEL_ARCH=mlp，LOCAL_DRL_V2_DESIGN.md §2）───────────────────────────
# 每個活躍 UE 各自從 5 檔「PRB 上限 = 檔位 × 106」中選一檔；最高檔 1.0 = 不設上限 = PF。
CAP_TIERS: tuple[float, ...] = (0.3, 0.5, 0.7, 0.85, 1.0)
# 2026-10-01：動作預設改成「時域遮罩」檔位（DRL_ACTION_DOMAIN=mask）。場景 P 試驗證實這個平台上頻域 PRB 上限無法重新分配資源
# （每 UE 每秒約 430 次傳輸的 ACK 限制，限制一個 UE 省下的 RB 別人用不到，−10%），時域 slot 遮罩才有效（+4%，HISTORY.md 續四十六）。
# 每檔對應一個 16 位元遮罩（第 k 位元＝frame 內 slot%16==k 可排程），PRB 不設上限；最後一檔 0xFFFF＝全開＝PF。
# 預設樣式取平台實測過的 0x9292／0x1111 加上模擬器評估過的 0x0101／0xab98（續四十七）；同節點被限制的 UE 共用同一組受限 slot。
# DRL_ACTION_DOMAIN=prb 退回舊版 PRB 上限檔位（CAP_TIERS）。
ACTION_DOMAIN: str = os.getenv("DRL_ACTION_DOMAIN", "mask").strip().lower()
def _even_masks() -> tuple[int, ...]:
    """v3（2026-10-03，使用者提議）：不預先挑「平台實測好用」的樣式，而是系統性產生 16 檔——可排程 slot 數 k=1..16，
    每檔把 k 個 slot 均勻分散在 slot%16 的 16 個位置（第 i 個在 round(i·16/k)）。k=2→0x0101、k=4→0x1111、k=8→0x5555（場景 P
    實測幾乎無效）、k=16→0xFFFF（PF）。哪些檔位在哪種狀態下好用由 DRL 自己學（規則只用固定一種）。"""
    out = []
    for k in range(1, 17):
        m = 0
        for i in range(k):
            m |= 1 << (round(i * 16 / k) % 16)
        out.append(m)
    return tuple(out)


# TDD（7D/1S/2U、週期 10 slot）下，frame 內 slot s 對應遮罩第 s%16 位元：位元 0～3 各管兩個 slot（0&16、1&17(S)、2&18(UL)、3&19(UL)），
# 位元 8、9 只管 UL slot（遮了沒作用）。所以真正決定遮罩強度的是「允許的 DL slot（＋S slot）集合」，不是位元數——even16 有重複
# （0x0001≡0x0101、0xfeff≡全開）。平台實測也符合：0x1111（4 DL）+11.5%、0x9292（4 DL＋2 S）+5.1%、0xab98（5 DL＋1 S）+6.2%、0x5555（8 DL）≈0。
# 預設 dl14（2026-10-03）：允許的 DL slot 數 m=1..13 各一檔、均勻分散在 14 個 DL slot 上（去掉效果相同的重複；m=1 因位元 0 同時管
# slot 0 與 16 而成為 2 個），再加平台實測過的 0x1111、0x9292、0xab98（若不重複），最後一檔全開＝PF，共 17 檔。選單只保證「每種強度都有」，
# 哪一檔在哪種狀態好用由 DRL 自己學（選單同時有不同位置、同強度的樣式，以及實測幾乎無效的強度）。even16／even16+ 保留可選。
_DL_SLOTS = (0, 1, 2, 3, 4, 5, 6, 10, 11, 12, 13, 14, 15, 16)
_DL_S_SET = frozenset(_DL_SLOTS) | {7, 17}


def _mask_effect(m: int) -> tuple[int, ...]:
    return tuple(s for s in range(20) if (m >> (s % 16)) & 1 and s in _DL_S_SET)


def _dl14_masks() -> tuple[int, ...]:
    out: list[int] = []
    seen: set = set()
    for k in range(1, 14):
        m = 0
        for i in range(k):
            m |= 1 << (_DL_SLOTS[round(i * 14 / k) % 14] % 16)
        if _mask_effect(m) not in seen:
            seen.add(_mask_effect(m)); out.append(m)
    for m in (0x1111, 0x9292, 0xAB98):
        if _mask_effect(m) not in seen:
            seen.add(_mask_effect(m)); out.append(m)
    return tuple(out + [0xFFFF])


# 預設 acktier（2026-10-03，取代 dl14）：位置敏感度分析（HISTORY 續六十三）顯示同強度不同位置效果差很多（access −1%～+12.8%），
# 由 OAI HARQ-ACK 時序推導出「允許的 slot 要平均分在兩個 ACK 群組」（續六十四）。選單＝強度 k=2..13 個 DL slot 的平衡樣式
# （同強度依最小間距排序；k=3,5,6 各 3 種、k=4 為實測最好的 0x1111、0x9044＋規則 1 種、其餘 1 種）＋全開，共 21 檔。
# 產生器：/home/lindor/mask_pos_20261003/gen_menu.py（結果寫死在這裡，不依賴外部檔）。
_ACKTIER_MASKS = (0x1004, 0x8404, 0x8408, 0x8410, 0x1111, 0x9044, 0x0411, 0x0449, 0x0849, 0x1049, 0x2449,
                  0x0455, 0x0855, 0x1455, 0x5455, 0x0C7D, 0x4C7D, 0x5C7D, 0xDC7D, 0xFC7D, 0xFFFF)
_MASK_SPEC = os.getenv("DRL_MASK_TIERS", "acktier").strip().lower()
if _MASK_SPEC == "acktier":
    MASK_TIERS: tuple[int, ...] = _ACKTIER_MASKS
elif _MASK_SPEC == "dl14":
    MASK_TIERS = _dl14_masks()
elif _MASK_SPEC in ("even16", "even16+"):
    _m = list(_even_masks())
    if _MASK_SPEC == "even16+":
        _m[-1:-1] = [x for x in (0x9292, 0xab98) if x not in _m]
    MASK_TIERS = tuple(_m)
else:
    MASK_TIERS = tuple(int(x, 16) & 0xFFFF for x in _MASK_SPEC.split(","))
assert MASK_TIERS[-1] == 0xFFFF, "DRL_MASK_TIERS 最後一檔必須是 0xffff（PF）"
assert ACTION_DOMAIN == "mask" or len(MASK_TIERS) == len(CAP_TIERS), "prb 模式只支援 5 檔"
N_TIERS: int = len(MASK_TIERS) if ACTION_DOMAIN == "mask" else len(CAP_TIERS)
PF_TIER: int = N_TIERS - 1
# Actor 初始化成「以此機率選 PF 檔」（residual policy learning 的零修正初始化，Silver et al. 2018／
# Johannink et al. 2019）：在上限動作空間裡 PF 的動作就是全部選 1.0 檔，所以「對 PF 做 BC」與「零修正
# 初始化」是同一件事，用輸出層 bias 閉式設定即可，不需要資料；其餘機率平均分給較低檔位當初始探索。
PF_INIT_PROB: float = float(os.getenv("DRL_PF_INIT_PROB", "0.8"))
# 共用 per-UE Actor 頭的輸入：自己 UE_FEAT_DIM 維 + 節點 5 維 + 其他活躍 UE 的 mean/max（各 UE_FEAT_DIM 維）
PER_UE_IN_DIM: int = UE_FEAT_DIM + NODE_CTX_DIM + 2 * UE_FEAT_DIM
# v3（2026-10-03）：relay 的子節點 MT 的 PF 檔 logit 額外加的初始偏置（可學參數）。遮 MT 會傷整條 branch，探索期少遮 MT；
# 不禁止，agent 仍可學會在需要時遮 MT。
MT_PF_BIAS_INIT: float = float(os.getenv("DRL_MT_PF_BIAS", "2.0"))
ACTOR_HIDDEN_DIM: int = 64

# ── Local DRL v2.1 節點層級宏動作（DRL_ACTION_SPACE=macro，預設；LOCAL_DRL_V2_DESIGN.md §10）──────────────
# 每個節點每秒選一個宏動作，「遮誰」由規則決定：候選＝活躍、非 MT（is_iab_child=0）、非當下 MCS 最高者，依 MCS 由低到高。
#   0＝不介入（PF）；1＝最差候選遮 MACRO_LIGHT_MASK；2＝最差候選遮 MACRO_STRONG_MASK；3＝最差兩個候選都遮 MACRO_STRONG_MASK。
# 候選數不足的宏動作視為無效（logit 設 −∞）。DRL_ACTION_SPACE=per_ue 退回 v2 的每 UE 5 檔。
ACTION_SPACE: str = os.getenv("DRL_ACTION_SPACE", "factored").strip().lower()   # v3.1（2026-10-03）預設 factored；per_ue＝v3 第一版、macro＝v2.1
# v3.1 兩段式動作（factored）：節點先選一個遮罩強度（MASK_TIERS 其中一檔，全部子節點共用），再對每個子節點決定是否套用。
# 原因（HISTORY 續六十六）：relay 熱點的有效動作是「兩個邊緣 UE 同時重遮」，只遮一個沒有效果（另一個吃掉讓出的 slot）；
# per_ue 每子節點獨立抽遮罩，兩個同時重遮的機率約 1%，Actor 收不到正訊號。兩段式下同強度的組合只需兩個「套用」都為是。
NODE_PF_INIT_PROB: float = float(os.getenv("DRL_NODE_PF_INIT_PROB", "0.6"))   # 節點層級初始選全開的機率
APPLY_INIT_PROB: float = float(os.getenv("DRL_APPLY_INIT_PROB", "0.5"))        # 非 MT 子節點初始「套用」機率
MT_APPLY_BIAS_INIT: float = float(os.getenv("DRL_MT_APPLY_BIAS", "-3.5"))      # MT 的套用 logit 另加可學偏置（初始套用≈0.03）
# v3.5（2026-10-04）兩段式的變體：apply_first＝沒有節點層級的「全開」選項；每個子節點自己決定是否被遮（Bernoulli），
# 至少一個被遮時才由節點選強度（只在 5 檔非全開之間選）。舊版（node_on）的「是否開遮罩」是節點層級共用的機率，
# access 遮到好 UE（−22%）的懲罰會連帶壓低「遮壞 UE」（+4～5%），Actor 學成整個不開；apply_first 讓懲罰只落在
# 「遮好 UE」那個子節點的機率上（HISTORY 續七十一）。初始套用機率 DRL_APPLY_FIRST_INIT_PROB（非 MT）。
FACTORED_MODE: str = os.getenv("DRL_FACTORED_MODE", "node_on").strip().lower()
APPLY_FIRST_INIT_PROB: float = float(os.getenv("DRL_APPLY_FIRST_INIT_PROB", "0.3"))
# 探索下限（v3.6，2026-10-04）：非 MT 子節點「被遮」機率不低於此值（ε 探索）。共用 reward 下，探索時兩個 UE 一起被遮（含好 UE，−26%）
# 會連帶壓低「遮壞 UE」的機率；不設下限時 access 的套用機率在「遮好 UE」降下來之前就先塌到 0、之後不再探索（離線測試，HISTORY 續七十二）。0＝不設。
APPLY_FLOOR: float = float(os.getenv("DRL_APPLY_FLOOR", "0"))
# ── v4 DQN（2026-10-04，DRL_ACTION_SPACE=dqn）────────────────────────────────────────────────
# 價值型 DRL：Q(s,a) 網路直接估計每個動作的 n 步回報，ε-greedy 選動作、Double DQN＋Polyak 目標網路更新。
# 動作＝「遮哪些非 MT 子節點（任意非空子集）＋共用強度（5 檔非全開）」或「不遮任何子節點（PF）」；MT（backhaul）不在動作集內
# （遮 MT 違背 relay 讓 slot 給 backhaul 的目的）。改用 DQN 的原因：共用 reward 下 PPO 的逐子節點信用分配雜訊大，
# 離線測試結論隨測法翻轉；Q 迴歸可直接在保留資料上驗證（HISTORY 續七十二）。暖身期（DRL_ACTOR_WARMUP_STEPS）只訓練 Q、
# 主要動作維持 PF，以 ε 探索；之後 greedy＝argmax Q，ε 線性降到 DQN_EPS_END。
DQN_EPS_START: float = float(os.getenv("DRL_DQN_EPS_START", "0.3"))
DQN_EPS_END: float = float(os.getenv("DRL_DQN_EPS_END", "0.05"))
DQN_EPS_DECAY_STEPS: int = int(os.getenv("DRL_DQN_EPS_DECAY_STEPS", "1500"))
DQN_LR: float = float(os.getenv("DRL_DQN_LR", "5e-4"))
DQN_TAU: float = float(os.getenv("DRL_DQN_TAU", "0.01"))
DQN_Q_SCALE: float = float(os.getenv("DRL_DQN_Q_SCALE", "50"))
# 相對 PF 的增益門檻（2026-10-04，Codex 建議）：貪婪動作只有在 Q(s,a*)−Q(s,PF) > δ（reward 單位）時才採用，否則維持 PF——
# argmax 必然選出某個動作，即使估值不可靠；N 類（遮罩沒有好處）小幅高估的誤遮會勝出。δ 用場景真值的保留資料選，不用量測 seed。
DQN_PF_MARGIN: float = float(os.getenv("DRL_DQN_PF_MARGIN", "0"))
DQN_EXPLORE_PF: float = float(os.getenv("DRL_DQN_EXPLORE_PF", "0"))   # ε 探索時選「不遮」的機率（0＝全部候選均勻）
# 凍結實測（/tmp/drl_pause 存在）時不探索（ε=0），量到的才是學成的貪婪策略
DQN_PAUSE_FILE: str = os.getenv("DRL_PAUSE_FILE", "/tmp/drl_pause")
N_MACRO: int = 4
MACRO_LIGHT_MASK: int = int(os.getenv("DRL_MACRO_LIGHT_MASK", "0x9292"), 16) & 0xFFFF
MACRO_STRONG_MASK: int = int(os.getenv("DRL_MACRO_STRONG_MASK", "0x1111"), 16) & 0xFFFF
# 「好通道 UE 積壓」（starved）判定，與動態規則（inference_server.py RULE_DYN_*）相同：介入才有好處的必要條件，也是 Actor 的決策點過濾
STARVE_GOOD_MCS: float = float(os.getenv("DRL_STARVE_GOOD_MCS", "20"))
STARVE_BUF_BYTES: float = float(os.getenv("DRL_STARVE_BUF_BYTES", "100000"))
# 2026-10-02：遮罩動作只在「被遮候選的 MCS ≤ 當下最好 UE 的 MCS − MACRO_MIN_GAP」時有效。平台實測：通道相近的 UE 之間互遮
# 沒有增益（HS 第二版混合節點、v2.1 訓練初期 access 節點的誤遮）；有增益的結構（PB：relay 邊緣 UE MCS 3～7 vs MT 28）差距 ≥ 20。
MACRO_MIN_GAP: float = float(os.getenv("DRL_MACRO_MIN_GAP", "10"))
MACRO_CAND_FEAT: int = 4                  # 每個候選：norm_mcs, norm_buf, norm_bsr, was_masked
MACRO_IN_DIM: int = 2 * 64 + NODE_CTX_DIM + 2 * (MACRO_CAND_FEAT + 1) + 2   # 編碼池化 + 節點 + 兩個候選(含有效位) + starved + n_cand/2
BC_LABEL_SMOOTH: float = float(os.getenv("DRL_BC_LABEL_SMOOTH", "0.1"))


def macro_context(state: torch.Tensor, mask: torch.Tensor) -> dict:
    """
    由 state 算出宏動作需要的節點層級資訊（推論與訓練共用，確保同一套定義）。
    回傳：cand（(b,2) 最差兩個候選的 UE index，無則 -1）、n_cand（(b,)）、starved（(b,) bool）、
    valid（(b,N_MACRO) bool，可選的宏動作）、cand_feat（(b, 2·(MACRO_CAND_FEAT+1))）。
    """
    b = state.shape[0]
    ue = state[:, :MAX_UE_COUNT * UE_FEAT_DIM].reshape(b, MAX_UE_COUNT, UE_FEAT_DIM)
    mcs = ue[..., 1] * 28.0
    buf = torch.expm1(ue[..., 2] * math.log1p(MAX_BUF_INFO))
    is_mt = ue[..., 3] > 0.5
    mcs_act = mcs.masked_fill(~mask, -1.0)
    top = mcs_act.argmax(dim=1)                                           # 當下 MCS 最高者（不遮）
    not_top = torch.ones_like(mask)
    not_top[torch.arange(b), top] = False
    elig = mask & ~is_mt & not_top
    key = mcs.masked_fill(~elig, 1e9)
    order = key.argsort(dim=1)
    n_cand = elig.sum(dim=1)
    c0 = torch.where(n_cand >= 1, order[:, 0], torch.full_like(order[:, 0], -1))
    c1 = torch.where(n_cand >= 2, order[:, 1], torch.full_like(order[:, 1], -1))
    starved = (mask & (mcs >= STARVE_GOOD_MCS) & (buf >= STARVE_BUF_BYTES)).any(dim=1)
    top_mcs = mcs_act.max(dim=1).values
    def gap_ok(c: torch.Tensor) -> torch.Tensor:
        return (c >= 0) & (mcs[torch.arange(b), c.clamp(min=0)] <= top_mcs - MACRO_MIN_GAP)
    ok0, ok1 = gap_ok(c0), gap_ok(c1)
    valid = torch.zeros(b, N_MACRO, dtype=torch.bool, device=state.device)
    valid[:, 0] = True
    valid[:, 1] = ok0
    valid[:, 2] = ok0
    valid[:, 3] = ok0 & ok1
    # 決策點（2026-10-02）：有可行的遮罩動作，且（好通道 UE 積壓 或 目前已有 UE 被遮——要學何時解除）
    any_masked = (mask & (ue[..., 4] > 0.5)).any(dim=1)
    decision = valid[:, 1:].any(dim=1) & (starved | any_masked)
    feats = []
    for c in (c0, c1):
        ok = (c >= 0).float().unsqueeze(1)
        g = ue[torch.arange(b), c.clamp(min=0)]                           # (b, UE_FEAT_DIM)
        f = torch.stack([g[:, 1], g[:, 2], g[:, 0], g[:, 4]], dim=1) * ok
        feats += [f, ok]
    cand_feat = torch.cat(feats, dim=1)
    return {"cand": torch.stack([c0, c1], dim=1), "n_cand": n_cand, "starved": starved,
            "valid": valid, "cand_feat": cand_feat, "decision": decision}


def macro_to_tiers(macro: int, cand: list[int], n: int) -> list[int]:
    """宏動作 → 每 UE 的檔位 index（MASK_TIERS 中的位置；PF_TIER＝全開）。"""
    tiers = [PF_TIER] * n
    light = MASK_TIERS.index(MACRO_LIGHT_MASK) if MACRO_LIGHT_MASK in MASK_TIERS else 2
    strong = MASK_TIERS.index(MACRO_STRONG_MASK) if MACRO_STRONG_MASK in MASK_TIERS else 1
    if macro in (1, 2) and cand[0] >= 0:
        tiers[cand[0]] = light if macro == 1 else strong
    elif macro == 3:
        for c in cand:
            if c >= 0:
                tiers[c] = strong
    return tiers
CRITIC_ENC_DIM: int = 64                  # DeepSets Critic 的 per-UE 編碼維度

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
GAMMA_MLP: float = float(os.getenv("DRL_GAMMA_MLP", "0.5"))   # 2026-10-03 v3：0.5（遮罩效果有 TCP 爬升／佇列消化的延遲）；v2／v2.1 為 0
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
# v3（2026-10-03）：Critic 線上暖身——訓練步數 < 此值時只更新 Critic、不更新 Actor（隨機初始化的 Critic 給出的 advantage 是雜訊）。
# 暖身期間策略維持初始的近 PF 行為；0＝不暖身。
ACTOR_WARMUP_STEPS: int = int(os.getenv("DRL_ACTOR_WARMUP_STEPS", "0"))
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
MIN_TRAIN_EXPERIENCES: int = int(os.getenv("DRL_MIN_TRAIN_EXP", "200"))        # 觸發第一次訓練所需的最少「原始經驗」數
                                         # （MLP：i.i.d. 抽樣的門檻本身；GRU：序列
                                         # 切窗之前的門檻，training_pipeline.py 用）

# MLP（MODEL_ARCH=mlp）訓練參數：打散抽樣獨立經驗，不要求時間連續性。
TRAIN_BATCH_SIZE: int = int(os.getenv("DRL_TRAIN_BATCH", "128"))     # 每次梯度更新的 mini-batch 大小（i.i.d. 抽樣）

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
# Local DRL v2（離散 per-UE Categorical）的 entropy 下限：初始 PF_INIT_PROB=0.8 時約 0.78，
# 上限 ln(5)≈1.61；低於此值代表幾乎每個 UE 都只剩一個檔位，探索快消失
ENTROPY_FLOOR: float = float(os.getenv("DRL_ENTROPY_FLOOR", "0.1"))

# Dirichlet 策略集中度 K：α = probs × K，K 越大越確定性、越小探索性越強。
# 修正記錄：舊版是寫死常數 K=5，訓練前後不變，代表即使 policy 已收斂，infer() 實際
# 下發的分配仍帶有恆定雜訊，不會隨訓練減少。改為隨 self._train_steps 指數退火，
# 從 DIRICHLET_K_MIN 逐漸升高至 DIRICHLET_K_MAX（見 DRLAgent._current_concentration()），
# 讓推論階段的隨機性隨訓練收斂真正變小，同時仍保留隨機策略架構（不改成完全確定性輸出，
# 維持系統持續探索、持續學習的能力）。
# 2026-09-29 修正：原本 tau=5000 是假設訓練會跑到數千~數萬步的量級去訂的，但本專案
# 實際可負擔的訓練時長只有 ~2.5~3 小時、train_steps 只到 ~600~2800——K(t) 在這個範圍內
# 連退火進度的一半都不到（t=2800 時只有 43%），代表 Stage 2~4 目前為止的每一次訓練，
# 全程都還在高雜訊探索階段、從未真正進入低雜訊可利用（exploit）的階段，訓練訊號可能被
# 這層雜訊蓋過。改成 tau=800，讓 2.5~3 小時（train_steps ~1500~2800）就能退火到 80~95%，
# 匹配平台實際可負擔的訓練預算。
DIRICHLET_K_MIN: float = 5.0             # 訓練初期集中度（與舊版固定值相同，不改變早期探索行為）
DIRICHLET_K_MAX: float = 50.0            # 訓練後期集中度上限，大幅降低採樣雜訊
DIRICHLET_K_ANNEAL_TAU: float = float(os.getenv("DRL_DIRICHLET_K_ANNEAL_TAU", "800"))   # 退火時間常數（訓練步數），越大退火越慢

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

def build_per_ue_inputs(state: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
    """
    把扁平 state (batch, STATE_DIM) 組成共用 per-UE 頭的輸入 (batch, MAX_UE_COUNT, PER_UE_IN_DIM)：
    [自己 UE_FEAT_DIM 維 | 節點 5 維 | 其他活躍 UE 的 mean | 其他活躍 UE 的 max]。
    「其他 UE 的摘要」讓每個 UE 的決策看得到同節點的競爭者，且對 UE 排列順序等變
    （同 Yamin & Permuter 以特徵串接做關聯推理、網路本身仍是 MLP 的做法）。
    """
    b = state.shape[0]
    ue = state[:, :MAX_UE_COUNT * UE_FEAT_DIM].reshape(b, MAX_UE_COUNT, UE_FEAT_DIM)
    ctx = state[:, MAX_UE_COUNT * UE_FEAT_DIM:].unsqueeze(1).expand(b, MAX_UE_COUNT, NODE_CTX_DIM)

    eye = torch.eye(MAX_UE_COUNT, dtype=torch.bool, device=state.device)
    others = mask.unsqueeze(1) & ~eye.unsqueeze(0)                     # (b, i, j)：j 是 i 以外的活躍 UE
    n_others = others.sum(dim=2, keepdim=True)                         # (b, i, 1)
    ue_j = ue.unsqueeze(1).expand(b, MAX_UE_COUNT, MAX_UE_COUNT, UE_FEAT_DIM)
    o = others.unsqueeze(-1)
    mean_o = (ue_j * o).sum(dim=2) / n_others.clamp(min=1)
    max_o = ue_j.masked_fill(~o, -1.0).max(dim=2).values
    max_o = torch.where(n_others > 0, max_o, torch.zeros_like(max_o))  # 沒有其他 UE → 0

    return torch.cat([ue, ctx, mean_o, max_o], dim=-1)


class ActorNetworkMLP(nn.Module):
    """
    Local DRL v2 Policy Network（LOCAL_DRL_V2_DESIGN.md §2）：全部 UE 共用同一個小 MLP 頭，
    每個 UE 各自輸出 N_TIERS 檔上限的 logits。

    跟舊版「整個 state → 16 個 slot 各一個 logit」相比：slot 順序是 xApp 的 RNTI 表順序、本身沒有
    意義，現行拓樸每節點只有 1~3 個 UE（16 個 slot 大多是空的）；共用頭讓每個活躍 UE 都是同一組權重
    的訓練樣本、對 UE 排列等變，在每輪只有數千個環境步的訓練預算下樣本效率高得多。

    初始化：最後一層權重縮到接近 0、bias 設成 log(PF_INIT_PROB) 等，讓初始策略以 PF_INIT_PROB 機率
    選 PF 檔（不設上限）——初始行為≈PF，RL 只在有好處的地方偏離（residual policy learning）。
    """

    def __init__(self) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(PER_UE_IN_DIM, ACTOR_HIDDEN_DIM),
            nn.ReLU(),
            nn.Linear(ACTOR_HIDDEN_DIM, ACTOR_HIDDEN_DIM),
            nn.ReLU(),
            nn.Linear(ACTOR_HIDDEN_DIM, N_TIERS),
        )
        head = self.net[-1]
        with torch.no_grad():
            head.weight.mul_(0.01)
            other = (1.0 - PF_INIT_PROB) / (N_TIERS - 1)
            head.bias.copy_(torch.tensor([math.log(other)] * (N_TIERS - 1) + [math.log(PF_INIT_PROB)]))
        self.mt_pf_bias = nn.Parameter(torch.tensor(MT_PF_BIAS_INIT))

    def forward(
        self,
        state: torch.Tensor,   # (batch, STATE_DIM)
        mask: torch.Tensor,    # (batch, MAX_UE_COUNT)  True = 活躍 UE
    ) -> torch.Tensor:
        """回傳每個 UE 的檔位 logits，形狀 (batch, MAX_UE_COUNT, N_TIERS)；非活躍 UE 的列由呼叫端遮蔽。"""
        logits = self.net(build_per_ue_inputs(state, mask))
        b = state.shape[0]
        is_mt = state[:, :MAX_UE_COUNT * UE_FEAT_DIM].reshape(b, MAX_UE_COUNT, UE_FEAT_DIM)[..., 3]
        bonus = torch.zeros_like(logits)
        bonus[..., PF_TIER] = self.mt_pf_bias * is_mt
        return logits + bonus


class ActorNetworkFactored(nn.Module):
    """
    v3.1 兩段式 Actor：共用 per-UE 編碼器 → (1) 每個子節點的「是否套用」logit；(2) 活躍子節點編碼的 mean/max 池化＋節點特徵
    → 節點層級的遮罩強度 logits（N_TIERS 檔）。初始化：強度選全開的機率 NODE_PF_INIT_PROB，非 MT 子節點套用機率
    APPLY_INIT_PROB，MT 的套用 logit 另加可學偏置 MT_APPLY_BIAS_INIT。
    """

    def __init__(self) -> None:
        super().__init__()
        self.enc = nn.Sequential(nn.Linear(PER_UE_IN_DIM, ACTOR_HIDDEN_DIM), nn.ReLU(),
                                 nn.Linear(ACTOR_HIDDEN_DIM, ACTOR_HIDDEN_DIM), nn.ReLU())
        self.apply_head = nn.Linear(ACTOR_HIDDEN_DIM, 1)
        self.tier_head = nn.Sequential(nn.Linear(2 * ACTOR_HIDDEN_DIM + NODE_CTX_DIM, ACTOR_HIDDEN_DIM), nn.ReLU(),
                                       nn.Linear(ACTOR_HIDDEN_DIM, N_TIERS))
        with torch.no_grad():
            self.apply_head.weight.mul_(0.01)
            p0 = APPLY_FIRST_INIT_PROB if FACTORED_MODE == "apply_first" else APPLY_INIT_PROB
            self.apply_head.bias.fill_(math.log(p0 / (1.0 - p0)))
            last = self.tier_head[-1]
            last.weight.mul_(0.01)
            other = (1.0 - NODE_PF_INIT_PROB) / (N_TIERS - 1)
            last.bias.copy_(torch.tensor([math.log(other)] * (N_TIERS - 1) + [math.log(NODE_PF_INIT_PROB)]))
        self.mt_apply_bias = nn.Parameter(torch.tensor(MT_APPLY_BIAS_INIT))

    def forward(self, state: torch.Tensor, mask: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        """回傳 (強度 logits (batch, N_TIERS), 每子節點套用 logits (batch, MAX_UE_COUNT))。"""
        b = state.shape[0]
        h = self.enc(build_per_ue_inputs(state, mask))                          # (b, U, H)
        is_mt = state[:, :MAX_UE_COUNT * UE_FEAT_DIM].reshape(b, MAX_UE_COUNT, UE_FEAT_DIM)[..., 3]
        apply_logits = self.apply_head(h).squeeze(-1) + self.mt_apply_bias * is_mt
        if APPLY_FLOOR > 0:   # 非 MT 子節點的套用機率下限（MT 不設，遮 MT 本來就該趨近 0）
            lo = math.log(APPLY_FLOOR / (1.0 - APPLY_FLOOR))
            apply_logits = torch.where(is_mt > 0.5, apply_logits, apply_logits.clamp(min=lo))
        m = mask.unsqueeze(-1)
        n = mask.sum(dim=1, keepdim=True).clamp(min=1)
        mean_h = (h * m).sum(dim=1) / n
        max_h = h.masked_fill(~m, 0.0).max(dim=1).values
        ctx = state[:, MAX_UE_COUNT * UE_FEAT_DIM:]
        tier_logits = self.tier_head(torch.cat([mean_h, max_h, ctx], dim=-1))
        return tier_logits, apply_logits


DQN_NET: str = os.getenv("DRL_DQN_NET", "flat").strip().lower()   # set＝以子節點為單位（排列不變）；flat＝舊版扁平
Q_IN_DIM: int = STATE_DIM + 2 * MAX_UE_COUNT + N_TIERS            # state｜每子節點是否被遮｜每子節點是否活躍｜強度 one-hot


class QNetworkSet(nn.Module):
    """v4.3 以子節點為單位的 Q(s,a)（DeepSets，2026-10-04）：每個活躍子節點的 [自己特徵｜節點特徵｜其他子節點 mean／max｜是否被遮｜
    被遮時的強度 one-hot] 經共用編碼器 φ，對活躍子節點做 sum／max 池化，接節點特徵經 ρ 輸出 Q／DQN_Q_SCALE。
    扁平版把「第 j 個子節點被遮」當成位置旗標，好／壞 UE 在各節點的位置隨機，網路必須逐位置學；這裡同一個 φ 對所有位置、所有節點共用。"""

    def __init__(self) -> None:
        super().__init__()
        self.phi = nn.Sequential(nn.Linear(PER_UE_IN_DIM + 1 + N_TIERS, 64), nn.ReLU(), nn.Linear(64, 64), nn.ReLU())
        self.rho = nn.Sequential(nn.Linear(2 * 64 + NODE_CTX_DIM, 64), nn.ReLU(), nn.Linear(64, 1))
        with torch.no_grad():
            self.rho[-1].weight.mul_(0.1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        st = x[:, :STATE_DIM]
        applied = x[:, STATE_DIM:STATE_DIM + MAX_UE_COUNT]
        active = x[:, STATE_DIM + MAX_UE_COUNT:STATE_DIM + 2 * MAX_UE_COUNT] > 0.5
        tier = x[:, STATE_DIM + 2 * MAX_UE_COUNT:]
        ue_in = build_per_ue_inputs(st, active)
        a = applied.unsqueeze(-1)
        h = self.phi(torch.cat([ue_in, a, tier.unsqueeze(1) * a], dim=-1))
        m = active.unsqueeze(-1)
        hs = (h * m).sum(dim=1)
        hm = h.masked_fill(~m, 0.0).max(dim=1).values
        return self.rho(torch.cat([hs, hm, st[:, MAX_UE_COUNT * UE_FEAT_DIM:]], dim=-1)).squeeze(-1)


class QNetworkAdd(nn.Module):
    """v4.4 可加 Q 網路（2026-10-04）：Q(s,a)＝V(s)＋Σ_{被遮的子節點 j} A_j。V 是對子節點排列不變的 DeepSets；
    A_j 由同一個共用網路依 [子節點 j 的特徵｜節點特徵｜同節點其他子節點 mean／max｜強度 one-hot｜同節點其他被遮子節點比例] 算出。
    共用權重讓「遮哪個子節點」只取決於該子節點的特徵（對位置天然一致）；「其他被遮比例」讓組合效果（relay 兩個邊緣 UE 一起遮）可學。
    動機：扁平版位置交換一致性只有 61%，access 的 M／N 學不開，但好 UE 佇列單一特徵就有 AUC 0.90（HISTORY 續七十三）。"""

    def __init__(self) -> None:
        super().__init__()
        self.phi_v = nn.Sequential(nn.Linear(PER_UE_IN_DIM, 64), nn.ReLU(), nn.Linear(64, 64), nn.ReLU())
        self.rho_v = nn.Sequential(nn.Linear(2 * 64 + NODE_CTX_DIM, 64), nn.ReLU(), nn.Linear(64, 1))
        self.adv = nn.Sequential(nn.Linear(PER_UE_IN_DIM + N_TIERS + 1, 64), nn.ReLU(), nn.Linear(64, 64), nn.ReLU(), nn.Linear(64, 1))
        with torch.no_grad():
            self.rho_v[-1].weight.mul_(0.1)
            self.adv[-1].weight.mul_(0.1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        st = x[:, :STATE_DIM]
        applied = x[:, STATE_DIM:STATE_DIM + MAX_UE_COUNT]
        active = x[:, STATE_DIM + MAX_UE_COUNT:STATE_DIM + 2 * MAX_UE_COUNT] > 0.5
        tier = x[:, STATE_DIM + 2 * MAX_UE_COUNT:]
        ue_in = build_per_ue_inputs(st, active)                                   # (b, U, PER_UE_IN_DIM)
        m = active.unsqueeze(-1)
        h = self.phi_v(ue_in)
        v = self.rho_v(torch.cat([(h * m).sum(1) / m.sum(1).clamp(min=1), h.masked_fill(~m, 0.0).max(1).values,
                                  st[:, MAX_UE_COUNT * UE_FEAT_DIM:]], dim=-1)).squeeze(-1)
        n_act = active.sum(1, keepdim=True).clamp(min=2).float()
        other = ((applied.sum(1, keepdim=True) - applied) / (n_act - 1)).unsqueeze(-1)   # 其他子節點被遮的比例
        b = st.shape[0]
        a_in = torch.cat([ue_in, tier.unsqueeze(1).expand(b, MAX_UE_COUNT, N_TIERS), other], dim=-1)
        adv = self.adv(a_in).squeeze(-1) * applied * active.float()
        return v + adv.sum(1)


class QNetwork(nn.Module):
    """v4 DQN 的 Q(s,a)：輸入＝state（STATE_DIM）＋每子節點是否被遮（MAX_UE_COUNT）＋強度 one-hot（N_TIERS），輸出純量 Q／DQN_Q_SCALE。
    離線可行性驗證（HISTORY 續七十二）：同架構在保留資料 R²≈0.89，argmax 在 relay 該遮選兩個 UE 都遮 68%（不該遮 35%）、access M 只遮壞 84%（N 33%）。"""

    def __init__(self) -> None:
        super().__init__()
        # 扁平版不使用「子節點是否活躍」欄位（與 2026-10-04 預訓練 checkpoint 的輸入維度一致）
        self.net = nn.Sequential(nn.Linear(STATE_DIM + MAX_UE_COUNT + N_TIERS, 128), nn.ReLU(),
                                 nn.Linear(128, 128), nn.ReLU(), nn.Linear(128, 1))
        with torch.no_grad():
            self.net[-1].weight.mul_(0.1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = torch.cat([x[:, :STATE_DIM + MAX_UE_COUNT], x[:, STATE_DIM + 2 * MAX_UE_COUNT:]], dim=-1)
        return self.net(x).squeeze(-1)


def dqn_candidates(state_vec, mask_vec) -> list[tuple[list[int], int]]:
    """候選動作：[(被遮的子節點 index 列表, 強度)]；第一個是「不遮」（強度＝PF）。只遮非 MT 的活躍子節點。"""
    ue = [j for j in range(min(len(mask_vec), MAX_UE_COUNT)) if mask_vec[j] and state_vec[j * UE_FEAT_DIM + 3] < 0.5]
    out: list[tuple[list[int], int]] = [([], PF_TIER)]
    for k in range(1, len(ue) + 1):
        for sub in itertools.combinations(ue, k):
            for t in range(N_TIERS - 1):
                out.append((list(sub), t))
    return out


# Global xApp 的 fairness_bias 特徵（state 第 MAX_UE_COUNT·UE_FEAT_DIM+1 維）：DRL_GLOBAL_FAIRNESS=0 時固定為中性值（bias=1.0）。
GLOBAL_FAIRNESS_FEATURE: bool = os.getenv("DRL_GLOBAL_FAIRNESS", "1").strip() not in ("0", "false", "False")
FAIRNESS_IDX: int = MAX_UE_COUNT * UE_FEAT_DIM + 1
FAIRNESS_NEUTRAL: float = (1.0 - 0.5) / (2.0 - 0.5)


def dqn_features(state_vec, mask_vec, cands: list[tuple[list[int], int]]) -> np.ndarray:
    """把 (state, 候選動作) 組成 Q 網路輸入矩陣 (len(cands), Q_IN_DIM)。"""
    x = np.zeros((len(cands), Q_IN_DIM), np.float32)
    x[:, :STATE_DIM] = np.asarray(state_vec, np.float32)
    if not GLOBAL_FAIRNESS_FEATURE:   # 2026-10-05：Global xApp 不提供公平性訊號（目標＝吞吐量）→ 該維固定中性值（訓練／推論一致，舊資料也適用）
        x[:, FAIRNESS_IDX] = FAIRNESS_NEUTRAL
    act = np.zeros(MAX_UE_COUNT, np.float32); act[:min(len(mask_vec), MAX_UE_COUNT)] = np.asarray(mask_vec[:MAX_UE_COUNT], np.float32)
    x[:, STATE_DIM + MAX_UE_COUNT:STATE_DIM + 2 * MAX_UE_COUNT] = act
    for i, (sub, t) in enumerate(cands):
        for j in sub:
            x[i, STATE_DIM + j] = 1.0
        x[i, STATE_DIM + 2 * MAX_UE_COUNT + t] = 1.0
    return x


_PF_ONEHOT = torch.zeros(1, N_TIERS, dtype=torch.bool)   # apply_first：強度不可選全開
_PF_ONEHOT[0, PF_TIER] = True


def factored_logp_entropy(tier_logits: torch.Tensor, apply_logits: torch.Tensor, masks: torch.Tensor,
                          tier: torch.Tensor, apply: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
    """log π(a|s)＝log π(強度)＋（強度≠全開時）Σ 活躍子節點 log π(套用_i)；entropy＝強度熵＋活躍子節點平均套用熵。形狀皆 (batch,)。"""
    lp_on = F.logsigmoid(apply_logits); lp_off = F.logsigmoid(-apply_logits)
    a = apply.float()
    lp_child = (a * lp_on + (1.0 - a) * lp_off).masked_fill(~masks, 0.0)
    if FACTORED_MODE == "apply_first":
        # log π＝Σ 活躍子節點 log π(套用_i)＋（至少一個套用時）log π(強度 | 非全開)
        logp_t = F.log_softmax(tier_logits.masked_fill(_PF_ONEHOT.to(tier_logits.device), float("-inf")), dim=-1)
        any_on = ((a * masks.float()).sum(dim=1) > 0).float()
        lp_t = logp_t.gather(1, tier.clamp(max=N_TIERS - 2).unsqueeze(1)).squeeze(1)
        lp = lp_child.sum(dim=1) + any_on * lp_t
        pt = logp_t.exp()
        ent = -(pt * logp_t.masked_fill(pt == 0, 0.0)).sum(dim=-1)
    else:
        logp_t = F.log_softmax(tier_logits, dim=-1)
        lp = logp_t.gather(1, tier.unsqueeze(1)).squeeze(1)
        ent = -(logp_t.exp() * logp_t).sum(dim=-1)
        use = (tier != PF_TIER).float()
        lp = lp + use * lp_child.sum(dim=1)
    p_on = torch.sigmoid(apply_logits)
    ent_child = -(p_on * lp_on + (1.0 - p_on) * lp_off).masked_fill(~masks, 0.0)
    ent = ent + ent_child.sum(dim=1) / masks.sum(dim=1).clamp(min=1)
    return lp, ent


class CriticNetworkMLP(nn.Module):
    """
    Local DRL v2 Value Network（DeepSets 集合架構，2026-09-30）：V(s) 對 UE 排列不變，跟 Actor 的
    共用 per-UE 頭一致。每個活躍 UE 的 [自己 UE_FEAT_DIM 維 | 節點 5 維] 先過共用編碼器 φ，對活躍 UE 做
    mean／max pooling，再接節點 5 維，經 ρ 輸出純量。舊版扁平 53→128→64→1 會把「slot 1 的 UE」跟
    「slot 2 的 UE」當成不同東西學，slot 順序本身沒有意義，資料少時估不準 V，advantage 品質跟著變差。
    沒有活躍 UE 時 pooling 結果為 0，V 只由節點特徵決定。
    """

    def __init__(self) -> None:
        super().__init__()
        enc_in = UE_FEAT_DIM + NODE_CTX_DIM
        self.phi = nn.Sequential(
            nn.Linear(enc_in, CRITIC_ENC_DIM), nn.ReLU(),
            nn.Linear(CRITIC_ENC_DIM, CRITIC_ENC_DIM), nn.ReLU(),
        )
        self.rho = nn.Sequential(
            nn.Linear(2 * CRITIC_ENC_DIM + NODE_CTX_DIM, 64), nn.ReLU(),
            nn.Linear(64, 1),
        )

    def forward(self, state: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
        """state (batch, STATE_DIM)、mask (batch, MAX_UE_COUNT) → 狀態價值估計，形狀 (batch,)。"""
        b = state.shape[0]
        ue = state[:, :MAX_UE_COUNT * UE_FEAT_DIM].reshape(b, MAX_UE_COUNT, UE_FEAT_DIM)
        ctx = state[:, MAX_UE_COUNT * UE_FEAT_DIM:]
        h = self.phi(torch.cat([ue, ctx.unsqueeze(1).expand(b, MAX_UE_COUNT, NODE_CTX_DIM)], dim=-1))
        m = mask.unsqueeze(-1)
        n = mask.sum(dim=1, keepdim=True).clamp(min=1)
        mean_h = (h * m).sum(dim=1) / n
        max_h = h.masked_fill(~m, 0.0).max(dim=1).values   # φ 的輸出經 ReLU ≥0，用 0 遮蔽不影響 max
        return self.rho(torch.cat([mean_h, max_h, ctx], dim=-1)).squeeze(-1)


class ActorNetworkMacro(nn.Module):
    """
    Local DRL v2.1 節點層級 Actor（宏動作，LOCAL_DRL_V2_DESIGN.md §10）：DeepSets 編碼全部活躍 UE（mean／max 池化，
    排列不變）＋節點特徵＋最差兩個候選的特徵＋starved 旗標＋候選數 → N_MACRO 個 logits；無效宏動作在 forward 設 −∞。
    初始化由行為複製（bc_pretrain）決定，不再用 PF 初始化。
    """

    def __init__(self) -> None:
        super().__init__()
        self.phi = nn.Sequential(
            nn.Linear(UE_FEAT_DIM + NODE_CTX_DIM, CRITIC_ENC_DIM), nn.ReLU(),
            nn.Linear(CRITIC_ENC_DIM, CRITIC_ENC_DIM), nn.ReLU(),
        )
        self.head = nn.Sequential(
            nn.Linear(MACRO_IN_DIM, ACTOR_HIDDEN_DIM), nn.ReLU(),
            nn.Linear(ACTOR_HIDDEN_DIM, ACTOR_HIDDEN_DIM), nn.ReLU(),
            nn.Linear(ACTOR_HIDDEN_DIM, N_MACRO),
        )

    def forward(self, state: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
        """回傳 (batch, N_MACRO) logits，無效宏動作為 −1e9。"""
        b = state.shape[0]
        ue = state[:, :MAX_UE_COUNT * UE_FEAT_DIM].reshape(b, MAX_UE_COUNT, UE_FEAT_DIM)
        ctx = state[:, MAX_UE_COUNT * UE_FEAT_DIM:]
        h = self.phi(torch.cat([ue, ctx.unsqueeze(1).expand(b, MAX_UE_COUNT, NODE_CTX_DIM)], dim=-1))
        m = mask.unsqueeze(-1)
        mean_h = (h * m).sum(dim=1) / mask.sum(dim=1, keepdim=True).clamp(min=1)
        max_h = h.masked_fill(~m, 0.0).max(dim=1).values
        mc = macro_context(state, mask)
        x = torch.cat([mean_h, max_h, ctx, mc["cand_feat"], mc["starved"].float().unsqueeze(1),
                       mc["n_cand"].float().unsqueeze(1) / 2.0], dim=1)
        return self.head(x).masked_fill(~mc["valid"], -1e9)


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
        elif ACTION_SPACE == "dqn":
            qn = {"set": QNetworkSet, "add": QNetworkAdd}.get(DQN_NET, QNetwork)
            self.actor = qn().to(self.device)    # 線上 Q 網路
            self.critic = qn().to(self.device)   # 目標 Q 網路（Polyak 更新；FL 照常平均）
            self.critic.load_state_dict(self.actor.state_dict())
        else:
            self.actor = (ActorNetworkMacro() if ACTION_SPACE == "macro" else
                          ActorNetworkFactored() if ACTION_SPACE == "factored" else ActorNetworkMLP()).to(self.device)
            self.critic = CriticNetworkMLP().to(self.device)
        # 最近一次推論選的宏動作（inference_server.py 存進經驗的 macro_action）；per_ue 動作空間時恆為 None
        self.last_macro: Optional[int] = None
        # v3.1 兩段式動作：最近一次推論的 {"tier": 強度檔位, "apply": 每子節點 0/1（非活躍 -1）}（存進經驗的 factored_action）
        self.last_factored: Optional[dict] = None
        self.actor_opt = optim.Adam(self.actor.parameters(), lr=DQN_LR if ACTION_SPACE == "dqn" else LR_ACTOR)
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
        parent_trend: float = 0.0,
        children_demand: float = 0.0,
        prev_masked: Optional[set] = None,
    ) -> tuple[np.ndarray, np.ndarray]:
        """
        將 UE 列表編碼為固定長度的 numpy 向量。prev_masked：上一步被遮罩（slot_mask≠0xFFFF）的 RNTI 集合（v2.1；
        讓無記憶的 Actor 看得到「目前誰正被遮」，被遮 UE 的佇列與 MCS 會因遮罩改變，沒有這一維會來回震盪）。

        Args:
            ues           : UE 狀態列表
            fairness_bias : Global xApp 廣播的全域公平性偏差，原始值域
                            [FAIRNESS_BIAS_MIN, FAIRNESS_BIAS_MAX]，尚未收到
                            廣播或無資料時傳中性值 1.0（預設值）。
            bh_ratio      : Backhaul-aware 可用 PRB 比例 ∈ [0,1]（E2 回報），預設 1.0。
            parent_trend  : p̂ ∈ [0,1]，parent DU 的 RLC 佇列總和（log 正規化；上游壅塞程度，0＝無 parent/過期/無佇列），
                            由 inference_server.py 的 relational 背景執行緒算好傳入。2026-10-01 前是 parent bh_ratio 的趨勢，
                            backhaul 預算停用後 bh_ratio 恆為 1、該特徵恆為 0.5 不帶資訊，故改義（參數名保留）。
            children_demand: ĉ ∈ [0,1]，children 節點的 RLC 佇列加總（log 正規化；葉節點/過期為 0）。

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
            k = i * UE_FEAT_DIM
            # Log 正規化 delta TBS → [0, 1]
            state_vec[k]     = np.log1p(bsr) / np.log1p(MAX_BSR)
            # 正規化 MCS → [0, 1]（MCS=0 合法，反映低通道品質）
            state_vec[k + 1] = mcs / 28.0
            # Log 正規化 buffer occupancy → [0, 1]（同樣用 log，數值跨數量級）
            state_vec[k + 2] = np.log1p(buf) / np.log1p(MAX_BUF_INFO)
            # 子節點類型：1＝IAB 子節點的 MT（backhaul），0＝UE（由 inference_server.py 依附著順序標記）
            state_vec[k + 3] = 1.0 if ue.get("is_iab_child") else 0.0
            state_vec[k + 4] = 1.0 if prev_masked and int(ue.get("rnti", -1)) in prev_masked else 0.0
            mask_vec[i] = True

        # 活躍 UE 比例作為全域 context 特徵
        c0 = MAX_UE_COUNT * UE_FEAT_DIM
        state_vec[c0] = n / MAX_UE_COUNT
        # Global xApp 廣播的全域公平性偏差，線性映射 [BIAS_MIN,BIAS_MAX] → [0,1]
        clipped_bias = np.clip(fairness_bias, FAIRNESS_BIAS_MIN, FAIRNESS_BIAS_MAX)
        state_vec[c0 + 1] = float(
            (clipped_bias - FAIRNESS_BIAS_MIN) / (FAIRNESS_BIAS_MAX - FAIRNESS_BIAS_MIN)
        )

        state_vec[c0 + 2] = float(np.clip(bh_ratio, 0.0, 1.0))
        state_vec[c0 + 3] = float(np.clip(parent_trend, 0.0, 1.0))
        state_vec[c0 + 4] = float(np.clip(children_demand, 0.0, 1.0))

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
        parent_trend: float = 0.0,
        children_demand: float = 0.0,
        prev_masked: Optional[set] = None,
    ) -> tuple[list[dict], np.ndarray, Optional[float]]:
        """
        執行 DRL Actor 推論，回傳 (PRB 分配結果, 動作向量, 行為策略 log π(a|s))。

        Args:
            ues           : [{"rnti": int, "bsr": int, "wb_cqi": int}, ...]
            fairness_bias : Global xApp 廣播的全域公平性偏差，見 encode_state()

        Returns:
            allocations   : [{"rnti": int, "prb_abs": int}, ...]
            action        : (MAX_UE_COUNT,) float32，用於 MongoDB 儲存。MLP（v2）= 每 UE 的檔位 index
                            （非活躍 UE 為 -1，存成 `action_tiers`）；GRU（舊版）= Dirichlet 份額（`action_ratios`）
            behavior_logp : 這次採樣動作在當時策略下的 log π(a|s)（存進經驗供 PPO 比例裁剪；n<2 或確定性輸出時為 None）
        """
        if not ues:
            return [], np.zeros(MAX_UE_COUNT, dtype=np.float32), None

        n = min(len(ues), MAX_UE_COUNT)
        state_vec, mask_vec = self.encode_state(ues, fairness_bias, bh_ratio, parent_trend, children_demand, prev_masked)
        state_t, mask_t = self._to_tensors(state_vec, mask_vec)

        if self.arch == "mlp" and ACTION_SPACE == "macro":
            return self._infer_macro(ues, n, state_t, mask_t)
        if self.arch == "mlp" and ACTION_SPACE == "factored":
            return self._infer_factored(ues, n, state_t, mask_t)
        if self.arch == "mlp" and ACTION_SPACE == "dqn":
            return self._infer_dqn(ues, n, state_t, mask_t)
        if self.arch == "mlp":
            return self._infer_mlp_v2(ues, n, state_t, mask_t)

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

    def _infer_mlp_v2(
        self, ues: list[dict], n: int, state_t: torch.Tensor, mask_t: torch.Tensor,
    ) -> tuple[list[dict], np.ndarray, Optional[float]]:
        """
        Local DRL v2 離散動作推論（LOCAL_DRL_V2_DESIGN.md §2）：每個活躍 UE 各自從 N_TIERS 檔選一檔，
        上限 = clip(檔位 × total_prb, 5, total_prb)。

        - 只有 1 個活躍 UE：直接 PF 檔。單一 UE 設上限只會降低自己的吞吐量、不可能有好處，
          也沒有分配決策可學（訓練端 _contended_mask 本來就要求 ≥2 個 UE）。
        - DRL_DETERMINISTIC=1（凍結評估）：每 UE 取 argmax，不採樣，behavior_logp=None。
        - 訓練（預設）：每 UE 獨立 Categorical 採樣，behavior_logp = Σ_i log π_i(a_i|s)。
        """
        tiers = np.full(MAX_UE_COUNT, -1, dtype=np.float32)
        behavior_logp: Optional[float] = None

        if n < 2:
            tiers[:n] = PF_TIER
        else:
            self.actor.eval()
            with torch.no_grad():
                logits = self.actor(state_t, mask_t)[0, :n]          # (n, N_TIERS)
                if DETERMINISTIC:
                    chosen = logits.argmax(dim=-1)
                else:
                    dist = torch.distributions.Categorical(logits=logits)
                    chosen = dist.sample()
                    behavior_logp = float(dist.log_prob(chosen).sum().item())
            tiers[:n] = chosen.cpu().numpy().astype(np.float32)

        return self._tiers_to_allocations(ues, n, tiers), tiers, behavior_logp

    def _infer_factored(
        self, ues: list[dict], n: int, state_t: torch.Tensor, mask_t: torch.Tensor,
    ) -> tuple[list[dict], np.ndarray, Optional[float]]:
        """v3.1 兩段式推論：抽節點強度；若非全開，再對每個活躍子節點抽「是否套用」。每子節點的實際檔位＝套用 ? 強度 : 全開。
        只有 1 個活躍子節點時直接全開、不記 logp。DRL_DETERMINISTIC=1 時取 argmax／套用機率 >0.5。"""
        tiers = np.full(MAX_UE_COUNT, -1, dtype=np.float32)
        apply = [-1] * MAX_UE_COUNT
        behavior_logp: Optional[float] = None
        if n < 2:
            tiers[:n] = PF_TIER
            tier = PF_TIER
            for i in range(n):
                apply[i] = 0
        else:
            self.actor.eval()
            with torch.no_grad():
                tl_all, al_all = self.actor(state_t, mask_t)
                tl, al = tl_all[0], al_all[0, :n]
                if FACTORED_MODE == "apply_first":
                    tl_np = tl.masked_fill(_PF_ONEHOT[0], float("-inf"))   # 強度只在非全開的檔之間選
                    if DETERMINISTIC:
                        ap = (torch.sigmoid(al) > 0.5).long(); tier = int(tl_np.argmax().item())
                    else:
                        ap = torch.bernoulli(torch.sigmoid(al)).long()
                        tier = int(torch.distributions.Categorical(logits=tl_np).sample().item())
                    if int(ap.sum().item()) == 0:
                        tier = PF_TIER   # 沒有子節點被遮＝PF（強度不計入 logp）
                elif DETERMINISTIC:
                    tier = int(tl.argmax().item())
                    ap = (torch.sigmoid(al) > 0.5).long()
                else:
                    tier = int(torch.distributions.Categorical(logits=tl).sample().item())
                    ap = torch.bernoulli(torch.sigmoid(al)).long()
                if tier == PF_TIER:
                    ap = torch.zeros_like(ap)
                if not DETERMINISTIC:
                    full_ap = torch.zeros(MAX_UE_COUNT, dtype=torch.long); full_ap[:n] = ap
                    lp, _ = factored_logp_entropy(tl_all, al_all, mask_t, torch.tensor([tier]), full_ap.unsqueeze(0))
                    behavior_logp = float(lp.item())
            for i in range(n):
                apply[i] = int(ap[i].item())
                tiers[i] = tier if apply[i] else PF_TIER
        self.last_factored = {"tier": int(tier), "apply": apply}
        return self._tiers_to_allocations(ues, n, tiers), tiers, behavior_logp

    def dqn_epsilon(self) -> float:
        if DETERMINISTIC or os.path.exists(DQN_PAUSE_FILE):
            return 0.0
        k = self._train_steps - ACTOR_WARMUP_STEPS
        if k <= 0:
            return DQN_EPS_START
        return max(DQN_EPS_END, DQN_EPS_START - (DQN_EPS_START - DQN_EPS_END) * k / max(DQN_EPS_DECAY_STEPS, 1))

    def dqn_q_values(self, state_vec, mask_vec) -> tuple[list[tuple[list[int], int]], np.ndarray]:
        cands = dqn_candidates(state_vec, mask_vec)
        with torch.no_grad():
            q = self.actor(torch.tensor(dqn_features(state_vec, mask_vec, cands), device=self.device)).cpu().numpy()
        return cands, q

    def _infer_dqn(
        self, ues: list[dict], n: int, state_t: torch.Tensor, mask_t: torch.Tensor,
    ) -> tuple[list[dict], np.ndarray, Optional[float]]:
        """v4 DQN 推論：ε-greedy。暖身期 greedy＝不遮（PF，Q 尚未可信）；之後 greedy＝argmax Q。behavior_logp 記 0.0（只作為「DRL 決策」標記，供動作持續與 FL 計數）。"""
        tiers = np.full(MAX_UE_COUNT, -1, dtype=np.float32)
        apply = [-1] * MAX_UE_COUNT
        sv = state_t[0].cpu().numpy(); mv = mask_t[0].cpu().numpy()
        cands = dqn_candidates(sv, mv)
        if len(cands) == 1:
            sub, tier = [], PF_TIER
        elif np.random.rand() < self.dqn_epsilon():
            # 探索（2026-10-05）：DQN_EXPLORE_PF＞0 時以該機率探索「不遮」，其餘在遮罩動作間均勻抽——均勻抽 16 個候選時 PF 只有 1/16，
            # 收探索資料時 PF 對照樣本太少；0＝舊行為（全部候選均勻）。
            if DQN_EXPLORE_PF > 0:
                sub, tier = cands[0] if np.random.rand() < DQN_EXPLORE_PF else cands[1 + np.random.randint(len(cands) - 1)]
            else:
                sub, tier = cands[np.random.randint(len(cands))]
        elif self._train_steps < ACTOR_WARMUP_STEPS:
            sub, tier = cands[0]
        else:
            self.actor.eval()
            _, q = self.dqn_q_values(sv, mv)
            b = int(q.argmax())
            if b != 0 and (q[b] - q[0]) * DQN_Q_SCALE <= DQN_PF_MARGIN:
                b = 0   # 相對 PF 的預估增益不夠大 → 維持 PF（cands[0]＝不遮）
            sub, tier = cands[b]
        for i in range(n):
            apply[i] = 1 if i in sub else 0
            tiers[i] = tier if i in sub else PF_TIER
        self.last_factored = {"tier": int(tier if sub else PF_TIER), "apply": apply}
        return self._tiers_to_allocations(ues, n, tiers), tiers, 0.0

    def _infer_macro(
        self, ues: list[dict], n: int, state_t: torch.Tensor, mask_t: torch.Tensor,
    ) -> tuple[list[dict], np.ndarray, Optional[float]]:
        """
        v2.1 宏動作推論：Categorical(N_MACRO) 採樣（DRL_DETERMINISTIC=1 取 argmax），宏動作經 macro_to_tiers() 轉成每 UE 檔位。
        只有不介入一個有效動作時（無候選）直接選 0、behavior_logp=None（該樣本不在決策點，不更新 Actor）。
        """
        tiers = np.full(MAX_UE_COUNT, -1, dtype=np.float32)
        behavior_logp: Optional[float] = None
        self.actor.eval()
        with torch.no_grad():
            mc = macro_context(state_t, mask_t)
            if not bool(mc["decision"][0]):
                macro = 0   # 非決策點一律不介入（Actor 只在決策點訓練，非決策點的輸出沒有梯度約束，會漂移）
            else:
                logits = self.actor(state_t, mask_t)[0]
                if DETERMINISTIC:
                    macro = int(logits.argmax().item())
                else:
                    dist = torch.distributions.Categorical(logits=logits)
                    a = dist.sample()
                    macro = int(a.item())
                    behavior_logp = float(dist.log_prob(a).item())
            cand = [int(x) for x in mc["cand"][0].tolist()]
        self.last_macro = macro
        tiers[:n] = macro_to_tiers(macro, cand, n)
        return self._tiers_to_allocations(ues, n, tiers), tiers, behavior_logp

    def _tiers_to_allocations(self, ues: list[dict], n: int, tiers: np.ndarray) -> list[dict]:
        """檔位 → 回傳給 C xApp 的控制：mask 模式＝不設 PRB 上限＋slot_mask；prb 模式＝舊版 PRB 上限（遮罩全開）。"""
        idx = tiers[:n].astype(np.int64)
        if ACTION_DOMAIN == "mask":
            return [{"rnti": int(ues[i]["rnti"]), "prb_abs": int(self.total_prb), "slot_mask": int(MASK_TIERS[idx[i]])}
                    for i in range(n)]
        cap_frac = np.array(CAP_TIERS, dtype=np.float32)[idx]
        prb_ints = np.clip(np.rint(cap_frac * self.total_prb), 5, self.total_prb).astype(np.int32)
        return [{"rnti": int(ues[i]["rnti"]), "prb_abs": int(prb_ints[i]), "slot_mask": 0xFFFF} for i in range(n)]

    def pf_action(self, ues: list[dict]) -> tuple[list[dict], np.ndarray]:
        """PF 等價動作（全部 UE 不設上限），供 DRL 推論失敗時的 fallback 用，動作格式與 _infer_mlp_v2 一致。"""
        n = min(len(ues), MAX_UE_COUNT)
        tiers = np.full(MAX_UE_COUNT, -1, dtype=np.float32)
        tiers[:n] = PF_TIER
        self.last_macro = 0
        self.last_factored = {"tier": PF_TIER, "apply": [0] * n + [-1] * (MAX_UE_COUNT - n)}
        return self._tiers_to_allocations(ues, n, tiers), tiers

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
        if ACTION_SPACE == "dqn":
            return self.train_on_batch_dqn(data)
        return self.train_on_batch_mlp(data)

    def evaluate_on_batch(self, data) -> dict:
        """依 self.arch 分派，資料形狀規則同 train_on_batch()。"""
        if self.arch == "gru":
            return self.evaluate_on_batch_gru(data)
        if ACTION_SPACE == "dqn":
            return self.evaluate_on_batch_dqn(data)
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
            and len(e.get("action_tiers", [])) == MAX_UE_COUNT   # Local DRL v2 的離散檔位動作
            and (ACTION_SPACE != "macro" or e.get("macro_action") is not None)
            and (ACTION_SPACE not in ("factored", "dqn") or e.get("factored_action") is not None)
        ]

    def _build_experience_tensors(
        self, batch: list[dict]
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        """把打散的獨立經驗批次組成 flat tensor，(batch, *) 無 seq_len 維度。actions = 每 UE 檔位 index（long，非活躍 -1）。"""
        states = torch.tensor(
            np.array([e["state_vec"] for e in batch], dtype=np.float32), device=self.device)
        masks = torch.tensor(
            np.array([e["mask_vec"] for e in batch], dtype=bool), device=self.device)
        if ACTION_SPACE == "macro":
            actions = torch.tensor(np.array([int(e["macro_action"]) for e in batch], dtype=np.int64), device=self.device)
        elif ACTION_SPACE in ("factored", "dqn"):
            # (batch, 1+MAX_UE_COUNT)：第 0 欄強度檔位，其後每子節點套用 0/1（非活躍記 0，logp 計算以 mask 排除）
            actions = torch.tensor(np.array([[int(e["factored_action"]["tier"])] +
                                             [max(int(x), 0) for x in e["factored_action"]["apply"]] for e in batch],
                                            dtype=np.int64), device=self.device)
        else:
            actions = torch.tensor(
                np.array([e["action_tiers"] for e in batch], dtype=np.int64), device=self.device)
        rewards = torch.tensor(
            np.array([e["reward"] for e in batch], dtype=np.float32), device=self.device)
        next_states = torch.tensor(
            np.array([e["next_state_vec"] for e in batch], dtype=np.float32), device=self.device)
        next_masks = torch.tensor(
            np.array([e["next_mask_vec"] for e in batch], dtype=bool), device=self.device)
        # v3.4 n 步回報（training_pipeline.attach_nstep_returns）：reward 已是 Σγ^i r_{k+i}、next_state 是第 m 步之後的狀態，
        # 自舉項的折扣為 γ^m（boot_discount）；沒有這個欄位的經驗＝一步 TD（γ）。供緊接著的 _advantages_mlp() 使用。
        self._batch_discounts = torch.tensor(
            np.array([float(e.get("boot_discount", GAMMA_MLP)) for e in batch], dtype=np.float32), device=self.device)
        return states, masks, actions, rewards, next_states, next_masks

    @staticmethod
    def _max_queue_bytes(states: torch.Tensor, masks: torch.Tensor) -> torch.Tensor:
        """
        由 state 向量還原每個樣本「活躍 UE 中最大的 RLC 佇列位元組數」，形狀 (batch,)。
        state 的第 UE_FEAT_DIM·i+2 維是 log1p(buf)/log1p(MAX_BUF_INFO)，反轉即得 bytes；非活躍 slot 以 mask 排除。
        """
        norm_buf = states[:, 2:MAX_UE_COUNT * UE_FEAT_DIM:UE_FEAT_DIM]   # (batch, MAX_UE_COUNT)
        buf_bytes = torch.expm1(norm_buf * math.log1p(MAX_BUF_INFO))
        buf_bytes = buf_bytes.masked_fill(~masks, 0.0)
        return buf_bytes.max(dim=1).values

    def _contended_mask(self, states: torch.Tensor, masks: torch.Tensor) -> torch.Tensor:
        """
        壅塞樣本遮罩 (batch,)：**決策當下**任一活躍 UE 的 RLC 佇列 ≥ CONTENDED_BUF_BYTES，
        且決策當下至少有 2 個活躍 UE（只有 1 個 UE 時沒有分配決策可學）。

        2026-09-30 起只看決策前的 s，不再看結果狀態 s'：s' 是動作造成的結果（對某 UE 設上限會讓它的
        佇列變長、s' 更容易過門檻），用 s' 篩選等於「哪些樣本拿來學」取決於「採取了什麼動作」，
        policy gradient 會有選擇偏差（方向是把 Actor 拉回 PF）。篩選只能用決策前的資訊。
        代價是壅塞相位剛開始、s 還沒堆起佇列的那幾步不會被計入。
        """
        return (self._max_queue_bytes(states, masks) >= CONTENDED_BUF_BYTES) & (masks.sum(dim=1) >= 2)

    def _actor_sample_mask(self, states: torch.Tensor, masks: torch.Tensor) -> torch.Tensor:
        """
        可更新 Actor 的樣本（決策當下的 s 判定，理由同 _contended_mask）：
          - macro（v2.1）：決策點＝好通道 UE 積壓（starved）且至少一個候選、即真的有「遮或不遮」可選；
          - per_ue（v2）：原本的壅塞判定。
        """
        if ACTION_SPACE == "macro":
            return macro_context(states, masks)["decision"]
        return self._contended_mask(states, masks)

    def _decision_actor_batch(self, experiences: list[dict]):
        """v2.1：從全部經驗中挑出「決策點且有 behavior_logp」的樣本，隨機抽最多 TRAIN_BATCH_SIZE 筆組成 Actor 批次。"""
        pool = [e for e in experiences if e.get("behavior_logp") is not None]
        if not pool:
            return (None,) * 6 + ([],)
        st = torch.tensor(np.array([e["state_vec"] for e in pool], dtype=np.float32), device=self.device)
        mk = torch.tensor(np.array([e["mask_vec"] for e in pool], dtype=bool), device=self.device)
        idx = torch.nonzero(self._actor_sample_mask(st, mk)).squeeze(1).cpu().numpy()
        if len(idx) == 0:
            return (None,) * 6 + ([],)
        pick = np.random.choice(idx, min(len(idx), TRAIN_BATCH_SIZE), replace=False)
        sub = [pool[i] for i in pick]
        return (*self._build_experience_tensors(sub), sub)

    def _policy_logp_entropy(self, states: torch.Tensor, masks: torch.Tensor,
                             actions: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        """依動作空間算 log π(a|s) 與 entropy，形狀皆 (batch,)。"""
        if ACTION_SPACE == "factored":
            tl, al = self.actor(states, masks)
            return factored_logp_entropy(tl, al, masks, actions[:, 0], actions[:, 1:])
        logits = self.actor(states, masks)
        if ACTION_SPACE == "macro":
            logp_all = F.log_softmax(logits, dim=-1)
            p = logp_all.exp()
            ent = -(p * logp_all.clamp(min=-30.0)).sum(dim=-1)
            return logp_all.gather(1, actions.unsqueeze(1)).squeeze(1), ent
        return self._categorical_log_probs_and_entropy(logits, masks, actions)

    def count_contended(self, experiences: list[dict]) -> int:
        """
        數一批經驗中「可更新 Actor 的壅塞樣本」有幾筆（同 _contended_mask 的定義）。供 FL 客戶端回報
        num-examples 用：FedAvg 應該用「對 Actor 有貢獻的樣本數」加權，沒壅塞的節點 Actor 沒被更新，
        不該用全部樣本數（緩衝區滿了之後每個節點都約 10000 筆）把有學到的節點稀釋掉。
        """
        exps = self._filter_valid_experiences(experiences)
        if not exps:
            return 0
        if ACTION_SPACE == "dqn":
            # DQN 用全部有效經驗更新 Q（2026-10-04 Codex 審查：沿用 PPO 的「壅塞且有 behavior_logp」計數與實際訓練資料不符）
            return len(exps)
        states, masks, _, _, next_states, next_masks = self._build_experience_tensors(exps)
        _, has_old = self._behavior_logp_tensors(exps)
        return int((self._actor_sample_mask(states, masks) & has_old).sum().item())

    def _advantages_mlp(
        self, rewards: torch.Tensor, current_values: torch.Tensor, next_values: torch.Tensor,
        update_std: bool,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """
        MLP 分支的 advantage：clip((target − V(s)) / running_std(r), ±ADV_CLIP)，不做每批 z-score。
        Returns: (advantages, targets)。update_std=True（訓練）時才更新獎勵滾動標準差。
        """
        disc = getattr(self, "_batch_discounts", None)
        if disc is None or disc.shape != rewards.shape:
            disc = GAMMA_MLP
        targets = rewards + disc * next_values
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

    @staticmethod
    def _categorical_log_probs_and_entropy(
        logits: torch.Tensor,    # (batch, MAX_UE_COUNT, N_TIERS)
        masks: torch.Tensor,     # (batch, MAX_UE_COUNT)
        actions: torch.Tensor,   # (batch, MAX_UE_COUNT) long，非活躍為 -1
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """
        Local DRL v2：每 UE 獨立 Categorical。log π(a|s) = Σ_活躍UE log π_i(a_i|s)（與推論端
        behavior_logp 同定義，PPO 比例才一致）；entropy = 活躍 UE 的平均 per-UE entropy。形狀皆 (batch,)。
        """
        logp_all = F.log_softmax(logits, dim=-1)
        idx = actions.clamp(min=0).unsqueeze(-1)
        logp_ue = logp_all.gather(-1, idx).squeeze(-1).masked_fill(~masks, 0.0)
        ent_ue = -(logp_all.exp() * logp_all).sum(dim=-1).masked_fill(~masks, 0.0)
        n_active = masks.sum(dim=1).clamp(min=1)
        return logp_ue.sum(dim=1), ent_ue.sum(dim=1) / n_active

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
            if REWARD_MODE not in ("throughput_only", "alpha_fair"):
                self._lambda = max(0.0, min(
                    LAMBDA_MAX,
                    self._lambda + LAMBDA_LR * (JFI_MIN - batch_jfi_mean),
                ))

        states, masks, actions, rewards, next_states, next_masks = self._build_experience_tensors(batch)

        # ── Critic 更新（γ=GAMMA_MLP，預設 0 → 回歸 V(s)≈E[r|s]；全部樣本都訓練）─────────────
        self.critic.train()
        current_values = self.critic(states, masks)
        with torch.no_grad():
            next_values = self.critic(next_states, next_masks)
        advantages, targets = self._advantages_mlp(rewards, current_values, next_values, update_std=True)

        critic_loss = F.mse_loss(current_values, targets)

        self.critic_opt.zero_grad()
        critic_loss.backward()
        torch.nn.utils.clip_grad_norm_(self.critic.parameters(), 1.0)
        self.critic_opt.step()

        # ── Actor 更新（PPO，只用決策點樣本：v2.1＝starved 且有候選；v2＝壅塞）────────────────────
        # v2.1（2026-10-02）：決策點只佔經驗約 3～7%，隨機 128 筆的批次裡常不到 MIN_CONTENDED_SAMPLES 筆、Actor 多數
        # 輪次被跳過（v2 學不起來的原因之一）。改為 Actor 批次只從「全部讀到的經驗中的決策點」抽（最多 TRAIN_BATCH_SIZE 筆）；
        # Actor 本來就只用決策點更新、PPO 比例照樣校正行為策略，不引入偏差。Critic 批次不變。
        crit_rewards, crit_n = rewards, len(batch)   # 記錄用：Critic 批次的統計
        if ACTION_SPACE == "macro":
            states, masks, actions, rewards, next_states, next_masks, batch = self._decision_actor_batch(experiences)
            if batch:
                with torch.no_grad():
                    cv = self.critic(states, masks); nv = self.critic(next_states, next_masks)
                advantages, _ = self._advantages_mlp(rewards, cv, nv, update_std=False)
        contended = self._actor_sample_mask(states, masks) if batch else torch.zeros(0, dtype=torch.bool)
        old_logp, has_old = self._behavior_logp_tensors(batch)
        actor_mask = contended & has_old     # 只有「壅塞」且「有行為策略 logπ」的樣本能更新 Actor
        n_contended = int(contended.sum().item())
        n_actor = int(actor_mask.sum().item())
        contended_frac = n_contended / max(len(batch), 1)

        actor_loss_val = 0.0
        entropy_val = 0.0
        actor_updated = False
        clip_frac = 0.0
        if n_actor >= MIN_CONTENDED_SAMPLES and self._train_steps >= ACTOR_WARMUP_STEPS:
            self.actor.train()
            log_probs_t, entropy_t = self._policy_logp_entropy(states, masks, actions)

            actor_loss, clip_frac = self._ppo_actor_loss(log_probs_t, old_logp, advantages, actor_mask)
            entropy_coeff = max(
                ENTROPY_COEFF_MIN, ENTROPY_COEFF_INIT * (ENTROPY_DECAY_RATE ** self._train_steps)
            )
            current_entropy = float(entropy_t[actor_mask].mean().detach())
            # 離散 entropy ≥ 0（上限 ln N_TIERS≈1.61）；低於 ENTROPY_FLOOR 視為策略快要塌成確定性，強制拉高係數
            if current_entropy < ENTROPY_FLOOR:
                entropy_coeff = max(entropy_coeff, 0.05)

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
            "mean_reward": float(crit_rewards.mean().item()),
            "mean_adv":    float(advantages.mean().item()) if advantages is not None and advantages.numel() else 0.0,
            "lambda":      self._lambda,
            "batch_jfi_mean": batch_jfi_mean,
            "n_experiences": crit_n,
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
            crit_n,
        )
        return metrics

    def _dqn_td(self, batch: list[dict]) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """回傳 (Q(s,a)／scale, 目標／scale, rewards)。目標＝n 步回報＋γ^m·Q_target(s', argmax_a' Q_online(s',a'))（Double DQN）。"""
        states, masks, actions, rewards, _, _ = self._build_experience_tensors(batch)
        disc = self._batch_discounts
        x = np.zeros((len(batch), Q_IN_DIM), np.float32)
        for i, e in enumerate(batch):
            fa = e["factored_action"]; tier = int(fa["tier"]); ap = fa["apply"]
            sub = [j for j in range(min(len(ap), MAX_UE_COUNT)) if ap[j] == 1] if tier != PF_TIER else []
            x[i] = dqn_features(e["state_vec"], e["mask_vec"], [(sub, tier if sub else PF_TIER)])[0]
        q_sa = self.actor(torch.tensor(x, device=self.device))
        rows, owner = [], []
        for i, e in enumerate(batch):
            c = dqn_candidates(e["next_state_vec"], e["next_mask_vec"])
            rows.append(dqn_features(e["next_state_vec"], e["next_mask_vec"], c)); owner += [i] * len(c)
        xn = torch.tensor(np.concatenate(rows), device=self.device); owner_t = torch.tensor(owner, device=self.device)
        with torch.no_grad():
            q_on = self.actor(xn); q_tg = self.critic(xn)
            best = torch.full((len(batch),), -1e9, device=self.device).scatter_reduce(0, owner_t, q_on, reduce="amax")
            is_best = q_on >= best[owner_t] - 1e-9
            nxt = torch.zeros(len(batch), device=self.device).scatter_reduce(0, owner_t, torch.where(is_best, q_tg, torch.full_like(q_tg, -1e9)), reduce="amax", include_self=False)
            target = rewards / DQN_Q_SCALE + disc * nxt
        return q_sa, target, rewards

    def train_on_batch_dqn(self, experiences: list[dict]) -> dict:
        experiences = self._filter_valid_experiences(experiences)
        if len(experiences) < TRAIN_BATCH_SIZE:
            self._log.info("經驗數量不足 (有 %d 筆，需 %d 筆)，跳過訓練", len(experiences), TRAIN_BATCH_SIZE)
            return {}
        batch = [experiences[i] for i in np.random.choice(len(experiences), TRAIN_BATCH_SIZE, replace=False)]
        self.actor.train()
        q_sa, target, rewards = self._dqn_td(batch)
        loss = F.smooth_l1_loss(q_sa, target)
        self.actor_opt.zero_grad(); loss.backward()
        torch.nn.utils.clip_grad_norm_(self.actor.parameters(), 1.0); self.actor_opt.step()
        with torch.no_grad():
            for pt, po in zip(self.critic.parameters(), self.actor.parameters()):
                pt.mul_(1.0 - DQN_TAU).add_(po, alpha=DQN_TAU)
        self._train_steps += 1
        self._is_trained = True
        bs = float(rewards.std().item()) if rewards.numel() > 1 else 0.0
        self._reward_std = bs if self._reward_std is None else (1.0 - REWARD_STD_EMA) * self._reward_std + REWARD_STD_EMA * bs
        metrics = {"train_step": self._train_steps, "actor_loss": 0.0, "critic_loss": float(loss.item()), "entropy": 0.0,
                   "mean_reward": float(rewards.mean().item()), "mean_adv": 0.0, "lambda": self._lambda,
                   "batch_jfi_mean": None, "n_experiences": len(batch), "contended_frac": 0.0, "n_actor_samples": len(batch),
                   "ppo_clip_frac": 0.0, "actor_updated": True, "reward_std": float(self._reward_std or 0.0)}
        if self._train_steps % 15 == 0:
            self._log.info("[訓練][MLP] step=%d DQN td_loss=%.5f mean_reward=%.3f eps=%.3f mean_Q=%.3f n=%d",
                           self._train_steps, metrics["critic_loss"], metrics["mean_reward"], self.dqn_epsilon(),
                           float(q_sa.mean().item()) * DQN_Q_SCALE, len(batch))
        return metrics

    def evaluate_on_batch_dqn(self, experiences: list[dict]) -> dict:
        experiences = self._filter_valid_experiences(experiences)
        if len(experiences) < TRAIN_BATCH_SIZE:
            return {}
        batch = [experiences[i] for i in np.random.choice(len(experiences), TRAIN_BATCH_SIZE, replace=False)]
        self.actor.eval()
        with torch.no_grad():
            q_sa, target, _ = self._dqn_td(batch)
            loss = F.smooth_l1_loss(q_sa, target)
        return {"test_actor_loss": 0.0, "test_critic_loss": float(loss.item())}

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
            current_values = self.critic(states, masks)
            next_values = self.critic(next_states, next_masks)
            advantages, targets = self._advantages_mlp(rewards, current_values, next_values, update_std=False)

            test_critic_loss = F.mse_loss(current_values, targets)

            log_probs_t, entropy_t = self._policy_logp_entropy(states, masks, actions)
            contended = self._actor_sample_mask(states, masks)
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
            if REWARD_MODE not in ("throughput_only", "alpha_fair"):
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
        """
        是否可以用 DRL 推論（否則 InferenceServer 退回 BSR 啟發式）。

        MLP（Local DRL v2）：恆為 True。Actor 一初始化就≈PF（見 ActorNetworkMLP 的 bias 初始化），
        從第一步起就是合法策略、且每筆經驗都帶 behavior_logp；舊版「先跑 BSR 啟發式收集資料」的暖身
        階段反而會混進會設上限、非 PF 的啟發式動作，已不需要。
        GRU（舊版）：維持原規則，訓練步數 >= MIN_DRL_TRAIN_STEPS 才切換。
        """
        if self.arch == "mlp":
            return True
        return self._is_trained and self._train_steps >= MIN_DRL_TRAIN_STEPS

    def pretrain_critic(
        self, states: np.ndarray, masks: np.ndarray, rewards: np.ndarray, epochs: int = 50,
        batch_size: int = 256,
        holdout_frac: float = 0.1,
    ) -> dict:
        """
        用 PF-shadow 資料離線預訓練 Critic（LOCAL_DRL_V2_DESIGN.md §1.5／§3）：V(s) ≈ PF 策略下的 E[r|s]。
        線上訓練開始時 Actor≈PF，所以預訓練好的 V(s) 一開始就是正確的 baseline，advantage = r − V(s)
        天然代表「比 PF 好多少」，不需要另外的反事實 reward 模型。同時用資料的 reward 標準差初始化
        `_reward_std`（advantage 的固定尺度），避免線上前幾輪用極少樣本估尺度。

        Args:
            states  : (N, STATE_DIM) float32
            masks   : (N, MAX_UE_COUNT) bool，活躍 UE
            rewards : (N,) float32，與線上 reward 同定義（throughput_only 的 r_throughput）
        Returns: {"n_train", "n_holdout", "train_mse", "holdout_mse", "holdout_baseline_mse"}
        """
        n = len(rewards)
        if n < batch_size:
            raise ValueError(f"PF-shadow 資料只有 {n} 筆，少於一個 batch（{batch_size}）")
        perm = np.random.permutation(n)
        n_hold = max(1, int(n * holdout_frac))
        hold_idx, train_idx = perm[:n_hold], perm[n_hold:]
        s = torch.tensor(states, dtype=torch.float32, device=self.device)
        mk = torch.tensor(masks, dtype=torch.bool, device=self.device)
        r = torch.tensor(rewards, dtype=torch.float32, device=self.device)

        self.critic.train()
        for _ in range(epochs):
            order = np.random.permutation(train_idx)
            for k in range(0, len(order), batch_size):
                b = torch.tensor(order[k:k + batch_size], device=self.device)
                loss = F.mse_loss(self.critic(s[b], mk[b]), r[b])
                self.critic_opt.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(self.critic.parameters(), 1.0)
                self.critic_opt.step()

        self.critic.eval()
        with torch.no_grad():
            tr = torch.tensor(train_idx, device=self.device)
            ho = torch.tensor(hold_idx, device=self.device)
            train_mse = float(F.mse_loss(self.critic(s[tr], mk[tr]), r[tr]).item())
            holdout_mse = float(F.mse_loss(self.critic(s[ho], mk[ho]), r[ho]).item())
            # 對照組：永遠猜訓練集平均 reward 的 MSE（Critic 應明顯低於它，否則沒學到 state 相關性）
            baseline_mse = float(((r[ho] - r[tr].mean()) ** 2).mean().item())
        self._reward_std = float(r[tr].std().item())
        return {
            "n_train": len(train_idx), "n_holdout": n_hold,
            "train_mse": train_mse, "holdout_mse": holdout_mse, "holdout_baseline_mse": baseline_mse,
        }

    def bc_pretrain_actor(
        self, states: np.ndarray, masks: np.ndarray, labels: np.ndarray, epochs: int = 80,
        batch_size: int = 256, holdout_frac: float = 0.2,
    ) -> dict:
        """
        v2.1：以動態規則的決策做行為複製（LOCAL_DRL_V2_DESIGN.md §10.2c）。labels＝宏動作 (N,)；交叉熵＋標籤平滑
        （BC_LABEL_SMOOTH，只分給有效動作），保留一點探索、避免 PPO 比例在 logπ≈0 時數值極端。
        holdout 用時間切分（呼叫端傳入時間排序的資料，最後 holdout_frac 不訓練），回報整體與決策點上的一致率。
        """
        n = len(labels)
        cut = int(n * (1.0 - holdout_frac))
        s = torch.tensor(states, dtype=torch.float32, device=self.device)
        mk = torch.tensor(masks, dtype=torch.bool, device=self.device)
        y = torch.tensor(labels, dtype=torch.long, device=self.device)
        valid = macro_context(s, mk)["valid"]
        y = torch.where(valid[torch.arange(n), y], y, torch.zeros_like(y))   # 規則的動作在新的有效集合外 → 視為不介入
        tgt = torch.zeros(n, N_MACRO, device=self.device)
        nv = valid.sum(dim=1, keepdim=True).float()
        tgt += valid.float() * (BC_LABEL_SMOOTH / nv.clamp(min=1))
        tgt[torch.arange(n), y] += 1.0 - BC_LABEL_SMOOTH
        tgt = tgt / tgt.sum(dim=1, keepdim=True)
        # 決策點樣本（規則真正在選「遮或不遮」）權重加大，避免被大量「無候選／不積壓→不介入」的樣本淹沒
        dec = self._actor_sample_mask(s, mk)
        w = torch.where(dec, torch.tensor(5.0, device=self.device), torch.tensor(1.0, device=self.device))
        self.actor.train()
        for _ in range(epochs):
            order = np.random.permutation(cut)
            for k in range(0, cut, batch_size):
                b = torch.tensor(order[k:k + batch_size], device=self.device)
                logp = F.log_softmax(self.actor(s[b], mk[b]), dim=-1)
                loss = (-(tgt[b] * logp.clamp(min=-30.0)).sum(dim=1) * w[b]).sum() / w[b].sum()
                self.actor_opt.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(self.actor.parameters(), 1.0)
                self.actor_opt.step()
        self.actor.eval()
        with torch.no_grad():
            pred = self.actor(s, mk).argmax(dim=1)
        def acc(sel):
            return float((pred[sel] == y[sel]).float().mean().item()) if int(sel.sum().item()) > 0 else float("nan")
        idx = torch.arange(n, device=self.device)
        tr, ho = idx < cut, idx >= cut
        return {"n_train": cut, "n_holdout": n - cut,
                "acc_train": acc(tr), "acc_holdout": acc(ho),
                "acc_holdout_decision": acc(ho & dec), "n_holdout_decision": int((ho & dec).sum().item()),
                "label_dist": np.bincount(labels, minlength=N_MACRO).tolist()}

    # -------------------------------------------------------------------------
    # 模型持久化
    # -------------------------------------------------------------------------

    def _apply_current_lr(self) -> None:
        """把 optimizer 的學習率設回現行環境設定（載入 checkpoint 後呼叫）。"""
        a_lr = DQN_LR if ACTION_SPACE == "dqn" else LR_ACTOR
        for g in self.actor_opt.param_groups:
            g["lr"] = a_lr
        for g in self.critic_opt.param_groups:
            g["lr"] = LR_CRITIC

    def export_state(self) -> dict:
        """權重、優化器狀態與訓練純量的深複本（2026-10-03：影子模型訓練用，見 inference_server._run_training_round）。"""
        import copy
        return copy.deepcopy({
            "actor": self.actor.state_dict(), "critic": self.critic.state_dict(),
            "actor_opt": self.actor_opt.state_dict(), "critic_opt": self.critic_opt.state_dict(),
            "train_steps": self._train_steps, "is_trained": self._is_trained,
            "lambda": self._lambda, "reward_std": self._reward_std,
        })

    def import_state(self, st: dict) -> None:
        """載入 export_state() 的內容（就地複製進現有模組，推論端拿到的物件不變）。"""
        self.actor.load_state_dict(st["actor"])
        self.critic.load_state_dict(st["critic"])
        self.actor_opt.load_state_dict(st["actor_opt"])
        self.critic_opt.load_state_dict(st["critic_opt"])
        self._train_steps = st["train_steps"]
        self._is_trained = st["is_trained"]
        self._lambda = st["lambda"]
        self._reward_std = st["reward_std"]

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
            self._apply_current_lr()   # optimizer state 會連舊學習率一起還原（2026-10-04 Codex 審查發現），改回現行設定
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
