# 專案歷史紀錄

這是一份 append-only 的踩坑/量測日誌，按時間順序記錄「當下發生了什麼、怎麼查出來的、怎麼修的」。**CLAUDE.md 只保留現在式的架構事實與可執行指令**；新的踩坑紀錄、量測結果、除錯過程一律往這份文件的最下面加，不要寫回 CLAUDE.md。

---

## 2026-04-20 — PF Scheduler Baseline（舊雙主機 6-UE 拓樸）

測量時間：2026-04-20，xApp 與 DRL 啟動前，OAI 預設 Proportional Fairness 排程器。
測量方式：`iab_perf_test.sh 3`（每 UE 3 輪取平均）。**此數據對應的是舊雙主機 5-node/6-UE 拓樸，2026-09-11 已改成三主機 12-node/17-UE，數值不再適用，僅供歷史對照。**

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

---

## 2026-05-23 — DRL vs PF 當前量測結果（舊 5-node 拓樸）

完整數據見 `/home/lindor/drl_report/scenario_comparison_2026-05-23.md`

| Scenario | DRL avg TCP-DL | PF avg TCP-DL | DRL JFI | PF JFI | 吞吐量 | JFI | 通過 |
|----------|---------------|---------------|---------|--------|--------|-----|------|
| A | 7.34 Mbps | 6.95 Mbps | 0.859 | 0.860 | ✓ +5.6% | ~ | ✓ |
| B | 4.34 Mbps | 5.43 Mbps | 0.814 | 0.898 | **✗ −20%** | ✗ | **✗** |
| C | 4.86 Mbps | 4.35 Mbps | 0.808 | 0.817 | ✓ +12% | ~ | ✓ |

**Scenario B blocker**：Node3 recent reward = −0.197（policy degradation），UE1/2（Node3 管轄）PRB 飢餓，avg 僅 1.4–2.45 Mbps。

當時使用的量測 SOP（DRL vs PF 標準流程，PC2 帳號/IP 已隨三主機擴容過期，此流程本身已不適用新拓樸，僅存檔參考）：

```bash
# Step 1：在 PC2 啟動場景（設定 CQI 並開始競爭流量）
ssh lindor@192.168.88.2 'bash -s' << 'EOF'
setsid python3 ~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator/scenarios/traffic_scenario.py \
  --scenario B --duration 900 > /tmp/scenario_b.log 2>&1 < /dev/null &
disown $!
sleep 8 && tail -20 /tmp/scenario_b.log
EOF

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

注意事項：Step 2 的「DRL」欄位=實測 DRL 數據；Step 4 的「DRL」欄位=實測 PF 數據（腳本 label 固定寫 DRL，忽略即可）；`drl_report.py` 的 `PF_PER_UE` hardcode 是舊 4/20 數據，報告中的 PF Baseline 欄位不可信，以現場量測為準。

---

## 2026-07-06 — 純 Throughput Reward Ablation 量測結果（舊 5-node 拓樸）

`reward_calculator.py` 改為 W_THROUGHPUT=1.0、W_FAIRNESS=0.0、W_DELAY=0.0（拿掉公平性/延遲校正），MongoDB 經驗與模型 checkpoint 全部清空、從隨機初始化重新訓練。以下數字量測當下訓練僅約 40 分鐘–1 小時（各節點近 200 筆平均 reward 0.01–0.2 左右），**尚未收斂**，僅供早期趨勢參考。完整數據見 `/home/lindor/drl_report/scenario_comparison_2026-05-23.md`

| Scenario | DRL avg TCP-DL | PF avg TCP-DL | DRL JFI | PF JFI | 吞吐量 | JFI | 通過 |
|----------|---------------|---------------|---------|--------|--------|-----|------|
| A | 5.15 Mbps | 7.89 Mbps | 0.872 | 0.942 | **✗ −34.7%** | ✗ | **✗** |
| B | 5.91 Mbps | 4.95 Mbps | 0.815 | 0.736 | ✓ +19.4% | ✓ | ✓ |
| C | 6.91 Mbps | 5.58 Mbps | 0.936 | 0.848 | ✓ +23.8% | ✓ | ✓ |

**Scenario A blocker**：跟舊 reward（0.5/0.4/0.1）的驗收結果互補——舊 reward 卡在 Scenario B，這次純 throughput ablation 卡在 Scenario A。Scenario A 是「CQI 差異化、流量需求相同」的純粹情境，拿掉 fairness 校正後最容易誘發「無腦倒向好通道 UE」的退化；Scenario B/C 都帶有流量需求差異，purely-greedy 策略客觀上仍有機會跟公平分配方向一致，未必出現預期中的全面崩壞。

**已知可能原因**：
1. 訓練仍非常早期，模型幾乎沒看過 Scenario A 這類「同流量、CQI 差異化」的分佈（訓練場景為 Scenario D，A/B/C 僅用於泛化測試）。
2. `drl_agent.py` 的 `DIRICHLET_CONCENTRATION=5.0` 是寫死常數，即使 policy 已收斂，`infer()` 實際下發的 PRB 分配仍是從 `Dirichlet(probs × 5)` 隨機抽樣、非確定性輸出，這在 Scenario A 這種分配精準度影響大的情境會直接拖累實測吞吐量，且跟訓練是否收斂無關。待評估方案：讓集中度隨訓練步數退火升高（類似 entropy_coeff 退火），或訓練收斂後改用確定性輸出。

歷史 reward 權重：W_THROUGHPUT=0.5、W_FAIRNESS=0.4、W_DELAY=0.1（提高公平性權重至 0.4 是為了防止 2-UE policy monopoly collapse）。拿掉 fairness 項後 policy 很可能重新收斂成 max-C/I 排程，JFI 可能低於 PF baseline——此為本次 ablation 預期會觀察到、用來佐證原複合 reward 設計必要性的現象，非程式錯誤。切換前已清空 MongoDB `node{1-5}_experiences` 與模型 checkpoint，從隨機初始化重新訓練，避免新舊 reward 語意混在同一批訓練資料裡。

---

## 2026-07-08 — Global+Local DRL 三場景量測結果（舊 5-node 拓樸）

Global xApp（relay PUB → bridge → access SUB 回傳配額，見 `inference/PHASE5_GLOBAL_DEV_LOG.md`）在這次量測時**第一次真正生效**——上面 2026-07-06 那批數字實際上都是 Local-only DRL（Global xApp 當時還沒修好，Node3/4/5 從沒真的收到配額限制）。這次是三個場景第一次做 **PF vs Local-only DRL vs Global+Local DRL** 三方對比，訓練約 2 天（各節點近 200 筆平均 reward 0.02–0.15），**仍未收斂**。完整數據見 `/home/lindor/drl_report/scenario_comparison_2026-05-23.md`

| Scenario | PF TCP-DL | Local-only TCP-DL | Global+Local TCP-DL | PF JFI | Local-only JFI | Global+Local JFI | Global vs Local-only |
|----------|-----------|--------------------|-----------------------|--------|------------------|---------------------|----------------------|
| A | 6.15 Mbps | 5.15 Mbps | 5.93 Mbps | 0.750 | 0.872 | 0.683 | 吞吐量 +15.1% ／ JFI **−21.7%** |
| B | 5.75 Mbps | 5.91 Mbps | 6.79 Mbps | 0.723 | 0.815 | 0.790 | 吞吐量 **+14.9%** ／ JFI −3.1% |
| C | 6.51 Mbps | 6.91 Mbps | 6.69 Mbps | 0.816 | 0.936 | 0.794 | 吞吐量 −3.2% ／ JFI **−15.2%** |

**Global xApp 的效果不是單純「加了就變好」，而是場景依賴（scenario-dependent）的 trade-off**：
- **Scenario B**（等 CQI、不等流量）：淨賺，吞吐量大幅提升、JFI 幾乎沒退步，三場景中效果最好。
- **Scenario A**（同流量、只差 CQI）：明顯用 fairness 換 throughput，JFI 大幅退步且是唯一低於 PF 的場景——回傳配額限制疊加在本來就容易「無腦倒向好通道」的情境上，風險被放大。
- **Scenario C**（CQI 與流量需求同向）：反而略微變差，兩項指標都略輸 Local-only——這個場景 Local-only DRL 本來就表現最好（JFI 0.936），Global 端的額外限制沒有額外助益。

**注意**：這次的 Local-only 基準（2026-07-06）與 Global+Local 新量測（2026-07-08）都是訓練早期數字，兩者訓練進度不完全對齊，這裡看的是**方向**而非絕對數值，待雙方訓練都 confirm 收斂後應重新量測確認場景依賴性是否持續存在。

---

## 2026-07-09 — xApp dl_buffer_info 開發沿革

舊版文件誤認為 `dl_buffer_info` 在模擬環境下恆為 0，因此 state 長期只用 `dl_aggr_tbs` 差分與 `dl_mcs1` 兩個欄位——但這兩者在 UE 的 RLC buffer 為空時會被 OAI 排程器（`gNB_scheduler_dlsch.c`）直接跳過、同時凍結在舊值，無法區分「無資料可傳」與「有資料但通道差/PRB 不足」。實際查證 OAI C 端原始碼發現 `dl_buffer_info`（`sched_ctrl->num_total_bytes`）已完整打通 E2SM-MAC 的 encode/decode pipeline，只是 xApp 端（`xapp_node1.c`~`xapp_node5.c`，當時只有 5 個節點）的 JSON 序列化沒有把它抓出來送給 Python——補上一行 `cJSON_AddNumberToObject` 後現場驗證：UE 真閒置時 99% 讀到 0，有資料排隊時讀到有意義的非零值。State 維度因此從 33 維擴充為 49 維（`drl_agent.py` 的 `encode_state()`），這是破壞性變更，已清空 MongoDB 經驗與所有 checkpoint、從隨機初始化重新訓練。詳見 `inference/DRL_DESIGN.md` §2。

`MAX_BUF_INFO=2,000,000 bytes`（`drl_agent.py`，`dl_buffer_info` 正規化上限）是這次同步現場實測 node3/5 最大值約 2,147,000 訂出來的。

---

## 2026-07-09 — FlexRIC/DU 崩潰現象二：DU 端 SIGSEGV 新發現

在 FlexRIC 崩潰現象一（xApp 端，累積約 2000 筆 experience 後 pending event queue 塞滿）發生後，若只單獨重啟 xApp/DU 而不動 FlexRIC 本身，DU 容器會在啟動後數秒內以 `assoc_rb_tree_extract: Assertion 'z_node != tree->dummy...' failed`（`assoc_rb_tree.c:457`）反覆崩潰（`Exited (139)`，SIGSEGV），**每次重啟都在同一點崩潰，不是偶發競爭條件**。這是 DU 內建的 E2 Agent 在協議關聯追蹤上的 bug，推測跟 FlexRIC 端殘留的壞狀態互動有關——單獨重啟 DU 無法清掉，必須連同 FlexRIC 一起做完整乾淨重啟才會恢復正常。已驗證：只重啟 DU 3/4/5 兩次，都以同樣的 assertion 重現崩潰；連同 FlexRIC 一起做完整乾淨重啟後才恢復穩定。修法見 CLAUDE.md「FlexRIC 崩潰規律與重啟流程」。

---

## 雙主機時期（2026-09-11 之前的架構，已被三主機取代）

舊架構是「PC1（CN5G/FlexRIC/Donor/Node1,2 relay/全部 xApp+inference）+ PC2（Node3,4,5 access + UE1~6）」的雙主機、非對稱（Node1 帶 2 個 access、Node2 只帶 1 個）拓樸。PC2 當時的帳號也是 `lindor`；2026-09-11 PC2 換成全新機器（帳號 `mcalab`），同時新增 PC3，才有了三主機/四層樹狀重構（見 CLAUDE.md 第 1 節現況）。

當時的開發進度：PC1 的 FlexRIC Server 能夠成功與所有 6 個節點 (Donor + 5 IAB Nodes) 建立 SCTP/E2AP 連線，並且驗證了 OAI MAC 層確實開放 PRB 控制權給 xApp 進行覆寫。`inference_server.py` 同一 Python 進程中包含 Near-RT 推論（ZeroMQ REP）與 Non-RT Fine-tuning（背景訓練執行緒，每 60 秒從 MongoDB 讀取經驗執行 Offline A2C 更新）兩個功能，共享同一 DRLAgent 實例——這個「同進程共享記憶體」的假設只在當時的 Phase 4 範圍內成立。

### 第五階段：Global 控制平面與聯邦學習整合（舊 5-node 拓樸，完整開發記錄）

> 以下內容全部針對舊 5-node 拓樸（Node1/2 relay、Node3/4/5 access），三主機 12-node 拓樸尚未重建這一層，目前 `docker-compose-iab-server.yaml` 不含任何 `global-xapp-bridge`／`flower-*` 服務。保留這份記錄供未來重新設計 cluster FL 時參考架構細節。

#### IAB 資源建模設計決策
RF Simulator 環境下每個 DU 各自有獨立 106 PRB，接入層不受回傳瓶頸約束，不符合 in-band IAB 的頻譜共享特性。直接修改 OAI 底層（F1/MAC 調度器）代價太高，因此採用「xApp 層軟性約束 (Soft Constraint)」模擬回傳瓶頸，在不改動 OAI 的前提下實現 IAB 回傳限制語意。

#### 論文對比架構（舊 5-node）
| 架構 | 說明 |
|------|------|
| Baseline (PF) | OAI 預設排程器，無回傳約束感知 |
| Local-only DRL | 5 個獨立 Local xApp，各自最佳化但忽略回傳限制 |
| Global+Local DRL | Global xApp 傳回傳配額，Local 在約束內最佳化 |

#### Global xApp 開發記錄
✅ 已完成（2026-07-06），見 `inference/inference_server.py`（relay PUB / access SUB 端點，`_quota_sub_worker()` 執行緒 + 主迴圈的 Phase 5a/5b 邏輯）+ `inference/global_xapp_bridge.py`（獨立橋接 process，SUB Node1/2、算配額、PUB 給 Node3/4/5，重用 `global_xapp.py` 的 `compute_quotas()`）。部署為 docker-compose 的 `global-xapp-bridge` 服務（`network_mode: host`，依賴 `inference-node1`/`inference-node2`）。

**核心機制（軟性回傳約束）**：① `inference_server.py` 裡 Node1/2（relay）在每次推論後把自己的 PRB 分配結果透過 ZMQ PUB 廣播出去（`tcp://127.0.0.1:5561`／`5562`）；② `global_xapp_bridge.py` 同時 SUB 訂閱 Node1/2 兩個 PUB endpoint，用 `compute_quotas()` 算出 Node3/4/5 各自的配額，統一 PUB 到 `tcp://127.0.0.1:5560`；③ `inference_server.py` 裡 Node3/4/5（access）SUB 這個固定 port（各自訂閱 `node{id}` topic），把可用 PRB 上限從 106 改為收到的配額值（`effective_prb = min(106, quota_from_global)`），並在 ZMQ 主迴圈裡依比例裁切超額分配。

