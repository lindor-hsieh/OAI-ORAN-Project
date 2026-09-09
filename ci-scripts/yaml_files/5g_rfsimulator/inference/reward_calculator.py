"""
reward_calculator.py — PRB DRL 獎勵函數

**現行公式（2026-07-09 起）：Lagrangian 限制式（Fairness-as-Constraint）**

  R = R_tp + λ · (JFI_raw - JFI_MIN)

throughput 是唯一要最大化的目標，fairness 變成一個「底線」——只要求 JFI 不能
低於 JFI_MIN，不追求 JFI 越高越好。λ 不是手動設的常數，由 DRLAgent 在訓練迴圈
用簡單規則自動調整（JFI 低於門檻時 λ 變大加重懲罰，達標時 λ 趨近 0 全力衝
throughput），解決「固定權重/固定退火時程表對不同場景需要的 fairness 強度不同」
這個問題（見 DRL_DESIGN.md §5 的完整推導）。JFI_MIN=0.8291，是 2026-07-09
現場量測（PF baseline、Scenario R 真實隨機流量條件下，82 個有效區間平均）
得出的數字，不是憑感覺選的。

本模組提供：
  - `compute_reward_breakdown()`：計算 R_tp／JFI_raw／R_fair（縮放版，監控用）／
    R_delay 四個分量，不做加權組合，是給上層公式呼叫的基礎元件
  - `compute_lagrangian_reward()`：現行公式，呼叫 compute_reward_breakdown() 並
    套用 Lagrangian 組合，`inference_server.py` 呼叫的是這個函式
  - `compute_reward()`／舊版加權和公式（`W_THROUGHPUT`/`W_FAIRNESS`/`W_DELAY`）：
    **保留但不再是現行路徑**，作為之後消融實驗的參考基準（沿用「保留 Scenario D
    程式碼」的同一原則），歷史沿革：
      - 複合 reward（0.5/0.4/0.1）：見 git 歷史與 CLAUDE.md §7，W_FAIRNESS 提高到
        0.4 是為了防止 2-UE policy monopoly collapse
      - 純 throughput ablation（1.0/0.0/0.0，2026-07-06 起）：刻意拿掉 fairness
        校正，觀察 policy 退化成 max-C/I 排程的現象，驗證了複合 reward 設計的
        必要性——但固定權重（不管是哪一組）都無法同時適應 Scenario A/B/C 不同的
        fairness 需求強度，這正是改用 Lagrangian 限制式的動機

三項基礎分量：
  R_throughput : 各 UE 實際 DL 吞吐量平均值（delta_dl_aggr_tbs 正規化）∈ [0, 1]
  JFI_raw      : Jain's Fairness Index 原始值，∈ [1/n, 1]，Lagrangian 公式用這個
  R_fairness   : JFI_raw 線性縮放至 [0, 1]，只給舊版加權和公式與監控用
  R_delay      : PRB 效率懲罰（PRB 浪費程度），∈ [0, 1]
                 定義：1 - (delta_tbs / MAX_BSR) / (prb_ratio + ε)
                 當 UE 佔用大量 PRB 但產出低 delta_tbs 時（如通道差或排程過度分配），
                 懲罰值升高，間接代表其他 UE 因等待 PRB 而產生的排隊延遲。
                 注：dl_buffer_info 現在已經接進 state（見 drl_agent.py），但
                 R_delay 這個 reward 分量目前仍用 PRB 效率當代理指標，尚未改用
                 dl_buffer_info 直接量測佇列深度（見 DRL_DESIGN.md §5.2 待評估項）。

輸入欄位語意（C 端已重新映射）：
  bsr    → delta_dl_aggr_tbs (bytes/10ms callback)，實際 DL 傳輸量
  wb_cqi → dl_mcs1 (0-28)，通道品質指標（OAI RF sim 原始 wb_cqi 恆為 0）
"""

from __future__ import annotations

import numpy as np

# =============================================================================
# 超參數
# =============================================================================

# ── Lagrangian 限制式（現行公式）─────────────────────────────────────────────
# JFI_MIN：2026-07-09 現場量測，PF baseline、Scenario R 真實隨機流量條件下
# （停 xApp、保留 Scenario R 產生流量，每 10 秒採樣 6 個 UE 吞吐量估算 JFI，
# 只算「當下有實際流量」的 UE 避免刻意閒置樣本稀釋），82 個有效區間平均值。
# 分布：min=0.6345 median=0.8597 max=0.9533，取平均值 0.8291 當門檻。
JFI_MIN: float = 0.8291

