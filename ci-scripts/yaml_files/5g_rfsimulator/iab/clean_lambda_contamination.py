#!/usr/bin/env python3
"""
clean_lambda_contamination.py — 移除 MongoDB 裡混入的 Lagrangian 污染經驗（2026-09-18 新增）

背景：收斂訓練期間 FlexRIC 反覆崩潰，`training_watchdog.sh` 的 `full_recovery()`
修復前有極少數窗口 `REWARD_MODE` 短暫被重設回預設值 `lagrangian`，導致少量經驗
混入 Lagrangian 公式算出的 reward（帶負的懲罰項，數值可能到 -1 以上），跟目前
`throughput_only` 的訓練目標語意不一致（`lambda_applied` 欄位是辨識標記，只有
`compute_lagrangian_reward()` 寫入的文件才有這個欄位）。

現場實測（2026-09-18）：污染比例很小（每個節點 3 小時內約 1~16 筆，佔比 <1%），
但因為 throughput_only 的 reward 本身量級很小，這些離群值會顯著扭曲
`check_convergence_mongo.py` 的趨勢判斷（該腳本已經改成查詢時排除這些文件，
本腳本是進一步的資料清潔動作：把它們從 MongoDB 實際刪除，而不是只在分析時濾掉）。

**這是刪除操作，預設是 dry-run（只統計、不刪除），要真的刪除需要 --execute。**
只刪除帶 `lambda_applied` 欄位的文件，不影響任何其他經驗資料，也不影響
checkpoint、不會中斷正在跑的訓練（MongoDB 刪除是原子性的單文件操作，跟背景
訓練執行緒的讀取不會互相鎖死）。

用法：
  python3 iab/clean_lambda_contamination.py                 # dry-run，只印出統計
  python3 iab/clean_lambda_contamination.py --execute        # 真的執行刪除
  python3 iab/clean_lambda_contamination.py --nodes 1 2 3    # 只處理指定節點
"""

from __future__ import annotations

import argparse
import os
import sys

try:
    import pymongo
except ImportError:
    print("[錯誤] 需要 pymongo： pip install pymongo", file=sys.stderr)
    sys.exit(1)

DEFAULT_NODES = list(range(1, 13))
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "iab_xapp")


def main() -> None:
    ap = argparse.ArgumentParser(description="移除 MongoDB 裡混入的 Lagrangian 污染經驗（預設 dry-run）")
    ap.add_argument("--nodes", type=int, nargs="+", default=DEFAULT_NODES)
    ap.add_argument("--execute", action="store_true", help="真的執行刪除（預設只統計，不刪除）")
    args = ap.parse_args()

    try:
        client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        client.server_info()
        db = client[MONGO_DB]
    except pymongo.errors.PyMongoError as exc:
        print(f"[錯誤] 連線 MongoDB 失敗: {exc}", file=sys.stderr)
        sys.exit(1)

    mode = "執行刪除" if args.execute else "dry-run（不會真的刪除）"
    print(f"模式：{mode}\n")
    print(f"{'Node':<8}{'總筆數':<12}{'污染筆數':<12}{'佔比':<10}")
    print("-" * 50)

    total_docs = 0
    total_contaminated = 0
    for node_id in args.nodes:
        col = db[f"node{node_id}_experiences"]
        total = col.count_documents({})
        contaminated = col.count_documents({"lambda_applied": {"$exists": True}})
        pct = f"{contaminated / total:.2%}" if total else "-"
        print(f"Node{node_id:<4}{total:<12}{contaminated:<12}{pct:<10}")
        total_docs += total
        total_contaminated += contaminated

        if args.execute and contaminated:
            result = col.delete_many({"lambda_applied": {"$exists": True}})
            print(f"         -> 已刪除 {result.deleted_count} 筆")

    print("-" * 50)
    print(f"合計：{total_docs} 筆，污染 {total_contaminated} 筆（{total_contaminated/total_docs:.2%}）")
    if not args.execute and total_contaminated:
        print("\n[提示] 這是 dry-run，尚未刪除。要真的刪除請加 --execute。")


if __name__ == "__main__":
    main()
