"""
server_app.py — Phase 5 Global rApp / Flower ServerApp

取代已 deprecated 的 `fl.server.start_server()` 寫法（舊草稿 flower_server.py 已刪除，
compute_global_jfi() 邏輯已搬進本檔案，本檔案是實際運作版本）。用新版
ServerApp + `flwr run` 部署，透過 SuperLink + SuperNode（非 Simulation
Engine，全部 NUM_NODES 個節點是實體分散的 process，不是模擬的虛擬 client）。
Stage 2 起套用於 12-node 拓樸（`FL_NUM_NODES=12`），程式邏輯本身不需為
節點數變動修改——本檔案是舊版 5-node 開發時期寫的，早已用 NUM_NODES 環境
變數泛化，只是這份 docstring 沿用了舊敘述，一併更新避免誤導。

職責：
  - 啟動時嘗試從 Node1 現有 checkpoint 讀取初始權重當作第一輪的種子
    （沒有的話用隨機初始化）——僅影響第一輪的 bootstrap，之後每輪都是
    上一輪真正聚合後的結果。
  - IABFedAvg（FL_MODE=avg，預設值，Stage 2）：標準 FedAvg（依各節點回傳的
    num-examples 加權平均 actor/critic 權重），聚合完成後額外從 MongoDB
    計算全網 Jain's Fairness Index 並記錄到這輪的 metrics——**目前僅供
    監控／log，尚未實作 JFI-guided 的聚合權重調整**（見 CLAUDE.md 第五階段
    待開發項目）。
  - IABClusterFedAvg（FL_MODE=cluster，Stage 3）：Soft/Weighted Clustered
    FedAvg，依每個節點的連續角色比例 ROLE_RATIO 算出 relay/access 兩個原型
    模型後，依各節點自己的比例混合廣播，取代硬性二分群，見 CLAUDE.md 第 3 節。
  - 強制全部 NUM_NODES 個節點參與（min_train_nodes=min_available_nodes=NUM_NODES）。
  - 聚合完成後把最終權重寫回全部 NUM_NODES 個節點的 checkpoint（不只是送出
    initial_arrays 的那個節點），讓各節點的 InferenceServer._reload_worker
    真正撿到「聚合後」的全域模型，而不只是自己聚合前的本地微調結果。
"""

from __future__ import annotations

import os
import sys

# 讓 /app 下的 drl_agent.py 可以被 import（flower-supernode/flower-superlink
# 執行 ServerApp/ClientApp 時的 sys.path 不保證包含 /app，改用環境變數
# PYTHONPATH=/app，見 docker-compose-iab-server.yaml 的 flower-* 服務）
sys.path.insert(0, os.environ.get("APP_ROOT", "/app"))

import numpy as np  # noqa: E402
import pymongo  # noqa: E402
from flwr.app import ArrayRecord, ConfigRecord, Context, MetricRecord  # noqa: E402
from flwr.serverapp import Grid, ServerApp  # noqa: E402
from flwr.serverapp.strategy import FedAvg  # noqa: E402

from drl_agent import DRLAgent  # noqa: E402

NUM_NODES: int = int(os.getenv("FL_NUM_NODES", "12"))  # smoke test 可設 1 跑單節點
JFI_LOOKBACK: int = 100  # 每節點取最近 N 筆 reward 算 JFI 代理值

MONGO_URI: str = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB: str = os.getenv("MONGO_DB", "iab_xapp")

# Stage 3（2026-09-13 設計定案，見 CLAUDE.md 第 3 節）：FL_MODE=avg 是 Stage 2
# 標準 FedAvg（預設值，維持既有行為不變）；FL_MODE=cluster 是 Soft/Weighted
# Clustered FL——兩種聚合邏輯並存，用這個環境變數切換，比照 REWARD_MODE 的既有模式。
FL_MODE: str = os.getenv("FL_MODE", "avg")

