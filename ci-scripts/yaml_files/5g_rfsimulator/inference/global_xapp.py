"""
global_xapp.py — Phase 5 Global xApp（compute_quotas() 為現行架構共用，main() 已被取代）

**本檔案的 main()／create_push_sockets()／IPC PUSH 機制已被
global_xapp_bridge.py 取代，不再是實際運作的 Global xApp 進程。**
inference_server.py 現在的 relay/access 節點是直接用 ZMQ PUB/SUB
（Node1/2 PUB 自己的分配、Node3/4/5 SUB 訂閱配額），不再透過本檔案的
「輪詢 MongoDB → IPC PUSH」路徑。保留本檔案是因為 compute_quotas() 這個
配額計算函式仍被 global_xapp_bridge.py 直接 import 重用，不是死碼。

以下是舊架構（未實際部署）的職責描述，僅供歷史參考：
  - 全域視野：每 200ms 從 MongoDB 讀取 Node1/Node2 最新動作記錄
  - 計算 Node3/4/5 的 PRB 回傳配額（模擬 in-band IAB 回傳瓶頸）
  - 透過 ZMQ PUSH 將配額下發給 Node3/4/5 C xApp

配額計算邏輯（compute_quotas()，現行架構仍在用）：
  Node1 服務 MT3 (→ Node3) 與 MT4 (→ Node4)，按 RNTI 排序分配：
    - quota_node3 = Node1 第一個 UE (低 RNTI) 的 prb_abs
    - quota_node4 = Node1 第二個 UE (高 RNTI) 的 prb_abs
    - 若 Node1 只有 1 個 UE: quota_node3 = quota_node4 = total / 2
  Node2 服務 MT5 (→ Node5)：
    - quota_node5 = Node2 所有 UE prb_abs 之和
  若 Node1/2 超過 DATA_STALE_S 秒無新資料，配額退回 106（全頻寬）。

舊版 ZMQ 端點（Global xApp BIND PUSH，C xApp CONNECT PULL，已不使用）：
  - ipc:///tmp/zmq_node3_quota.ipc
  - ipc:///tmp/zmq_node4_quota.ipc
  - ipc:///tmp/zmq_node5_quota.ipc

現行架構的 ZMQ 端點（見 global_xapp_bridge.py）：
  Node1 PUB tcp://127.0.0.1:5561 ─┐
                                   ├─→ global_xapp_bridge.py SUB → compute_quotas() → PUB tcp://127.0.0.1:5560 → Node3/4/5 SUB
  Node2 PUB tcp://127.0.0.1:5562 ─┘
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import pymongo
import zmq

# =============================================================================
# 設定常數
# =============================================================================

TOTAL_PRB: int    = 106
MIN_QUOTA: int    = 10        # 避免 Node3/4/5 完全被餓死
POLL_INTERVAL_S   = 0.2       # MongoDB 輪詢間隔（秒）
DATA_STALE_S      = 5.0       # 超過此秒數無新資料則退回全頻寬
LOG_INTERVAL      = 50        # 每 N 次輪詢印一次狀態

QUOTA_ENDPOINTS: dict[int, str] = {
    3: "ipc:///tmp/zmq_node3_quota.ipc",
    4: "ipc:///tmp/zmq_node4_quota.ipc",
    5: "ipc:///tmp/zmq_node5_quota.ipc",
}

MONGO_URI: str = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB:  str = os.getenv("MONGO_DB",  "iab_xapp")

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


def get_latest_action(
    col: pymongo.collection.Collection,
    stale_threshold: float,
) -> Optional[list[dict]]:
    """
    取得該 collection 最新一筆含有 action 欄位的記錄。
    若記錄時間早於 stale_threshold 秒前，回傳 None。
    """
    try:
        doc = col.find_one(
            {"action": {"$exists": True, "$type": "array"}},
            sort=[("timestamp", pymongo.DESCENDING)],
            projection={"action": 1, "timestamp": 1, "_id": 0},
        )
    except pymongo.errors.PyMongoError:
        return None

    if doc is None:
        return None

    ts = doc.get("timestamp")
    if isinstance(ts, datetime):
        age = (datetime.now(timezone.utc) - ts.replace(tzinfo=timezone.utc)).total_seconds()
        if age > stale_threshold:
            return None

    action = doc.get("action", [])
    if not isinstance(action, list) or len(action) == 0:
        return None
    return action


# =============================================================================
# 配額計算
# =============================================================================

def compute_quotas(
    node1_action: Optional[list[dict]],
    node2_action: Optional[list[dict]],
) -> dict[int, int]:
    """
    根據 Node1/Node2 的最新 PRB 分配計算 Node3/4/5 的配額。

    Args:
        node1_action: Node1 的動作列表 [{"rnti": ..., "prb_abs": ...}, ...]
        node2_action: Node2 的動作列表

    Returns:
        {3: quota_node3, 4: quota_node4, 5: quota_node5}
    """
    quotas = {3: TOTAL_PRB, 4: TOTAL_PRB, 5: TOTAL_PRB}

    # Node3 & Node4 配額：由 Node1 的 MT PRB 分配決定
    if node1_action:
        sorted_ues = sorted(node1_action, key=lambda u: u.get("rnti", 0))
        if len(sorted_ues) >= 2:
            q3 = max(MIN_QUOTA, int(sorted_ues[0].get("prb_abs", TOTAL_PRB // 2)))
            q4 = max(MIN_QUOTA, int(sorted_ues[1].get("prb_abs", TOTAL_PRB // 2)))
        elif len(sorted_ues) == 1:
            half = max(MIN_QUOTA, int(sorted_ues[0].get("prb_abs", TOTAL_PRB)) // 2)
            q3 = q4 = half
        else:
            q3 = q4 = TOTAL_PRB
        quotas[3] = min(TOTAL_PRB, q3)
        quotas[4] = min(TOTAL_PRB, q4)

    # Node5 配額：由 Node2 的 MT PRB 分配決定（Node2 只有 1 個 MT）
    if node2_action:
        total = sum(u.get("prb_abs", 0) for u in node2_action)
        quotas[5] = min(TOTAL_PRB, max(MIN_QUOTA, int(total)))

    return quotas


# =============================================================================
# ZMQ 發布
# =============================================================================

def create_push_sockets(ctx: zmq.Context) -> dict[int, zmq.Socket]:
    socks: dict[int, zmq.Socket] = {}
    for node_id, endpoint in QUOTA_ENDPOINTS.items():
        sock = ctx.socket(zmq.PUSH)
        sock.setsockopt(zmq.LINGER, 0)
        sock.setsockopt(zmq.SNDHWM, 2)          # 限制排隊，避免記憶體爆炸
        sock.bind(endpoint)
        log.info("PUSH socket 已綁定至 %s (→ Node%d)", endpoint, node_id)
        socks[node_id] = sock
    return socks


def send_quota(sock: zmq.Socket, quota: int, node_id: int) -> None:
    msg = json.dumps({"quota": quota}, separators=(",", ":"))
    try:
        sock.send_string(msg, zmq.NOBLOCK)
    except zmq.Again:
        pass    # C xApp 尚未連線或 HWM 滿，靜默丟棄


# =============================================================================
# 主迴圈
# =============================================================================

def main() -> None:
    """[已被取代] 見檔頭說明——實際部署請用 global_xapp_bridge.py，本函式不再被任何
    docker-compose 服務呼叫，保留僅供參考。"""
    db = connect_mongo()
    if db is None:
        log.error("無法連線 MongoDB，程式退出")
        sys.exit(1)

    col1 = db["node1_experiences"]
    col2 = db["node2_experiences"]

    ctx = zmq.Context()
    socks = create_push_sockets(ctx)

    _running = True

    def _shutdown(signum, frame):
        nonlocal _running
        log.info("收到訊號 %d，正在關閉...", signum)
        _running = False

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    log.info("Global xApp 啟動，開始監控 Node1/Node2 回傳 PRB 分配")

    tick = 0
    last_quotas = {3: TOTAL_PRB, 4: TOTAL_PRB, 5: TOTAL_PRB}

    while _running:
        t0 = time.monotonic()

        # 讀取最新動作
        action1 = get_latest_action(col1, DATA_STALE_S)
        action2 = get_latest_action(col2, DATA_STALE_S)

        # 計算配額
        quotas = compute_quotas(action1, action2)
        last_quotas = quotas

        # 下發配額
        for node_id, sock in socks.items():
            send_quota(sock, quotas[node_id], node_id)

        # 定期列印狀態
        if tick % LOG_INTERVAL == 0:
            n1_status = f"total={sum(u.get('prb_abs',0) for u in action1)}" if action1 else "stale"
            n2_status = f"total={sum(u.get('prb_abs',0) for u in action2)}" if action2 else "stale"
            log.info(
                "Node1[%s] → quota3=%d quota4=%d | Node2[%s] → quota5=%d",
                n1_status, quotas[3], quotas[4],
                n2_status, quotas[5],
            )
        tick += 1

        # 精確間隔控制
        elapsed = time.monotonic() - t0
        sleep_s = max(0.0, POLL_INTERVAL_S - elapsed)
        time.sleep(sleep_s)

    # 清理
    for sock in socks.values():
        sock.close()
    ctx.term()
    log.info("Global xApp 已關閉。最後配額: %s", last_quotas)


if __name__ == "__main__":
    main()