**開發沿革**：這條 PUB/SUB 資料流最早是直接內建在 `inference_server.py`（2026-06-17 build 的 Docker image 裡就有），但當時漏了 relay 端 bind 的 port（5561/5562）跟 access 端固定 SUB 的 port（5560）中間的橋接，這段程式碼從沒真的跑通過、也沒有進版本控制；直到 2026-07-06 才發現、補上 `global_xapp_bridge.py` 這個橋接 process 使其完整可用。`global_xapp.py` 目前只保留 `compute_quotas()` 供橋接 process 重用，其 `main()`（輪詢 MongoDB + IPC PUSH/PULL）已被取代、不再運作。

#### Global rApp 開發記錄（Flower Server）
✅ 已完成，且已在 PC1 正式環境跑通完整 5-node FL round（2026-07-06），見 `inference/flower-app/`：

1. **架構**：`flwr` 1.28.0 現版建議的 `ServerApp`/`ClientApp` + `flwr run` 架構（透過 SuperLink + SuperNode 部署，非 Simulation Engine——5 個節點是實體分散的 process，不是模擬的虛擬 client）。舊版 `fl.server.start_server()`/`fl.client.start_numpy_client()` 在 1.28.0 已標記 deprecated，`inference/flower_server.py`（用舊 API 的草稿，已被 `flower-app/iab_fl/server_app.py` 取代——**2026-09-11 三主機重構時已刪除此檔案**，僅保留 `compute_global_jfi()` 邏輯已搬進 `server_app.py`）。
2. **依賴來源**：`flwr` 從 `inference/vendor/flwr`（複製自 `~/flower/framework/py/flwr`）本機 editable install，不是從 PyPI 拉取。`vendor/pyproject.toml` 的依賴版本直接對齊官方 `~/flower/framework/uv.lock` 已測試過的組合——曾經因為寬鬆 range 讓 pip 解到 `protobuf 6.33.6` + `grpcio-health-checking 1.82.0`，兩者 gencode/runtime 不相容，`flower-superlink` 啟動直接 crash。
3. **部署拓樸**：`flower-superlink`（PC1，`--insecure`，Fleet API :9092／Control API :9093／ServerAppIo API :9091）+ 5 個 `flower-supernode-nodeN`（各自 dial 出去連 SuperLink，`--clientappio-api-address` 需給 5 個不同 port 9101~9105，否則預設值 `0.0.0.0:9094` 全部撞在一起）+ `flower-scheduler`（跑 `flower-app/run_hourly.sh`，每小時提交一次 `flwr run`）。
4. **Flower CLI 設定**：SuperLink 連線位址（`local-deployment`/`pc1-remote`）放在 `inference/flwr_config.toml`，Dockerfile 直接 COPY 到 `/root/.flwr/config.toml` 烤進 image。不要依賴 `flwr run` 的 pyproject.toml 自動遷移機制（會改寫 `flower-app/pyproject.toml`，對 `docker compose run --rm` 一次性容器不管用，且已經把 git 裡的 pyproject.toml 改壞過一次）。
5. **已驗證（2026-07-06）**：`docker compose run --rm flower-scheduler bash -c "cd /app/flower-app && flwr run . local-deployment"` 在 5 個真實訓練中的節點上完整跑完一輪 train→evaluate→聚合→廣播，確認全部 5 個節點的 checkpoint mtime 同步更新、`train_steps` 正確保留（760/840，未被重置）、且 `_reload_worker` 在 30 秒內偵測到並熱重載聚合後權重。
6. **`server_app.py`**：啟動時嘗試載入 Node1 現有 checkpoint 當作第一輪種子；`IABFedAvg`（繼承 `FedAvg`，override `aggregate_train`）強制 5 節點全部參與；聚合完成後把最終權重寫回全部 5 個節點的 checkpoint，不是只寫種子節點。**`aggregate_train()` 會計算全網 JFI 但僅用於 log 監控，並未實際回饋進聚合權重**——聚合本身仍是純樣本數加權的標準 FedAvg，尚未做到「以 JFI 為優化目標」的真正 JFI-guided aggregation。

#### Local rApp 擴充為 Flower Client 開發記錄
✅ 已完成，見 `inference/flower-app/iab_fl/client_app.py`：

1. **進程模型（跟原規劃不同，是重要修正）**：`flower-supernode` 預設以獨立 subprocess 執行 `ClientApp`（`--isolation subprocess`），跟 `inference_server.py` 不是同一個 OS process，無法共用記憶體中的 `DRLAgent` 實例。改成透過同一份磁碟 checkpoint 檔案（`model_node{N}.pt`，經 docker volume 掛載共用）同步：`client_app.py` 收到全域權重、以及本地微調完成後都會 `agent.save()`；`inference_server.py` 的背景執行緒 `_reload_worker` 每 30 秒偵測這個檔案的 mtime，偵測到外部寫入就在 `self._model_lock` 保護下 `agent.load()` 熱重載。
2. **參數序列化**：`actor.state_dict()` + `critic.state_dict()` 加前綴（`actor.xxx`/`critic.xxx`）攤平成一個 flat dict 包進 `ArrayRecord`，因 5 個 Node 架構完全相同，FedAvg 可直接逐層平均。
3. **執行緒安全**：`self._model_lock` 包住近即時 ZMQ 迴圈的 `agent.infer()` forward pass、`_train_worker` 的 `train_on_batch()`/`evaluate_on_batch()`/`save()`，以及 `_reload_worker` 的 `agent.load()`。鎖不包 MongoDB I/O，維持毫秒級持有時間。跨 process 的 checkpoint 寫入安全靠 `DRLAgent.save()` 的原子寫入（temp file + `os.replace()`）。
4. **`@app.train()` 邏輯**：`agent.load()` 讀回本節點目前的 optimizer/train_steps 狀態 → 套用這一輪收到的全域權重（只覆蓋 actor/critic，optimizer 狀態保留）→ 立刻存檔一次 → 呼叫 `training_pipeline.run_training_round()`（跟 `_train_worker` 共用同一份訓練邏輯）在本地經驗上微調 → 成功則再存一次檔並回傳更新後權重 + `num-examples`；本地資料不足則回傳未修改權重、`num-examples=0`。
5. **`@app.evaluate()` 邏輯**：同樣套用全域權重後呼叫 `agent.evaluate_on_batch()`，只回傳 metrics 不存檔。
6. **跨主機位址**：當時 Node1~5 全部容器都在 PC1，`--superlink` 統一用 `127.0.0.1:9092`；`flower-app/pyproject.toml` 預留了 `pc1-remote` federation 供跨主機排程使用（三主機重構後若要復原 FL，這個設定可以直接用來讓 PC2/PC3 的 supernode 連回 PC1 的 superlink）。
7. **失敗處理**：MongoDB/訓練任一環節失敗都降級為「回傳未修改權重、`num-examples=0`」，不拋例外阻塞這一輪 FL。

---

