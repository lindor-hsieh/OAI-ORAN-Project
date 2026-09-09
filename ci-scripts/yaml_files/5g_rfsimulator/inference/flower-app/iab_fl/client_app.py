"""
client_app.py — Phase 5 Local rApp → Flower ClientApp

取代已 deprecated 的 `fl.client.start_numpy_client()` 寫法。用新版
ClientApp + `flower-supernode` 部署。**與 inference_server.py 是不同的
OS process**（flower-supernode 預設以 subprocess 隔離執行 ClientApp）——
彼此不共用 Python 記憶體中的 DRLAgent 實例，只透過同一份磁碟 checkpoint
（model_node{N}.pt，經 volume 掛載共用）同步：本檔案負責寫入，
InferenceServer 的背景執行緒 `_reload_worker` 負責偵測 mtime 變化並讀取。

@app.train()：
  1. `agent.load()` 讀回本節點目前的 optimizer/train_steps 狀態
     （若尚無檔案則維持隨機初始化）。
  2. 套用這一輪收到的全域權重（只覆蓋 actor/critic 權重，optimizer
     狀態保留，讓本地 Adam 動量不因每輪 FL 而重置）。
  3. **立刻存檔一次**：即使底下的本地微調因資料不足被跳過，近即時
     process 也能盡快撿到這一輪聚合後的全域權重。
  4. 呼叫共用的 training_pipeline.run_training_round() 在本地經驗上
     微調；若成功則再存一次檔並回傳更新後權重 + num-examples；
     資料不足則回傳「剛收到、未修改」的權重、num-examples=0——該節點
     仍會回覆以滿足 min_train_nodes，但這輪聚合權重貢獻為 0。
  MongoDB／訓練任一環節失敗都降級為「回傳未修改權重、num-examples=0」，
  不拋例外阻塞這一輪 FL（比照 inference_server.py 的 MongoDB 降級模式）。
"""

from __future__ import annotations

import logging
import os
import sys

# 讓 /app 下的 drl_agent.py / training_pipeline.py 可以被 import
# （見 docker-compose-iab-server.yaml 的 flower-supernode-nodeN 服務 PYTHONPATH=/app）
sys.path.insert(0, os.environ.get("APP_ROOT", "/app"))

import pymongo  # noqa: E402
from flwr.app import ArrayRecord, Context, Message, MetricRecord, RecordDict  # noqa: E402
from flwr.clientapp import ClientApp  # noqa: E402

from drl_agent import DRLAgent  # noqa: E402
from training_pipeline import fetch_sequences, run_training_round  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="[FL-Client] %(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("flower_client_app")

NODE_ID: int = int(os.environ["NODE_ID"])
MONGO_URI: str = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB: str = os.getenv("MONGO_DB", "iab_xapp")
MODEL_DIR: str = os.getenv("MODEL_DIR", "/app/models")
# 序列化評估（GRU）比舊版打散抽樣需要多得多的原始經驗才能湊到
# evaluate_on_batch() 要求的 TRAIN_SEQ_COUNT 個序列（見 drl_agent.py），
# 200 筆對序列窗口（stride=TRAIN_SEQ_LEN）來說太小，改對齊
# training_pipeline.TRAIN_FETCH_LIMIT 的量級。
EVAL_FETCH_LIMIT: int = 2000

app = ClientApp()


def _split_flat_state_dict(flat: dict) -> tuple[dict, dict]:
    """把攤平、加了前綴的 state_dict 還原成 (actor_sd, critic_sd)。"""
    actor_sd = {k[len("actor."):]: v for k, v in flat.items() if k.startswith("actor.")}
    critic_sd = {k[len("critic."):]: v for k, v in flat.items() if k.startswith("critic.")}
    return actor_sd, critic_sd


def _flatten_agent(agent: DRLAgent) -> dict:
    flat: dict = {}
    flat.update({f"actor.{k}": v for k, v in agent.actor.state_dict().items()})
    flat.update({f"critic.{k}": v for k, v in agent.critic.state_dict().items()})
    return flat


def _connect_mongo_col():
    try:
        client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        client.server_info()
        return client[MONGO_DB][f"node{NODE_ID}_experiences"]
    except pymongo.errors.PyMongoError as exc:
        log.warning("MongoDB 連線失敗: %s", exc)
        return None


@app.train()
def train(msg: Message, context: Context) -> Message:
    agent = DRLAgent(node_id=NODE_ID, model_dir=MODEL_DIR)
    agent.load()  # 讀回本節點目前的 optimizer/train_steps 狀態

    # 套用這一輪的全域權重（只覆蓋 actor/critic，optimizer 狀態保留）
    actor_sd, critic_sd = _split_flat_state_dict(
        msg.content["arrays"].to_torch_state_dict()
    )
    agent.actor.load_state_dict(actor_sd)
    agent.critic.load_state_dict(critic_sd)

    try:
        agent.save()
    except Exception as exc:
        log.warning("儲存全域權重失敗: %s", exc)

    mongo_col = _connect_mongo_col()
    local_epochs = int(context.run_config.get("local-epochs", 10))

    metrics: dict = {}
    if mongo_col is not None:
        try:
            metrics = run_training_round(agent, mongo_col, epochs=local_epochs, log=log)
        except Exception as exc:
            log.warning("本地微調失敗: %s", exc)
            metrics = {}

    if metrics:
        try:
            agent.save()
        except Exception as exc:
            log.warning("儲存本地微調後權重失敗: %s", exc)
        num_examples = metrics.get("n_train_seq", 0)
    else:
        num_examples = 0

    reply_arrays = ArrayRecord(_flatten_agent(agent))
    reply_metrics = MetricRecord(
        {
            "num-examples": num_examples,
            "mean_reward": float(metrics.get("mean_reward", 0.0)),
        }
    )
    content = RecordDict({"arrays": reply_arrays, "metrics": reply_metrics})
    return Message(content=content, reply_to=msg)


@app.evaluate()
def evaluate(msg: Message, context: Context) -> Message:
    agent = DRLAgent(node_id=NODE_ID, model_dir=MODEL_DIR)
    actor_sd, critic_sd = _split_flat_state_dict(
        msg.content["arrays"].to_torch_state_dict()
    )
    agent.actor.load_state_dict(actor_sd)
    agent.critic.load_state_dict(critic_sd)

    mongo_col = _connect_mongo_col()
    num_examples = 0
    eval_metrics: dict = {}
    if mongo_col is not None:
        try:
            # 改用共用的 fetch_sequences()，不自己維護一份查詢/切窗邏輯——
            # GRU 需要時間連續的序列，不是打散抽樣的獨立經驗，跟
            # training_pipeline.run_training_round() 用的是同一套規則。
            sequences, _ = fetch_sequences(mongo_col, fetch_limit=EVAL_FETCH_LIMIT, log=log)
            if sequences:
                eval_metrics = agent.evaluate_on_batch(sequences)
                if eval_metrics:
                    num_examples = len(sequences)
        except Exception as exc:
            log.warning("評估失敗: %s", exc)

    reply_metrics = MetricRecord(
        {
            "num-examples": num_examples,
            "eval_loss": float(eval_metrics.get("test_actor_loss", 0.0)),
        }
    )
    content = RecordDict({"metrics": reply_metrics})
    return Message(content=content, reply_to=msg)
