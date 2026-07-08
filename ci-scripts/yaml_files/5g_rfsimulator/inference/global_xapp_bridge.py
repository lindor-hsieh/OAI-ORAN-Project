"""
global_xapp_bridge.py — Phase 5 Global xApp 橋接 process

職責：
  補齊 inference_server.py 裡 relay（Node1/2）PUB 與 access（Node3/4/5）SUB
  之間缺的中間橋接：SUB 訂閱 Node1/2 各自的 PUB endpoint，用
  global_xapp.py 既有的 compute_quotas() 算出配額，再統一 PUB 到
  Node3/4/5 SUB 的固定 port，模擬 in-band IAB 的回傳瓶頸限制。

  這是從 Docker image（建置於 2026-06-17，未進版本控制）回收、修好 port
  對不上的問題後的版本：relay 端 bind tcp://127.0.0.1:5561（Node1）／5562
  （Node2），access 端固定 SUB tcp://127.0.0.1:5560——中間原本沒有任何
  process 真正跨過這兩組 port，本檔案就是補上的橋接。

資料流：
  Node1 PUB tcp://127.0.0.1:5561 ─┐
                                   ├─→ 本 process SUB → compute_quotas() → PUB tcp://127.0.0.1:5560 → Node3/4/5 SUB
  Node2 PUB tcp://127.0.0.1:5562 ─┘

配額計算邏輯與逾時規則：沿用 global_xapp.py 的 compute_quotas()／TOTAL_PRB／
MIN_QUOTA；若 Node1/2 超過 DATA_STALE_S 秒沒有新訊息，該節點視為 stale，
compute_quotas() 收到 None 會照原邏輯退回全頻寬。
"""

from __future__ import annotations

import json
import logging
import signal
import sys
import time
from typing import Optional

import zmq

from global_xapp import DATA_STALE_S, TOTAL_PRB, compute_quotas

RELAY_ENDPOINTS: dict[int, str] = {
    1: "tcp://127.0.0.1:5561",
    2: "tcp://127.0.0.1:5562",
}
ACCESS_PUB_ENDPOINT = "tcp://127.0.0.1:5560"
ACCESS_NODES = (3, 4, 5)

POLL_INTERVAL_MS = 200
LOG_INTERVAL = 25  # 每 N 次迴圈印一次狀態

logging.basicConfig(
    level=logging.INFO,
    format="[GlobalXAppBridge] %(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    force=True,  # global_xapp 的 import 已呼叫過 basicConfig，這裡強制覆蓋格式
)
log = logging.getLogger("global_xapp_bridge")


def create_sub_socket(ctx: zmq.Context) -> zmq.Socket:
    """建立一個 SUB socket，同時連到 Node1/Node2 的 PUB endpoint。"""
    sock = ctx.socket(zmq.SUB)
    sock.setsockopt(zmq.LINGER, 0)
    sock.setsockopt_string(zmq.SUBSCRIBE, "")  # relay 端訊息沒有 topic 前綴，全收
    for node_id, endpoint in RELAY_ENDPOINTS.items():
        sock.connect(endpoint)
        log.info("SUB 已連線至 %s (← Node%d)", endpoint, node_id)
    return sock


def create_pub_socket(ctx: zmq.Context) -> zmq.Socket:
    """建立 PUB socket，供 Node3/4/5 訂閱配額。"""
    sock = ctx.socket(zmq.PUB)
    sock.setsockopt(zmq.LINGER, 0)
    sock.setsockopt(zmq.SNDHWM, 5)
    sock.bind(ACCESS_PUB_ENDPOINT)
    log.info("PUB 已綁定至 %s (→ Node3/4/5)", ACCESS_PUB_ENDPOINT)
    return sock


def publish_quotas(pub_sock: zmq.Socket, quotas: dict[int, int]) -> None:
    for node_id in ACCESS_NODES:
        quota = quotas.get(node_id, TOTAL_PRB)
        # SUB 端用 setsockopt_string(SUBSCRIBE, f"node{id}") 做 topic 過濾，
        # 訊息開頭必須是 "node{id} " 前綴，格式需與 inference_server.py
        # 的 _quota_sub_worker() 解析邏輯（split(" ", 1) 再 json.loads）一致。
        msg = f"node{node_id} " + json.dumps({"quota": quota}, separators=(",", ":"))
        try:
            pub_sock.send_string(msg, zmq.NOBLOCK)
        except zmq.Again:
            pass  # 尚未有訂閱者或 HWM 滿，靜默丟棄


def main() -> None:
    ctx = zmq.Context()
    sub_sock = create_sub_socket(ctx)
    pub_sock = create_pub_socket(ctx)
    poller = zmq.Poller()
    poller.register(sub_sock, zmq.POLLIN)

    _running = True

    def _shutdown(signum, frame):
        nonlocal _running
        log.info("收到訊號 %d，正在關閉...", signum)
        _running = False

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info("Global xApp Bridge 啟動，開始橋接 Node1/2 → Node3/4/5 的回傳配額")

    last_action: dict[int, list[dict]] = {}
    last_seen: dict[int, float] = {}
    last_quotas = {n: TOTAL_PRB for n in ACCESS_NODES}
    tick = 0

    while _running:
        events = dict(poller.poll(POLL_INTERVAL_MS))
        if sub_sock in events:
            try:
                msg = sub_sock.recv_string(zmq.NOBLOCK)
                data = json.loads(msg)
                node_id = int(data.get("node_id", 0))
                allocations = data.get("allocations", [])
                if node_id in RELAY_ENDPOINTS and isinstance(allocations, list):
                    last_action[node_id] = allocations
                    last_seen[node_id] = time.monotonic()
            except (json.JSONDecodeError, ValueError, zmq.Again):
                pass
            except Exception as exc:
                log.debug("SUB 接收例外: %s", exc)

        now = time.monotonic()
        action1 = last_action.get(1) if now - last_seen.get(1, 0.0) <= DATA_STALE_S else None
        action2 = last_action.get(2) if now - last_seen.get(2, 0.0) <= DATA_STALE_S else None

        quotas = compute_quotas(action1, action2)
        last_quotas = quotas
        publish_quotas(pub_sock, quotas)

        if tick % LOG_INTERVAL == 0:
            n1_status = f"total={sum(u.get('prb_abs', 0) for u in action1)}" if action1 else "stale"
            n2_status = f"total={sum(u.get('prb_abs', 0) for u in action2)}" if action2 else "stale"
            log.info(
                "Node1[%s] → quota3=%d quota4=%d | Node2[%s] → quota5=%d",
                n1_status, quotas[3], quotas[4],
                n2_status, quotas[5],
            )
        tick += 1

    sub_sock.close()
    pub_sock.close()
    ctx.term()
    log.info("Global xApp Bridge 已關閉。最後配額: %s", last_quotas)


if __name__ == "__main__":
    main()