# role_ratio_i = 直連 UE 數量 / (直連 UE 數量 + 透過下游 DU 節點間接服務的 UE 數量)。
# 結構性常數，依現行 12-node 拓樸算出，不需即時量測：Node1~3 純 relay（下游都是
# 其他有 DU 的節點）；Node4 混合（直連 UE17 + 經 Node11/12 服務的 UE13~16）；
# Node5~12 純 access（下游都是純 UE）。混合節點的存在正是 Stage 3 從硬性二分群
# 改為連續加權的動機，見 CLAUDE.md 第 3 節 Stage 3 段落。
ROLE_RATIO: dict[int, float] = {
    1: 0.0, 2: 0.0, 3: 0.0,
    4: 0.2,  # UE17 直連(1) / (UE17(1) + 經 Node11,12 服務的 UE13~16(4)) = 1/5
    5: 1.0, 6: 1.0, 7: 1.0, 8: 1.0,
    9: 1.0, 10: 1.0, 11: 1.0, 12: 1.0,
}

app = ServerApp()


def _flatten_state_dicts(actor_sd: dict, critic_sd: dict) -> dict:
    """把 actor/critic 兩個 state_dict 攤平成一個 flat dict（key 加前綴避免衝突）。"""
    flat: dict = {}
    flat.update({f"actor.{k}": v for k, v in actor_sd.items()})
    flat.update({f"critic.{k}": v for k, v in critic_sd.items()})
    return flat


def _split_flat_state_dict(flat: dict) -> tuple[dict, dict]:
    """把攤平、加了前綴的 state_dict 還原成 (actor_sd, critic_sd)。"""
    actor_sd = {k[len("actor."):]: v for k, v in flat.items() if k.startswith("actor.")}
    critic_sd = {k[len("critic."):]: v for k, v in flat.items() if k.startswith("critic.")}
    return actor_sd, critic_sd


def _model_dir_for_node(node_id: int) -> str:
    """各節點 checkpoint 目錄——flower-superlink 容器把全部 NUM_NODES 個節點的
    inference_models_nodeN volume 都掛在 /app/models_node{N}（見 compose）。"""
    return os.environ.get(f"MODEL_DIR_NODE{node_id}", f"/app/models_node{node_id}")


