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
