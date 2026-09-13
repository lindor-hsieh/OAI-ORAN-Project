"""
global_xapp.py — Global xApp（Stage 2 起全新設計，取代舊版 relay→access ZMQ 配額廣播）

**架構定位**：跟 Local xApp（C，每 ~100ms 的節點內控制迴圈）對稱，這是一個
跑在全域視角、節奏介於 Local xApp（ms 級）與 Global rApp（分鐘級 FedAvg）
之間的協調迴圈（預設每 2 秒一輪）。職責是提供單一節點無法從自己的局部資料
算出來的東西——「我在整個 12-node 樹狀拓樸裡，相對其他節點是不是被犧牲
了」——並把這個資訊廣播回去，讓 Local DRL 的 Actor 有機會學會在決策時
多考慮一點全域公平性。

**跟 C 層「Backhaul-aware 動態 PRB 預算」機制（gNB_scheduler_dlsch.c）的分工**：
C 層機制依「節點自己 MT 的真實 backhaul 使用量」動態縮小該節點 DU 這一輪
實際可用的 PRB 資源池大小，對 PF 與 DRL 一視同仁生效，是硬性的資源池限制。
本檔案完全不碰資源池大小，只提供一個**軟性 state 特徵**（fairness_bias）
給 DRL 當額外輸入——兩者作用在不同層次，天生不會疊加節流。

**跟舊版設計（已移除）的差異**：舊版 `global_xapp_bridge.py` 是 relay 節點
（Node1/2）把自己的 PRB 分配透過 ZMQ PUB 廣播出去，由 bridge 算出配額後
再 PUB 給 access 節點（Node3/4/5）「硬性裁切」自己的輸出。這個設計：
  (a) 只是局部視角（relay 對自己直接子節點的猜測值），不是真正全域；
  (b) 跟 C 層機制做的是同一件事（縮小可用資源），會雙重節流；
  (c) 硬編碼在 2-relay/3-access 拓樸，無法套用到現在 4-relay/8-access。
`compute_quotas()`／`global_xapp_bridge.py` 仍保留在磁碟供歷史參考，
不再被任何 docker-compose 服務呼叫。

**運作方式**：
  1. 每輪對 MongoDB 全部 `NUM_NODES` 個節點的 `node{i}_experiences`
     collection 各自查詢最近 `GLOBAL_XAPP_LOOKBACK` 筆、排除
     `is_idle=True` 的文件，取 `r_throughput` 欄位算平均吞吐量。
     沒有資料的節點記為 None（不計入全域平均）。
  2. `global_mean` = 有資料節點的平均吞吐量之平均值。
  3. `fairness_bias_i = clip(global_mean / (mean_i + eps), BIAS_MIN, BIAS_MAX)`
     ——吞吐量低於全域平均 → bias > 1（代表被犧牲，可以更積極）；
     高於平均 → bias < 1。沒有資料的節點給中性值 1.0。
  4. 額外算一個 `global_jfi`（Jain's Fairness Index，同一組平均吞吐量）
     僅供人工觀察列印，不寫回 MongoDB——避免跟 `server_app.py` 每輪
     FedAvg 各自算的 JFI 混淆成兩個不同時間粒度的「權威值」。
  5. 透過 ZMQ PUB（bind `tcp://127.0.0.1:5560`）對每個節點送
     `"node{i} " + json.dumps({"fairness_bias": bias})`。

所有例外都被捕捉並降級：單一節點查詢失敗不影響其他節點，本輪失敗不影響
下一輪，MongoDB 整個連不上則等待重試（不崩潰退出，avoid crash-loop）。
"""

from __future__ import annotations

import json
import logging
import os
import signal
import time
from typing import Optional

import numpy as np
import pymongo
import zmq

# =============================================================================
# 設定常數
# =============================================================================

MONGO_URI: str = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB: str = os.getenv("MONGO_DB", "iab_xapp")

NUM_NODES: int = int(os.getenv("GLOBAL_XAPP_NUM_NODES", "12"))
INTERVAL_S: float = float(os.getenv("GLOBAL_XAPP_INTERVAL_S", "2.0"))
LOOKBACK: int = int(os.getenv("GLOBAL_XAPP_LOOKBACK", "50"))

# 必須跟 drl_agent.py 的 FAIRNESS_BIAS_MIN/MAX 一致，否則 Actor 收到的
# state 特徵正規化區間會跟這裡廣播的原始值域對不上。
BIAS_MIN: float = 0.5
BIAS_MAX: float = 2.0
NEUTRAL_BIAS: float = 1.0

PUB_ENDPOINT: str = "tcp://127.0.0.1:5560"
LOG_INTERVAL: int = 10  # 每 N 輪印一次全部節點的狀態摘要