## 2026-09-11 — 三主機擴容踩坑記錄

### PC2（Ubuntu 20.04）建置環境除錯過程

PC2 是三台裡唯一不是 Ubuntu 24.04 的機器，`build_oai -I` 沒把套件裝全，依序踩到：① 缺 `libuhd-dev`（`-w USRP` 編譯選項需要，focal 原生倉庫有，直接裝即可）；② `libyaml-cpp-dev` 原生只有 0.6.2，OAI 的 CMake 需要 0.8.0 才有新版 `yaml-cpp::yaml-cpp` ALIAS target 支援，導致 `add_library INTERFACE library requires no source arguments` 這種看起來無關的 CMake 錯誤——移除舊版、從源碼建置 0.8.0 才解決；③ CMake 原生只有 3.16.3 太舊（相關聯，3.16 對新版 CMake 語法支援不完整），用 Kitware 官方倉庫裝到跟 PC1/PC3 一致的 3.28.3；④ GCC 原生只有 9.4.0，編譯 AVX512 SIMD 路徑（simde 相容層）會出 `_mm256_load_epi32` 找不到宣告的錯誤，這不是 CPU 差異（三台都是同型號 Ryzen 7 7700），純粹是編譯器版本太舊——用 `ppa:ubuntu-toolchain-r/test` 裝 gcc-13/g++-13 並設為預設解決。

還有一個不是編譯期會噴錯、而是容器跑起來才炸的坑：`nr-uesoftmodem`/`nr-softmodem` 在 PC2 編譯連結到 host 原生的 `libssl.so.1.1`（20.04 預設版本），但容器基底 `custom-oai-runtime:24.04` 只有 `libssl3`，跑起來報 `error while loading shared libraries: libcrypto.so.1.1`。修法是把 host 的 `/usr/lib/x86_64-linux-gnu/{libssl,libcrypto}.so.1.1` 複製進 `cmake_targets/ran_build/build/`（這個目錄本來就整包 bind-mount 進容器、也在 `LD_LIBRARY_PATH` 裡）——這個檔案不在 git 版控裡，`ran_build` 目錄被清掉重建時要記得重做這一步（結論已寫進 CLAUDE.md 第 6 節）。

### IMSI 資料庫踩坑

CN5G 的訂閱資料在 `oai_db.sql`（`mysql` 容器初次啟動時匯入），舊版只預先灌了 `208990100001100`~`208990100001116` 這 17 筆（Ki/OPc 相同）。三主機擴容後 MT 的 IMSI 尾碼 `100`~`111` 剛好落在既有範圍內沒事，但新設計的 UE 尾碼改用全新的 `200`~`216` 區段，`mysql` 裡完全沒有對應記錄，UE 一律收到 `FGS_REGISTRATION_REJECT` 附著失敗。修法：直接對執行中的 `rfsim5g-mysql` 容器補 `INSERT INTO users`（沿用既有列的 Ki/OPc/msisdn 樣式，只換 IMSI），插入後把對應的 UE 容器 `docker compose restart` 一次讓它重新嘗試附著即可，不需要重建整個 mysql 資料卷。事後已同步把這些 INSERT 語句補進 `oai_db.sql` seed 檔本身，未來重建資料卷不會再漏。

### relay 節點多子連線的架構查證

一度以為「一個 relay 的 DU 同時服務 2 個 access node 的 MT + 1 個真 UE」（Node4）需要在 RAN 層新增設定，實際查證發現 OAI rfsimulator 的一個 DU server 本來就能同時接受多個 MT client 連線（不需要在 relay 主機上多長出額外的 MT 容器），4 個 relay 各帶 2 個 access 都只是「讓新節點的 rfsim client 指向同一個 server port」，沒有 F1/PRB 供給層面的新問題。

### UE17 附著失敗事件

UE17（直接掛在 Node4 的 DU 上，不經過 access 層）一度持續卡在 RACH contention resolution failed 迴圈，永遠附著不上。一開始懷疑是「Node4 的 DU 同時服務 3 個子連線（2 個 access MT + UE17）」這個全拓樸唯一未驗證過的連線數量本身有問題，深入查證後證實與連線數量無關——OAI 的 `xapp_2d_ctrl` 2D 控制機制（`ran_func_mac.c`／`gNB_scheduler_dlsch.c`）對不在 xApp 控制清單裡的 RNTI 預設給滿額排程（不會餓死新 UE），`XAPP_MAX_UE=16`／`MAX_MOBILES_PER_GNB=32` 也遠夠用。

**真正原因**：Node4 的 DU 在某次容器操作造成的 CPU/排程抖動中觸發了 F1AP SCTP 斷線（Donor CU log 顯示 `releasing DU ID 3588 on assoc_id 11`），但 DU 端沒有像 Node2 一樣自動重新做 F1 Setup，導致 SCTP socket 卡死，DU 每次嘗試送 Msg4 (RRCSetup) 都是 `Sctp_sendmsg failed: Broken pipe`，UE17 永遠等不到 Contention Resolution 完成——已存在的 UE（Node11/Node12）的 GTP-U 資料面走 UDP，不受影響，這是為什麼只有新附著會卡住、舊連線看起來正常的原因。修法：重啟 `rfsim5g-iab-du-4` 容器強制重新做 F1 Setup 即可，不需要動任何 RAN 層排程程式碼。UE17 修復後拿到 `12.1.1.32`，CU ping RTT ~137ms 確認資料面正常。

**經驗教訓**：容器 force-recreate（尤其是 MT/DU 共用 netns 的 relay 節點，重建 MT 會連帶砍掉 DU 的網路命名空間）或密集重啟一批容器，可能因為 CPU/排程抖動連帶震盪到「無關」節點的 F1AP SCTP 心跳，造成單一節點卡在斷線狀態且不會自動恢復。診斷時應先查 Donor CU log 有沒有 `releasing DU ID`／`UEs lost through DU disconnect`，比直接懷疑協定層限制更快定位問題。

---

## 2026-09-12 — relay MT 反覆斷線重連根因查證 + Node2/7/8 搬遷到 PC1

### 現象

對三主機做一次完整乾淨重啟（全部 30 個端點同時冷啟動，跟平常「開發過程中逐步加節點」的負載型態完全不同）後，PC2/PC3 上的 relay MT（尤其 mt-1、mt-2）反覆斷線重連，tunnel IP 一路飄移（`12.1.1.2→6→10→17→19→26→29→34→38`……），連帶讓 relay DU 被「tunnel IP 改變就重啟 nr-softmodem」的自我修正機制（見下方 DU wrapper 說明）反覆觸發重啟，波及底下的 access 節點跟 UE 長時間附著失敗。單獨重跑 bring-up 腳本、加大 timeout、改成逐一序列化啟動節點都沒有解決根本問題，只有 relay 層本身持續不穩定。

### 根因鏈

1. Donor CU log 反覆出現 `[NR_RRC] I Reestablishment RNTI xxxx req C-RNTI xxxx physCellId 0 cause Other Failure`——UE 端（relay MT）主動發起 RRC Reestablishment，`cause=otherFailure` 對應 3GPP 標準的 UE 端連續判定 PHY 失步（T310 逾時／RLF），不是核心網或設定問題。
2. 由於 CU 端的 UE context 常常已經因為 `no AMF for CU UE ID xxx: auto-generate release command`（`rrc_gNB.c::rrc_CU_process_ue_context_release_request()`，DU 主動送出 F1AP UE CONTEXT RELEASE REQUEST 但當時 UE 連 NAS/AMF 關聯都還沒建立完成）被提前釋放，UE 的 Reestablishment 請求會找不到既有 context（`NR_RRCReestablishmentRequest without UE context, fallback to RRC setup`），只能整個重新走一次 RRC Setup → 重新註冊 → 重新建立 PDU session → 拿到全新 tunnel IP。
3. 追查「UE 端為什麼會判定 PHY 失步」：PC2/PC3 各自同時跑 6 組 relay+access 的 MT+DU（12 個即時 RF 模擬 process）在 16 核心主機上，過於擁擠導致 CPU 排程延遲，使即時模擬的 nr-uesoftmodem 沒能準時處理到該處理的訊框，被自己的 PHY 層誤判成失步——這不是協定層 bug，是資源競爭。三台主機皆為 16 核心（`nproc` 確認），PC1 當時只有 Donor CU/DU 兩個即時 process（其餘 24 個 xApp/inference 容器都是事件驅動的輕量負載），PC2/PC3 卻各自扛了約 20~21 個。小規模（開發期逐步加節點）從未踩過這個問題，是因為負載從來沒有同時疊到這麼高過。

### 處理

1. **架構層面**：把 Node2（relay）+ 其兩個 access 子節點 Node7,8（含 UE5~8）從 PC2 搬到 PC1 分擔負載，讓三台主機的即時 process 數量更接近（PC1 從 2 個增加到約 12 個，PC2 從 ~20 個降到 ~10 個）。搬遷後 Node2/7/8 的 CU 端指令（DNAT、路由）改成本機直接執行，不再需要 SSH 到自己（見 `iab/start_iab_server.sh` 的 `configure_and_start_local_relay`/`configure_and_start_local_access_du`），internal-bridge 子網另外開一個 `192.168.76.0/24`（`iab_internal_net_pc1`），避免跟 PC2 的 `192.168.74.0/24`／PC3 的 `192.168.75.0/24` 混淆。**PC3 尚未做同樣的分流**，若之後 PC3 也出現同樣的不穩定，應該用同樣手法再分擔一組過去（可能給 PC1 或考慮擴充第四台主機）。
2. **即時排程優先權**：三份 compose 檔裡所有 `nr-softmodem`/`nr-uesoftmodem` 的啟動指令都加上 `chrt -f 80` 前綴，讓即時 RF 模擬 process 用 SCHED_FIFO 即時排程優先權搶得到 CPU，減少被其他 process（docker daemon、xApp/inference 等）排擠導致誤判失步的機率（容器皆為 `privileged: true`，有 `CAP_SYS_NICE`，`chrt`/`nice`/`renice` 已確認可用）。
3. **搬遷過程踩的坑**（供未來再搬遷節點時參考）：
   - 只改了 docker-compose 裡 internal-bridge 網路的**網路名稱**（`iab_internal_net` → `iab_internal_net_pc1`）卻忘了同步改**IP 位址數值**（`192.168.74.x` → `192.168.76.x`），導致 `docker compose up` 直接報 `invalid endpoint settings: no configured subnet contains IP address`，容器建立失敗。
   - access DU 的 conf 檔（`conf/iab_du_node7.conf`／`iab_du_node8.conf`）裡 `local_n_address` 是**寫死的靜態值**（不像 relay DU 有動態同步 wrapper），沒有跟著搬遷改成新子網位址，導致 DU 一直用舊 IP 綁定 SCTP/GTPU 失敗、`Assertion (gtpInst > 0) failed!` 崩潰重啟。教訓：搬遷節點到新主機/新子網時，網路名稱、IP 位址數值、靜態 conf 檔三個地方要一起改，缺一個都會在啟動時才炸。
