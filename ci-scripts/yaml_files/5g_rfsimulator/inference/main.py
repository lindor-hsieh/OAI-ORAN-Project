"""
main.py — Local xApp Python 推論伺服器入口 (Phase 4 DRL 版本)

透過環境變數決定節點，5 個容器共用同一個 image：
  NODE_ID   : IAB Node 編號 (1~5)，必填
  MONGO_URI : MongoDB 連線位址，預設 mongodb://localhost:27017
  MONGO_DB  : MongoDB 資料庫名稱，預設 iab_xapp
  MODEL_DIR : DRL 模型儲存目錄，預設 /app/models

ZMQ endpoint 由 NODE_ID 自動推導：
  ipc:///tmp/zmq_node{NODE_ID}_inference.ipc
"""

import os
import sys

from inference_server import InferenceServer


def main() -> None:
    # NODE_ID 為必填環境變數，缺少時立即終止
    raw_id = os.environ.get("NODE_ID")
    if raw_id is None:
        print("[main] 錯誤：環境變數 NODE_ID 未設定，請在 docker-compose 中指定")
        sys.exit(1)

    try:
        node_id = int(raw_id)
        assert 1 <= node_id <= 5, f"NODE_ID 必須在 1~5 之間，收到 {node_id}"
    except (ValueError, AssertionError) as exc:
        print(f"[main] 錯誤：NODE_ID 無效 — {exc}")
        sys.exit(1)

    server = InferenceServer(
        node_id=node_id,
        zmq_endpoint=f"ipc:///tmp/zmq_node{node_id}_inference.ipc",
        mongo_uri=os.getenv("MONGO_URI", "mongodb://localhost:27017"),
        mongo_db=os.getenv("MONGO_DB", "iab_xapp"),
        model_dir=os.getenv("MODEL_DIR", "/app/models"),
    )
    server.run()


if __name__ == "__main__":
    main()