# ── 舊版加權和公式常數（保留供消融實驗參考，非現行路徑）───────────────────────
# 歷史值 0.5/0.4/0.1 見 git 歷史與 CLAUDE.md §7；目前這組是純 throughput
# ablation（2026-07-06 起）用的設定，已不再是 compute_lagrangian_reward()
# 使用的路徑，只有呼叫舊版 compute_reward() 時才會用到。
W_THROUGHPUT: float = 1.0
W_FAIRNESS:   float = 0.0
W_DELAY:      float = 0.0

# delta_tbs 正規化參考值 (bytes/100ms)
# C xApp rate limiter 每 10 個 MAC callback 才送一次 ZMQ，測量窗口為 100ms。
# 實測 106 PRB 高負載下 delta_tbs 可達 1~2.5M bytes/100ms（80~200 Mbps），
# 設為 2,000,000 確保 r_throughput 在實際範圍內有完整梯度，不被截斷。
MAX_BSR: float = 2_000_000.0


# =============================================================================
# 公開介面
# =============================================================================

def compute_reward(
    ues: list[dict],
    allocations: list[dict],
    total_prb: int = 106,
) -> float:
    """
    計算複合獎勵值，夾緊至 [-1.0, 1.0]。

    Args:
        ues         : 當前 UE 狀態，[{"rnti", "bsr": delta_tbs_bytes, "wb_cqi": mcs}, ...]
        allocations : AI 決策，[{"rnti", "prb_abs"}, ...]
        total_prb   : 系統 PRB 總數

    Returns:
        reward : float ∈ [-1.0, 1.0]
    """
    breakdown = compute_reward_breakdown(ues, allocations, total_prb)
    return breakdown["reward"]


def compute_reward_breakdown(
    ues: list[dict],
    allocations: list[dict],
    total_prb: int = 106,
) -> dict:
    """
    計算獎勵並回傳各分量明細（供監控、除錯，以及上層 compute_lagrangian_reward()
    組合公式使用）。

    Returns:
        {
            "reward":       float,   # 舊版加權和最終獎勵（compute_lagrangian_reward
                                      # 不使用這個欄位，自己組合 reward）
            "r_throughput": float,   # 吞吐量分量 ∈ [0, 1]
            "jfi_raw":      float,   # JFI 原始值 ∈ [1/n, 1]，Lagrangian 公式用這個
            "r_fairness":   float,   # JFI 線性縮放至 [0, 1]，舊公式/監控用
            "r_delay":      float,   # PRB 效率懲罰 ∈ [0, 1]（越高越差）
        }
    """
    empty = {"reward": 0.0, "r_throughput": 0.0, "jfi_raw": 0.0,
             "r_fairness": 0.0, "r_delay": 0.0}
    if not ues or not allocations:
        return empty

    alloc_map: dict[int, int] = {
        int(a["rnti"]): int(a["prb_abs"]) for a in allocations
    }

    throughputs: list[float] = []
    delay_penalties: list[float] = []

    for ue in ues:
        rnti      = int(ue.get("rnti", 0))
        delta_tbs = max(float(ue.get("bsr", 0)), 0.0)
        prb       = float(alloc_map.get(rnti, 1))

        # ── 吞吐量正規化 ────────────────────────────────────────────────────
        tp_norm = min(delta_tbs / MAX_BSR, 1.0)
        throughputs.append(tp_norm)

        # ── PRB 效率懲罰 ────────────────────────────────────────────────────
        # 每單位 PRB 產出的正規化吞吐量。
        # 若 UE 佔用大比例 PRB 但 delta_tbs 低（壞通道 / 過度分配），
        # tp_per_prb << 1 → 懲罰 = 1 - tp_per_prb → 趨近 1。
        # 若 UE 以小量 PRB 達到高吞吐（好通道 / 精準分配），
        # tp_per_prb >= 1 → 夾緊至 0（不額外獎勵，吞吐項已涵蓋）。
        prb_ratio  = prb / total_prb
        tp_per_prb = tp_norm / (prb_ratio + 1e-6)
        penalty    = float(np.clip(1.0 - tp_per_prb, 0.0, 1.0))
        delay_penalties.append(penalty)

    tp_arr  = np.array(throughputs,     dtype=np.float64)
    dp_arr  = np.array(delay_penalties, dtype=np.float64)

    r_tp    = float(tp_arr.mean())

    # 所有 UE 皆無流量（idle 狀態）→ 不懲罰，直接回傳 0
    # 原本 r_delay=1.0 會導致 reward=-0.2，污染訓練資料
    if r_tp < 1e-9:
        return {"reward": 0.0, "r_throughput": 0.0, "jfi_raw": 0.0,
                "r_fairness": 0.0, "r_delay": 0.0}

    # JFI 正規化：將自然值域 [1/n, 1.0] 線性縮放至 [0, 1]
    # JFI([0, x]) 原本 = 0.5（固定，對 DRL 無梯度）→ 正規化後 = 0.0（明確懲罰）
    # JFI([x, x]) = 1.0 → 正規化後 = 1.0（完全公平，最高獎勵）
    # 注意：這裡的 jfi_min 是「每次呼叫依活躍 UE 數算出的縮放下限 1/n」，
    # 跟模組層級的 JFI_MIN（Lagrangian 限制式門檻常數）是完全不同的兩個東西，
    # 只是剛好中英文命名接近，注意不要混淆。
    n = len(tp_arr)
    jfi_raw = _jains_fairness(tp_arr)
    jfi_min = 1.0 / n if n > 1 else 1.0
    r_fair  = float((jfi_raw - jfi_min) / (1.0 - jfi_min)) if n > 1 else jfi_raw
    r_delay = float(dp_arr.mean())

    reward = float(np.clip(
        W_THROUGHPUT * r_tp + W_FAIRNESS * r_fair - W_DELAY * r_delay,
        -1.0, 1.0,
    ))

    return {
        "reward":       reward,
        "jfi_raw":      float(jfi_raw),
        "r_throughput": r_tp,
        "r_fairness":   r_fair,
        "r_delay":      r_delay,
    }


