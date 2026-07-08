#!/usr/bin/env python3
"""
check_convergence.py — DRL 訓練收斂狀態檢查

在 PC1 執行：
  python3 iab/check_convergence.py                  # 檢查 Node1~5，預設看最近 15 輪
  python3 iab/check_convergence.py --window 20       # 改看最近 20 輪
  python3 iab/check_convergence.py --nodes 3 4 5     # 只看指定節點

做法：
  從 `docker logs inference-node{N}` 擷取每輪「訓練完成」摘要行（inference_server.py
  的 _run_training_round() 印出的格式），取最近 --window 輪，對 train reward 與
  critic_loss 各自算線性回歸斜率，並統計這段視窗內 ⚠ OVERFIT 警告出現次數：

    - reward / critic_loss 的「視窗內總變化量」相對於其自身平均值的比例
      若都低於 --flat-threshold（預設 15%），視為「趨勢打平」
    - OVERFIT 出現比例超過 --overfit-threshold（預設 1/3 輪）視為不穩定
    - 兩者都通過才判定「疑似收斂」，否則說明卡在哪個條件

這只是輔助判斷用的啟發式工具，不是嚴謹的統計檢定——疑似收斂之後仍建議
再觀察幾輪，且最終要不要採信收斂結果，還是要配合實際跑 Scenario A/B/C
量測（iab/drl_report.py）的數字來看。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Optional

try:
    import numpy as np
except ImportError:
    print("[錯誤] 需要 numpy： pip install numpy", file=sys.stderr)
    sys.exit(1)

DEFAULT_NODES = [1, 2, 3, 4, 5]
DEFAULT_WINDOW = 15
DEFAULT_FLAT_THRESHOLD = 0.15     # 視窗內總變化量 / 平均值，低於此比例視為打平
DEFAULT_OVERFIT_THRESHOLD = 1 / 3  # 視窗內 OVERFIT 出現比例上限

# 對應 inference_server.py `_run_training_round()` 印出的格式：
#   訓練完成 10 epochs | step=100 train[actor=-0.4365 critic=0.2397 entropy=-0.2689
#   reward=0.5347] test[actor=-0.1289 critic=0.2622 entropy=-0.2703 reward=0.5391]
#   train_n=381 test_n=128 ⚠ OVERFIT | DRL/啟發式=250/282
LOG_LINE_RE = re.compile(
    r"訓練完成 \d+ epochs \| step=(?P<step>\d+) "
    r"train\[actor=(?P<train_actor>-?[\d.]+) critic=(?P<train_critic>-?[\d.]+) "
    r"entropy=(?P<train_entropy>-?[\d.]+) reward=(?P<train_reward>-?[\d.]+)\] "
    r"test\[actor=(?P<test_actor>-?[\d.]+) critic=(?P<test_critic>-?[\d.]+) "
    r"entropy=(?P<test_entropy>-?[\d.]+) reward=(?P<test_reward>-?[\d.]+)\] "
    r"train_n=(?P<train_n>\d+) test_n=(?P<test_n>\d+)(?P<overfit>.*?) \| "
    r"DRL/啟發式=(?P<drl_n>\d+)/(?P<heur_n>\d+)"
)


@dataclass
class Round:
    step: int
    train_reward: float
    train_critic: float
    train_entropy: float
    test_reward: float
    overfit: bool


@dataclass
class NodeVerdict:
    node_id: int
    rounds: list[Round] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and len(self.rounds) > 0


def fetch_rounds(node_id: int, tail_lines: int = 5000) -> NodeVerdict:
    """從 docker logs 擷取 inference-node{N} 的訓練輪次紀錄。"""
    container = f"inference-node{node_id}"
    try:
        raw = subprocess.run(
            ["docker", "logs", "--tail", str(tail_lines), container],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        return NodeVerdict(node_id=node_id, error=f"docker logs 執行失敗: {exc}")

    if raw.returncode != 0:
        return NodeVerdict(node_id=node_id, error=f"容器 {container} 讀不到 log（可能沒在跑）")

    rounds: list[Round] = []
    for line in (raw.stdout + raw.stderr).splitlines():
        m = LOG_LINE_RE.search(line)
        if not m:
            continue
        rounds.append(Round(
            step=int(m["step"]),
            train_reward=float(m["train_reward"]),
            train_critic=float(m["train_critic"]),
            train_entropy=float(m["train_entropy"]),
            test_reward=float(m["test_reward"]),
            overfit="OVERFIT" in m["overfit"],
        ))

    if not rounds:
        return NodeVerdict(node_id=node_id, error="還沒有任何一輪訓練完成的 log")
    return NodeVerdict(node_id=node_id, rounds=rounds)


def _relative_drift(values: list[float]) -> float:
    """視窗內線性回歸斜率 × 視窗長度，相對於數值平均量級的比例。"""
    n = len(values)
    if n < 2:
        return float("inf")
    x = np.arange(n, dtype=np.float64)
    y = np.array(values, dtype=np.float64)
    slope = float(np.polyfit(x, y, 1)[0])
    scale = max(float(np.mean(np.abs(y))), 1e-6)
    return abs(slope * n) / scale


def judge(verdict: NodeVerdict, window: int, flat_threshold: float,
          overfit_threshold: float) -> dict:
    if not verdict.ok:
        return {"status": "無法判斷", "reason": verdict.error}

    if len(verdict.rounds) < window:
        return {
            "status": "資料不足",
            "reason": f"只有 {len(verdict.rounds)} 輪，需要 {window} 輪才能判斷趨勢",
        }

    recent = verdict.rounds[-window:]
    reward_drift = _relative_drift([r.train_reward for r in recent])
    critic_drift = _relative_drift([r.train_critic for r in recent])
    overfit_ratio = sum(r.overfit for r in recent) / len(recent)

    reasons = []
    if reward_drift >= flat_threshold:
        reasons.append(f"reward 仍有趨勢（視窗內變化量佔平均值 {reward_drift:.0%}）")
    if critic_drift >= flat_threshold:
        reasons.append(f"critic_loss 仍有趨勢（視窗內變化量佔平均值 {critic_drift:.0%}）")
    if overfit_ratio > overfit_threshold:
        reasons.append(f"OVERFIT 警告過於頻繁（{overfit_ratio:.0%} 的輪次）")

    status = "疑似收斂" if not reasons else "仍在變動"
    return {
        "status": status,
        "reason": "；".join(reasons) if reasons else "reward/critic_loss 都已打平，OVERFIT 不頻繁",
        "reward_drift": reward_drift,
        "critic_drift": critic_drift,
        "overfit_ratio": overfit_ratio,
        "latest_reward": recent[-1].train_reward,
        "latest_step": recent[-1].step,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="DRL 訓練收斂狀態檢查")
    ap.add_argument("--nodes", type=int, nargs="+", default=DEFAULT_NODES)
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW,
                     help="檢查最近幾輪訓練（預設 %(default)s）")
    ap.add_argument("--flat-threshold", type=float, default=DEFAULT_FLAT_THRESHOLD,
                     help="reward/critic_loss 視窗內變化量佔平均值的比例上限，"
                          "低於此值視為打平（預設 %(default)s）")
    ap.add_argument("--overfit-threshold", type=float, default=DEFAULT_OVERFIT_THRESHOLD,
                     help="OVERFIT 警告出現比例上限（預設 %(default)s）")
    ap.add_argument("--tail-lines", type=int, default=5000,
                     help="docker logs 往回讀取的行數上限（預設 %(default)s）")
    args = ap.parse_args()

    print(f"{'Node':<6}{'狀態':<10}{'最新 reward':<14}{'最新 step':<10}原因")
    print("-" * 90)

    any_pending = False
    for node_id in args.nodes:
        verdict = fetch_rounds(node_id, tail_lines=args.tail_lines)
        result = judge(verdict, args.window, args.flat_threshold, args.overfit_threshold)

        reward_str = f"{result.get('latest_reward'):.4f}" if "latest_reward" in result else "-"
        step_str = str(result.get("latest_step", "-"))
        print(f"Node{node_id:<5}{result['status']:<10}{reward_str:<14}{step_str:<10}{result['reason']}")

        if result["status"] in ("資料不足", "仍在變動", "無法判斷"):
            any_pending = True

    print()
    if any_pending:
        print("[提示] 還有節點未達「疑似收斂」，建議之後再跑一次本腳本追蹤趨勢。")
    else:
        print("[提示] 全部節點都已疑似收斂，可以考慮跑 iab/drl_report.py 做正式的 Scenario 對比量測。")


if __name__ == "__main__":
    main()