def compute_global_jfi(db: pymongo.database.Database) -> float:
    """計算全網 Jain's Fairness Index（以各節點最近 N 筆 reward 的 mean_reward 代理）。"""
    rewards = []
    for node_id in range(1, NUM_NODES + 1):
        col = db[f"node{node_id}_experiences"]
        try:
            docs = list(
                col.find(
                    {"reward": {"$exists": True}},
                    projection={"reward": 1, "_id": 0},
                )
                .sort("timestamp", pymongo.DESCENDING)
                .limit(JFI_LOOKBACK)
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


def _attach_global_jfi(db: pymongo.database.Database | None, metrics: MetricRecord | None) -> None:
    """算全網 JFI 並寫回這輪的 metrics（Stage 2 IABFedAvg／Stage 3 IABClusterFedAvg 共用，
    目前僅供監控／log，尚未實作 JFI-guided 的聚合權重調整）。"""
    if metrics is None:
        return
    jfi = 0.0
    if db is not None:
        try:
            jfi = compute_global_jfi(db)
        except Exception:
            jfi = 0.0
    metrics["global_jfi"] = jfi


def _weighted_average_flat(items: list[tuple[dict, float]]) -> dict | None:
    """items = [(flat_state_dict, weight), ...]。回傳權重總和 > 0 時的逐 key 加權平均；
    權重總和 <= 0（例如這一側全部節點 num-examples=0）時回傳 None，代表這一側這輪
    沒有新資料可聚合（呼叫端要比照 Stage 2 的 ZeroDivisionError 降級語意處理）。"""
    positive = [(flat, w) for flat, w in items if w > 0]
    total = sum(w for _, w in positive)
    if total <= 0:
        return None
    keys = positive[0][0].keys()
    return {k: sum(flat[k] * w for flat, w in positive) / total for k in keys}


def _apply_weights_to_node(node_id: int, actor_sd: dict, critic_sd: dict) -> None:
    """單節點 checkpoint 安全寫入（Stage 2/3 共用，是廣播邏輯裡風險最高的部分）。

    **關鍵**：先 `agent.load()` 讀回該節點目前的 train_steps／optimizer 狀態，才套用
    聚合後的 actor/critic 權重——若用全新 DRLAgent 直接覆寫存檔，train_steps 會被
    重置為 0，導致 `DRLAgent.load()` 算出的 `is_trained = train_steps > 0` 變成
    False，`_reload_worker` 下次熱重載時會誤判模型「尚未訓練」，整個 InferenceServer
    悄悄退回 BSR 啟發式，等於讓這一輪 FL 聚合的成果完全作廢（見 Stage A smoke test）。
    """
    agent = DRLAgent(node_id=node_id, model_dir=_model_dir_for_node(node_id))
    agent.load()
    agent.actor.load_state_dict(actor_sd)
    agent.critic.load_state_dict(critic_sd)
    agent.save()  # 原子寫入，見 drl_agent.py


class IABFedAvg(FedAvg):
    """標準 FedAvg（依 num-examples 加權平均）+ JFI 監控 log（不影響聚合權重）。"""

    def __init__(self, db: pymongo.database.Database | None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._db = db

    def aggregate_train(self, server_round, replies):
        # 冷啟動邊界情況：MongoDB 剛清空、還沒有任何節點累積到足夠訓練資料
        # 時，全部節點的 num-examples 皆為 0，Flower 內建的
        # aggregate_arrayrecords() 會用 total_weight=sum(weights)=0 做除法
        # 直接 ZeroDivisionError。這不是錯誤，只是「這輪沒有新資料可聚合」，
        # 跳過本輪聚合（回傳 arrays=None，strategy.start() 會自動沿用上一輪
        # 的權重，等同 no-op），不能讓常駐的 flower-scheduler 因此崩潰。
        try:
            arrays, metrics = super().aggregate_train(server_round, replies)
        except ZeroDivisionError:
            print(
                f"[server_app] Round {server_round}: 全部節點 num-examples=0"
                "（尚無足夠訓練資料），跳過本輪聚合"
            )
            arrays, metrics = None, MetricRecord({"num-examples": 0})

        _attach_global_jfi(self._db, metrics)

        return arrays, metrics

    def aggregate_evaluate(self, server_round, replies):
        # 同 aggregate_train() 的冷啟動邊界情況：evaluate 階段全部節點
        # num-examples=0 時（尚無足夠序列可評估）一樣會在 Flower 內建的
        # aggregate_metricrecords() 除以 0，同樣降級為「這輪沒有 metrics」。
        try:
            return super().aggregate_evaluate(server_round, replies)
        except ZeroDivisionError:
            print(
                f"[server_app] Round {server_round}: evaluate 全部節點 "
                "num-examples=0，跳過本輪 evaluate 聚合"
            )
            return None


class IABClusterFedAvg(IABFedAvg):
    """Soft/Weighted Clustered FedAvg（Stage 3，2026-09-13 設計定案，見 CLAUDE.md 第 3 節）。

    不是硬性把節點分兩群各自 FedAvg，而是依每個節點的連續角色比例 role_ratio_i
    （見 ROLE_RATIO）算出 relay/access 兩個「原型」模型，再依每個節點自己的
    role_ratio_i 混合廣播——硬性二分群是 role_ratio_i∈{0,1} 時的特例。

    繼承 IABFedAvg 而非直接繼承 FedAvg：aggregate_evaluate()（跟分群無關的
    診斷用 eval_loss 平均）直接沿用不用重寫；只覆寫 aggregate_train()。

    `self._last_w_relay`/`self._last_w_access` 是這個 strategy 實例跟 main() 之間
    的資料通道——Flower 的 Result 物件只能裝一個 ArrayRecord，裝不下「兩個原型」，
    所以 aggregate_train() 算完直接存在 self 上，main() 在 strategy.start() 結束後
    直接讀這兩個屬性做混合廣播，不透過 aggregate_train() 的回傳值。
    """

    def __init__(self, db: pymongo.database.Database | None, **kwargs) -> None:
        super().__init__(db=db, **kwargs)
        self._last_w_relay: dict | None = None
        self._last_w_access: dict | None = None

    def aggregate_train(self, server_round, replies):
        replies = list(replies)  # 要走訪計算 relay/access 兩組加權，必須先物化成 list

        relay_items: list[tuple[dict, float]] = []
        access_items: list[tuple[dict, float]] = []
        total_num_examples = 0

        for msg in replies:
            metrics = msg.content["metrics"]
            node_id = int(metrics["node_id"])
            num_examples = float(metrics["num-examples"])
            total_num_examples += int(num_examples)
            role = ROLE_RATIO.get(node_id, 1.0)
            flat = msg.content["arrays"].to_torch_state_dict()
            relay_items.append((flat, (1.0 - role) * num_examples))
            access_items.append((flat, role * num_examples))

        w_relay = _weighted_average_flat(relay_items) if relay_items else None
        w_access = _weighted_average_flat(access_items) if access_items else None

        if w_relay is not None:
            self._last_w_relay = w_relay
        if w_access is not None:
            self._last_w_access = w_access

        if w_relay is None and w_access is None:
            print(
                f"[server_app] Round {server_round}: 全部節點 num-examples=0"
                "（尚無足夠訓練資料），跳過本輪聚合（cluster 模式）"
            )
            arrays, metrics_out = None, MetricRecord({"num-examples": 0})
        else:
            # 這個 arrays 只是滿足 Flower 內部 Result.arrays 非空判斷用，main()
            # 廣播時看的是 self._last_w_relay/_last_w_access，不是這個回傳值。
            arrays = ArrayRecord(w_relay if w_relay is not None else w_access)
            metrics_out = MetricRecord({"num-examples": total_num_examples})

        _attach_global_jfi(self._db, metrics_out)

        return arrays, metrics_out


def _broadcast_cluster_weights(w_relay: dict | None, w_access: dict | None) -> None:
    """把 relay/access 兩個原型依每個節點的 role_ratio_i 混合後寫回各自 checkpoint。

    W_i(廣播) = (1-role_ratio_i)·W_relay + role_ratio_i·W_access

    任一原型為 None（該側這輪沒有新資料，見 _weighted_average_flat）時整段退化用
    另一個原型；兩者皆 None 時全部節點這輪都不更新（等同 Stage 2 no-op 語意）。
    """
    if w_relay is None and w_access is None:
        print("[server_app] 本次執行沒有任何一輪成功聚合（cluster 模式，全程無訓練資料），跳過廣播")
        return

    for node_id in range(1, NUM_NODES + 1):
        role = ROLE_RATIO.get(node_id, 1.0)
        try:
            if w_relay is None:
                mixed = w_access
            elif w_access is None:
                mixed = w_relay
            else:
                mixed = {k: (1.0 - role) * w_relay[k] + role * w_access[k] for k in w_relay}
            actor_sd, critic_sd = _split_flat_state_dict(mixed)
            _apply_weights_to_node(node_id, actor_sd, critic_sd)
        except Exception as exc:
            print(f"[server_app] 廣播聚合權重至 Node{node_id} 失敗（cluster 模式）: {exc}")


def _seed_initial_arrays() -> ArrayRecord:
    """啟動時嘗試載入 Node1 現有 checkpoint 當作第一輪的初始權重種子；沒有則隨機初始化。"""
    agent = DRLAgent(node_id=1, model_dir=_model_dir_for_node(1))
    agent.load()  # 找不到檔案時回傳 False，維持隨機初始化，不例外
    flat = _flatten_state_dicts(agent.actor.state_dict(), agent.critic.state_dict())
    return ArrayRecord(flat)


def _broadcast_aggregated_weights(arrays: ArrayRecord) -> None:
    """把聚合後的全域權重寫回全部 NUM_NODES 個節點的 checkpoint。

    只把 initial_arrays 種子存回 Node1、或只讓各節點各自保留自己聚合前
    ClientApp.train() 存的本地微調結果，都不是真正的聯邦學習——FedAvg
    產出的「平均後」模型必須讓全部節點都撿到，否則 Global rApp 聚合
    等於沒有效果。單一節點寫入失敗不中斷整體流程，下一輪 FL 會再試一次。

    **關鍵**：每個節點先 `agent.load()` 讀回自己目前的 train_steps／
    optimizer 狀態，才套用聚合後的 actor/critic 權重（Stage A smoke test
    實測發現：若用全新 DRLAgent 直接覆寫存檔，train_steps 會被重置為 0，
    導致 `DRLAgent.load()` 算出的 `is_trained = train_steps > 0` 變成
    False——近即時 process 的 `_reload_worker` 下次熱重載時會誤判模型
    「尚未訓練」，整個 InferenceServer 悄悄退回 BSR 啟發式，等於讓這一輪
    FL 聚合的成果完全作廢）。
    """
    flat = arrays.to_torch_state_dict()
    actor_sd, critic_sd = _split_flat_state_dict(flat)
    for node_id in range(1, NUM_NODES + 1):
        try:
            _apply_weights_to_node(node_id, actor_sd, critic_sd)
        except Exception as exc:
            print(f"[server_app] 廣播聚合權重至 Node{node_id} 失敗: {exc}")


@app.main()
def main(grid: Grid, context: Context) -> None:
    """ServerApp 進入點：跑 num-server-rounds 輪 FedAvg，結束後廣播聚合結果。"""
    num_rounds: int = int(context.run_config["num-server-rounds"])
    local_epochs: int = int(context.run_config["local-epochs"])

    db = None
    try:
        client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        client.server_info()
        db = client[MONGO_DB]
    except pymongo.errors.PyMongoError:
        db = None

    strategy_cls = IABClusterFedAvg if FL_MODE == "cluster" else IABFedAvg
    strategy = strategy_cls(
        db=db,
        fraction_train=1.0,
        fraction_evaluate=1.0,
        min_train_nodes=NUM_NODES,
        min_evaluate_nodes=NUM_NODES,
        min_available_nodes=NUM_NODES,
    )
    print(f"[server_app] FL_MODE={FL_MODE}（strategy={strategy_cls.__name__}）")

    result = strategy.start(
        grid=grid,
        initial_arrays=_seed_initial_arrays(),
        train_config=ConfigRecord({"local-epochs": local_epochs}),
        num_rounds=num_rounds,
    )

    # 冷啟動邊界情況（見 IABFedAvg.aggregate_train() 的說明）：若每一輪都
    # 因為全部節點 num-examples=0 而跳過聚合，result.arrays 會是空的
    # ArrayRecord（Strategy.start() 只在 aggregate_train 回傳非 None 時才會
    # 寫入 result.arrays，从未寫入時維持預設空值）——這種情況下不能廣播，
    # 否則每個節點的 agent.actor.load_state_dict() 會因缺 key 直接拋例外
    # （已在真實驗證中發生過一次）。沒有任何一輪真正聚合成功，代表這次
    # `flwr run` 完全沒有新東西可以分享，維持各節點目前的權重不變即可。
    if FL_MODE == "cluster":
        # cluster 模式的 result.arrays 只是滿足 Flower 內部非空判斷用（見
        # IABClusterFedAvg.aggregate_train()），真正廣播看的是 strategy 實例上
        # 存的兩個原型，不是 result.arrays。
        _broadcast_cluster_weights(strategy._last_w_relay, strategy._last_w_access)
    elif result.arrays:
        _broadcast_aggregated_weights(result.arrays)
    else:
        print("[server_app] 本次執行沒有任何一輪成功聚合（全程無訓練資料），跳過廣播")
