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
  - IABCapaFedAvg（FL_MODE=capa，新 Stage 3）／IABElasticFedAvg（FL_MODE=elastic，
    新 Stage 4）：見各自 class docstring 與 inference/STAGE4_CUSTOM_FL_DESIGN.md
    §10／§11。舊版 IABClusterFedAvg（cluster FL）與 IABCustomFedAvg（AW-FedAvg）
    已於 2026-09-29 路線圖重新定案後整段移除（含程式碼與封存資料），不是需要
    復原的架構，過程見 HISTORY.md。
  - 強制全部 NUM_NODES 個節點參與（min_train_nodes=min_available_nodes=NUM_NODES）。
  - 聚合完成後把最終權重寫回全部 NUM_NODES 個節點的 checkpoint（不只是送出
    initial_arrays 的那個節點），讓各節點的 InferenceServer._reload_worker
    真正撿到「聚合後」的全域模型，而不只是自己聚合前的本地微調結果。
"""

from __future__ import annotations

import math
import os
import sys

# 讓 /app 下的 drl_agent.py 可以被 import（flower-supernode/flower-superlink
# 執行 ServerApp/ClientApp 時的 sys.path 不保證包含 /app，改用環境變數
# PYTHONPATH=/app，見 docker-compose-iab-server.yaml 的 flower-* 服務）
sys.path.insert(0, os.environ.get("APP_ROOT", "/app"))

import numpy as np  # noqa: E402
import pymongo  # noqa: E402
import torch  # noqa: E402
from flwr.app import ArrayRecord, ConfigRecord, Context, MetricRecord  # noqa: E402
from flwr.serverapp import Grid, ServerApp  # noqa: E402
from flwr.serverapp.strategy import FedAvg  # noqa: E402

from drl_agent import DRLAgent  # noqa: E402

NUM_NODES: int = int(os.getenv("FL_NUM_NODES", "12"))  # smoke test 可設 1 跑單節點
JFI_LOOKBACK: int = 100  # 每節點取最近 N 筆 reward 算 JFI 代理值

MONGO_URI: str = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB: str = os.getenv("MONGO_DB", "iab_xapp")

# FL_MODE=avg 是 Stage 2 標準 FedAvg（預設值）；FL_MODE=capa／elastic 分別是新
# Stage 3（CAPA-Fed）／新 Stage 4（ERA-Fed），見下方各自的常數區塊與 class docstring。
FL_MODE: str = os.getenv("FL_MODE", "avg")

# ── 新 Stage 3（2026-09-28 路線圖重新定案，CAPA-Fed：Critic-Aggregated,
#    Personalized-Actor Federation，見 inference/STAGE4_CUSTOM_FL_DESIGN.md §10）──
#
# 取代舊版 soft/weighted cluster FL（IABClusterFedAvg，2026-09-29 已整段移除，含
# 封存資料，過程見 HISTORY.md）。核心構想：Actor 與 Critic 用不同方式聚合——Critic
# （價值估計）全域池化、同一份廣播給全部節點（沿用已驗證穩定的動量+學習率機制防
# 過衝）；Actor（PRB 分配策略）不整份覆寫，改成每個節點依自己這輪的訓練樣本數
# n_i（信心訊號）在「自己的本地權重」與「全體加權平均」之間做個人化混合，資料越
# 充足的節點越信任自己、資料越稀疏的節點（尤其 relay）越依賴集體共識。

# Actor 個人化混合係數 β_i 的下界／上界（clip 範圍）：β_min 確保資料再多也保留一點
# 集體知識注入，β_max 確保資料再少也保留一點本地身份，不會完全退化成純全域廣播。
CAPA_BETA_MIN: float = float(os.getenv("CAPA_BETA_MIN", "0.1"))
CAPA_BETA_MAX: float = float(os.getenv("CAPA_BETA_MAX", "0.9"))

# Critic 路徑沿用 AW-FedAvg v2 已通過 180 輪延長合成測試驗證過的動量+學習率參數
# （同一個「伺服器端聚合如何避免過衝」的問題、同一組已驗證的解法，不重新調參）。
CAPA_CRITIC_MOMENTUM: float = float(os.getenv("CAPA_CRITIC_MOMENTUM", "0.7"))
CAPA_CRITIC_LR: float = float(os.getenv("CAPA_CRITIC_LR", "0.3"))

# ── 新 Stage 4（2026-09-28 路線圖重新定案，ERA-Fed：Elastic Role-Aware Federation，
#    見 inference/STAGE4_CUSTOM_FL_DESIGN.md §11）────────────────────────────────
#
# 在 CAPA-Fed 的結構上修正一個殘留問題：CAPA-Fed 的 Critic 路徑用動量+學習率只是把
# 「收斂到群體共識」這件事在時間上拖慢，訓練輪數夠多最終還是會完全收斂、不會永久
# 保留本地身份，跟 Actor 路徑「永遠保留 (1-β_i) 比例本地權重」性質不同。ERA-Fed 借鑑
# Elastic Averaging SGD（Zhang, Choromanska, LeCun, NeurIPS 2015）的部分拉扯機制，
# 把 Critic 也改成跟 Actor 同一種「彈性拉扯」（不是動量）：每輪只朝群體平均的方向
# 移動一小步（彈性係數 ρ_i），節點自己在 checkpoint 裡累積的權重不會被整份覆寫，
# Critic 的拉力係數用一個 >1 的倍率放大、確實比 Actor 更快同步，但兩者都永遠保留
# 一部分本地身份，沒有任何子網路最終被完全覆寫。

# 基礎彈性係數 ρ_i 的下界／上界（clip 範圍），語意與 CAPA_BETA_MIN/MAX 相同（沿用
# 同一套「信心加權」構想，只是這裡套用在彈性拉扯框架而非直接混合）。
ERA_RHO_MIN: float = float(os.getenv("ERA_RHO_MIN", "0.1"))
ERA_RHO_MAX: float = float(os.getenv("ERA_RHO_MAX", "0.9"))

# Critic 拉力放大倍率 κ：ρ_i^critic = clip(ρ_i·κ, 0, 1)，κ>1 讓 Critic 比 Actor 更快
# 朝群體共識移動（呼應「Critic 該較快同步知識、Actor 該保留更多本地特化」的設計動機），
# 但 clip 上限 1 之下仍是「當輪」彈性拉扯、不是「永久」覆寫——下一輪 θ_{i,own} 又會
# 重新累積本地訓練，不像 CAPA-Fed 的動量會讓 Critic 最終完全趨同。
ERA_CRITIC_KAPPA: float = float(os.getenv("ERA_CRITIC_KAPPA", "2.5"))

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


class IABCapaFedAvg(IABFedAvg):
    """CAPA-Fed（新 Stage 3，2026-09-28 路線圖重新定案，取代舊版 soft/weighted cluster FL，
    見 inference/STAGE4_CUSTOM_FL_DESIGN.md §10）。

    Actor 與 Critic 分軌處理：
      - Critic（價值估計）：全域池化，標準樣本數加權平均後套用 AW-FedAvg v2 已驗證穩定的
        動量+學習率機制防過衝，同一份廣播給全部節點（見 CAPA_CRITIC_MOMENTUM/CAPA_CRITIC_LR）。
      - Actor（PRB 分配策略）：不整份覆寫，逐節點依這輪訓練樣本數 n_i 在「自己的本地權重」與
        「全體加權平均」之間做信心加權個人化混合（見 CAPA_BETA_MIN/MAX）。

    跟 IABClusterFedAvg 一樣需要「每個節點拿到不同權重」，但軸線不同（這裡是逐節點連續的
    β_i，不是 relay/access 兩個離散原型）——用 self._last_actor_per_node（dict[node_id,
    actor_sd]）+ self._last_critic（單一 dict，全體共用）當 main() 廣播時的資料通道，
    因為 Flower 的 Result 物件只能裝一個 ArrayRecord，裝不下「12 份不同的 Actor」。
    """

    def __init__(self, db: pymongo.database.Database | None, **kwargs) -> None:
        super().__init__(db=db, **kwargs)
        self._last_actor_per_node: dict[int, dict] = {}
        self._last_critic: dict | None = None

    def aggregate_train(self, server_round, replies):
        replies = list(replies)

        entries: list[tuple[int, dict, dict, float]] = []  # (node_id, actor_sd, critic_sd, n_i)
        total_num_examples = 0
        for msg in replies:
            metrics = msg.content["metrics"]
            node_id = int(metrics["node_id"])
            num_examples = float(metrics["num-examples"])
            total_num_examples += int(num_examples)
            flat = msg.content["arrays"].to_torch_state_dict()
            actor_sd, critic_sd = _split_flat_state_dict(flat)
            entries.append((node_id, actor_sd, critic_sd, num_examples))

        if not entries or total_num_examples <= 0:
            print(
                f"[server_app] Round {server_round}: 全部節點 num-examples=0"
                "（尚無足夠訓練資料），跳過本輪聚合（capa 模式）"
            )
            arrays, metrics_out = None, MetricRecord({"num-examples": 0})
            _attach_global_jfi(self._db, metrics_out)
            return arrays, metrics_out

        # ── Critic：全域池化（標準加權平均）＋ 動量+學習率防過衝，同一份廣播給全部節點 ──
        critic_items = [(critic_sd, n_i) for _, _, critic_sd, n_i in entries]
        critic_avg = _weighted_average_flat(critic_items)
        state = _load_capa_state(_capa_state_path()) or {}
        v_prev_critic: dict = state.get("critic_momentum") or {}
        theta_prev_critic = self._last_critic
        if theta_prev_critic is None:
            _, theta_prev_critic = _split_flat_state_dict(_load_current_global_flat())
        v_new_critic: dict = {}
        theta_new_critic: dict = {}
        for k in critic_avg:
            prev_v = v_prev_critic.get(k, torch.zeros_like(critic_avg[k]))
            delta = critic_avg[k] - theta_prev_critic[k]
            v_new_critic[k] = CAPA_CRITIC_MOMENTUM * prev_v + (1.0 - CAPA_CRITIC_MOMENTUM) * delta
            theta_new_critic[k] = theta_prev_critic[k] + CAPA_CRITIC_LR * v_new_critic[k]
        _save_capa_state(_capa_state_path(), {"critic_momentum": v_new_critic})
        self._last_critic = theta_new_critic

        # ── Actor：逐節點信心加權個人化混合（不整份覆寫）──
        actor_items = [(actor_sd, n_i) for _, actor_sd, _, n_i in entries]
        actor_avg = _weighted_average_flat(actor_items)
        max_n = max(n_i for _, _, _, n_i in entries)
        for node_id, actor_sd_own, _, n_i in entries:
            beta_i = min(max(1.0 - n_i / (max_n + 1e-9), CAPA_BETA_MIN), CAPA_BETA_MAX)
            mixed_actor = {
                k: (1.0 - beta_i) * actor_sd_own[k] + beta_i * actor_avg[k] for k in actor_avg
            }
            self._last_actor_per_node[node_id] = mixed_actor

        print(
            f"[server_app] Round {server_round}: CAPA-Fed critic_momentum={CAPA_CRITIC_MOMENTUM} "
            f"critic_lr={CAPA_CRITIC_LR} num_examples_total={total_num_examples} "
            f"beta_range=[{CAPA_BETA_MIN},{CAPA_BETA_MAX}]"
        )

        # 只是滿足 Flower 內部 Result.arrays 非空判斷用，main() 真正廣播看的是
        # self._last_actor_per_node/_last_critic，不是這個回傳值（同 IABClusterFedAvg 模式）。
        arrays = ArrayRecord(_flatten_state_dicts(actor_avg, theta_new_critic))
        metrics_out = MetricRecord({"num-examples": total_num_examples})
        _attach_global_jfi(self._db, metrics_out)
        return arrays, metrics_out


class IABElasticFedAvg(IABFedAvg):
    """ERA-Fed（新 Stage 4，2026-09-28 路線圖重新定案，見
    inference/STAGE4_CUSTOM_FL_DESIGN.md §11）。在 IABCapaFedAvg 的結構上修正一個殘留問題：
    CAPA-Fed 的 Critic 路徑用動量+學習率，訓練輪數夠多最終仍會完全收斂到群體共識，跟 Actor
    路徑「永遠保留一部分本地身份」的性質不同。ERA-Fed 把 Critic 也改成跟 Actor 同一種彈性
    拉扯（Elastic Averaging SGD 精神，Zhang, Choromanska, LeCun, NeurIPS 2015）：兩者都是
    「朝群體平均移動一小步，不整份覆寫」，差別只在 Critic 的拉力係數用 ERA_CRITIC_KAPPA
    放大、確實比 Actor 更快同步。

    因此不需要 IABCapaFedAvg 的動量狀態檔（_capa_state_path）——「記憶」直接就是每個節點
    自己的 checkpoint 本身，彈性拉扯後存回，天然跨輪持久化，不需要額外的磁碟狀態。Actor 與
    Critic 都是逐節點個人化（跟 CAPA-Fed 的「Critic 單一全域共用」不同），用
    self._last_actor_per_node + self._last_critic_per_node 兩個 dict[node_id, sd] 當
    main() 廣播時的資料通道。
    """

    def __init__(self, db: pymongo.database.Database | None, **kwargs) -> None:
        super().__init__(db=db, **kwargs)
        self._last_actor_per_node: dict[int, dict] = {}
        self._last_critic_per_node: dict[int, dict] = {}

    def aggregate_train(self, server_round, replies):
        replies = list(replies)

        entries: list[tuple[int, dict, dict, float]] = []  # (node_id, actor_sd, critic_sd, n_i)
        total_num_examples = 0
        for msg in replies:
            metrics = msg.content["metrics"]
            node_id = int(metrics["node_id"])
            num_examples = float(metrics["num-examples"])
            total_num_examples += int(num_examples)
            flat = msg.content["arrays"].to_torch_state_dict()
            actor_sd, critic_sd = _split_flat_state_dict(flat)
            entries.append((node_id, actor_sd, critic_sd, num_examples))

        if not entries or total_num_examples <= 0:
            print(
                f"[server_app] Round {server_round}: 全部節點 num-examples=0"
                "（尚無足夠訓練資料），跳過本輪聚合（elastic 模式）"
            )
            arrays, metrics_out = None, MetricRecord({"num-examples": 0})
            _attach_global_jfi(self._db, metrics_out)
            return arrays, metrics_out

        actor_avg = _weighted_average_flat([(a, n) for _, a, _, n in entries])
        critic_avg = _weighted_average_flat([(c, n) for _, _, c, n in entries])
        max_n = max(n_i for _, _, _, n_i in entries)

        for node_id, actor_sd_own, critic_sd_own, n_i in entries:
            rho_i = min(
                max(ERA_RHO_MIN + (ERA_RHO_MAX - ERA_RHO_MIN) * (1.0 - n_i / (max_n + 1e-9)), ERA_RHO_MIN),
                ERA_RHO_MAX,
            )
            # Critic 拉力係數用 κ>1 放大後可能超過 1，另外 clip 到 [0,1]（必要，不是選配）。
            rho_i_critic = min(max(rho_i * ERA_CRITIC_KAPPA, 0.0), 1.0)

            mixed_actor = {
                k: (1.0 - rho_i) * actor_sd_own[k] + rho_i * actor_avg[k] for k in actor_avg
            }
            mixed_critic = {
                k: (1.0 - rho_i_critic) * critic_sd_own[k] + rho_i_critic * critic_avg[k]
                for k in critic_avg
            }
            self._last_actor_per_node[node_id] = mixed_actor
            self._last_critic_per_node[node_id] = mixed_critic

        print(
            f"[server_app] Round {server_round}: ERA-Fed rho_range=[{ERA_RHO_MIN},{ERA_RHO_MAX}] "
            f"critic_kappa={ERA_CRITIC_KAPPA} num_examples_total={total_num_examples}"
        )

        arrays = ArrayRecord(_flatten_state_dicts(actor_avg, critic_avg))
        metrics_out = MetricRecord({"num-examples": total_num_examples})
        _attach_global_jfi(self._db, metrics_out)
        return arrays, metrics_out


def _broadcast_capa_weights(strategy: "IABCapaFedAvg") -> None:
    """把 IABCapaFedAvg 這輪算出的「逐節點個人化 Actor」＋「全域共用 Critic」寫回各自 checkpoint。"""
    if not strategy._last_actor_per_node or strategy._last_critic is None:
        print("[server_app] 本次執行沒有任何一輪成功聚合（capa 模式，全程無訓練資料），跳過廣播")
        return
    for node_id in range(1, NUM_NODES + 1):
        actor_sd = strategy._last_actor_per_node.get(node_id)
        if actor_sd is None:
            continue
        try:
            _apply_weights_to_node(node_id, actor_sd, strategy._last_critic)
        except Exception as exc:
            print(f"[server_app] 廣播聚合權重至 Node{node_id} 失敗（capa 模式）: {exc}")


def _broadcast_elastic_weights(strategy: "IABElasticFedAvg") -> None:
    """把 IABElasticFedAvg 這輪算出的「逐節點個人化 Actor＋Critic」寫回各自 checkpoint。"""
    if not strategy._last_actor_per_node:
        print("[server_app] 本次執行沒有任何一輪成功聚合（elastic 模式，全程無訓練資料），跳過廣播")
        return
    for node_id in range(1, NUM_NODES + 1):
        actor_sd = strategy._last_actor_per_node.get(node_id)
        critic_sd = strategy._last_critic_per_node.get(node_id)
        if actor_sd is None or critic_sd is None:
            continue
        try:
            _apply_weights_to_node(node_id, actor_sd, critic_sd)
        except Exception as exc:
            print(f"[server_app] 廣播聚合權重至 Node{node_id} 失敗（elastic 模式）: {exc}")


def _capa_state_path() -> str:
    """CAPA-Fed（新 Stage 3）的跨輪次狀態檔路徑（只需要 Critic 的動量緩衝——Actor 是逐節點
    個人化混合，不需要動量；「記憶」就是每個節點自己的 checkpoint，見 IABCapaFedAvg）。"""
    return os.path.join(_model_dir_for_node(1), "capa_fed_state.pt")


def _load_capa_state(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    try:
        return torch.load(path, map_location="cpu")
    except Exception as exc:
        print(f"[server_app] 讀取 CAPA-Fed 狀態失敗（視為冷啟動）: {exc}")
        return None


def _save_capa_state(path: str, state: dict) -> None:
    tmp = path + ".tmp"
    try:
        torch.save(state, tmp)
        os.replace(tmp, path)
    except Exception as exc:
        print(f"[server_app] 儲存 CAPA-Fed 狀態失敗: {exc}")


def _load_current_global_flat() -> dict:
    """讀取「目前的全域模型」（用 Node1 checkpoint 代表）。Stage 2 廣播後全部節點權重相同，
    Node1 的 checkpoint 可以代表目前的全域狀態。找不到檔案時回傳隨機初始化的權重，不例外
    （冷啟動）。"""
    agent = DRLAgent(node_id=1, model_dir=_model_dir_for_node(1))
    agent.load()
    return _flatten_state_dicts(agent.actor.state_dict(), agent.critic.state_dict())


def _seed_initial_arrays() -> ArrayRecord:
    """啟動時嘗試載入 Node1 現有 checkpoint 當作第一輪的初始權重種子；沒有則隨機初始化。"""
    return ArrayRecord(_load_current_global_flat())


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

    if FL_MODE == "capa":
        strategy_cls = IABCapaFedAvg  # 新 Stage 3
    elif FL_MODE == "elastic":
        strategy_cls = IABElasticFedAvg  # 新 Stage 4
    else:
        strategy_cls = IABFedAvg
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
    if FL_MODE == "capa":
        # capa 模式同理：result.arrays 只是滿足非空判斷，真正廣播看的是
        # strategy._last_actor_per_node（逐節點個人化）+ strategy._last_critic（全域共用）。
        _broadcast_capa_weights(strategy)
    elif FL_MODE == "elastic":
        # elastic 模式同理：Actor/Critic 皆逐節點個人化，看
        # strategy._last_actor_per_node + strategy._last_critic_per_node。
        _broadcast_elastic_weights(strategy)
    elif result.arrays:
        _broadcast_aggregated_weights(result.arrays)
    else:
        print("[server_app] 本次執行沒有任何一輪成功聚合（全程無訓練資料），跳過廣播")
