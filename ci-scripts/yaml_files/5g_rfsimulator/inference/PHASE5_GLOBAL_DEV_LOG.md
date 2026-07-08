# Phase 5：Global xApp 與 Global rApp（Flower Server）開發紀錄

> 涵蓋期間：2026-07-01 ～ 2026-07-06
> 範圍：`Global xApp`（IAB 回傳配額軟性約束）＋ `Global rApp / Flower Server`（階層式聯邦學習）
> 對應 CLAUDE.md 第五階段「開發項目」的前兩項；`Local rApp → Flower Client` 一併記錄，因為跟
> Global rApp 是同一套 Flower ServerApp/ClientApp 架構的兩端，開發過程綁在一起。

---

## 1. 起點：CLAUDE.md 原始設計 vs. 實際發現的落差

開發開始前，CLAUDE.md 第 2 節對 Global xApp／Global rApp 的描述是概念性的：

- **Global xApp**：獨立 Python 容器，10ms 全域迴圈，監控 Node1/2 的 PRB 分配、下發回傳配額給
  Node3/4/5，用 `effective_prb = min(106, quota)` 模擬 in-band IAB 回傳瓶頸。
- **Global rApp / Flower Server**：小時級 Non-RT 迴圈，強制 5 個節點參與聚合，用 Jain's Fairness
  Index 為目標做聯邦學習。

實際開發時發現兩個關鍵落差，直接決定了後續的技術路線：

1. **Global rApp 的舊草稿用了已被官方棄用的 API**（`fl.server.start_server()`／
   `fl.client.start_numpy_client()`，`flwr` 1.28.0 標記為 deprecated），必須改用新版
   `ServerApp`/`ClientApp` + `flwr run` 架構重寫。
2. **Global xApp 早在這次開發之前，就已經有一份沒進 git 版本控制的實作**存在於 Docker image
   裡（2026-06-17 build），用的是直接內建在 `inference_server.py` 的 ZMQ PUB/SUB，而不是後來
   委託開發的獨立輪詢 process；但這份實作有 port 沒接上的 bug，從沒真的跑通過。

以下依開發先後順序記錄。

---

## 2. Global rApp / Flower Server：從舊 API 草稿到現版架構

### 2.1 技術選型：為什麼不能沿用舊草稿

專案裡原本已有 `flower_server.py`（`fl.server.start_server()` + 自訂 `FedAvg` 子類別）與
`drl_agent.py` 裡的 `get_parameters()`/`set_parameters()` 搭配 `flower_client.py`（`fl.client.
start_numpy_client()`）。開發時直接去讀 `~/flower`（vendor 進來之前，先用官方原始碼確認 API
現況）發現：

```python
# flwr/compat/server/app.py
from ..compat.server.app import start_server as start_server  # Deprecated
# flwr/compat/client/app.py
from ..compat.client.app import start_numpy_client as start_numpy_client  # Deprecated
```

官方目前建議架構是 `ServerApp`/`ClientApp` + `flwr run`，透過 **SuperLink + SuperNode** 部署
（Deployment Engine），不是 Simulation Engine——因為我們的 5 個節點是實體分散的 process，不是
模擬的虛擬 client。於是決定整個重寫，而不是修補舊草稿。

### 2.2 依賴管理：vendor 而非 PyPI

`flwr` 沒有直接 `pip install`，而是把 `~/flower/framework/py/flwr`（官方 repo 原始碼，4.8MB，
剔除 122 個 `*_test.py`）複製進 `inference/vendor/flwr/`，搭配一份精簡過的
`vendor/pyproject.toml`（`[tool.poetry] packages = [{include = "flwr"}]`），用
`pip install -e ./vendor` 做本機 editable install。動機：以後如果要真的實作
JFI-guided aggregation，可以直接改框架原始碼，不用等上游發版。

**踩到的坑（詳見第 5 節）**：`vendor/pyproject.toml` 一開始用寬鬆版本 range
（如 `protobuf>=5.28.0,<7.0.0`），pip 解析到的版本組合跟框架內部生成的 protobuf gencode
不相容，`flower-superlink` 啟動直接 crash。最終修法是直接把版本鎖死對齊
`~/flower/framework/uv.lock` 裡官方已經測試過的組合。