logging.basicConfig(
    level=logging.INFO,
    format="[GlobalxApp] %(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("global_xapp")


# =============================================================================
# MongoDB 工具
# =============================================================================

def connect_mongo() -> Optional[pymongo.database.Database]:
    try:
        client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        client.server_info()
        db = client[MONGO_DB]
        log.info("MongoDB 已連線: %s/%s", MONGO_URI, MONGO_DB)
        return db
    except pymongo.errors.PyMongoError as exc:
        log.error("MongoDB 連線失敗: %s", exc)
        return None


def mean_recent_throughput(
    db: pymongo.database.Database,
    node_id: int,
    lookback: int,
) -> Optional[float]:
    """
    取得某節點最近 `lookback` 筆非閒置經驗的平均 r_throughput。
    無資料或查詢失敗時回傳 None（呼叫端視為「沒有資料，給中性值」）。
    """
    try:
        col = db[f"node{node_id}_experiences"]
        docs = list(
            col.find(
                {"is_idle": {"$ne": True}, "r_throughput": {"$exists": True}},
                projection={"r_throughput": 1, "_id": 0},
            )
            .sort("timestamp", pymongo.DESCENDING)
            .limit(lookback)
        )
    except pymongo.errors.PyMongoError as exc:
        log.debug("Node%d 查詢失敗: %s", node_id, exc)
        return None

    if not docs:
        return None
    return float(np.mean([d["r_throughput"] for d in docs]))


# =============================================================================
# 全域公平性偏差計算
# =============================================================================

def compute_fairness_biases(
    db: pymongo.database.Database,
    num_nodes: int,
    lookback: int,
) -> tuple[dict[int, float], dict[int, Optional[float]], float]:
    """
    回傳 (fairness_biases, mean_throughputs, global_jfi)。

    fairness_biases  : {node_id: bias}，全部 num_nodes 個節點皆有值
                        （沒資料的節點給 NEUTRAL_BIAS）。
    mean_throughputs : {node_id: mean_r_throughput or None}，供 log 用。
    global_jfi        : Jain's Fairness Index（僅供監控列印）。
    """
    mean_throughputs: dict[int, Optional[float]] = {}
    for node_id in range(1, num_nodes + 1):
        mean_throughputs[node_id] = mean_recent_throughput(db, node_id, lookback)

    valid_values = [v for v in mean_throughputs.values() if v is not None]

    if not valid_values:
        biases = {nid: NEUTRAL_BIAS for nid in mean_throughputs}
        return biases, mean_throughputs, 0.0

    global_mean = float(np.mean(valid_values))

    biases: dict[int, float] = {}
    eps = 1e-6
    for node_id, mean_tp in mean_throughputs.items():
        if mean_tp is None:
            biases[node_id] = NEUTRAL_BIAS
        else:
            raw_bias = global_mean / (mean_tp + eps)
            biases[node_id] = float(np.clip(raw_bias, BIAS_MIN, BIAS_MAX))

    x = np.array(valid_values)
    global_jfi = float(x.sum() ** 2 / (len(x) * (x ** 2).sum() + 1e-9))

    return biases, mean_throughputs, global_jfi


# =============================================================================
# ZMQ 發布
# =============================================================================

def create_pub_socket(ctx: zmq.Context) -> zmq.Socket:
    sock = ctx.socket(zmq.PUB)
    sock.setsockopt(zmq.LINGER, 0)
    sock.setsockopt(zmq.SNDHWM, 20)
    sock.bind(PUB_ENDPOINT)
    log.info("PUB socket 已綁定至 %s", PUB_ENDPOINT)
    return sock


def publish_biases(sock: zmq.Socket, biases: dict[int, float]) -> None:
    for node_id, bias in biases.items():
        msg = f"node{node_id} " + json.dumps({"fairness_bias": bias}, separators=(",", ":"))
        try:
            sock.send_string(msg, zmq.NOBLOCK)
        except zmq.Again:
            pass  # HWM 滿，靜默丟棄，下一輪會送新值


# =============================================================================
# 主迴圈
# =============================================================================

def main() -> None:
    db = connect_mongo()
    ctx = zmq.Context()
    sock = create_pub_socket(ctx)

    _running = True

    def _shutdown(signum, frame):
        nonlocal _running
        log.info("收到訊號 %d，正在關閉...", signum)
        _running = False

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info(
        "Global xApp 啟動 | num_nodes=%d interval=%.1fs lookback=%d",
        NUM_NODES, INTERVAL_S, LOOKBACK,
    )

    tick = 0
    while _running:
        t0 = time.monotonic()

        if db is None:
            db = connect_mongo()
            if db is None:
                time.sleep(INTERVAL_S)
                continue

        try:
            biases, mean_throughputs, global_jfi = compute_fairness_biases(
                db, NUM_NODES, LOOKBACK
            )
            publish_biases(sock, biases)

            if tick % LOG_INTERVAL == 0:
                summary = " ".join(
                    f"n{nid}={mean_throughputs[nid]:.2f}Mbps(bias={biases[nid]:.2f})"
                    if mean_throughputs[nid] is not None
                    else f"n{nid}=stale(bias={biases[nid]:.2f})"
                    for nid in range(1, NUM_NODES + 1)
                )
                log.info("global_jfi=%.4f | %s", global_jfi, summary)
        except Exception as exc:
            # 任何非預期例外都不能讓這個常駐 process 掛掉——降級成本輪跳過。
            log.warning("本輪計算失敗，跳過: %s", exc)

        tick += 1
        elapsed = time.monotonic() - t0
        time.sleep(max(0.0, INTERVAL_S - elapsed))

    sock.close()
    ctx.term()
    log.info("Global xApp 已關閉。")


if __name__ == "__main__":
    main()
