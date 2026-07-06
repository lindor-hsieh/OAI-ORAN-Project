"""
flower_server.py — [已被取代] Phase 5 Global rApp 舊草稿

**本檔案已被 flower-app/iab_fl/server_app.py 取代，不再被任何 docker-compose
服務引用。** 用的 fl.server.start_server() 是 flwr 1.28.0 裡標記為
deprecated 的 legacy API（見 flwr/compat/server/app.py），官方目前建議
改用 ServerApp/ClientApp + `flwr run`（透過 SuperLink/SuperNode 部署）。

保留本檔案只是為了 compute_global_jfi() 的邏輯參考——這段已經照抄進
flower-app/iab_fl/server_app.py 了。確認新架構穩定後可以整份刪除。

以下是原始草稿的職責描述（僅供歷史參考，不代表目前實際運作方式）：
  - 每 ROUND_INTERVAL_S 秒觸發一輪 Federated Learning
  - 強制等待全部 5 個 IAB Node 的 Flower Client 加入後才執行聚合
  - 聚合策略：FedAvg（加權平均，以各節點訓練樣本數為權重）
  - 聚合後計算全網 Jain's Fairness Index（從 MongoDB 讀取 reward 數據）

Flower Server 監聽端口：0.0.0.0:8080
  所有 inference 容器 (Node1~5) 透過 localhost:8080 連接，
  因為全部容器均使用 host network mode，TCP 轉發無需跨主機。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional, Union

import flwr as fl
import numpy as np
import pymongo
from flwr.common import (
    FitRes,
    Parameters,
    Scalar,
    ndarrays_to_parameters,
    parameters_to_ndarrays,
)
from flwr.server.client_proxy import ClientProxy
from flwr.server.strategy import FedAvg

# =============================================================================
# 設定
# =============================================================================

NUM_NODES: int      = 5          # IAB Node 1~5，全部必須參與
SERVER_ADDR: str    = "0.0.0.0:8080"
ROUND_INTERVAL_S    = 3600.0     # 每小時執行一輪 FL
NUM_ROUNDS: int     = 1          # 每次啟動執行的輪數（搭配 docker restart 實現週期化）
ROUND_TIMEOUT_S     = 600.0      # 單輪超時（秒），防止 client 掉線卡住

MONGO_URI: str = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB:  str = os.getenv("MONGO_DB",  "iab_xapp")

logging.basicConfig(
    level=logging.INFO,
    format="[FlowerServer] %(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("flower_server")


# =============================================================================
# Jain's Fairness Index 計算（從 MongoDB 讀取，用於日誌）
# =============================================================================

def compute_global_jfi(db: pymongo.database.Database) -> float:
    """計算全網 Jain's Fairness Index（以各節點最近 100 筆 reward 的 mean_reward 代理）。"""
    rewards = []
    for node_id in range(1, 6):
        col = db[f"node{node_id}_experiences"]
        try:
            docs = list(
                col.find(
                    {"reward": {"$exists": True}},
                    projection={"reward": 1, "_id": 0},
                )
                .sort("timestamp", pymongo.DESCENDING)
                .limit(100)
            )
            if docs:
                mean_r = float(np.mean([d["reward"] for d in docs]))
                rewards.append(mean_r)
        except Exception:
            pass
    if not rewards:
        return 0.0
    x = np.array(rewards)
    return float(x.sum() ** 2 / (len(x) * (x ** 2).sum() + 1e-9))


# =============================================================================
# 自訂聚合策略（繼承 FedAvg，加入 JFI 日誌）
# =============================================================================

class IABFedAvg(FedAvg):
    """FedAvg with Jain's Fairness Index logging."""

    def __init__(self, db: Optional[pymongo.database.Database], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.db = db
        self._round = 0

    def aggregate_fit(
        self,
        server_round: int,
        results: list[tuple[ClientProxy, FitRes]],
        failures: list[Union[tuple[ClientProxy, FitRes], BaseException]],
    ) -> tuple[Optional[Parameters], dict[str, Scalar]]:
        params, metrics = super().aggregate_fit(server_round, results, failures)
        self._round = server_round

        # 計算全網 JFI（非阻塞，失敗不影響聚合）
        jfi = 0.0
        if self.db is not None:
            try:
                jfi = compute_global_jfi(self.db)
            except Exception as exc:
                log.warning("JFI 計算失敗: %s", exc)

        num_clients = len(results)
        total_examples = sum(r.num_examples for _, r in results)
        log.info(
            "Round %d 聚合完成 | clients=%d total_examples=%d JFI=%.4f",
            server_round, num_clients, total_examples, jfi,
        )
        metrics["global_jfi"] = jfi
        return params, metrics


# =============================================================================
# 主函數
# =============================================================================

def main() -> None:
    # 連接 MongoDB（失敗可繼續，只是 JFI 會是 0）
    db = None
    try:
        client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        client.server_info()
        db = client[MONGO_DB]
        log.info("MongoDB 已連線: %s/%s", MONGO_URI, MONGO_DB)
    except pymongo.errors.PyMongoError as exc:
        log.warning("MongoDB 連線失敗 (%s)，JFI 功能停用", exc)

    strategy = IABFedAvg(
        db=db,
        min_fit_clients=NUM_NODES,
        min_evaluate_clients=NUM_NODES,
        min_available_clients=NUM_NODES,
        fraction_fit=1.0,
        fraction_evaluate=1.0,
    )

    log.info(
        "Flower Server 啟動 | addr=%s | rounds=%d | min_clients=%d",
        SERVER_ADDR, NUM_ROUNDS, NUM_NODES,
    )

    fl.server.start_server(
        server_address=SERVER_ADDR,
        config=fl.server.ServerConfig(
            num_rounds=NUM_ROUNDS,
            round_timeout=ROUND_TIMEOUT_S,
        ),
        strategy=strategy,
    )

    log.info("本次 FL 輪次完成，等待 %.0f 秒後下一輪 (若容器未重啟)", ROUND_INTERVAL_S)
    time.sleep(ROUND_INTERVAL_S)


if __name__ == "__main__":
    main()
