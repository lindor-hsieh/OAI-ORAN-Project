# 碩士論文實驗流程與環境配置配置指南 (O-RAN IAB 架構)

## 1. 實驗實體環境與網路拓撲設置

本研究採用**雙主機 (Dual-host) 實體部署**，以模擬真實 O-RAN IAB 網路中的實體隔離與傳輸延遲。底層 IAB 架構採用 **MT + DU 串接模式** (無 BAP 層)，所有跨節點傳輸皆透過標準 5G Uu 介面與 Linux IP Routing 進行轉發。

### 硬體與節點配置表

| 實體主機 | 部署元件 | 網路角色 | 備註說明 |
| :--- | :--- | :--- | :--- |
| **PC 1** | 5G Core (CN5G)<br>FlexRIC Server<br>Donor Node<br>IAB Node 1, 2 | 核心網與全域控制中心<br>Relay Nodes (骨幹轉發) | FlexRIC 綁定實體 IP，接受跨主機 E2 連線。Node 1, 2 負責上下游流量調度。 |
| **PC 2** | IAB Node 3, 4, 5<br>UE 1 ~ 6 | Access Nodes (邊緣存取)<br>終端使用者 | 透過實體網路與 PC 1 連線。Node 3, 4, 5 各自獨立運行專屬的 Local xApp 容器。 |

> **開發進度註記**：目前已確認 PC 1 的 FlexRIC Server 能夠成功與所有 6 個節點 (Donor + 5 IAB Nodes) 建立 SCTP/E2AP 連線，並且驗證了 OAI MAC 層確實開放 PRB 控制權給 xApp 進行覆寫，已開發 Node 1 到 Node 5 各自獨立的 Local xApp 程式碼。
> **Local rApp 暫時完成開發**：`inference_server.py` 同一 Python 進程中包含 Near-RT 推論（ZeroMQ REP）與 Non-RT Fine-tuning（背景訓練執行緒，每 60 秒從 MongoDB 讀取經驗執行 Offline A2C 更新）兩個功能，共享同一 DRLAgent 實例。**這個「同進程共享記憶體」的假設只在 Phase 4 範圍內成立**；Phase 5 的 Flower ClientApp 是 `flower-supernode` 另開的獨立 subprocess，跟 `inference_server.py` 改用磁碟 checkpoint 檔案同步，細節見第 2 節第 2 點與第五階段。

---

## 2. 軟體架構與 O-RAN 職責映射

本系統跨越 C 語言的底層通訊與 Python 的 AI 推論，並透過共享記憶體機制整合 Near-RT 與 Non-RT 迴圈，構築實體隔離的「Local / Global 雙層階層式控制平面」。

* **底層協議棧 (C 語言)**：使用 OAI (OpenAirInterface) 實作 DU/CU 與 UE。
* **Near-RT RIC (C 語言)**：使用 FlexRIC 作為 E2 代理伺服器與 xApp 框架。
* **Non-RT RIC & AI (Python)**：使用 Flower Framework 進行階層式聯邦學習 (Hierarchical FL)，並透過 ZeroMQ 建立跨語言 IPC 通訊。

### 核心控制元件定義

1.  **Local xApp (PC 1 & PC 2)**：
    * **實作**：純 C 語言 FlexRIC 程式。
    * **職責**：局部控制迴圈（實際 **100ms**，C xApp 設有 Rate Limiter：每 10 個 10ms MAC callback 才觸發一次 ZMQ，以避免 FlexRIC pending event queue 滿載崩潰）。**只負責 PRB 分配這一個動作**：透過 E2SM-MAC 擷取所屬 Node 的 **Δ DL TBS 與 MCS**（OAI RF Simulator 的 `wb_cqi` 與 `dl_buffer_info` 在模擬環境下恆為 0，以 `dl_aggr_tbs` 差分值作為吞吐量代理、`dl_mcs1` 作為通道品質代理），將 JSON 狀態透過 ZeroMQ REQ 送給 Local rApp Python 端，收回 PRB 權重陣列後立即寫回 OAI MAC 層。C 語言端不含任何 AI 邏輯。