### 2.3 `flower-app/iab_fl/server_app.py` 設計

用 `ServerApp` + `@app.main()` 裝飾器：

```python
app = ServerApp()

@app.main()
def main(grid: Grid, context: Context) -> None:
    num_rounds = int(context.run_config["num-server-rounds"])
    local_epochs = int(context.run_config["local-epochs"])
    strategy = IABFedAvg(db=..., fraction_train=1.0, min_train_nodes=5,
                          min_evaluate_nodes=5, min_available_nodes=5)
    result = strategy.start(grid=grid, initial_arrays=_seed_initial_arrays(),
                             train_config=ConfigRecord({"local-epochs": local_epochs}),
                             num_rounds=num_rounds)
    _broadcast_aggregated_weights(result.arrays)
```

幾個設計決策：

- **`IABFedAvg` 繼承 `flwr.serverapp.strategy.FedAvg`，override `aggregate_train`**（不是
  `aggregate_fit`——1.28.0 API 把這個方法改名了，是讀原始碼才發現的細節，光看官方文件容易
  沿用舊名稱寫錯）。
- **JFI 目前只做 log，不影響聚合權重**：`aggregate_train()` 呼叫 `super().aggregate_train()`
  拿到標準 FedAvg（依 `num-examples` 加權平均）的結果後，額外從 MongoDB 算一次全網 JFI 塞進
  `metrics["global_jfi"]`，但不會拿這個值去動態調整聚合權重。這是刻意的範圍限制，
  真正的 JFI-guided aggregation 列為待開發項目。
- **`_seed_initial_arrays()`**：第一輪的初始權重種子來自 Node1 現有 checkpoint（沒有就隨機
  初始化），只影響第一輪 bootstrap。
- **`_broadcast_aggregated_weights()`**：聚合完成後把最終權重寫回**全部 5 個節點**的
  checkpoint，不是只寫種子節點——這點很重要，否則 FedAvg 平均後的效果不會真正傳播出去。
  這個函式後來也是抓到一個真實 bug 的地方（見第 5.1 節）。

### 2.4 `flower-app/iab_fl/client_app.py` 設計：一個推翻原始規劃的關鍵發現

**原始規劃**（見 CLAUDE.md 舊版描述）是 Flower Client 跟 `inference_server.py`
的近即時推論**共用同一個 Python 進程、同一個 `DRLAgent` 記憶體實例**。但讀
`flwr/supernode/cli/flower_supernode.py` 發現：

```python
parser.add_argument(
    "--isolation",
    default=ISOLATION_MODE_SUBPROCESS,  # 預設值！
    ...
    help="...Use `subprocess` to configure SuperNode to run a `ClientApp` "
         "in a subprocess...",
)
```

`flower-supernode` 預設會用**獨立 subprocess** 執行 `ClientApp`，代表 `client_app.py` 的
`train()`/`evaluate()` 函式跑在跟 `inference_server.py` **完全不同的 OS process**，
不可能共用記憶體中的物件。這推翻了原本的「同進程共享模型」假設，必須改成：

- `client_app.py` 收到全域權重、以及本地微調完成後，都對**同一份磁碟 checkpoint 檔案**
  （`model_node{N}.pt`，經 docker volume 掛載共用）呼叫 `agent.save()`。
- `inference_server.py` 新增一個背景執行緒 `_reload_worker`，每 30 秒檢查這個檔案的
  mtime，偵測到「不是自己剛寫的」外部更新就在鎖保護下 `agent.load()` 熱重載。

**`@app.train()` 的關鍵細節**：

```python
@app.train()
def train(msg: Message, context: Context) -> Message:
    agent = DRLAgent(node_id=NODE_ID, model_dir=MODEL_DIR)
    agent.load()  # 讀回本節點目前的 optimizer/train_steps 狀態
    actor_sd, critic_sd = _split_flat_state_dict(msg.content["arrays"].to_torch_state_dict())
    agent.actor.load_state_dict(actor_sd)      # 只覆蓋權重
    agent.critic.load_state_dict(critic_sd)    # optimizer 狀態保留
    agent.save()  # 立刻存一次，讓近即時 process 盡快撿到全域權重
    metrics = run_training_round(agent, mongo_col, epochs=local_epochs, log=log)
    if metrics:
        agent.save()  # 本地微調完成再存一次
        num_examples = metrics.get("n_train", 0)
    else:
        num_examples = 0  # 資料不足，回傳未修改權重，貢獻度為 0 但仍回覆滿足 min_train_nodes
    ...
```

