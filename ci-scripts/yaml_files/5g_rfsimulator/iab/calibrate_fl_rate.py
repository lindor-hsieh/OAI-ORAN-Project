#!/usr/bin/env python3
"""
calibrate_fl_rate.py — Stage 3 後續調整方向：FL 經驗累積速率校準探測腳本

背景（見 inference/STAGE3_CLUSTER_FL_DESIGN.md 第 8 節）：C xApp 的 Rate
Limiter 理論上限是每 100ms 一筆經驗（10 筆/秒），照這個理論值反推，跨過
`drl_agent.py::MIN_TRAIN_EXPERIENCES=200` 只要 20 秒、跨過訓練真正需要的
`TRAIN_SEQ_LEN*TRAIN_SEQ_COUNT=512` 筆也只要 ~51 秒。但實測
`flower-supernode-node1` 的訓練 log 顯示連續 57 分鐘都沒有跨過 200 筆這第一
道門檻——跟理論值差了兩個數量級以上，不能拿理論值去反推 Stage 3 後續量測
該拉長到多久的視窗。

本腳本取代理論估計：在真正的三主機 RAN + `traffic_scenario.py` 跑起來時，
直接輪詢 MongoDB，量出每個節點「reward + next_state_vec 皆存在」（跟
`training_pipeline.py::fetch_sequences()` 完全相同的 filter）的經驗筆數
隨時間的實際成長曲線，用實測斜率反推所需視窗長度。

**只需在 PC1 執行一次**（MongoDB 只跑在 PC1，見 CLAUDE.md 第 1 節），
搭配三主機 RAN 基礎設施 + `traffic_scenario.py` 同時在跑，且最好搭配
MongoDB 剛清空的乾淨起點（`docker volume` 清空或至少知道起始筆數）—— 否則
「成長量」會被舊資料汙染，估出來的速率偏高。

用法：
    python3 iab/calibrate_fl_rate.py --duration 600 --interval 30 \\
        --out /tmp/fl_calibration.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from pathlib import Path

import pymongo

MONGO_URI: str = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB: str = os.getenv("MONGO_DB", "iab_xapp")
NUM_NODES: int = int(os.getenv("FL_NUM_NODES", "12"))

# 跟 drl_agent.py 保持同步的門檻常數（校準腳本刻意不 import drl_agent.py，
# 避免把整個 torch/DRLAgent 依賴鏈拉進這支輕量輪詢腳本）。
#
# 2026-09-18 起 drl_agent.py 多了 MODEL_ARCH 開關（見模組 docstring）：
# MIN_TRAIN_EXPERIENCES 兩種架構都適用；MIN_FOR_FULL_BATCH（512 序列門檻）
# 只有 MODEL_ARCH=gru 才會卡——MODEL_ARCH=mlp（Stage 2~4 現在的預設）訓練走
# 打散抽樣，跨過 MIN_TRAIN_EXPERIENCES 就能訓練，不需要額外湊滿時間連續的
# 512 筆。下方印出的兩欄數字在 MODEL_ARCH=mlp 時只有第一欄（MIN_TRAIN_EXPERIENCES
# 那欄）有實際意義，第二欄僅供未來若切回 MODEL_ARCH=gru 時參考。
MIN_TRAIN_EXPERIENCES: int = 200          # 第一道門檻：原始經驗筆數（MLP／GRU 皆適用）
TRAIN_SEQ_LEN: int = 32                   # 每個訓練序列的步數（GRU only）
TRAIN_SEQ_COUNT: int = 16                 # 一次梯度更新要幾個序列（GRU only）
MIN_FOR_FULL_BATCH: int = TRAIN_SEQ_LEN * TRAIN_SEQ_COUNT  # 512，GRU 專用門檻的粗略下界
# 實際需要的原始經驗數只會 >= 512（若經驗流有中斷，_is_contiguous() 切段後
# 會有零散殘料湊不滿 32 步而被丟棄），512 是理論下界、不是保證值。

SAFETY_FACTOR: float = 3.0  # 建議視窗長度的安全係數，見腳本結尾說明


def _count_ready(col: pymongo.collection.Collection) -> int:
    """跟 training_pipeline.py::fetch_sequences() 用同一個 filter，只算真正
    可能被算進訓練序列候選池的經驗，不是 collection 的全部文件數。"""
    return col.count_documents({"reward": {"$exists": True}, "next_state_vec": {"$exists": True}})


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--duration", type=int, default=600, help="校準總秒數（預設 600 = 10 分鐘）")
    parser.add_argument("--interval", type=int, default=30, help="每幾秒輪詢一次（預設 30）")
    parser.add_argument("--out", required=True, help="輸出 CSV 路徑（逐節點逐時間點的原始樣本）")
    args = parser.parse_args()

    try:
        client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        client.server_info()
    except pymongo.errors.PyMongoError as exc:
        print(f"[calibrate_fl_rate] MongoDB 連線失敗: {exc}", file=sys.stderr)
        sys.exit(1)

    db = client[MONGO_DB]
    cols = {n: db[f"node{n}_experiences"] for n in range(1, NUM_NODES + 1)}

    print(f"[calibrate_fl_rate] 開始輪詢 {NUM_NODES} 個節點，共 {args.duration}s，每 {args.interval}s 一次")
    print("[calibrate_fl_rate] 前提：traffic_scenario.py 已同時在跑，且最好是從 MongoDB 剛清空的乾淨起點開始")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 只記第一筆與最新一筆（掐頭去尾算平均斜率），CSV 裡保留全部樣本供事後畫成長曲線。
    first_snapshot: dict[int, tuple[float, int]] = {}
    last_snapshot: dict[int, tuple[float, int]] = {}

    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["elapsed_s", "node_id", "ready_count"])

        t0 = time.time()
        deadline = t0 + args.duration
        while time.time() < deadline:
            elapsed = time.time() - t0
            for node_id, col in cols.items():
                try:
                    count = _count_ready(col)
                except pymongo.errors.PyMongoError:
                    count = -1
                writer.writerow([f"{elapsed:.1f}", node_id, count])
                if count >= 0:
                    first_snapshot.setdefault(node_id, (elapsed, count))
                    last_snapshot[node_id] = (elapsed, count)
            f.flush()
            time.sleep(args.interval)

    print(f"[calibrate_fl_rate] 完成，原始樣本寫入 {out_path}\n")

    # ── 逐節點算出實測速率，反推跨過兩道門檻所需時間 ──
    print(f"{'Node':<6}{'速率(筆/秒)':<14}{'200筆預估(s)':<16}{'512筆預估(s)':<16}")
    rates: list[float] = []
    for node_id in sorted(cols):
        if node_id not in first_snapshot:
            print(f"{node_id:<6}{'無資料（MongoDB 連線異常？）':<14}")
            continue
        t_first, c_first = first_snapshot[node_id]
        t_last, c_last = last_snapshot[node_id]
        dt, dc = t_last - t_first, c_last - c_first
        if dt <= 0 or dc <= 0:
            print(f"{node_id:<6}{'0（無成長，該節點可能無流量或已斷線）':<14}")
            continue
        rate = dc / dt
        rates.append(rate)
        print(f"{node_id:<6}{rate:<14.4f}{MIN_TRAIN_EXPERIENCES/rate:<16.1f}{MIN_FOR_FULL_BATCH/rate:<16.1f}")

    if rates:
        # 用全網最慢的節點當瓶頸——FL 要求全部 NUM_NODES 節點都回覆
        # （min_train_nodes=NUM_NODES，見 server_app.py），單一節點沒湊到
        # 訓練門檻不影響它「回覆」，但會讓它那份貢獻的 num-examples=0，
        # 拖累該節點自己的模型品質，也是 Stage 3 role_ratio 加權要吃到
        # 有意義訊號的前提，所以用最慢節點而非平均值來訂安全視窗長度。
        min_rate = min(rates)
        safe_window_s = MIN_FOR_FULL_BATCH / min_rate * SAFETY_FACTOR
        print(f"\n[calibrate_fl_rate] 全網最慢節點實測速率 = {min_rate:.4f} 筆/秒")
        print(
            f"[calibrate_fl_rate] 建議量測視窗 >= {safe_window_s:.0f} 秒"
            f"（約 {safe_window_s/60:.1f} 分鐘），已乘上安全係數 {SAFETY_FACTOR}x"
            "，確保 FL_ROUND_INTERVAL_S=180 秒的多輪聚合有機會反覆命中"
            "（不是壓線剛好跨過一次）"
        )
    else:
        print(
            "\n[calibrate_fl_rate] 全部節點皆無成長，無法估計速率——"
            "請確認 traffic_scenario.py 是否真的在跑、RAN 是否正常運作、"
            "13/13 E2 是否已連線。"
        )


if __name__ == "__main__":
    main()
