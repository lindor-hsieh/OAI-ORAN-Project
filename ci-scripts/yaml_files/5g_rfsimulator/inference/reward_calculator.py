"""
reward_calculator.py — PRB DRL 複合獎勵函數

Reward = W_THROUGHPUT × R_tp + W_FAIRNESS × R_fair - W_DELAY × R_delay

三項組成：
  R_throughput : 各 UE 實際 DL 吞吐量平均值（delta_dl_aggr_tbs 正規化）∈ [0, 1]
  R_fairness   : Jain's Fairness Index，衡量各 UE 吞吐量的公平程度 ∈ [0, 1]
  R_delay      : PRB 效率懲罰（PRB 浪費程度），∈ [0, 1]
                 定義：1 - (delta_tbs / MAX_BSR) / (prb_ratio + ε)
                 當 UE 佔用大量 PRB 但產出低 delta_tbs 時（如通道差或排程過度分配），
                 懲罰值升高，間接代表其他 UE 因等待 PRB 而產生的排隊延遲。
                 注：OAI RF sim dl_buffer_info 恆為 0，無法直接量測佇列深度。
                 本 proxy 從資源效率角度逼近延遲概念，適合論文呈現。

輸入欄位語意（C 端已重新映射）：
  bsr    → delta_dl_aggr_tbs (bytes/10ms callback)，實際 DL 傳輸量
  wb_cqi → dl_mcs1 (0-28)，通道品質指標（OAI RF sim 原始 wb_cqi 恆為 0）
"""

from __future__ import annotations

import numpy as np

# =============================================================================
# 超參數
# =============================================================================

# 獎勵權重（三項總計 1.0）
W_THROUGHPUT: float = 0.4   # 吞吐量最大化
W_FAIRNESS:   float = 0.4   # Jain's Fairness 公平性（與吞吐量並重）
W_DELAY:      float = 0.2   # PRB 效率懲罰（間接延遲代理）

# delta_tbs 正規化參考值 (bytes/100ms)
# C xApp rate limiter 每 10 個 MAC callback 才送一次 ZMQ，測量窗口為 100ms。
# 106 PRB × MCS28 ≈ 80 Mbps = 1,000,000 bytes per 100ms（峰值上限）
MAX_BSR: float = 1_000_000.0


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
    計算獎勵並回傳各分量明細（供監控與除錯使用）。

    Returns:
        {
            "reward":       float,   # 最終獎勵
            "r_throughput": float,   # 吞吐量分量 ∈ [0, 1]
            "r_fairness":   float,   # 公平性分量 ∈ [0, 1]
            "r_delay":      float,   # PRB 效率懲罰 ∈ [0, 1]（越高越差）
        }
    """
    empty = {"reward": 0.0, "r_throughput": 0.0, "r_fairness": 0.0, "r_delay": 0.0}
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
        return {"reward": 0.0, "r_throughput": 0.0, "r_fairness": 0.0, "r_delay": 0.0}

    r_fair  = _jains_fairness(tp_arr)
    r_delay = float(dp_arr.mean())

    reward = float(np.clip(
        W_THROUGHPUT * r_tp + W_FAIRNESS * r_fair - W_DELAY * r_delay,
        -1.0, 1.0,
    ))

    return {
        "reward":       reward,
        "r_throughput": r_tp,
        "r_fairness":   r_fair,
        "r_delay":      r_delay,
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