2.  **Local rApp / Flower Client (PC 1 & PC 2)**：
    * **實作**：Python ZeroMQ REP 伺服器（部署於 5 個獨立容器，`inference_server.py`）。Phase 5 的 Flower ClientApp（`flower-app/iab_fl/client_app.py`）是 `flower-supernode` 另開的**獨立 subprocess**，兩者不共用記憶體中的 DRLAgent 實例，而是共用同一份磁碟 checkpoint（`model_node{N}.pt`，經 volume 掛載共用）：ClientApp 收到全域聚合權重與完成本地微調後都會存檔，`inference_server.py` 有一個背景執行緒（`_reload_worker`）每 30 秒偵測這個檔案的 mtime 變化，偵測到外部寫入就熱重載，讓近即時推論撿到 FL 聚合後的權重。
    * **職責（雙重角色）**：
      * **Near-RT 推論（毫秒級）**：接收 Local xApp 的 ZeroMQ 請求，執行 DRL Actor 網路 forward pass，回傳 PRB 權重陣列，並將 State/Action/Reward 非同步寫入 MongoDB。
      * **Non-RT Fine-tuning（秒/分鐘級）**：從 MongoDB 讀取歷史資料，執行本地模型微調，準備作為 Flower Client 參與聯邦學習聚合。
3.  **Global xApp (PC 1)**：
    * **實作**：獨立的 Python Docker 容器。
    * **職責**：毫秒級全域迴圈 (10ms)。具備「全域視野 (Global View)」，透過 ZeroMQ 接收全網 5 個 Node 的瞬時狀態，負責跨節點的巨觀調度。**核心機制（軟性回傳約束）**：RF Simulator 下每個 DU 各自有獨立 106 PRB，不符合 in-band IAB 頻譜共享；修改 OAI 底層代價太高，改以 Global xApp 模擬回傳瓶頸。具體邏輯：① 監控 Node1/Node2 xApp 對 MT3/MT4 各分配了多少 PRB（代表流量通過 Node1→Node3/4 或 Node2→Node4/5 的回傳容量）；② 將該 PRB 配額透過 ZeroMQ 下發給對應的 Node3/4/5 Local xApp；③ Node3/4/5 的 Local xApp 將可用 PRB 上限從 106 改為收到的配額值（`effective_prb = min(106, quota_from_global)`），以此在 xApp 層模擬 in-band IAB 的回傳瓶頸限制。
4.  **Global rApp / Flower Server (PC 1)**：
    * **實作**：`flower-app/iab_fl/server_app.py`（`ServerApp` + `flwr run`，透過 SuperLink + SuperNode 部署，取代已 deprecated 的 `fl.server.start_server()` 舊寫法）。
    * **職責**：小時級 Non-RT 迴圈（由外部排程 wrapper `flower-app/run_hourly.sh` 每小時提交一次 `flwr run`）。強制 5 個 IAB Nodes 參與聚合（`min_train_nodes=min_evaluate_nodes=min_available_nodes=5`），聚合策略為 `IABFedAvg`（繼承 `flwr.serverapp.strategy.FedAvg`，依各節點本輪 `num-examples` 加權平均 Actor/Critic 權重）。聚合完成後把最終權重廣播寫回全部 5 個節點的 checkpoint（不只是種子節點），完成階層式 AI 的學習閉環。**目前實作現況**：`IABFedAvg.aggregate_train()` 會在每輪聚合完成後，從 MongoDB 讀取各節點最近 100 筆 reward 均值計算全網 Jain's Fairness Index，但**僅用於 log 監控，並未實際回饋進聚合權重**——聚合本身仍是純樣本數加權的標準 FedAvg，尚未做到「以 JFI 為優化目標」的真正 JFI-guided aggregation。若論文要主張後者，`aggregate_train()` 需改為依各節點 JFI 貢獻度動態調整聚合權重，屬於待開發項目。

---

## 3. 六階段標準開發流程

為確保系統穩定度，開發採「由下而上 (Bottom-Up)」策略，逐步將控制權由 OAI 預設排程器移交給 AI。

