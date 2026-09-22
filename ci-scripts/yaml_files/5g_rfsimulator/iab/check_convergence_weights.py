#!/usr/bin/env python3
"""
check_convergence_weights.py — DRL 訓練收斂狀態檢查（權重穩定度版，2026-09-18 新增）

背景：`check_convergence_mongo.py`（reward 趨勢版）現場實測發現一個根本性問題——
reward 的「視窗內變化量」幾乎完全由「目前輪替到哪個訓練場景 slot」決定，不是
policy 本身的學習進度。現場證據（node1，150 分鐘窗口，5 分鐘一個桶）：

    bucket 3  median=0.000005   ← 低流量 slot
    bucket 4  median=0.187013   ← 高流量 slot 開始
    bucket 5~7        ≈0.19~0.20 ← 同一個 slot 內，數值穩定
    bucket 8  median=0.000012   ← 換回低流量 slot
    bucket 11~16       ≈0.17~0.18 ← 下一個高流量 slot

這是清楚的「隨場景切換的方波」，不是雜訊——`training_scenario_driver.sh` 的
輪替表刻意讓訓練場景涵蓋低/中/高流量 × 低/中/高路徑損耗（Scenario T）＋
真實隨機（R）＋邊界案例（A/B/C），每個 slot 之間 reward 的「合理範圍」本來
就天差地遠。任何橫跨多個 slot 的滾動視窗，用「reward 變化量」當收斂判準，
都會被這個環境本身的非平穩性淹沒，不管 policy 有沒有真的收斂都一樣不會打平。

這支腳本改成直接量測 **policy 權重本身有沒有停止變化**——這是收斂的定義本身
（"訓練收斂" = 梯度更新不再讓權重有意義地移動），跟當下在跑哪個訓練場景完全
無關，不會被場景輪替干擾。

做法：
  每次執行時，從 inference-nodeN 容器 `docker cp` 出 `model_nodeN.pt`，載入
  actor 的全部參數攤平成一個向量，計算跟「上一次執行時存的快照」之間的
  相對 L2 距離：
      relative_change = ||W_now - W_prev||_2 / ||W_prev||_2
  低於 --flat-threshold（預設 5%）視為這次穩定；連續 --min-stable-checks
  （預設 3 次）都穩定才判定「疑似收斂（權重穩定）」。快照與歷史紀錄存在
  --state-dir（預設 /tmp/weight_convergence_state），跨執行持續累積，不受
  容器重啟影響（inference-nodeN 重啟後 checkpoint 本來就會從磁碟重新載入
  回訓練前的權重，快照比較的是「磁碟上的權重內容」，跟容器生命週期無關）。

在 PC1 執行：
  python3 iab/check_convergence_weights.py                  # 檢查 Node1~12
  python3 iab/check_convergence_weights.py --nodes 3 4 5

跟 reward 版一樣，這仍然是輔助判斷用的啟發式工具，不是嚴謹的統計檢定；但
比 reward 版更貼近「收斂」這個詞本身的定義，建議兩者一起看：reward 版看
「部署中的 policy 實際拿到的 reward 分佈長怎樣」，這支腳本看「policy 本身
還在不在動」。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import torch
except ImportError:
    print("[錯誤] 需要 torch： pip install torch", file=sys.stderr)
    sys.exit(1)

DEFAULT_NODES = list(range(1, 13))
DEFAULT_FLAT_THRESHOLD = 0.05
DEFAULT_MIN_STABLE_CHECKS = 3
DEFAULT_STATE_DIR = "/tmp/weight_convergence_state"


def _flatten_actor_params(ckpt_path: Path) -> Optional[torch.Tensor]:
    """載入 checkpoint，把 actor 的全部參數攤平成一個 1D tensor。"""
    try:
        ckpt = torch.load(ckpt_path, map_location="cpu")
    except Exception as exc:
        print(f"  [警告] 載入 checkpoint 失敗: {exc}", file=sys.stderr)
        return None
    actor_sd = ckpt.get("actor")
    if not actor_sd:
        return None
    parts = [v.flatten().float() for v in actor_sd.values() if hasattr(v, "flatten")]
    if not parts:
        return None
    return torch.cat(parts)


def _fetch_checkpoint(node_id: int, tmp_dir: Path) -> Optional[Path]:
    """從 inference-nodeN 容器 docker cp 出 checkpoint。"""
    container = f"inference-node{node_id}"
    dst = tmp_dir / f"model_node{node_id}.pt"
    try:
        result = subprocess.run(
            ["docker", "cp", f"{container}:/app/models/model_node{node_id}.pt", str(dst)],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        print(f"  [警告] docker cp 失敗: {exc}", file=sys.stderr)
        return None
    if result.returncode != 0 or not dst.exists():
        print(f"  [警告] {container} 找不到 checkpoint（尚未產生第一份存檔？）", file=sys.stderr)
        return None
    return dst


def main() -> None:
    ap = argparse.ArgumentParser(description="DRL 訓練收斂狀態檢查（權重穩定度版，不受訓練場景輪替干擾）")
    ap.add_argument("--nodes", type=int, nargs="+", default=DEFAULT_NODES)
    ap.add_argument("--flat-threshold", type=float, default=DEFAULT_FLAT_THRESHOLD,
                     help="相對 L2 權重變化量上限，低於此值視為這次穩定（預設 %(default)s）")
    ap.add_argument("--min-stable-checks", type=int, default=DEFAULT_MIN_STABLE_CHECKS,
                     help="連續幾次穩定才判定疑似收斂（預設 %(default)s）")
    ap.add_argument("--state-dir", type=str, default=DEFAULT_STATE_DIR,
                     help="快照與歷史紀錄存放目錄（預設 %(default)s）")
    args = ap.parse_args()

    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    history_path = state_dir / "history.json"
    history: dict = json.loads(history_path.read_text()) if history_path.exists() else {}

    print(f"{'Node':<6}{'狀態':<20}{'本次變化量':<14}{'連續穩定次數':<14}原因")
    print("-" * 90)

    now_iso = datetime.now(timezone.utc).isoformat()
    any_pending = False

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for node_id in args.nodes:
            key = str(node_id)
            snap_path = state_dir / f"node{node_id}_prev.pt"

            ckpt_path = _fetch_checkpoint(node_id, tmp_dir)
            if ckpt_path is None:
                print(f"Node{node_id:<5}{'無法判斷':<20}{'-':<14}{'-':<14}docker cp 或載入失敗")
                any_pending = True
                continue

            current = _flatten_actor_params(ckpt_path)
            if current is None:
                print(f"Node{node_id:<5}{'無法判斷':<20}{'-':<14}{'-':<14}checkpoint 格式不符（可能還沒訓練過第一輪）")
                any_pending = True
                continue

            node_hist = history.get(key, {"stable_streak": 0, "last_change": None})

            if not snap_path.exists():
                torch.save(current, snap_path)
                history[key] = {"stable_streak": 0, "last_change": None}
                print(f"Node{node_id:<5}{'資料不足':<20}{'-':<14}{'0':<14}第一次執行，尚無上次快照可比較")
                any_pending = True
                continue

            previous = torch.load(snap_path, map_location="cpu")
            if previous.shape != current.shape:
                # 架構換過（例如 MODEL_ARCH 切換），舊快照不能比，當作全新起點
                torch.save(current, snap_path)
                history[key] = {"stable_streak": 0, "last_change": None}
                print(f"Node{node_id:<5}{'資料不足':<20}{'-':<14}{'0':<14}checkpoint 形狀跟上次不同（架構換過？），重新起算")
                any_pending = True
                continue

            prev_norm = float(torch.norm(previous).item())
            diff_norm = float(torch.norm(current - previous).item())
            rel_change = diff_norm / prev_norm if prev_norm > 1e-9 else float("inf")

            is_stable_this_time = rel_change < args.flat_threshold
            streak = (node_hist.get("stable_streak", 0) + 1) if is_stable_this_time else 0

            history[key] = {"stable_streak": streak, "last_change": rel_change, "last_check": now_iso}
            torch.save(current, snap_path)  # 這次變成下次比較的基準

            if streak >= args.min_stable_checks:
                status = "疑似收斂（權重穩定）"
                reason = f"連續 {streak} 次變化量都 < {args.flat_threshold:.0%}"
            elif is_stable_this_time:
                status = "趨於穩定"
                reason = f"這次變化量 {rel_change:.2%} < 門檻，但還沒連續 {args.min_stable_checks} 次"
                any_pending = True
            else:
                status = "仍在變動"
                reason = f"變化量 {rel_change:.2%} >= 門檻 {args.flat_threshold:.0%}"
                any_pending = True

            print(f"Node{node_id:<5}{status:<20}{rel_change:<14.2%}{streak:<14}{reason}")

    history_path.write_text(json.dumps(history, indent=2))

    print()
    if any_pending:
        print("[提示] 還有節點未達「疑似收斂」，建議之後再跑一次本腳本追蹤趨勢（快照已存，下次會比較真實變化）。")
    else:
        print("[提示] 全部節點的權重都已穩定，可以考慮跑 iab/drl_report.py 做正式的 Scenario 對比量測。")


if __name__ == "__main__":
    main()