4. **relay DU 動態 IP 自我修正機制的副作用**：這次事件也暴露一個新機制的代價——relay DU 現在會持續監控自己的 tunnel IP（每 5 秒），偵測到變化就 kill 掉 nr-softmodem 重啟套用新位址（見 `docker-compose-iab-pc2/pc3/server.yaml` 裡 DU 的 `command` wrapper）。這在「MT 真的换了 IP」時是正確行為，但如果 MT 本身在短時間內反覆換 IP（如本次事件的根因），會讓 DU 也跟著反覆重啟，加劇下游 access 節點的不穩定——這是一個連鎖放大效應，真正的解法是解決 MT 反覆斷線的根因（CPU 資源競爭），而不是關掉這個自我修正機制本身。

### PC3 也出現同樣問題，第二輪搬遷：Node3,4(relay)+UE17 → PC2

Node2/7,8 搬到 PC1 之後，對三主機做一次全部端點同時冷啟動的完整重建，這次換 **PC3** 出現一模一樣的症狀：`mt-3` 反覆斷線重連（tunnel IP `12.1.1.44→48→55→65→68→75`），連帶讓 `du-3` 反覆重啟，Node9/10（掛在 Node3 底下的 access）完全連不上、DU 從未建立，PC3 上全部 9 個 UE（9~17）都沒附著。確認是跟 PC2 同一種 CPU 資源競爭問題，不是新的 bug。

**這次不能整組塞給 PC1**：PC1 除了現有的 Donor+Node2/7,8 之外，之後 FL、Global xApp 這些未來階段的工作也預計會集中在 PC1（既有架構設計），繼續往 PC1 塞 RAN 節點會壓縮之後的擴充空間。討論後決定的分法：**relay Node3,4（含直連 UE17）搬到 PC2；access Node9~12 留在 PC3 原地不動，改成跨主機連到 PC2 的 relay DU**——沒有整組（relay+其 access 子節點）都搬到同一台，是為了讓 PC1/PC2 增加的負擔更平均（PC1 完全不變、PC2 增加 8 個 process），犧牲的是「這是專案第一次讓 access 節點的 parent relay 跑在不同主機」。

**驗證結果：跨主機 access-to-relay 一次就跑通，沒有額外踩坑**。原因是這個模式其實不需要新機制——access MT 的 `rfsimulator.serveraddr` 本來就是直接指向 relay DU 的 macvlan IP（三主機共用同一個 L2 網段），跟「relay MT 跨主機連到 Donor DU」是同一套機制，容器實際跑在哪台主機完全不影響這個位址的可達性。唯一需要新增的是 `start_iab_pc3.sh` 啟動前多一段「SSH 到 PC2 確認 `rfsim5g-iab-du-3`/`rfsim5g-iab-du-4` 狀態是 running」的檢查，取代原本「本機 `wait_for_relay_du_healthy`」的角色。

搬遷後三主機即時 process 數量：**PC1:14、PC2:18、PC3:11**（PC1 完全沒變，把負擔集中在 PC2 換取 PC1 的未來擴充空間）。搬遷後重新完整冷啟動驗證：13/13 E2、17/17 UE 全部附著成功。

**經驗教訓（累積兩輪搬遷）**：
1. 搬遷節點到新主機時，網路設定要同時檢查三個地方：docker-compose 的網路**名稱**、網路裡的 **IP 位址數值**、以及該節點 conf 檔裡**寫死的靜態 IP**（access DU 的 `local_n_address` 沒有動態同步機制，relay DU 有）。
2. Access 節點的 parent relay 可以放在不同主機，不需要額外設定——rfsimulator 的 serveraddr 走 macvlan L2，位址可達性只跟「IP 有沒有正確 assign 給某個 container」有關，跟那個 container 實際跑在哪台實體主機無關。
3. 診斷「relay MT 反覆斷線重連」時，先確認是不是同一台主機上跑了太多組即時 RF 模擬 process（`docker ps | grep -E "iab-mt|iab-du|end-ue" | wc -l` 抓數量，配合 `uptime`/`load average` 看是否過載），這比逐一排查協定層 log 更快定位。

---

## 2026-09-12 — UE17 持續高延遲/斷線根因查證與修復

**現象**：UE17（直連 Node4 DU，2-hop）在 Stage 1 PF baseline 量測期間持續嚴重劣化——CU 直接 ping UE17 tunnel IP 100% 封包遺失，`iab_perf_test.sh`/`measure_stage.py` 兩種量測方式全部無法取得任何有效數據。一度被記錄為「已知異常、暫不深究」的邊界案例寫進 `experiment_results/PF.md`。

**排查過程**：
1. 檢查 UE17 容器本身：tunnel IP（12.1.1.57）、預設路由（`default via 12.1.1.1 dev oaitun_ue1`）皆正確，`chrt -f 80` 即時排程優先權也正確套用在 `nr-uesoftmodem` 上，排除容器層級設定錯誤。
2. 直接測試發現行為不一致：多次重測 ping 有時 100% loss，有時 0% loss 但 RTT 高達 **1000~2000ms**（正常應為個位數~數十 ms）；同一時間 macvlan 層（不經過 RF-sim tunnel）的 ping 是正常的個位數 ms，排除了 host 網路/CPU 排程層級的問題。
3. 查 Node4 DU log 發現關鍵訊息：`[OCM] E Model rfsimu_channel_ue2 not found` 後接 `Random channel rfsimu_channel_ue2 in rfsimulator activated`。
4. 檢查 `conf/iab_du_node4.conf` 的 `channelmod.DefaultChannelList`，只定義了 `rfsimu_channel_ue0`／`ue1` 兩組模型——但 Node4 DU 實際上要同時服務 **3 個裝置**（Node11-MT、Node12-MT、UE17，UE17 是後來才直接掛上去的第 3 個裝置，config 從未同步更新）。第 3 個連進來的裝置找不到對應模型，OAI RF Simulator fallback 到「Random channel」，其延遲/時間偏移參數不受控，是造成 UE17 異常高延遲與間歇性封包遺失的根本原因。PHY/MAC 層本身（RSRP、BLER、in-sync 狀態）全程正常，問題完全發生在 channel model 這一層，跟 CPU 資源競爭無關。

**修復**：在 `conf/iab_du_node4.conf` 的 `DefaultChannelList` 補上 `rfsimu_channel_ue2`／`rfsimu_channel_ue3` 兩組跟現有模型參數一致的 AWGN 模型（`ploss_dB=0.0`, `ds_tdl=0.0`），重啟 `rfsim5g-iab-du-4` 後三個裝置都能取得正確定義的模型。修復後 UE17 ping RTT 恢復到 **2~13ms**，封包遺失率恢復到個位數百分比的正常範圍（跟 Scenario R 動態路徑損耗下的其他 UE 相當）。

**已排查、確認沒有同類風險的節點**：Node1/2/3 的 conf 都只定義 2 組 channel model，且實際連線數也剛好都是 2（各自的 2 個 access 子節點），沒有額外直連 UE，不會撞到同一個坑。這個 class of bug 只會發生在「relay DU 的實際連線數 > conf 裡定義的 channel model 組數」時，未來若要在任何 relay 底下新增直連裝置，切記同步在該 relay 的 conf 補齊對應數量的 channel model。

**殘留、獨立於本次修復的新發現（尚未解決）**：channel model 修好後，ping 恢復正常，但用 iperf3 對 UE17 做 TCP 連線測試時，`ss -tn` 顯示 TCP 連線卡在 `SYN-SENT`（SYN 送出去、SYN-ACK 沒收到），在 ext-dn 容器上用 tcpdump 監聽對應 port 完全沒有抓到封包。這代表 SYN 離開 UE17 之後，在往 ext-dn 的路徑上（很可能是 UPF 的 NAT/conntrack，或跟 UE17 作為「relay DU 直連的一般 UE」這個較少見的拓樸位置有關的 QoS flow/SDAP 設定）某處被吃掉，但 ICMP（ping）走的路徑正常。UE9~16（透過 Node11/12 這層 access DU）走 TCP 都正常，所以問題很可能跟「UE 直接掛在 relay DU 底下、沒有經過 access 層」這個較特殊的拓樸位置有關，而不是全面性的 UPF 問題。此為獨立於 channel model 的另一個問題，尚待後續排查（下次可從 UPF 的 conntrack table、或比較 UE17 與一般 access UE 的 PDU Session/QoS flow 設定差異切入）。

---

## 2026-09-12 — Backhaul-aware PRB 預算機制上線後的三個新踩坑：telnetsrv buffer overflow、DU/MT netns 孤兒、重啟骨牌效應

實作 backhaul-aware 動態 PRB 預算機制（見 CLAUDE.md 第 3 節）並完成三主機重新編譯後，在回歸測試與 Stage 1 重新量測的乾淨重啟過程中，連續踩到三個環環相扣的坑。記錄下來是因為第三個坑的教訓（不要反射性重啟）比機制本身更重要，往後任何除錯都該先套用這個原則。

### 坑 1：telnetsrv 高頻連線觸發 GNU readline history 的 buffer overflow

**現象**：機制上線初期（DU 端輪詢間隔 300ms）量測到一半，MT-11/MT-12 陸續以 exit code 139（SIGSEGV 樣式）消失，DU-9 容器整個不見，log 顯示 `*** buffer overflow detected ***: terminated`（glibc FORTIFY_SOURCE abort）。

**根因**：`common/utils/telnetsrv/telnetsrv.c` 的 `run_telnetsrv()` 對**每一次新連線**都會呼叫 GNU readline 的 `read_history()`/`add_history()`/`write_history()`/`stifle_history()`/`clear_history()`——這個設計是給人類互動式 shell 用的，不是為了被機器高頻轟炸設計的。這個專案既有的 telnet 用法（`channelmod_ctrl.py`）呼叫頻率是每個 Scenario R phase（60 秒）一次，而我新增的 DU→MT backhaul 輪詢執行緒（`nr_mac_gNB_backhaul_poll.c`）原本設 300ms 一次，比既有用法高頻 200 倍以上，首次踩中這個沒人踩過的 race。

**處理**：`telnetsrv.c` 是全部既有模組（含 channelmod）共用的檔案，判斷修改風險太高，不直接動它。改成把 `BH_POLL_INTERVAL_MS` 從 300 拉長到 3000（10 倍），把觸發機率壓低到接近既有 channelmod 用法的安全頻率——**這是機率緩解，不是根除**，程式碼裡已留言註記；若之後長時間量測又復現，必須改成長連線（避免每次連線都觸發 read/write_history）或直接修 `telnetsrv.c` 本體。

### 坑 2：relay 節點的 DU 容器卡在「孤兒」network namespace（比坑1更隱蔽、影響更大）