### 第一階段：底層資料平面暢通與基準建立 (已完成)
* 建立 MT+DU 串接架構，確認 Linux IP Routing 規則轉發正常。
* 使用 `iperf3` 測量 Proportional Fairness (PF) 排程器下的基準效能 (Baseline)。
* **觀測結果**：確立了下行吞吐量不均 (Jain's Fairness 低落) 與長尾延遲 (Bufferbloat) 的優化靶心。

### 第二階段：Local xApp C 語言控制權驗證 (已完成)
* 實作單節點的 C 語言 FlexRIC xApp。
* 成功擷取 `mac_ind_data_t` 中的 `ue_stats_len`、`bsr` 與 `wb_cqi`。
* 成功透過 Hardcode 寫死 PRB 分配陣列，並下發 `MAC_CTRL_REQ` 驗證 OAI 確實套用覆寫。

### 第三階段：獨立 xApp 開發與跨語言 IPC (已完成)
* **實體化 xApp**：為 Node 1 到 Node 5 撰寫各自獨立的 C 語言 FlexRIC xApp 程式碼與部署在 Docker 裡,每開發完一個xApp,請先編譯FlexRIC xApp和OAI RAN with E2 Agent,編譯過了我再審核。
* **ZeroMQ 整合**：在 C 語言 xApp 中引入 `libzmq` 與 `libcjson`，建立 REQ 模式並設定 5ms Timeout 防呆機制。
* **Python 對接**：建立 Python 端的 ZeroMQ REP 伺服器，解析 JSON 狀態並回傳推論結果。
* **資料持久化**：將每次的 State (狀態)、Action (決策) 寫入 MongoDB，為聯邦學習準備歷史資料集。

### 第四階段：Local 單節點 AI 閉環控制 + Local rApp (暫時完成)
* **模型建構**：在 Python 端建立深度強化學習 (DRL) Actor 網路。✅ 已完成（Actor-Critic + Dirichlet Policy Gradient）
* **Reward 設計**：撰寫複合獎勵函數，結合 Throughput 最大化、Delay 懲罰與 Fairness 補償。✅ 已完成
* **Local rApp 開發**：與 Local xApp 推論伺服器運行於**同一 Python 進程、共享 DRL 模型**（此假設僅適用於 Phase 4 的近即時執行緒與 `_train_worker`；Phase 5 的 Flower ClientApp 是另開的獨立 subprocess，改用磁碟 checkpoint 同步，見第 2 節第 2 點）。從 MongoDB 讀取歷史 State/Action/Reward，執行本地模型 Fine-tuning。✅ 已完成（`inference_server.py` 背景訓練執行緒）
* **穩定度測試**：讓 AI 取代 Hardcode 邏輯，觀察系統在高併發流量下的穩定度與收斂情況。⬜ 進行中
* **DRL vs PF 吞吐量驗證**：使用 Scenario A/B/C（訓練用 D，測試用 A/B/C 驗證泛化能力）作為測試條件。**TCP-DL 吞吐量與 Jain's Fairness Index 兩項在 A/B/C 三個場景下須全部優於對應 PF Baseline 才算完成本階段**（延遲允許小幅劣化）。⬜ 進行中（A✓ B✗ C✓）

#### DRL vs PF 標準量測流程

> **重要**：PF baseline 必須在相同場景 CQI 條件下現場量測，不得使用 4/20 舊 baseline（無 scenario CQI）

```bash
# Step 1：在 PC2 啟動場景（設定 CQI 並開始競爭流量）
ssh lindor@192.168.88.2 'bash -s' << 'EOF'
setsid python3 ~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator/scenarios/traffic_scenario.py \
  --scenario B --duration 900 > /tmp/scenario_b.log 2>&1 < /dev/null &
disown $!
sleep 8 && tail -20 /tmp/scenario_b.log
EOF
# 確認 6 個 UE 的 iperf3 loop start 均已出現後繼續

# Step 2：量測 DRL 效能（xApp 運行中）
cd ~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator
python3 iab/drl_report.py --iters 3

# Step 3：停止所有 xApp（切換至 OAI PF 排程器）
docker stop xapp-node1 xapp-node2 xapp-node3 xapp-node4 xapp-node5

# Step 4：量測 PF 效能（同一場景 CQI，scenario 仍在跑維持 CQI 設定）
python3 iab/drl_report.py --iters 3

# Step 5：重啟 xApp 恢復 DRL 模式
docker start xapp-node1 xapp-node2 xapp-node3 xapp-node4 xapp-node5
```

**注意事項：**
- Step 2 的「DRL」欄位 = 實測 DRL 數據
- Step 4 的「DRL」欄位 = 實測 PF 數據（腳本 label 固定為 DRL，忽略即可）
- `drl_report.py` 的 `PF_PER_UE` hardcode 是舊 4/20 數據，報告中的 PF Baseline 欄位**不可信**，以現場量測為準
- 量測前確認 data plane 正常：`docker exec rfsim5g-donor-cu ping -c 2 12.1.1.9`
- **每次量測完成後，將 per-UE 原始數據與對比結果補記至 `/home/lindor/drl_report/scenario_comparison_2026-05-23.md`**

#### 當前量測結果（2026-05-23）

完整數據見 `/home/lindor/drl_report/scenario_comparison_2026-05-23.md`

| Scenario | DRL avg TCP-DL | PF avg TCP-DL | DRL JFI | PF JFI | 吞吐量 | JFI | 通過 |
|----------|---------------|---------------|---------|--------|--------|-----|------|
| A | 7.34 Mbps | 6.95 Mbps | 0.859 | 0.860 | ✓ +5.6% | ~ | ✓ |
| B | 4.34 Mbps | 5.43 Mbps | 0.814 | 0.898 | **✗ −20%** | ✗ | **✗** |
| C | 4.86 Mbps | 4.35 Mbps | 0.808 | 0.817 | ✓ +12% | ~ | ✓ |

**Scenario B blocker**：Node3 recent reward = −0.197（policy degradation），UE1/2（Node3 管轄）PRB 飢餓，avg 僅 1.4–2.45 Mbps。

### 第五階段：Global 控制平面與聯邦學習整合 (待開發)

#### IAB 資源建模設計決策
RF Simulator 環境下每個 DU 各自有獨立 106 PRB，接入層不受回傳瓶頸約束，不符合 in-band IAB 的頻譜共享特性。直接修改 OAI 底層（F1/MAC 調度器）代價太高，因此採用「**xApp 層軟性約束 (Soft Constraint)**」模擬回傳瓶頸，在不改動 OAI 的前提下實現 IAB 回傳限制語意。

#### 論文對比架構
| 架構 | 說明 |
|------|------|
| Baseline (PF) | OAI 預設排程器，無回傳約束感知 |
| Local-only DRL | 5 個獨立 Local xApp，各自最佳化但忽略回傳限制 |
| **Global+Local DRL** | Global xApp 傳回傳配額，Local 在約束內最佳化 |

Global xApp 的差異化價值：跨層 IAB 回傳協調，Local-only 架構做不到。

#### 開發項目
* **Global xApp 開發**：建構全域 Python 容器，監控 Node1/Node2 對 MT 的 PRB 分配，計算 Node3/4/5 的回傳配額上限，透過 ZeroMQ PUB/SUB 或 PUSH/PULL 下發配額；Node3/4/5 Local xApp 收到配額後以 `min(106, quota)` 作為實際可用 PRB 上限。
* **Global rApp 開發（Flower Server）**：✅ 已完成，見 `inference/flower-app/`：
  1. **架構**：`flwr` 1.28.0 現版建議的 `ServerApp`/`ClientApp` + `flwr run` 架構（透過 SuperLink + SuperNode 部署，非 Simulation Engine——5 個節點是實體分散的 process，不是模擬的虛擬 client）。舊版 `fl.server.start_server()`/`fl.client.start_numpy_client()` 在 1.28.0 已標記 deprecated（`flwr/compat/*`），`inference/flower_server.py` 是用舊 API 的草稿，已被 `flower-app/iab_fl/server_app.py` 取代，僅保留 `compute_global_jfi()` 邏輯參考。
  2. **依賴來源**：`flwr` 從 `inference/vendor/flwr`（複製自 `~/flower/framework/py/flwr`）本機 editable install，不是從 PyPI 拉取，方便之後直接修改框架原始碼（例如真正實作 JFI-guided aggregation）。
  3. **部署拓樸**：`flower-superlink`（PC1，`--insecure`，Fleet API `:9092`／Control API `:9093`／ServerAppIo API `:9091`）+ 5 個 `flower-supernode-nodeN`（各自 dial 出去連 SuperLink，`--clientappio-api-address` 需給 5 個不同 port 9101~9105，否則預設值 `0.0.0.0:9094` 全部撞在一起）+ `flower-scheduler`（跑 `flower-app/run_hourly.sh`，每小時提交一次 `flwr run`，SuperLink/SuperNode 本身是常駐服務，`flwr run` 只是週期性送出一組有限輪數的 job）。
  4. **`server_app.py`**：啟動時嘗試載入 Node1 現有 checkpoint 當作第一輪種子；`IABFedAvg`（繼承 `FedAvg`，override `aggregate_train`——1.28.0 API 把舊版 `aggregate_fit` 改名了）強制 5 節點全部參與（`min_train_nodes=min_evaluate_nodes=min_available_nodes=5`）；聚合完成後把最終權重寫回全部 5 個節點的 checkpoint（`flower-superlink` 容器把 5 個 `inference_models_nodeN` volume 都掛進去），不是只寫種子節點——否則 FedAvg 平均後的效果不會真正傳播到 5 個節點。
* **Local rApp 擴充為 Flower Client**：✅ 已完成，見 `inference/flower-app/iab_fl/client_app.py`：
  1. **進程模型（跟原規劃不同，是重要修正）**：`flower-supernode` 預設以**獨立 subprocess** 執行 `ClientApp`（`--isolation subprocess`），跟 `inference_server.py` **不是同一個 OS process**，無法共用記憶體中的 `DRLAgent` 實例。改成透過**同一份磁碟 checkpoint 檔案**（`model_node{N}.pt`，經 docker volume 掛載共用）同步：`client_app.py` 收到全域權重、以及本地微調完成後都會 `agent.save()`；`inference_server.py` 新增的背景執行緒 `_reload_worker` 每 30 秒偵測這個檔案的 mtime，偵測到外部寫入就在 `self._model_lock` 保護下 `agent.load()` 熱重載。
  2. **參數序列化**：`actor.state_dict()` + `critic.state_dict()` 加前綴（`actor.xxx`/`critic.xxx`）攤平成一個 flat dict 包進 `ArrayRecord`，因 5 個 Node 架構完全相同，FedAvg 可直接逐層平均。
  3. **執行緒安全**：`inference_server.py` 的 `self._model_lock` 包住近即時 ZMQ 迴圈的 `agent.infer()` forward pass、`_train_worker` 的 `train_on_batch()`/`evaluate_on_batch()`/`save()`，以及 `_reload_worker` 的 `agent.load()`。鎖不包 MongoDB I/O，維持毫秒級持有時間。跨 process 的 checkpoint 寫入安全則靠 `DRLAgent.save()` 的**原子寫入**（temp file + `os.replace()`），不需要額外的跨 process 檔案鎖。
  4. **`@app.train()` 邏輯**：`agent.load()` 讀回本節點目前的 optimizer/train_steps 狀態 → 套用這一輪收到的全域權重（只覆蓋 actor/critic，optimizer 狀態保留）→ 立刻存檔一次 → 呼叫共用的 `training_pipeline.run_training_round()`（跟 `inference_server.py` 的 `_train_worker` 共用同一份訓練邏輯，避免重複維護）在本地經驗上微調 → 成功則再存一次檔並回傳更新後權重 + `num-examples`；本地資料不足（`< MIN_TRAIN_EXPERIENCES`）則回傳剛收到、未修改的權重、`num-examples=0`（仍回覆滿足 `min_train_nodes=5`，但這輪聚合權重貢獻為 0）。
  5. **`@app.evaluate()` 邏輯**：同樣套用全域權重後呼叫 `agent.evaluate_on_batch()`，只回傳 metrics 不存檔。
  6. **跨主機位址**：目前 Node1~5 全部容器都在 PC1，`--superlink` 統一用 `127.0.0.1:9092`；若之後 Node3/4/5 真的遷到 PC2，對應 3 個 `flower-supernode-nodeN` 的 `--superlink` 要改成 `192.168.88.1:9092`（`flower-app/pyproject.toml` 也已預留 `pc1-remote` federation 供 PC2 端排程使用）。
  7. **失敗處理**：MongoDB/訓練任一環節失敗都降級為「回傳未修改權重、`num-examples=0`」，不拋例外阻塞這一輪 FL，比照 `inference_server.py` 既有的 MongoDB 降級模式。

### 第六階段：實驗數據驗證與論文撰寫 (待開發)
* 設定動態干擾與高負載測試情境。
* 對比 FL 雙層 AI 架構與 OAI 預設 PF 架構的效能差異。
* 匯出實驗數據圖表，完成論文結論。

---

## 4. AI 協作與程式碼開發規範

### C 語言 (OAI & FlexRIC xApp)
* **記憶體管理**：所有 `malloc`/`calloc` 必須配對 `free`。特別是在每 10ms 觸發的 MAC 迴圈中，嚴禁 Memory Leak。使用 `libcjson` 解析完畢後務必呼叫 `cJSON_Delete`。
* **錯誤與超時處理**：使用 ZeroMQ (libzmq) 時，必須實作 Null pointer 檢查與 Timeout 機制 (上限 5ms)。若 Python 端無回應，需有 Fallback 機制 (例如退回 OAI 預設排程)，絕不能阻塞底層 MAC 迴圈。
* **日誌記錄**：適度加入 `printf` 標示 `[Local xApp]` 方便 Debug，但避免在熱路徑 (Hot path) 中過度 I/O。

### Python (AI 推論與 MongoDB)
* **程式碼風格**：遵循 PEP8 規範，並使用 Type Hinting (型別提示) 增加可讀性。
* **效能最佳化**：處理來自 ZeroMQ 的 JSON 狀態與神經網路推論時，需極小化 NumPy 陣列轉換的開銷，確保能滿足 5ms 內的實時性要求。
* **資料庫寫入**：與 MongoDB 互動時，採用非同步寫入 (Async I/O) 或批次寫入 (Batch insert)，避免拖慢主迴圈的推論速度。

---

## 5. 專案目錄結構與關鍵檔案路徑

* `/openairinterface5g/`：OAI 底層核心碼 
* `/openairinterface5g/openair2/E2AP/flexric`：FlexRIC 專案目錄
* `/openairinterface5g/openair2/E2AP/flexric/src/`：C 語言 Local xApp 的主要開發目錄
* `/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator`：docker部屬目錄
* `/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator/docker-compose-iab-server.yaml`：for PC 1（CN5G、Donor CU/DU、FlexRIC、MongoDB、全部 5 個 xapp-nodeN + inference-nodeN 容器，由 `run_local_pc1.sh` 啟動；PC1 LAN IP = `192.168.88.1`）
* `/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator/docker-compose-iab-client.yaml`：for PC 2（MT-3/4/5、DU-3/4/5、UE1~6，由 `run_local_pc2.sh` → `start_iab_client.sh` 啟動）

> **注意**：目前 Node3/4/5 的 `xapp-node3~5`／`inference-node3~5` 容器實際上跟 Node1/2 一樣全部部署在 PC1（`docker-compose-iab-server.yaml`），並未真正搬到 PC2。硬體配置表第 1 節「PC2 各自運行專屬 Local xApp 容器」是**目標架構**，尚未實現；PC2 目前只跑資料面（MT/DU/UE）。這代表 `MONGO_URI`／未來 Flower Client 目前都還能用 `localhost` 連線，但 Phase 5/6 若真的把 Node3/4/5 遷到 PC2，所有跨主機連線位址都要改用 PC1 的 `192.168.88.1`。
* `/flower/`：Python 推論伺服器、Flower Client/Server 與 MongoDB 讀寫腳本所在目錄。

---

## 6. 常用建置與執行指令 

* **編譯 FlexRIC xApp**：
  ```bash
  cd ~/openairinterface5g/openair2/E2AP/flexric/build  
  cmake -G Ninja -DCMAKE_BUILD_TYPE=Release -DKPM_VERSION=KPM_V3_00 -DE2AP_VERSION=E2AP_V2 ..
  ninja  
  sudo ninja install

* **OAI RAN with E2 Agent**：
  cd ~/openairinterface5g/cmake_targets
  sudo ./build_oai --gNB --nrUE --build-e2 --ninja -w USRP -C --cmake-opt -DE2AP_VERSION=E2AP_V2 --cmake-opt -DKPM_VERSION=KPM_V3_00 --cmake-opt -DCMAKE_BUILD_TYPE=Release

## 7. 注意事項

目前進行中為**第四階段**（DRL 閉環控制 + Local rApp 訓練）並準備進入**第五階段**（Global xApp 設計）。第五階段開始前需等待 Local DRL 訓練確認收斂。開發 xApp 至少要修改以下這些檔案：

**xApp 本體（每個 Node 各自獨立，共 5 份）**
`~/openairinterface5g/openair2/E2AP/flexric/examples/xApp/c/ctrl/mac_ctrl_node1.c`
`~/openairinterface5g/openair2/E2AP/flexric/examples/xApp/c/ctrl/mac_ctrl_node2.c`
`~/openairinterface5g/openair2/E2AP/flexric/examples/xApp/c/ctrl/mac_ctrl_node3.c`
`~/openairinterface5g/openair2/E2AP/flexric/examples/xApp/c/ctrl/mac_ctrl_node4.c`
`~/openairinterface5g/openair2/E2AP/flexric/examples/xApp/c/ctrl/mac_ctrl_node5.c`

**共用底層檔案（Node 1~5 共用，修改須謹慎）**
`~/openairinterface5g/openair2/E2AP/flexric/src/sm/mac_sm/ie/mac_data_ie.c`
`~/openairinterface5g/openair2/E2AP/flexric/src/sm/mac_sm/ie/mac_data_ie.h`
`~/openairinterface5g/openair2/E2AP/flexric/src/sm/mac_sm/enc/mac_enc_plain.c`
`~/openairinterface5g/openair2/E2AP/flexric/src/sm/mac_sm/dec/mac_dec_plain.c`
`~/openairinterface5g/openair2/E2AP/flexric/src/sm/mac_sm/mac_sm_agent.c`
`~/openairinterface5g/openair2/E2AP/flexric/src/sm/mac_sm/mac_sm_ric.c`
`~/openairinterface5g/openair2/E2AP/flexric/src/xApp/sm_ran_function_def.c`
`~/openairinterface5g/openair2/E2AP/flexric/src/sm/mac_sm/test/main.c`
`~/openairinterface5g/openair2/E2AP/RAN_FUNCTION/CUSTOMIZED/ran_func_mac.c`
`~/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/nr_mac_gNB.h`
`~/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_dlsch.c`
`~/openairinterface5g/openair2/E2AP/flexric/src/xApp/db/sqlite3/sqlite3_wrapper.c`

> **規則**：刪除整份檔案需經過授權。所有修改先在 PC 1 完成，再將修改好的檔案傳給 PC 2。

### 狀態觀測窗口（100ms）與 MAX_BSR 正規化

C xApp 的 Rate Limiter 每 10 個 10ms MAC callback 才觸發一次 ZMQ，因此每筆 State 的 `bsr` 欄位實際上是 **100ms 累積的 `delta_dl_aggr_tbs`（bytes）**，而非單一 10ms 窗口值。對應的正規化常數：

```
MAX_BSR = 2,000,000 bytes/100ms（實測高負載下 delta_tbs 可達 1~2.5M bytes）
```

`reward_calculator.py` 中的所有吞吐量計算均以此為分母。若未來修改 Rate Limiter 的觸發間隔，MAX_BSR 必須同步調整。

獎勵權重（`reward_calculator.py`）：**目前為純 Throughput Ablation** W_THROUGHPUT=**1.0**、W_FAIRNESS=**0.0**、W_DELAY=**0.0**（2026-07-06 起）——刻意拿掉公平性校正，直接對比 PF 的 sum throughput。歷史值 W_THROUGHPUT=0.5、W_FAIRNESS=0.4、W_DELAY=0.1（提高公平性權重至 0.4 是為了防止 2-UE policy monopoly collapse）。**已知風險**：拿掉 fairness 項後 policy 很可能重新收斂成 max-C/I 排程，JFI 可能低於 PF baseline（甚至比 Scenario B 的 Node3 policy degradation 更明顯）——此為本次 ablation 預期會觀察到、用來佐證原複合 reward 設計必要性的現象，非程式錯誤。切換前已清空 MongoDB `node{1-5}_experiences` 與模型 checkpoint，從隨機初始化重新訓練，避免新舊 reward 語意混在同一批訓練資料裡。

### FlexRIC 崩潰規律與重啟流程

**現象**：每次執行 `run_local_pc1.sh` 後，累積約 **2000 筆** experience 時 FlexRIC 容器會崩潰（E2 connection 中斷，xApp 停止收到 MAC indication）。

**恢復流程**：
1. 重新執行完整腳本（FlexRIC 須先於 DU 啟動）：
   ```bash
   # PC1
   bash ~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator/iab/run_local_pc1.sh
   # PC2（等 PC1 FlexRIC healthy 後）
   bash ~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator/iab/run_local_pc2.sh
   ```
2. `inference_server.py` 啟動時會自動從 `/app/models/model_nodeX.pt` 載入 checkpoint，**訓練進度與 MongoDB experience 不會丟失**，直接從上次停止點繼續。
3. 啟動順序強制要求：**FlexRIC → DU → xApp**，單獨重啟 FlexRIC 無效（DU 未重啟則無法重建 E2 連線）。

---

## 8. 實驗基準數據 (PF Scheduler Baseline)

測量時間：2026-04-20，xApp 與 DRL 啟動前，OAI 預設 Proportional Fairness 排程器。
測量方式：`iab_perf_test.sh 3`（每 UE 3 輪取平均）

### 各 UE 結果

| UE | Latency | TCP-DL | TCP-UL | UDP-DL | UDP-UL |
|---|---|---|---|---|---|
| UE1 (12.1.1.9) | 76.36 ms | 6.81 Mbps | 9.21 Mbps | 20.25 Mbps | 13.80 Mbps |
| UE2 (12.1.1.8) | 67.84 ms | 10.66 Mbps | 9.59 Mbps | 19.43 Mbps | 13.23 Mbps |
| UE3 (12.1.1.12) | 83.25 ms | 11.35 Mbps | 9.34 Mbps | 18.80 Mbps | 12.26 Mbps |
| UE4 (12.1.1.10) | 71.39 ms | 6.81 Mbps | 9.90 Mbps | 18.43 Mbps | 13.26 Mbps |
| UE5 (12.1.1.7) | 65.12 ms | 9.44 Mbps | 8.95 Mbps | 19.20 Mbps | 12.73 Mbps |
| UE6 | 81.43 ms | 6.99 Mbps | 9.67 Mbps | 19.76 Mbps | 13.66 Mbps |

### 全網平均

| 指標 | 數值 |
|---|---|
| Avg. Latency | **74.23 ms** |
| Avg. TCP-DL | **8.67 Mbps** |
| Avg. TCP-UL | **9.44 Mbps** |
| Avg. UDP-DL | **19.31 Mbps** |
| Avg. UDP-UL | **13.15 Mbps** |

### Jain's Fairness Index (TCP-DL)

xi = [6.81, 10.66, 11.35, 6.81, 9.44, 6.99]，JFI ≈ **0.924**

> DRL 目標：TCP-DL 總量相近或更高，JFI > 0.924（各 UE 分佈更均勻），Latency 降低。