`agent.load()` **先讀回**再套用全域權重，而不是直接建立全新 `DRLAgent`，是為了保留
optimizer（Adam 動量）跟 `train_steps` 計數器——這個順序在 Stage A 測試時被驗證出「不這樣做
會出真實 bug」（見第 5.1 節）。

### 2.5 執行緒/進程安全設計

三個角色會碰到同一組 actor/critic 權重：近即時 ZMQ 主迴圈的 `forward pass`、`_train_worker`
（每 60 秒本地微調）、`_reload_worker`（熱重載）。設計：

- **`self._model_lock`**（`threading.Lock`）：只包住實際碰觸權重的段落（forward pass、
  梯度更新、`load()`/`save()`），**刻意不把 MongoDB I/O 包進鎖裡**，確保鎖持有時間維持
  在毫秒級，不影響 ZMQ 的 5ms 回應預算。
- **跨 process 的 checkpoint 寫入安全**：`DRLAgent.save()` 改成原子寫入
  （先寫 `.pt.tmp.<pid>` 暫存檔，再用 `os.replace()` 覆蓋正式檔名，POSIX rename 在同檔案系統
  上是原子操作）。這一個改動同時解決了 in-process 與跨 process 兩種寫入衝突，
  不需要額外的跨 process 檔案鎖（collision window 是幾十毫秒 vs. 60 秒/3600 秒的寫入間隔，
  機率可忽略，就算真的撞上也只是「後寫蓋掉先寫」，不是災難性問題）。

---

## 3. Global xApp：從「以為要重寫」到「發現半成品、修好橋接」

### 3.1 意外發現：Docker image 裡已經有一份沒進 git 的實作

在處理跟 Global xApp 無關的 Phase 4 reward function 改動時，注意到 `inference-nodeN`
容器的 log 出現 `[Global] Alloc PUB bound`／`[Global] Quota SUB connected` 這類沒印象的訊息。
比對後確認：**Docker image（`local-xapp-inference:latest`，2026-06-17 build）裡打包的
`inference_server.py`，跟當時 git 版本控制裡的原始碼不是同一份**，前者多了一段 Phase 5
Global xApp 整合，是直接內建在 `inference_server.py` 而不是外部獨立 process。

進一步發現：這份沒進版控的程式碼被我在處理 Phase 4 reward ablation 時**意外蓋掉了**——
`run_local_pc1.sh` 既有的熱更新機制會把 git 裡的 `inference_server.py`（沒有 Global xApp 整合）
`docker cp` 進容器再重啟，蓋掉了 image 裡原本較完整的版本。好在 Docker image 本身沒被動到，
還能重新拉出來救回。

### 3.2 修 Bug：relay 與 access 之間少了橋接

拉出來的程式碼比對後發現一個真實的邏輯缺陷：

```python
# relay 端（Node1/2）
alloc_port = 5560 + self.node_id       # Node1 → 5561，Node2 → 5562
self._alloc_pub_sock.bind(f"tcp://127.0.0.1:{alloc_port}")

# access 端（Node3/4/5）
self._quota_sub_sock.connect("tcp://127.0.0.1:5560")   # 固定連 5560！
```

relay 端各自 bind 5561／5562，access 端卻固定連 5560——**沒有任何 process 在 5560 上
PUB**，這段程式碼從設計上就不可能真的收得到配額，從沒真正跑通過。

### 3.3 修法：新增橋接 process，不改既有端點

決定不改動 relay/access 兩端已經寫好的 PUB/SUB 端點，另外新增一個獨立、極簡的橋接
process `global_xapp_bridge.py`：

```
Node1 PUB tcp://127.0.0.1:5561 ─┐
                                  ├─→ global_xapp_bridge.py SUB → compute_quotas() → PUB tcp://127.0.0.1:5560 → Node3/4/5 SUB
Node2 PUB tcp://127.0.0.1:5562 ─┘
```

