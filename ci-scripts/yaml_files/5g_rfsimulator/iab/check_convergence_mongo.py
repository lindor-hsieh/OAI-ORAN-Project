#!/usr/bin/env python3
"""
check_convergence_mongo.py — DRL 訓練收斂狀態檢查（MongoDB 版，2026-09-18 新增）

在 PC1 執行：
  python3 iab/check_convergence_mongo.py                          # 檢查 Node1~12，預設看最近 120 分鐘
  python3 iab/check_convergence_mongo.py --window-minutes 60      # 改看最近 60 分鐘
  python3 iab/check_convergence_mongo.py --nodes 3 4 5

背景：`check_convergence.py`（舊版）從 `docker logs inference-node{N}` 擷取每輪
「訓練完成」摘要行來判斷收斂趨勢——但 `training_watchdog.sh` 的 `full_recovery()`
每次崩潰復原都會讓 `inference-nodeN` 容器重新啟動，`docker logs` 的歷史會被砍掉，
長時間收斂訓練期間平均每 20~30 分鐘崩潰一次，導致舊版腳本幾乎永遠湊不齊判斷收斂
所需的連續輪數（見 2026-09-18 HISTORY.md 條目）。

這支腳本改用 MongoDB 裡的原始經驗資料（`node{N}_experiences`，含 `timestamp`／
`reward`／`is_idle`）——這份資料完全不受容器重啟影響（`training_watchdog.sh`
`full_recovery()` 絕對不會清空 MongoDB），可以完整跨越任意次數的崩潰復原持續
累積、判斷趨勢。

做法：
  抓最近 --window-minutes 分鐘內、非閒置轉換（`is_idle` != True）且不含
  `lambda_applied` 欄位（排除 REWARD_MODE 曾經短暫被重設成 lagrangian 時寫入
  的污染資料，見下方「資料清潔」說明）的經驗，依 --bucket-minutes 分鐘一組
  切成連續時間區間，算每個區間的「中位數」reward（不是平均數，避免離群值
  扭曲判斷），對這串「區間中位數 reward」做線性回歸：
    - 視窗內總變化量相對於其自身平均值的比例，低於 --flat-threshold
      （預設 15%，跟舊版一致）視為「趨勢打平」
    - 區間數量不足 --min-buckets（預設 8）視為資料不足，尚無法判斷

資料清潔（2026-09-18 現場發現後補上）：
  長時間收斂訓練期間 FlexRIC 反覆崩潰，`training_watchdog.sh` 的
  `full_recovery()` 修復前有極少數窗口 `REWARD_MODE` 短暫被重設回預設值
  `lagrangian`（見 HISTORY.md），導致少量經驗混入 Lagrangian 公式算出的
  reward（帶負的懲罰項，數值可能到 -1 以上，跟 throughput_only 的 [0,1]
  語意完全不同）。這支腳本會自動排除這些文件（用 `lambda_applied` 欄位是
  否存在來辨識），並在報告裡列出排除筆數；MongoDB 裡的原始文件本身**不會**
  被這支腳本刪除——如果要真的清掉，用 `iab/clean_lambda_contamination.py`
  （另外提供，需要明確執行才會刪除）。

跟舊版一樣，這只是輔助判斷用的啟發式工具，不是嚴謹的統計檢定；且因為改用原始
經驗（inference 當下觀測到的 reward），不是訓練輪次的 train/test loss，兩者
反映的訊號略有不同（這裡看的是「部署中的 policy 實際拿到的 reward 有沒有隨時間
穩定下來」，不是「梯度更新本身的 loss 有沒有收斂」）——沒有 OVERFIT 偵測，那是
train/test loss 才有的概念，這裡沒有對應的訊號可用。
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    import numpy as np
except ImportError:
    print("[錯誤] 需要 numpy： pip install numpy", file=sys.stderr)
    sys.exit(1)

try:
    import pymongo
except ImportError:
    print("[錯誤] 需要 pymongo： pip install pymongo", file=sys.stderr)
    sys.exit(1)

DEFAULT_NODES = list(range(1, 13))
DEFAULT_WINDOW_MINUTES = 120
DEFAULT_BUCKET_MINUTES = 5
DEFAULT_MIN_BUCKETS = 8
DEFAULT_FLAT_THRESHOLD = 0.15

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "iab_xapp")


@dataclass
class NodeVerdict:
    node_id: int
    bucket_rewards: list[float]
    bucket_counts: list[int]
    total_samples: int
    n_excluded_lambda: int = 0
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def fetch_bucket_rewards(
    col: "pymongo.collection.Collection",
    node_id: int,
    window_minutes: int,
    bucket_minutes: int,
) -> NodeVerdict:
    """
    從 MongoDB 抓最近 window_minutes 分鐘的非閒置經驗，切成 bucket_minutes 一組算
    每個區間的「中位數」reward（不是平均數）。

    兩個防呆（2026-09-18 現場發現後補上）：
      1. 排除 `lambda_applied` 存在的文件——這代表該筆是 REWARD_MODE=lagrangian
         公式算出來的（現場查到少量污染，來自修復前的崩潰復原窗口，REWARD_MODE
         短暫被重設回預設值），跟目前 throughput_only 的訓練目標語意不一致，
         數值可能是大幅負值（Lagrangian 懲罰項），混進來會嚴重扭曲趨勢判斷。
      2. 每個區間用中位數而非平均數：throughput_only 的 reward 本身量級很小
         （多數 UE 傳輸量遠低於 MAX_BSR 正規化上限），平均數對離群值（即使是
         合法的 Lagrangian-mode 污染之外、單純訓練場景本身造成的極端值）非常
         敏感，中位數更能反映「一般情況」的趨勢。
    """
    since = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    try:
        cursor = col.find(
            {
                "timestamp": {"$gte": since},
                "reward": {"$exists": True},
                "is_idle": {"$ne": True},
                "lambda_applied": {"$exists": False},
            },
            projection={"timestamp": 1, "reward": 1, "_id": 0},
        ).sort("timestamp", pymongo.ASCENDING)
        docs = list(cursor)
        n_excluded = col.count_documents({"timestamp": {"$gte": since}, "lambda_applied": {"$exists": True}})
    except pymongo.errors.PyMongoError as exc:
        return NodeVerdict(node_id=node_id, bucket_rewards=[], bucket_counts=[], total_samples=0,
                            error=f"MongoDB 查詢失敗: {exc}")

    if not docs:
        return NodeVerdict(node_id=node_id, bucket_rewards=[], bucket_counts=[], total_samples=0,
                            error=f"最近 {window_minutes} 分鐘內沒有非閒置經驗資料")

    bucket_span = timedelta(minutes=bucket_minutes)
    n_buckets = max(1, window_minutes // bucket_minutes)
    values: list[list[float]] = [[] for _ in range(n_buckets)]

    for d in docs:
        ts = d["timestamp"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        idx = int((ts - since) / bucket_span)
        if idx < 0:
            idx = 0
        if idx >= n_buckets:
            idx = n_buckets - 1
        values[idx].append(float(d["reward"]))

    bucket_rewards = [float(np.median(v)) for v in values if v]
    bucket_counts = [len(v) for v in values if v]

    return NodeVerdict(node_id=node_id, bucket_rewards=bucket_rewards, bucket_counts=bucket_counts,
                        total_samples=len(docs), n_excluded_lambda=n_excluded)


def _relative_drift(values: list[float]) -> float:
    """視窗內線性回歸斜率 × 視窗長度，相對於數值平均量級的比例（跟舊版同一套算法）。"""
    n = len(values)
    if n < 2:
        return float("inf")
    x = np.arange(n, dtype=np.float64)
    y = np.array(values, dtype=np.float64)
    slope = float(np.polyfit(x, y, 1)[0])
    scale = max(float(np.mean(np.abs(y))), 1e-6)
    return abs(slope * n) / scale


def judge(verdict: NodeVerdict, min_buckets: int, flat_threshold: float) -> dict:
    if not verdict.ok:
        return {"status": "無法判斷", "reason": verdict.error}

    if len(verdict.bucket_rewards) < min_buckets:
        extra = f"，另排除 {verdict.n_excluded_lambda} 筆 lagrangian 污染資料" if verdict.n_excluded_lambda else ""
        return {
            "status": "資料不足",
            "reason": f"只有 {len(verdict.bucket_rewards)} 個時間區間有資料，需要 {min_buckets} 個才能判斷趨勢"
                      f"（共 {verdict.total_samples} 筆非閒置經驗{extra}）",
        }

    drift = _relative_drift(verdict.bucket_rewards)
    status = "疑似收斂" if drift < flat_threshold else "仍在變動"
    reason = (
        f"reward 已打平（視窗內變化量佔平均值 {drift:.0%}）" if status == "疑似收斂"
        else f"reward 仍有趨勢（視窗內變化量佔平均值 {drift:.0%}）"
    )
    if verdict.n_excluded_lambda:
        reason += f"（另排除 {verdict.n_excluded_lambda} 筆 lagrangian 污染資料）"
    return {
        "status": status,
        "reason": reason,
        "drift": drift,
        "latest_reward": verdict.bucket_rewards[-1],
        "n_buckets": len(verdict.bucket_rewards),
        "total_samples": verdict.total_samples,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="DRL 訓練收斂狀態檢查（MongoDB 版，跨容器重啟持續累積）")
    ap.add_argument("--nodes", type=int, nargs="+", default=DEFAULT_NODES)
    ap.add_argument("--window-minutes", type=int, default=DEFAULT_WINDOW_MINUTES,
                     help="檢查最近幾分鐘的經驗資料（預設 %(default)s）")
    ap.add_argument("--bucket-minutes", type=int, default=DEFAULT_BUCKET_MINUTES,
                     help="每幾分鐘算一次平均 reward（預設 %(default)s）")
    ap.add_argument("--min-buckets", type=int, default=DEFAULT_MIN_BUCKETS,
                     help="至少要有幾個非空區間才判斷趨勢（預設 %(default)s）")
    ap.add_argument("--flat-threshold", type=float, default=DEFAULT_FLAT_THRESHOLD,
                     help="reward 視窗內變化量佔平均值的比例上限，低於此值視為打平（預設 %(default)s）")
    args = ap.parse_args()

    try:
        client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        client.server_info()
        db = client[MONGO_DB]
    except pymongo.errors.PyMongoError as exc:
        print(f"[錯誤] 連線 MongoDB 失敗: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"{'Node':<6}{'狀態':<10}{'最新區間 reward':<18}{'區間數':<8}{'樣本數':<10}原因")
    print("-" * 100)

    any_pending = False
    for node_id in args.nodes:
        col = db[f"node{node_id}_experiences"]
        verdict = fetch_bucket_rewards(col, node_id, args.window_minutes, args.bucket_minutes)
        result = judge(verdict, args.min_buckets, args.flat_threshold)

        reward_str = f"{result.get('latest_reward'):.4f}" if "latest_reward" in result else "-"
        n_buckets_str = str(result.get("n_buckets", "-"))
        total_str = str(result.get("total_samples", verdict.total_samples))
        print(f"Node{node_id:<5}{result['status']:<10}{reward_str:<18}{n_buckets_str:<8}{total_str:<10}{result['reason']}")

        if result["status"] in ("資料不足", "仍在變動", "無法判斷"):
            any_pending = True

    print()
    if any_pending:
        print("[提示] 還有節點未達「疑似收斂」，建議之後再跑一次本腳本追蹤趨勢。")
    else:
        print("[提示] 全部節點都已疑似收斂，可以考慮跑 iab/measure_stage.py 做正式的 15 分鐘量測。")


if __name__ == "__main__":
    main()