def compute_lagrangian_reward(
    ues: list[dict],
    allocations: list[dict],
    lambda_val: float,
    total_prb: int = 106,
) -> dict:
    """
    現行 reward 公式（Lagrangian 限制式，取代舊版加權和）：

        R = R_tp + λ · (JFI_raw - JFI_MIN)

    throughput 是唯一要最大化的目標，fairness 是底線（JFI 不能低於 JFI_MIN）。
    λ 由呼叫端（DRLAgent）傳入，DRLAgent 在訓練迴圈依觀測到的 JFI 自動調整，
    這裡只是單純套用當下的 λ 值，不在這個函式內更新 λ。

    刻意不做 [-1,1] 硬裁切：train_on_batch() 已經對 advantage 做 z-score
    標準化，梯度尺度不受 reward 絕對量級影響；裁切反而會在 λ 變大時扭曲
    懲罰訊號強度，等於讓限制式失效。

    Args:
        ues         : 當前 UE 狀態
        allocations : AI 決策
        lambda_val  : 當下的 Lagrangian 乘子（DRLAgent.lambda_）
        total_prb   : 系統 PRB 總數

    Returns:
        跟 compute_reward_breakdown() 相同的 dict，但 "reward" 欄位換成
        Lagrangian 公式算出的值，並新增 "lambda_applied" 記錄這筆經驗用的 λ。
    """
    breakdown = compute_reward_breakdown(ues, allocations, total_prb)
    jfi_raw = breakdown["jfi_raw"]

    # 空閒狀態（compute_reward_breakdown 已回傳全 0）：reward 維持 0，
    # 不额外套用 Lagrangian 項，理由跟 compute_reward_breakdown 的空閒特判一致——
    # 沒有真實流量時 JFI 沒有意義，不該產生 fairness 懲罰/獎勵訊號。
    if breakdown["r_throughput"] < 1e-9:
        return {**breakdown, "lambda_applied": lambda_val}

    reward = breakdown["r_throughput"] + lambda_val * (jfi_raw - JFI_MIN)

    return {
        **breakdown,
        "reward": float(reward),
        "lambda_applied": lambda_val,
    }


# =============================================================================
# 工具函式
# =============================================================================

def _jains_fairness(x: np.ndarray) -> float:
    """
    Jain's Fairness Index：(Σxᵢ)² / (n × Σxᵢ²)

    值域 ∈ [1/n, 1.0]，1.0 為完全公平。
    當所有值為 0 時回傳 0.0。
    """
    if len(x) == 0:
        return 0.0
    total = float(x.sum())
    if total < 1e-9:
        return 0.0
    num = total ** 2
    den = len(x) * float((x ** 2).sum())
    if den < 1e-9:
        return 0.0
    return float(num / den)