配額計算邏輯**直接重用**既有 `global_xapp.py` 的 `compute_quotas()`（原本是設計給
MongoDB 輪詢版本用的，函式簽名剛好跟橋接 process 收到的即時 PUB 訊息格式相容，
`import` 過來用，不重複實作），避免同一套邏輯維護兩份。`global_xapp.py` 原本的
`main()`（輪詢 MongoDB + IPC PUSH/PULL）標註為已被取代，僅保留 `compute_quotas()`
可以被 import。

### 3.4 合併回 `inference_server.py`

Docker image 裡拉出來的版本，是這次 Flower 重構**之前**的舊版 `inference_server.py`
（沒有 `training_pipeline.py` 委派、沒有 `_model_lock`、沒有 `_reload_worker`）。
不能直接整份覆蓋回去，而是逐段手動合併：`__init__` 新增 relay/access 狀態欄位、
`_init_zmq()` 新增 PUB/SUB socket 建立、新增 `_quota_sub_worker()` 方法、
`run()` 新增第 4 條背景執行緒、主 ZMQ 迴圈的 `_infer()` 呼叫後面插入 Phase 5a
（access 端依配額裁切超額分配）／Phase 5b（relay 端發布分配結果）邏輯、`_shutdown()`
新增 socket 清理——同時保留原本 Flower 重構帶來的 `_model_lock`／`_reload_worker`／
`training_pipeline` 委派不動。

---

## 4. 部署方式：從熱補檔案到真正 build 進 image

開發過程中經歷了三種部署方式，是逐步升級的：

1. **一開始**：用專案既有慣例（`run_local_pc1.sh` 的 `docker cp` + `docker restart`）
   熱補 Python 檔案進運行中的容器，不重建 image——這是專案原本就有的快速迭代方式。
2. **新增的 `global-xapp-bridge`／`flower-superlink`／`flower-supernode-nodeN` 這些
   全新容器**，因為容器本身還不存在，第一次啟動時 image 裡沒有對應檔案，用
   `docker compose up -d` 建出容器後、再 `docker cp` 補檔案 + `docker restart` 補救。
3. **最後**：把所有新增/修改的檔案都加進 `Dockerfile`（`COPY global_xapp.py`／
   `COPY global_xapp_bridge.py`／`COPY flower-app/`／`COPY vendor/` 等），
   跑 `docker build` 真正把所有東西烤進 `local-xapp-inference:latest`，之後
   `docker compose up -d --force-recreate` 全部容器都不用再手動補檔案。

選擇「先熱補、驗證可行後才 build 進 image」的順序，是因為這樣可以先用低成本的方式
快速反覆試錯（改一行、cp 進去、重啟、看 log），確認邏輯正確後才付出 build image
的時間成本，同時也避免把還在除錯中的程式碼提早烤進 image。

---

## 5. 開發過程中發現並修好的真實 Bug（依發現順序）

這些不是理論推演，全部是實際跑出錯誤訊息或用資料驗證出來的，記錄下來避免重蹈覆轍。

### 5.1 `_broadcast_aggregated_weights()` 重置 `train_steps` 導致 DRL 悄悄退化成啟發式

**現象**：Stage A 單節點裸機 smoke test 時，跑完一輪 FL，檢查最終 checkpoint 發現
`train_steps=0`（應該要保留客戶端本地微調後的步數）。

**根因**：`_broadcast_aggregated_weights()` 對每個節點都建立**全新** `DRLAgent()`
（沒有先 `agent.load()`）再套用聚合後權重、直接存檔，導致 optimizer 狀態與
`train_steps` 被重置。而 `DRLAgent.load()` 的邏輯是 `self._is_trained = self._train_steps > 0`
——`train_steps` 被重置為 0，代表**下一次**近即時 process 的 `_reload_worker`
撿到這個 checkpoint 時，`is_trained` 會變成 `False`，`InferenceServer._infer()`
的判斷式 `use_drl = self._agent.is_trained` 就會讓整個節點悄悄退回 BSR 啟發式，
即使實際上剛完成一輪有效的 FL 聚合。