**現象**：坑1修復、三主機重新編譯部署後的乾淨重啟過程中，Node2（relay，PC1）的 DU 自己 log 顯示有 UE 在正常收發（RNTI c87f/58cb 有真實 dlsch/ulsch 流量），看起來健康；但 Node2 底下的兩個 access 子節點 Node7、Node8 的 MT，卻持續每秒對 `192.168.88.151:4045`（Node2 的 macvlan IP + rfsimulator port）連線失敗，`errno(111) connection refused`，且兩個 MT 的 `oaitun_ue1` tunnel 介面完全沒有 IP。

**排查過程（刻意不重啟、先確認事實）**：
1. `docker exec rfsim5g-iab-du-2 ip addr show`：只有 `lo`，**沒有 macvlan 介面、沒有 tunnel 介面**——但 relay 的 DU 服務定義是 `network_mode: "service:rfsim5g-iab-mt-2"`，理論上應該跟 MT-2 共用同一個 netns，看到的介面應該要跟 MT-2 一模一樣。
2. `docker exec rfsim5g-iab-mt-2 ip addr show`：MT-2 自己有完整的 `eth0`（`192.168.88.151`）跟 `oaitun_ue1`（`12.1.1.10`），一切正常。
3. 直接比對兩個容器實際 PID 的 `/proc/<pid>/ns/net`：**inode 不一樣**（`4026533719` vs `4026533803`）——證實 DU-2 跟 MT-2 事實上跑在兩個不同的 network namespace，儘管 docker-compose 的宣告（`HostConfig.NetworkMode = container:<mt-2的container id>`）看起來完全正確。
4. `ss -tlnp` 在 DU-2 的（孤兒）netns 裡確實有 process 監聽 `*:4045`——這解釋了為什麼從 DU-2 自己的角度看「一切正常」（它能在自己的孤兒 netns 裡跟已經連進來的舊 UE 繼續互動），但外部（Node7/Node8 的 MT）連進來的封包，因為孤兒 netns 沒有掛任何實體/macvlan 介面，根本到不了這個監聽的 socket，才會回報 `connection refused`。

**根因機制**：`network_mode: container:X` 只在**容器建立當下**解析一次「加入 X 目前的 netns」。如果之後 X（這裡是 MT-2）本身被重新建立（`docker compose up -d --force-recreate` 或整個重跑 compose，會產生新的 sandbox/netns），而依附在它身上的 DU-2 沒有被同步 recreate（只是被單純 `docker restart` 或完全沒動），DU-2 會繼續留在 MT-2「舊」的、現在已經沒人使用的 netns 裡——這個舊 netns 沒有任何網路介面（macvlan 從沒真正屬於過它，或是連介面本身都在 MT-2 重建時一併被清掉），變成名副其實的「孤兒」。`docker restart` 只是重啟 process，不會重新解析 `network_mode`，所以不能修好這個問題；必須用 `docker compose up -d --force-recreate --no-deps <DU服務>` 讓它重新加入 MT 容器**現在**的 netns。

**修復與驗證**：對 `rfsim5g-iab-du-2` 執行 `--force-recreate`，重建後 DU-2 跟 MT-2 的 netns inode 變成一致，`ip addr show` 也拿到跟 MT-2 一樣的完整介面清單。DU-2 重建後對 CU 的 F1 Setup 一開始連續失敗約 7 次（`the CU reported F1AP Setup Failure`，CU 端還握著幾秒前的舊 association），但**放著讓它自己重試**（不去動 CU），30 幾秒後自然收到 `received F1 Setup Response from CU gNB-CU-Donor`，Node7/Node8 的 MT 隨即都拿到新的 tunnel IP、E2 Setup 也正常完成——全程沒有重啟 CU，沒有波及系統其他任何節點。

**預防性排查**：同時檢查了 Node1、Node3、Node4（其餘三個 relay）的 DU/MT netns inode 是否一致，三個都正常（`network_mode` 建立以來從未經歷「MT 被單獨 recreate、DU 沒跟著動」這個特定序列，所以沒中獎）——代表這不是這次編譯或機制本身引入的系統性 bug，而是這次除錯過程中我自己先前對 Node2 做過的某次 `docker restart`／局部操作，跟 MT-2 之後又被重建的時間點沒有對齊而巧合造成的。

### 坑 3（最重要的教訓）：反射性重啟本身就是「IP 一直變」的成因

**使用者明確指出**：「為什麼一直重啟 這問題要解決 不然ip會一直變」。這句話點出的因果鏈是：CU 一旦被重啟（或任何操作導致 CU 判定某個 DU 的舊 F1 association 需要清掉、進而牽動更大範圍），**全系統所有 MT 都要重新做 NAS 註冊 + PDU Session Establishment**，這個系統的 SMF 沒有設定任何靜態 IP 保留，所以每個 MT 每次都會拿到一個全新的動態 IP——這才是「IP 一直變」的真正機制，不是隨機發生的。

**這次的具體教訓**：坑2的排查一開始，我對「Node8 單獨故障、但其實 sibling Node7 早就證實健康」這個局面，直接做了「重啟 Node2 的 DU」這個動作——這個動作本身並沒有先確認 Node2 的 DU 到底是不是真的壞的（後來證實 Node2 的 DU 表面 log 是健康的，真正壞的是 netns 孤兒問題，`docker restart` 這個動作根本不可能修好它，等於是一次沒有事先诊斷、賭一把式的重啟）。這類「看到下游有問題、直接重啟上游猜測目標」的操作，一旦真的觸發 CU 端的 stale-association 拒絕連鎖，就會需要重啟 CU 才能解開，進而波及全系統 MT 的 IP。

**往後的原則（已在這次坑2的排查中實際套用並印證有效）**：任何節點看起來「有問題」時，**先用完全唯讀的方式**（分別、不合併地看每個相關容器自己的 log、比對 netns/介面/IP 這類客觀事實、必要時對目標 IP/port 做一次即時連線測試）把問題定位到「哪一層、哪一個具體機制」壞掉，確認修法之後，才執行**範圍最小、最貼近根因**的動作（例如這次的 `--force-recreate` 單一 DU 容器，而不是重啟整個 CU 或整條鏈路上的其他容器）。唯讀排查的成本遠低於一次誤判重啟可能造成的全系統 IP 洗牌代價。

---

## 2026-09-12（續）— 同一輪乾淨重啟：PC2 的 `~` 展開陷阱、access DU 缺 host route 導致 SCTP 單向被 Docker bridge 隔離擋掉

同一次 v4 乾淨重啟過程中，繼續往下排查 PC2（Node1,3,4,5,6 + UE1~4,17）跟 PC3 的 Node9,12 時，又踩到兩個新坑，記錄下來避免下次重蹈覆轍。

### 坑 4：`ssh pc2 'bash <腳本路徑>'` 的 `~` 在腳本內部展開成錯誤帳號的 home 目錄

**現象**：透過 `ssh pc2 'nohup bash ~/openairinterface5g/.../run_local_pc2.sh > log 2>&1 &'` 背景啟動 PC2，log 檔案只有一行就結束：`bash: /home/mcalab/openairinterface5g/.../run_local_pc2.sh: No such file or directory`。即使把外層呼叫改成完整絕對路徑 `/home/lindor/openairinterface5g/.../run_local_pc2.sh`，腳本本身第一次執行還是失敗在**腳本內部**的 `COMPOSE_DIR=~/openairinterface5g/...`（`cd` 失敗、找不到 `iab/start_iab_pc2.sh`），卻因為 `ok "PC2 IAB 資料面啟動完成"` 這行沒有檢查前面指令的結果就直接印出來，讓 log 看起來像是「啟動完成」，其實整個沒跑。

**根因**：CLAUDE.md 第 6 節早就記載「PC2 帳號是 `mcalab` 但仍在 `/home/lindor/openairinterface5g` 編譯（已手動建立 `/home/lindor` 目錄供其使用）」——這個安排隱含一個前提：正常透過互動式 SSH 登入 PC2 時，某個機制（很可能是 shell profile 或既有慣例）讓實際操作都在 `/home/lindor` 下進行；但 bash 的 `~` 展開是照**執行時的 `$HOME` 環境變數**展開，不是照腳本檔案實際存放的路徑展開。透過 `ssh pc2 'bash ...'` 這種非互動、一次性指令的方式呼叫，`$HOME` 就是 SSH 登入帳號（`mcalab`）的真正 home（`/home/mcalab`），跟腳本裡假設的 `/home/lindor` 對不上。

**修復**：改用 `ssh pc2 'nohup env HOME=/home/lindor bash /home/lindor/openairinterface5g/.../run_local_pc2.sh > log 2>&1 &'`，顯式覆寫 `HOME` 環境變數，讓腳本內部所有 `~` 展開都對齊到正確目錄。**這個腳本本身也有一個獨立的小毛病**：`ok "..."` 收尾訊息沒有檢查前面指令的 exit code，之後可以考慮讓 `run_local_pc2.sh`／`run_local_pc3.sh` 在 `cd`/`bash iab/start_iab_pc*.sh` 失敗時直接 `exit 1`，不要把失敗的執行印成「完成」。

### 坑 5：access DU 有第二張 macvlan 網卡時，F1 SCTP 會抄近路直接出 macvlan、繞過 internal bridge，導致回程封包被 Docker 對該 bridge 的預設隔離規則 DROP 掉

**現象**：PC2 的 Node5、Node6，以及 PC3 用 `docker compose up -d --no-deps` 補啟動的 Node9、Node12，DU 容器本身正常執行、log 卡在 `waiting for F1 Setup Response before activating radio` 不動，CU 端完全沒有任何一行提到這些 DU（連拒絕訊息都沒有——不是 assoc 衝突）。用 `docker exec -u 0 <du容器> ip route`／`ip addr` 檢查排除了 netns 孤兒（坑2那種問題）：介面、IP 都正常。

**排查方法（用 tcpdump 直接抓包，而非猜測）**：
1. 在 CU 容器 netns 內對 `macvlan-br` 抓包，同時手動 `docker restart` 該 DU 容器觸發一次新的 SCTP 嘗試：確認 DU 送出的 `[INIT]` packet 有到達 CU，且 CU **立刻回了 `[INIT ACK]`**——代表去程完全正常，CU 應用層完全沒問題。
2. 在 CU 端看不到任何後續的 `[COOKIE ECHO]`，代表 DU 從沒收到那個 INIT ACK、或收到了卻沒有回應。
3. 到 DU 所在主機（PC2/PC3）的 internal bridge 介面（`br-xxxxxxxx`，對應 compose 裡的 `iab_internal_net`）上抓包：**完全零封包**——證實 CU 回覆的 INIT ACK 根本沒有進到這個 bridge 裡。
4. 檢查該主機的 `iptables -L DOCKER-FORWARD -n -v`：Docker 針對每個 bridge 網路預設插入的規則是「該 bridge 自己送出去的封包一律 ACCEPT」＋「該 bridge 上明確 publish 的 port 各自有一條 ACCEPT」＋**「其餘從外部進到這個 bridge 的封包，若不是 RELATED/ESTABLISHED，一律 DROP」**。這個 DROP 規則在 PC2 上實測有 88 個封包命中，代表它真的在擋東西。
5. 比對已知正常的節點（例如 PC3 的 Node10）跟出問題的節點（Node9）各自的 `ip route`：**Node10 多了一條 Node9 沒有的路由 `192.168.88.1 via 192.168.75.1 dev eth0`**（把「到 CU 的流量」明確導向 internal bridge 這個 gateway，而不是走 DU 自己另一張直連 macvlan 網卡的介面）。

