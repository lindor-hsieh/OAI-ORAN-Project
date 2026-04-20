"""
reward_calculator.py — PRB DRL 複合獎勵函數

Reward = W_THROUGHPUT × R_tp + W_FAIRNESS × R_fair - W_DELAY × R_delay

三項組成：
  R_throughput : 估計吞吐量 (CQI 頻譜效率 × 分配 PRB 數)，正規化至 [0, 1]
  R_fairness   : Jain's Fairness Index，衡量各 UE 吞吐量的公平程度 ∈ [0, 1]
  R_delay      : 佇列壓力 (BSR / 分配 PRB)，代表等待延遲，越高越差

CQI 到頻譜效率的映射採用 3GPP TS 36.213 Table 7.2.3-1。
"""

from __future__ import annotations

import numpy as np

# =============================================================================
# CQI → 頻譜效率 (bits/s/Hz) 映射表
# 參考 3GPP TS 36.213 Table 7.2.3-1 (64QAM 最高效率約 5.55 bits/s/Hz)
# =============================================================================
CQI_TO_EFFICIENCY: list[float] = [
    0.0,    # CQI  0：無效值
    0.1523, # CQI  1：QPSK, code rate 78/1024
    0.2344, # CQI  2：QPSK, code rate 120/1024
    0.3770, # CQI  3：QPSK, code rate 193/1024
    0.6016, # CQI  4：QPSK, code rate 308/1024
    0.8770, # CQI  5：QPSK, code rate 449/1024
    1.1758, # CQI  6：QPSK, code rate 602/1024
    1.4766, # CQI  7：16QAM, code rate 378/1024
    1.9141, # CQI  8：16QAM, code rate 490/1024
    2.4063, # CQI  9：16QAM, code rate 616/1024
    2.7305, # CQI 10：64QAM, code rate 466/1024
    3.3223, # CQI 11：64QAM, code rate 567/1024
    3.9023, # CQI 12：64QAM, code rate 666/1024
    4.5234, # CQI 13：64QAM, code rate 772/1024
    5.1152, # CQI 14：64QAM, code rate 873/1024
    5.5547, # CQI 15：64QAM, code rate 948/1024
]
MAX_EFFICIENCY: float = max(CQI_TO_EFFICIENCY)

# =============================================================================
# 獎勵權重 (可調參數)
# =============================================================================
W_THROUGHPUT: float = 0.5   # 吞吐量最大化的重要程度
W_FAIRNESS:   float = 0.3   # Jain's Fairness 公平性補償
W_DELAY:      float = 0.2   # 延遲懲罰

# BSR 正規化參考值 (bytes)，大致對應 150 KB 的佇列深度
MAX_BSR: float = 150_000.0


# =============================================================================
# 公開介面
# =============================================================================

def compute_reward(
    ues: list[dict],
    allocations: list[dict],
    total_prb: int = 106,
) -> float:
    """
    計算複合獎勵值 R ∈ [-W_DELAY, W_THROUGHPUT + W_FAIRNESS]。

    Args:
        ues         : 當前 UE 狀態，[{"rnti", "bsr", "wb_cqi"}, ...]
        allocations : AI 決策，[{"rnti", "prb_abs"}, ...]
        total_prb   : 系統 PRB 總數

    Returns:
        reward : float，夾緊至 [-1.0, 1.0]
    """
    if not ues or not allocations:
        return 0.0

    # RNTI → 分配 PRB 數的快速查詢字典
    alloc_map: dict[int, int] = {
        int(a["rnti"]): int(a["prb_abs"]) for a in allocations
    }

    throughputs: list[float] = []
    delay_pressures: list[float] = []

    for ue in ues:
        rnti = int(ue.get("rnti", 0))
        bsr  = max(float(ue.get("bsr", 0)), 0.0)
        cqi  = int(ue.get("wb_cqi", 7))
        cqi  = max(0, min(15, cqi))       # 夾緊至合法範圍
        prb  = float(alloc_map.get(rnti, 1))

        # 估計吞吐量：頻譜效率 × 分配 PRB，正規化至 [0, 1]
        eff = CQI_TO_EFFICIENCY[cqi]
        tp_norm = eff * prb / (MAX_EFFICIENCY * total_prb)
        throughputs.append(tp_norm)

        # 佇列壓力：(BSR 正規化值) / (PRB 分配比例)
        # 物理意義：每單位 PRB 需要排送的資料量，越高代表延遲越大
        prb_ratio = prb / total_prb
        dp = (bsr / MAX_BSR) / (prb_ratio + 1e-6)
        delay_pressures.append(min(dp, 1.0))   # 夾緊至 [0, 1]

    tp_arr = np.array(throughputs, dtype=np.float64)
    dp_arr = np.array(delay_pressures, dtype=np.float64)

    # 三個分量
    r_throughput = float(tp_arr.mean())
    r_fairness   = _jains_fairness(tp_arr)
    r_delay      = float(dp_arr.mean())

    reward = (
        W_THROUGHPUT * r_throughput
        + W_FAIRNESS * r_fairness
        - W_DELAY    * r_delay
    )
    return float(np.clip(reward, -1.0, 1.0))


def compute_reward_breakdown(
    ues: list[dict],
    allocations: list[dict],
    total_prb: int = 106,
) -> dict:
    """
    回傳獎勵的細節分解（供監控與除錯使用）。

    Returns:
        {
            "reward"      : float,
            "r_throughput": float,
            "r_fairness"  : float,
            "r_delay"     : float,
        }
    """
    if not ues or not allocations:
        return {"reward": 0.0, "r_throughput": 0.0, "r_fairness": 0.0, "r_delay": 0.0}

    alloc_map: dict[int, int] = {
        int(a["rnti"]): int(a["prb_abs"]) for a in allocations
    }

    throughputs: list[float] = []
    delay_pressures: list[float] = []

    for ue in ues:
        rnti = int(ue.get("rnti", 0))
        bsr  = max(float(ue.get("bsr", 0)), 0.0)
        cqi  = int(ue.get("wb_cqi", 7))
        cqi  = max(0, min(15, cqi))
        prb  = float(alloc_map.get(rnti, 1))

        eff = CQI_TO_EFFICIENCY[cqi]
        tp_norm = eff * prb / (MAX_EFFICIENCY * total_prb)
        throughputs.append(tp_norm)

        prb_ratio = prb / total_prb
        dp = (bsr / MAX_BSR) / (prb_ratio + 1e-6)
        delay_pressures.append(min(dp, 1.0))

    tp_arr = np.array(throughputs, dtype=np.float64)
    dp_arr = np.array(delay_pressures, dtype=np.float64)

    r_tp   = float(tp_arr.mean())
    r_fair = _jains_fairness(tp_arr)
    r_dly  = float(dp_arr.mean())
    reward = float(np.clip(
        W_THROUGHPUT * r_tp + W_FAIRNESS * r_fair - W_DELAY * r_dly,
        -1.0, 1.0,
    ))

    return {
        "reward":       reward,
        "r_throughput": r_tp,
        "r_fairness":   r_fair,
        "r_delay":      r_dly,
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