**修法**：`_broadcast_aggregated_weights()` 對每個節點也先 `agent.load()` 讀回
該節點原本的 optimizer/train_steps 狀態，才套用聚合後的 actor/critic 權重再存檔。

**驗證**：修好後重跑，`train_steps=10`（保留），`is_trained=True`；順帶觀察到
checkpoint 檔案大小從 147KB 漲到 442KB——因為現在存的是真的有動過的 Adam
optimizer state（`exp_avg`/`exp_avg_sq` 張量），不是全新優化器的空狀態，
這個檔案大小變化本身也是修法生效的間接證據。

### 5.2 FAB 目錄名稱不能有底線

`flwr build`／`flwr run` 會驗證 Flower App Bundle 根目錄名稱，只允許
`^[A-Za-z][A-Za-z0-9-]*$`（字母開頭、只能有字母數字連字號）。原本取名
`flower_app/`（底線）會直接被拒絕：

```
The Flower App name flower_app is invalid, a valid app name must start
with a letter, and can only contain letters, digits, and hyphens.
```

改名為 `flower-app/`，同步修正 `Dockerfile`／`docker-compose-iab-server.yaml`／
`run_hourly.sh`／`CLAUDE.md` 裡所有引用路徑。

### 5.3 環境變數不會跨 process tree 繼承

`ServerApp`/`ClientApp` 是由 `flower-superlink`/`flower-supernode` **各自**動態
spawn 出來的子行程，不是 `flwr run` 提交端那個 shell 的子行程。第一次測試時把
`MONGO_DB`/`MODEL_DIR_NODE1` 這些環境變數設在提交 `flwr run` 的 shell 上，結果
被完全忽略，`ServerApp` 用了預設值（`/app/models_node1`），在裸機環境嘗試建立
這個路徑時因為權限不足直接 crash。修法：環境變數要設在 `flower-superlink`／
`flower-supernode` 這兩個長駐 process 自己的啟動環境上，而不是 submit 端。

### 5.4 `run_local_pc1.sh` 的熱更新清單少了新檔案

Phase 5 把 `inference_server.py` 的訓練迴圈抽成 `training_pipeline.py` 之後，
沒有同步更新 `run_local_pc1.sh` 既有的熱補檔案清單（原本只複製
`reward_calculator.py`／`inference_server.py`／`drl_agent.py` 三個檔案）。
使用者重新執行這支腳本時，複製了新版 `inference_server.py`（裡面 import
`training_pipeline`）卻沒帶上 `training_pipeline.py` 本體，5 個
`inference-nodeN` 容器全部進入 `ModuleNotFoundError` crash loop。
修法：把 `training_pipeline.py` 加進腳本的複製清單。

### 5.5 `vendor/pyproject.toml` 寬鬆版本 range 導致 protobuf 版本衝突

部署 `flower-superlink` 時直接 crash：

```
google.protobuf.runtime_version.VersionError: Detected incompatible
Protobuf Gencode/Runtime versions when loading grpc_health/v1/health.proto:
gencode 7.35.0 runtime 6.33.6.
```

原因：`vendor/pyproject.toml` 用寬鬆 range（`protobuf>=5.28.0,<7.0.0`、
`grpcio-health-checking>=1.70.0,<2.0.0`），pip 各自解析到能滿足 range 的
**最新版本**（`protobuf-6.33.6`、`grpcio-health-checking-1.82.0`），但這兩個
最新版本彼此的 protobuf gencode/runtime 不相容。修法：查
`~/flower/framework/uv.lock`（官方已經測試過、鎖死的依賴組合），把
`vendor/pyproject.toml` 所有依賴改成精確鎖版本（`==`），對齊官方組合
（`protobuf==5.29.6`、`grpcio-health-checking==1.71.2` 等）。

### 5.6 `flwr run` 的自動遷移機制悄悄改壞了 git 裡的原始碼