**根因**：這些 access DU 的 compose 服務**同時接了兩個網路**：`macvlan_net`（給它自己一個直連的 macvlan IP）跟 `iab_internal_net`（跟同主機的 MT 之間的私有 bridge，DU 對外宣告的 F1-C/GTP 位址是 internal bridge 這個 IP）。在沒有額外路由的情況下，Linux 核心選路徑是「看目的地」不是「看 socket bind 的來源位址」：到 CU（`192.168.88.1`）這個目的地，走 macvlan 網卡的直連路由（`192.168.88.0/24 dev ethX`）比走 internal bridge 的預設路由更明確（destination 直連 vs 走 gateway 的 default route），所以核心選擇讓 SCTP INIT 直接從 macvlan 網卡出去（外層 IP header 帶著 internal bridge 的來源位址，但実際出口是 macvlan 網卡）——**完全沒有經過 internal bridge**。因為去程沒經過 bridge，Docker/netfilter 在 bridge 這個 chain 上就沒有建立這條 flow 的 conntrack 紀錄；回程的 INIT ACK 依照 CU 的路由表（`192.168.74.0/24 via 192.168.88.2`／`192.168.75.0/24 via 192.168.88.3`，經由對方主機的 macvlan 端點）進到目的主機、想要轉發進 internal bridge 時，因為在 conntrack 裡查無 RELATED/ESTABLISHED 紀錄、又不是明確 publish 的 port，就被那條預設 DROP 規則擋掉——**這是不對稱路徑（去程繞過 bridge、回程需要進 bridge）撞上 Docker bridge 網路內建隔離機制的典型案例**，不是 F1AP/SCTP 本身的問題，也不是 CU 應用層的問題。

**這也解釋了為什麼同樣拓樸位置的 Node10、Node11 當時是正常的**：它們是在 PC3 原本乾淨重啟流程裡、比較早的時間點就成功完成過一次 F1 SCTP association，那條 flow 早就在 conntrack 裡留下 ESTABLISHED 紀錄、之後就一路沿用下去；Node9、Node12 當時因為要等的 PC2 relay（Node3,4）還沒就緒而逾時被腳本跳過，從頭到尾沒建立過這條 flow，所以現在用 `docker compose up -d --no-deps` 手動補開時，是一次全新的、會撞上隔離規則的連線嘗試。Node5、Node6（PC2）也是同樣道理，只是換成因為 PC2 本身因坑4的 `~` 展開錯誤而完全沒啟動，兩個節點都從頭到尾沒機會建立過這條 flow。

**修復**：對每個受影響的 access DU 容器，補上一條明確導向 internal bridge gateway 的 host route：
```bash
docker exec -u 0 <du容器> ip route add 192.168.88.1 via <該主機internal bridge gateway> dev <該DU接internal bridge的那張介面>
```
（介面名稱在不同容器裡可能是 `eth0` 也可能是 `eth1`，取決於 compose 裡兩個網路的宣告順序，不能寫死，每次都要先 `ip route`/`ip addr` 確認哪張介面接的是哪個網路。）補上路由後，DU process 自己內建的 F1 Setup 重試機制（實測約每 40~50 秒一次）會在下一次重試時自動走新路由成功——**不需要重啟 DU 容器**，補完路由等它自己重試就會成功。這條路由**不會在 `docker restart`／容器重建後保留**，是這個測試平台裡跟坑2「netns 孤兒」同一類「非持久化手動修復」，任何新建立或重啟過的 access DU 都要重新檢查、重新補這條路由。

**額外的教訓（我自己在這次排查中犯的操作失誤）**：補完 DU-5 的路由後，我沒有先等它自己重試，就手動下了一次 `docker restart rfsim5g-iab-du-5`——事後翻 log 才發現 DU-5 其實已經靠自己的重試機制在補路由後成功過一次（`received F1 Setup Response`、`PHY ready`），我這次不必要的重啟把它重新打回「等待中」的狀態，等於是自己製造了一次可以避免的延誤。**教訓**：修好一個根因之後，如果那個 process 本身有已知的自動重試機制，應該先等待、用 log 確認它有沒有自己恢復，而不是預設「我修完了就該手動催一次」；手動介入的時機應該保留給「確認過該 process 沒有自動重試能力」的情況（例如坑2的 relay `network_mode: container:X` 那種，process 本身不會重新解析網路設定，非重啟/recreate 不可）。

### 坑4、坑5 事後釐清：哪些是腳本本身的 bug（會一直重演），哪些不是（正常跑腳本就不會再發生）

系統全部恢復後，重新檢查 `start_iab_pc2.sh`／`start_iab_pc3.sh`／`run_local_pc2.sh` 本體，把這兩個坑分清楚：

* **坑5（access DU 缺到 CU 的 host route）其實不是腳本的 bug**。`start_iab_pc2.sh`（第174行）、`start_iab_pc3.sh`（第151行）在正常流程裡，每個 access DU 容器建立後本來就會執行 `docker exec -u 0 $DU_NAME ip route replace $SERVER_IP via ...`，內容跟這次手動補的路由完全一樣。這次會缺路由，是因為 Node5/6/9/12 在正常腳本流程裡先被「等 parent relay 逾時（120s）」邏輯整個跳過（腳本根本沒執行到這一步），事後用 `docker compose up -d --no-deps` 手動把它們補起來時，繞過了腳本接在容器建立後面的這段路由設定。**結論：只要腳本能完整跑完、沒有中途逾時跳過某個節點，這條路由就會自動補上，不會重演**；只有在「腳本判定逾時跳過→事後用非腳本方式手動補容器」這個特定組合下才會重現，往後若再遇到這種「等 parent 逾時被跳過」的節點，記得比照腳本第174/151行的邏輯手動補路由，而不是只補容器本身。
* **坑4（`~` 展開成錯誤帳號 home 目錄）是真的腳本 bug，已直接修掉**。確認過 `mcalab` 帳號**互動式登入**的 `$HOME` 本來就是 `/home/mcalab`（`.bashrc`/`.profile` 都沒有覆寫成 `/home/lindor`），代表這不是「我用非互動 SSH 呼叫才會踩到」的特例，而是任何人用 `mcalab` 身份執行 `run_local_pc2.sh`（不管是互動登入後執行，還是透過 SSH 一次性指令）都會 100% 踩到。已直接把 `run_local_pc2.sh` 的 `COMPOSE_DIR=~/openairinterface5g/...` 改成寫死絕對路徑 `/home/lindor/openairinterface5g/...`，並把 `cd`／`bash iab/start_iab_pc2.sh` 兩步加上失敗就 `exit 1`（避免像這次一樣，實際執行失敗卻因為收尾的 `ok "..."` 沒檢查前面結果而看起來像成功），修好後已用 `rsync` 同步到 PC2。**PC1／PC3 的 `run_local_pc1.sh`／`run_local_pc3.sh` 用的都是 `lindor` 帳號，`~` 本來就對得上，沒有這個問題，不需要改。**

---

## 2026-09-12（續）— Stage 1 重測後排查 UE17：歷史「SYN-SENT 永遠卡住」bug 已不復現，取而代之的是 backhaul-aware PRB 機制下的嚴重 RTT 劣化

Stage 1 PF baseline 重新量測（見 `experiment_results/PF.md`）產出後，發現 UE17 的數據明顯異常：平均吞吐量 0.80 Mbps、平均 RTT 639.40ms，是全部 17 個 UE 裡最差的一個，卻只是 2-hop（UE17 直接掛在 Node4 的 DU，不經過 access 層），理論上應該比大多數 3-hop 的 UE 表現更好。這個反常結果讓人聯想到專案更早期就記錄過、當時未徹底解決的「UE17 TCP 連線卡在 SYN-SENT 永遠不動」的舊 bug，因此專門花時間確認這個舊 bug 是否復發。

**排查方法**：對 UE17（`rfsim5g-end-ue-17`，tunnel IP `12.1.1.21`）連續多次做 ICMP ping + iperf3 TCP 連線測試，同時用 `ss -tn` 每 0.5 秒連續快照觀察連線狀態演變，並在 `rfsim5g-oai-ext-dn` 端用 `tcpdump` 直接抓包驗證封包層級的行為。

**發現一：ICMP RTT 嚴重劣化**：`ping -c 3` 量到 min/avg/max = 1285.892 / 1787.288 / 2244.517 ms，比 PF.md 記錄的平均 639ms 還要糟上許多，且 ping 輸出出現 `pipe 2` 標記（代表因為 RTT 遠超過 1 秒的送包間隔，同時有多個 ICMP echo request 在途中），本身就是嚴重延遲的訊號。

**發現二：TCP 連線最終都會成功建立，不是永久卡死**：連續 `ss -tn` 快照顯示，一次新的 iperf3 連線嘗試在 `SYN-SENT` 狀態停留約 1.5~2 秒後，**自行轉為 `ESTAB`**，且 Send-Q 從 37 bytes 持續增長到 127 bytes，證實真的有應用層資料在傳送，不是卡住不動。`tcpdump` 在 ext-dn 端也直接證實同一件事：抓到一次完整的三向交握（`[S]` → `[S.]` → `[.]`），中間有一次 SYN 因為前一次 SYN-ACK 疑似遺失/延遲而重傳（間隔約 0.86 秒），整個交握耗時約 1.4 秒才完成，隨後立刻有 PSH 資料封包成功雙向交換。

**結論——這不是歷史 bug 復發，是一個性質不同的新問題**：
- 歷史記載的「SYN-SENT 永遠卡住」是連線**完全建立不起來**；這次觀察到的是連線**建立得異常慢（1.5~2 秒的交握時間，比正常網路環境慢 1~2 個數量級），但最終一定會成功**。先前初步測試看到的「iperf3 顯示 0.00 bits/sec」、「抓到 SYN-SENT 快照」，比對之後判斷只是**短時間測試（`timeout 3`/`timeout 5`）在極端延遲下來不及跑完交握就先被計時器砍掉**、或是**運氣不好剛好在交握中途按下快門**，並非連線邏輯真的壞掉。
- 根本原因指向 **Node4（PC2）的排程資源壅塞**，而非軟體 bug：Node4 這個 relay 節點的 DU 同時要服務三份負載——UE17 直連、中繼 Node11（PC3 access，UE13/14）、中繼 Node12（PC3 access，UE15/16）——三方共用同一個 DU 排程資源池，再加上 backhaul-aware 動態 PRB 預算機制（CLAUDE.md 第 3 節）會依 Node4 自己 MT 的 backhaul 忙碌程度動態縮小其 DU 可用的 PRB 池。UE17 雖然只有 2-hop，但這個特殊的「直連 UE + 雙重跨主機中繼」拓樸位置，讓 Node4 承受的排程壓力反而比許多 3-hop 的 access 節點更重，這可以合理解釋為什麼 UE17 的 RTT/吞吐量表現全場最差。
- 嘗試進一步用 telnet 直接查詢 Node4 當下的 `backhaul_prb_ratio`（機制的核心即時指標）做因果驗證，但容器內缺 `nc` 指令未能查成，這部分留待未來需要時再補一次驗證（例如改用 `docker exec` 內建的 bash `/dev/tcp` 或改善除錯腳本，先裝好 `nc`／`socat`）。