`flwr` 1.26.0 起把 SuperLink 連線設定從 `pyproject.toml` 的
`[tool.flwr.federations]` 搬到 `$FLWR_HOME/config.toml` 的 `[superlink]`
區塊，**第一次執行 `flwr run` 時會自動把 pyproject.toml 裡的舊區塊直接改寫
成註解**、寫一份新的到 `~/.flwr/config.toml`。這個自動遷移發生在裸機
smoke test 時，直接修改了 git 版控裡的 `flower-app/pyproject.toml`
（把 federation 設定通通註解掉），這個改動後來被 commit 進 git、烤進
Docker image，導致每個全新的一次性容器（`docker compose run --rm`）
都因為 `$HOME` 是全新的、找不到 `local-deployment` 這個 federation 名稱
而失敗：

```
SuperLink connection 'local-deployment' not found in the Flower
configuration file (/root/.flwr/config.toml).
```

修法：不依賴這個會動態改寫原始碼的自動遷移機制，改成寫一份固定的
`inference/flwr_config.toml`（`[superlink]`/`[superlink.<name>]` 格式），
透過 `Dockerfile` 直接 `COPY` 到 `/root/.flwr/config.toml` 烤進 image，
每個容器一啟動就有現成設定，不需要靠 `flwr run` 自己去遷移。

---

## 6. 最終端到端驗證（2026-07-06，正式環境，非模擬）

修完第 5 節所有 bug 後，在 PC1 正式環境（不是裸機 venv、不是隔離測試）完整跑過：

1. `flower-superlink` + 5 個 `flower-supernode-nodeN`（各自唯一 `--clientappio-api-address`
   port 9101~9105，避免預設值 `0.0.0.0:9094` 全部撞在一起）全部連上。
2. `docker compose run --rm flower-scheduler bash -c "cd /app/flower-app && flwr run . local-deployment"`
   送出一輪 FL，5 個**真實在訓練中**的節點同步完成 `train`→`evaluate`。
3. 確認全部 5 個節點的 checkpoint mtime 幾乎同時更新（聚合結果真的廣播回全部節點）。
4. 確認 `train_steps` 正確保留（760/840，未被重置——5.1 節的 bug 修法在正式環境下也生效）。
5. 確認 `inference_server.py` 的 `_reload_worker` 在 30 秒內偵測到並熱重載聚合後權重
   （log 出現「已從磁碟熱重載模型...可能來自 Phase 5 FL 聚合」）。
6. Global xApp 部分：`global-xapp-bridge` 收到 Node1 的即時分配後，Node3 的
   `[Global] PRB quota updated` log 即時跟著變動（10→21→42→...），確認橋接邏輯
   在真實流量下正確運作。

---

## 7. 檔案異動總覽

**新增**
- `inference/vendor/flwr/`（vendor 進來的官方框架原始碼）
- `inference/vendor/pyproject.toml`
- `inference/flower-app/`（`pyproject.toml`／`iab_fl/server_app.py`／
  `iab_fl/client_app.py`／`iab_fl/__init__.py`／`run_hourly.sh`）
- `inference/flwr_config.toml`
- `inference/training_pipeline.py`
- `inference/global_xapp_bridge.py`
- `iab/check_convergence.py`（訓練收斂狀態檢查工具，非本次核心但同期產出）

**修改**
- `inference/inference_server.py`（Flower checkpoint 熱重載機制 + 合併回 Global
  xApp PUB/SUB 整合）
- `inference/drl_agent.py`（`save()` 改原子寫入）
- `inference/global_xapp.py`（`main()` 標註取代，保留 `compute_quotas()`）
- `inference/Dockerfile`（vendor install、`flwr_config.toml`、新檔案 COPY）
- `docker-compose-iab-server.yaml`（新增 `global-xapp-bridge`／`flower-superlink`／
  5 個 `flower-supernode-nodeN`／`flower-scheduler` 服務）
- `iab/run_local_pc1.sh`（熱更新清單補上 `training_pipeline.py`）
- `CLAUDE.md`（第 2 節元件定義、第五階段開發項目狀態、§5 PC 對應標示更正）

**標註為已取代（保留供參考，不再實際運作）**
- `inference/flower_server.py`（舊版 legacy API 草稿）
- `inference/flower_client.py`（Docker image 裡發現的另一份舊版 legacy API 草稿）
- `inference/global_xapp.py` 的 `main()`（MongoDB 輪詢 + IPC PUSH/PULL）