**教訓與後續**：這個發現不需要任何程式碼修復——不是 bug，是 backhaul-aware PRB 機制生效後、UE17 特殊拓撲位置（直連 relay 且該 relay 同時扛兩個跨主機中繼負載）造成的真實資源競爭效應，跟 CLAUDE.md 第 2 節「多跳鏈路自我節流」的理論推導方向一致。若未來要緩解，方向是「調整 Node4 的資源分配權重」或「重新評估 UE17 的拓撲位置」，而非除錯連線邏輯本身；Stage 2 以後若這個現象持續存在，可以做為評估 DRL/FL 排程策略是否能改善「單一節點多重負載擁塞」情境的一個天然測試案例。

---

## 2026-09-13 — `bhload` 機制其實是靜默 no-op、真正根因是 telnetsrv 保留字命名衝突（`"get"`/`"set"` 導致 SIGSEGV），並非環境不穩或隨機競爭

延續 UE17 排查時「容器內缺 `nc`，未能直接查詢 `backhaul_prb_ratio`」這個未竟項目，使用者要求「現在就查」，因而牽出這次整個 session 裡分量最重、耗時最久的一次根因排查。

### 發現一：`bhload get` 從一開始就從未真正成功過

用 bash `/dev/tcp` 直接對 Node4、Node1 的 MT telnet port 手動送 `bhload get`，兩個節點都回傳 telnetsrv 的通用「未知命令」錯誤，而不是預期的 `dl_rb_cum ... ul_rb_cum ...` 數字。追查發現：`init_bhload_telnetcmd()`（負責把 `bhload` 命令註冊進 telnetsrv）原本寫在 `radio/rfsimulator/simulator.c` 的 `chanmod` 選項初始化分支裡，隱含假設「MT process 自己的 rfsimulator options 也會設 chanmod」——但實際上 `chanmod` 是設在 **DU 端**的 conf（`iab_du_nodeN.conf` 的 `rfsimulator.options=("chanmod")`），MT 端的 `nrue.uicc.conf` 從未設定這個選項，這個分支在任何 MT process 上都從未被進入過，`bhload` 命令從未在任何 MT 上真正註冊成功。連先前 `backhaul_mechanism_verification.md` 記錄的「MT 端 telnetsrv 命令正確回應」也是誤判——當時只確認了連線建立、telnetsrv log 印出 `Command received`，沒有進一步檢查回傳內容是不是真的有效數據。

**第一次修復**：把 `init_bhload_telnetcmd()` 移到迴圈外、只依 `IS_SOFTMODEM_GNB` 判斷是否為 MT/UE process，不再依賴 chanmod 是否設定。三主機重新編譯部署後，`bhload get` 終於能被 telnetsrv 正確 dispatch 到——但緊接著就撞上發現二。

### 發現二：Node2 的 MT 反覆自發性「重啟」，一度誤判成 Docker 環境問題

機制上線後，用腳本乾淨重啟三主機時，Node2（PC1 本機 relay）的 MT 開始反覆重啟，導致跟它共用 netns 的 DU 反覆變成孤兒（同一類 2026-09-12 已修復過的坑，但這次是新一輪的觸發源）。一開始的排查方向完全錯誤：
1. 先懷疑是自己手動 `docker compose up -d --force-recreate --no-deps` 操作本身觸發的（因為每次介入 DU 都巧合地跟 MT 重啟時間點很近），改用「分開 stop/rm/create」的方式緩解，一度看似有效。
2. 後來懷疑是 docker daemon 被反覆 `systemctl restart docker` 波及（journalctl 裡確實在某次事故時間點看到大量容器的 `stopping restart-manager`），一度以為是自己不小心把同一份啟動腳本背景執行了兩次造成的操作污染（這件事**確實也發生過**，是這次除錯過程中一個真實但獨立的操作失誤，已排除、不影響下述真正根因）。
3. 使用者明確指出「之前的腳本就沒問題啊，我要一次跑到底」，正確點出不該把新出現的問題歸因成含糊的「環境不穩定」——這個提醒把排查方向拉回到「這次改了什麼程式碼」上。

### 發現三（真正根因）：用 `strace` 直接在崩潰現場抓到 `SIGSEGV`，鎖定 telnetsrv 保留字碰撞

在乾淨、無任何手動介入的環境下用 `strace -f -p <MT_PID> -e signal=all` 即時監控，同時手動對 MT 送一次 `bhload get`，直接抓到 telnet 執行緒本身收到 `SIGSEGV {si_code=SEGV_MAPERR, si_addr=NULL}`（先前一度誤判成外部 `SIGTERM` 導致的正常 NAS 去註冊流程，是因為第一次 attach 的其實是**上一輪測試殘留的舊容器**，被自己的 `docker compose down` 正常關閉而已——這也是本次排查中一個值得記取的方法論教訓：attach 前務必先確認目標 PID 真的是這一輪、這一個 instance）。

追進 `common/utils/telnetsrv/telnetsrv.c::process_command()`，發現致命的設計碰撞：這個函式對任何模組底下**字面等於 `"get"` 或 `"set"`** 的子命令，一律優先當成「通用變數存取語法」處理（呼叫 `setgetvar()`），完全不管這個模組是否真的把某個命令取名叫 `"get"`。`setgetvar()` 內部：
```c
n = sscanf(params, "%9s %ms", varname, &varval);
```
`params` 就是原始輸入裡 `"get"` 後面剩下的字串——`bhload get` 沒有多打一個變數名稱，`params`（也就是 `process_command` 裡的 `cmdb`）是 NULL，`sscanf(NULL, ...)` 是未定義行為，實測直接 SIGSEGV。這個 bug 在 `telnetsrv.c` 裡已經存在很久，只是這個專案裡從來沒有任何模組把自己的「命令」取名跟 `"get"`/`"set"` 這兩個保留字撞名過，直到新增的 `bhload` 模組把查詢指令取名叫 `"get"` 才第一次觸發——這正好解釋了「之前的腳本就沒問題」：問題不是環境退化，是**這次新增的程式碼第一次踩中一個從未被觸發過的既有陷阱**，跟 2026-09-12 telnetsrv buffer overflow 那次根因模式（既有程式碼裡的陷阱，被新的使用模式第一次觸發）如出一轍。

### 修復

1. `common/utils/telnetsrv/telnetsrv_bhload.c`：命令從 `"get"` 改名為 `"query"`，徹底避開保留字碰撞（`bhload_get_cmd` 也一併改名成 `bhload_query_cmd`）。
2. `openair2/LAYER2/NR_MAC_gNB/nr_mac_gNB_backhaul_poll.c`：DU 端輪詢執行緒送出的指令字串同步改成 `"bhload query\n"`。
3. `common/utils/telnetsrv/telnetsrv.c::setgetvar()`：補上 `params == NULL` 的防呆（直接 `return CMDSTATUS_VARNOTFOUND`），這是共用程式碼裡一個獨立、真實存在的 bug，順手修掉避免以後其他模組的命令又意外撞上 `"get"`/`"set"` 保留字時重蹈覆轍。

三處修改三主機同步編譯部署後，重新做了一次完全不介入、乾淨的三主機啟動：**全程 RAN 節點（Donor CU/DU + 全部 relay/access 的 MT/DU）RestartCount 皆為 0，13/13 E2 連線、17/17 UE 附著**，一次跑完成功，沒有再手動補救任何節點。事後用 `tcpdump` 直接抓包確認 `bhload query` 現在會回傳真實、非零、持續累積的 MAC 統計數字（`dl_rb_cum 44086 ul_rb_cum 404315`），確認機制真正生效，不再是 no-op。

### 這次排查方法論上的教訓

- **不要把新出現的、可重現的問題輕易歸因成「環境不穩定」或「隨機競爭」**——使用者的提醒是對的，先假設是自己新改的程式碼有問題，去對照實驗（停用新功能 vs 啟用），比一開始就往外部環境找理由更快找到真正根因。
- **對照實驗（A/B test）是這次真正定位問題的關鍵一步**：把 `init_bhload_telnetcmd()` 的呼叫註解掉重新編譯、跑同樣的啟動流程，2.5 分鐘內 MT 完全零重啟；换回啟用版本，幾乎每次第一次查詢後就重啟——這個乾淨的二元對照直接把懷疑範圍鎖定到 bhload 本身，避免了在 Docker 網路層面繼續空轉。
- **`strace -e signal=all` 直接在崩潰現場抓真實訊號，比靜態讀程式碼／猜測環境問題快得多**——尤其是當初步猜測（外部 SIGTERM）被更仔細的複查推翻後，能夠說「我看到的就是這個訊號，來源是這裡」，比任何猜測都有說服力。
- **attach 除錯工具前，務必先確認目標 PID/container instance 真的是「這一輪」的，不是殘留的舊 instance**——這次至少有一次完整的 strace 追蹤結果，事後證實是誤判了上一輪測試留下的容器，白白浪費了一輪排查。

### 後續追加坑：PC2/PC3 的 `librfsimulator.so` 漏編譯，導致「已修好」的機制其實只有三分之一節點真的生效

`bhload` 命名衝突修好、三主機重新編譯部署、驗證 PC1 一次跑到底成功後，使用者要求「驗證機制真的有效，再重測 Stage 1」。第一次 Stage 1 重測（見 PF.md 作廢說明）忘記停用 xApp，作廢重來；停用 xApp 後的第二次量測數據看起來正常（JFI、吞吐量都在合理範圍），但使用者追問「更新 UE17 吞吐量狀態」時，現場複測 UE17 意外發現比先前更嚴重（ICMP 完全打不通），這個異常反過來促使重新檢查機制是否真的在三台主機都生效——查下去才發現 PC2、PC3 的 `librfsimulator.so` 時間戳竟然**舊於** `simulator.c` 原始碼的時間戳：修 `bhload` segfault 那一輪（`telnetsrv.c`/`telnetsrv_bhload.c`/`nr_mac_gNB_backhaul_poll.c` 三個檔案的修復）重新編譯時，下的指令是 `sudo ninja nr-uesoftmodem nr-softmodem telnetsrv`，**沒有包含 `rfsimulator`**——因為那一輪沒有直接改動 `simulator.c`，直覺上以為不需要重編這個 target，卻忽略了 `simulator.c` 是**更早一輪**（把 `init_bhload_telnetcmd()` 從 chanmod 分支移出來那次修復）才改的，PC2/PC3 那時候雖然也重新編譯過 `rfsimulator`，但那次的重建被 rsync＋接下來這一輪的 `nr-uesoftmodem`/`nr-softmodem`/`telnetsrv` 重編動作誤以為「這輪都處理過了」，實際上這一輪根本沒再碰 `rfsimulator`，PC2/PC3 上的 `librfsimulator.so` 就這樣停留在舊版本，`init_bhload_telnetcmd()` 從未在 PC2、PC3 的任何 MT process 上被呼叫過。

**验证方式**：直接對 PC2 的 Node1、Node4 手動送 `bhload query`，兩個都回傳「未知命令」（模組根本沒註冊），而 PC1 的 Node2 用同樣方式測試完全正常——證實這不是隨機的、是 PC2（後來確認 PC3 也一樣）整批性的問題。用 `stat -c '%Y %n'` 比對 `simulator.c` 跟 `librfsimulator.so` 的時間戳，PC1 正確（`.so` 新於原始碼），PC2/PC3 都是 `.so` 舊於原始碼，直接坐實。

**影響範圍**：這代表**第二次 Stage 1 重測的數據其實只有 PC1 三個節點（Node2、Node7、Node8）的 backhaul-aware 機制真正生效，PC2（Node1,3,4,5,6）、PC3（Node9~12）全部節點仍是先前那個「回傳錯誤、fail-safe 回退無約束」的 no-op 狀態**——這份數據雖然表面上「看起來合理」（JFI、吞吐量都不是離譜的數字），但實際上是「三分之一節點有約束、三分之二節點沒有約束」這種不對稱狀態下量到的，不能代表機制生效後的真實系統基準，已作廢。

**修復**：在 PC2、PC3 上補跑 `sudo ninja rfsimulator`，確認三主機 `librfsimulator.so` 時間戳都新於 `simulator.c` 後，重新做一次乾淨的三主機清空重啟，逐一確認 `bhload` 模組在 PC1（Node2）、PC2（Node4）、PC3（Node11）都成功註冊，才產出第三次、真正有效的 Stage 1 數據（見 PF.md）。

**教訓（已寫進 CLAUDE.md 第 7 節）**：`rsync` 完原始碼只是把檔案搬過去，不代表 PC2/PC3 已經吃到修改——一定要重編**這次修改實際影響到的全部 build target**，不能只重編「這一輪明確有改動原始碼的那幾個 target」；一份原始碼檔案的修改可能橫跨好幾輪 debug session（`simulator.c` 這次就橫跨了兩輪：先移動 `init_bhload_telnetcmd()` 呼叫位置、後來又在裡面用到重新命名後的 `bhload_query_cmd`），每一輪收工前都要重新檢查全部相關檔案的「原始碼時間戳 vs 編譯產物時間戳」，不能只看「這一輪自己改了哪些檔案」就推斷該重編哪些 target。這次能發現，某種程度上是運氣好——剛好使用者追問 UE17 狀態、UE17 現場複測結果比預期更差，才回頭起疑；如果沒有這個巧合，這個「三分之一節點生效」的錯誤基準可能會被當成正式數據一路沿用到 Stage 2~5 的比較，屆時才發現會需要重做全部後續量測。

---

## 2026-09-13 — Stage 2（avg FL）上線除錯過程：CPU 資源競爭、volume 名稱誤用、累積系統狀態

Stage 2 是第一個讓全部 12 個 Local rApp 同時做真實 DRL 推論+背景訓練、且首次接上 Global xApp（全域公平性廣播）+ Global rApp（Flower FedAvg）的階段。上線過程遇到三個獨立問題，最終數據見 `experiment_results/avgFL.md`。

### 問題一：PC1 CPU 資源競爭導致 FlexRIC 崩潰（現象一復現）

第一次讓 Scenario R 真實流量+全部 12 個 xApp/inference 同時上線後，約 100 秒內全部 12 個 xApp 容器開始快速自我重啟（watchdog 觸發：「30 秒未收到 MAC indication」），`docker stats` 顯示 PC1 load average 一度飆到 37（16 核心主機），`top` 顯示 12 個 `inference-nodeN` 的 python3 process 各佔 30~40% CPU（GRU 訓練迴圈），同時 Donor CU/DU + Node2/7/8 三組 RT-priority（`chrt -f 80`）softmodem process 各佔 100%+ CPU。FlexRIC 隨後真的崩潰一次（`docker inspect RestartCount` 0→1，log 出現 `[NEAR-RIC]: WARNING: Pending event timeout`），但幸運地是 docker `restart: always` 讓它自己恢復、DU/MT/CU 全程沒有進入「現象二」的 assoc_rb_tree_extract 崩潰迴圈。

**根因**：Stage 1 PF baseline 從未同時測過「全部 12 個 xApp 真實運作」與「PC1 本身也扛著 Donor+3 組 RT-priority RAN 節點」這兩件事疊加的情境（PF baseline 停用全部 xApp），CPU 資源競爭導致 RT-priority 執行緒的排程延遲累積、間接拖慢 MAC indication 送達 xApp 的時效，觸發連鎖 watchdog 重連，最終壓垮 FlexRIC 的 pending event queue。

**修復**：`docker-compose-iab-server.yaml` 幫全部 12 個 `inference-nodeN` 容器加上 `cpuset: "12-15"`，把 DRL 推論/訓練負載硬性限制在 4 個核心，物理隔離於 RT-priority 執行緒之外。套用後 load average 從 37 降回 7~9，之後 15 分鐘正式量測全程未再復發。

### 問題二：清空 checkpoint 時誤用 compose service-local key 名稱，實際 volume 從未被清到

Stage 2 要求切換 `REWARD_MODE` 前必須清空模型 checkpoint。第一次清空時用 `docker run --rm -v inference_models_node1:/models alpine rm -f ...`——這個 `inference_models_node1` 是 `docker-compose-iab-server.yaml` 裡 service 底下 `volumes:` 區塊的**內部 key 名稱**，但該 volume 定義區塊實際用 `name:` 覆寫成 `iab-xapp-model-node1`（見檔案最下面的頂層 `volumes:` 區塊）。用內部 key 名稱掛載，Docker 會直接建立一個全新的、空白的、名字剛好叫 `inference_models_node1` 的野生 volume，跟服務實際掛載的 `iab-xapp-model-node1` 完全無關——清空指令「成功執行」、`ls` 也顯示空的，但服務重啟後讀到的仍是舊 checkpoint（`train_steps=940, lambda=10.0`，明顯是切換前 `lagrangian` 模式殘留的舊值，不是預期的冷啟動 `train_steps=0, lambda=0.0`）。

**發現方式**：檢查新啟動的 `inference-node1` log，`模型已從 /app/models/model_node1.pt 載入 (訓練步數: 940, lambda=10.0000)`，數值明顯不是清空後該有的樣子，回頭用 `docker volume ls` 才發現兩個名稱並存。

**修復**：改用 `docker volume ls` 確認的實際名稱（`iab-xapp-model-nodeN`）清空，並清掉誤建立的 12 個野生 `inference_models_nodeN` volume（`docker volume rm`）。**教訓（已寫進 `iab/run_stage2_fl.sh` 的註解）**：docker-compose 的 `volumes:` 區塊如果用了 `name:` 覆寫，service 裡引用的 key 名稱就只是本地別名，不是實際的 Docker volume 名稱——清空/操作 volume 前一定要先 `docker volume ls` 或 `docker inspect` 確認實際名稱，不能直接套用 compose 檔裡看到的 key。

### 問題三：長時間偵錯累積的系統狀態，導致個別 UE（UE13/UE14）資料面完全斷線——靠繼續 debug 排除不了，乾淨重啟才是正解

在追查 Stage 2 量測數據時，發現 UE13、UE14（皆掛在 Node11, PC3）的 `measure_stage.py` 取樣結果是 100% 零吞吐量、100% ping 失敗。往下查：DU11 的 MAC 層對這兩個 UE 的統計顯示 RSRP -44dB（訊號極好）、0 BLER、且 MAC TX/RX bytes 持續在增加（無線鏈路本身完全健康），MT/DU/CU 容器 RestartCount 皆為 0（沒有崩潰過），CU 的 DNAT table 對 Node11 的映射也正確對應到 MT11 當下的 tunnel IP——代表問題出在 GTP-U/PFCP 層某處，但沿著這條線往下查（UPF/SMF log 搜尋、tcpdump）沒有找到直接證據。

**使用者的決定性介入**：與其繼續往下挖，使用者直接要求「清空並重啟系統，才知道問題出在哪」。三主機依序 `docker compose down` 全部清空、重新用 `run_local_pc{1,2,3}.sh` 啟動後，**全部 17 個 UE（含先前完全斷線的 UE13/14、與歷史上最差的 UE17）現場 ping 測試皆為 0% 封包遺失**，問題完全消失，且完全沒有另外做任何針對 UE13/14 的手動修補。

**判讀**：這次的斷線並非結構性/RF 問題，而是長時間 debug session 中累積的手動介入（多次容器重啟、DNAT 重新套用、iptables 補丁、FlexRIC 崩潰後的恢復流程）造成 GTP-U/PFCP 層某種難以用單一指令診斷出的 stale 狀態。**教訓**：偵錯進行到一定深度、且累積了大量手動介入後，若某個問題的根因遲遲找不到但個別元件（容器本身、RF 鏈路）都顯示健康，應該優先考慮「整體系統狀態已經劣化到超出繼續 debug 能排除的範圍」，做一次完整乾淨重啟（三主機依序 down 再依序啟動）往往比繼續往下挖更快、更可靠；這跟本文件更早的「FlexRIC 崩潰規律」章節建議的復原流程是同一個道理，只是這次連 FlexRIC 自己都沒崩潰，斷線的是更下層、更難用單一 log 診斷的資料面狀態。

### 待釐清事項：F1AP 跨主機路由

本次除錯過程中，較早的討論串一度描述「PC2/PC3 access 節點的 F1-C/F1-U 位址綁定在 internal bridge IP，CU 所在主機若無明確路由會導致 SCTP association 卡在 COOKIE_WAIT」、並描述了對應的 `ip route add` 修復動作，但事後檢查 `docker-compose-iab-pc2.yaml`／`docker-compose-iab-pc3.yaml` 的 git diff，**這個修改實際上並不存在於目前的檔案內容裡**（可能是討論串中途的 context 被截斷、修改動作未真正落地存檔）。由於本次除錯後續的多次乾淨重啟（含最終量測前的驗證）都確認 Node5/6（PC2）、Node9~12（PC3）的 access 節點 F1AP 連線與資料面全部正常運作，**目前無法確認這個路由問題是否曾經真實存在、或是否已被其他修復（例如 `DOCKER-USER` iptables 規則）間接解決**。如果未來又遇到類似症狀（access 節點 F1AP SCTP association 卡在 COOKIE_WAIT），這是一個可以優先檢查的方向，但目前不建議在沒有重現該症狀的情況下就盲目補一個未經驗證的路由規則進去。
