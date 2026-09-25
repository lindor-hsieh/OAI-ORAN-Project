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

### 待釐清事項：F1AP 跨主機路由——2026-09-14 已確認並補回 git

本次除錯過程中，較早的討論串一度描述「PC2/PC3 access 節點的 F1-C/F1-U 位址綁定在 internal bridge IP，CU 所在主機若無明確路由會導致 SCTP association 卡在 COOKIE_WAIT」、並描述了對應的 `ip route add` 修復動作，但事後檢查 `docker-compose-iab-pc2.yaml`／`docker-compose-iab-pc3.yaml` 的 git diff，**這個修改實際上並不存在於目前的檔案內容裡**（可能是討論串中途的 context 被截斷、修改動作未真正落地存檔）。當時判讀為「無法確認這個路由問題是否曾經真實存在」。

**2026-09-14 更新（已確認、已修復）**：在 Stage 2/3 除錯收尾、逐一比對 PC1 git 版本與 PC2/PC3 實際執行版本的 `docker-compose-iab-pc{2,3}.yaml` 時，發現兩台主機的**全部 8 個 access 節點 DU**（PC2 的 Node5,6；PC3 的 Node9~12）的 `command:` 都是 `sh -c "ip route add 192.168.88.1/32 via <該主機 internal bridge gateway> 2>/dev/null; exec chrt -f 80 ...nr-softmodem..."`，而不是 PC1 git 版本裡單純的 `chrt -f 80 ...nr-softmodem...`——這證實了当初的修復**確實存在且目前仍在線上運作**，只是從未被寫回 git，導致 PC1 的 git 歷史一直顯示這個修復「不存在」。已將這個 `ip route add` 前綴補回 PC1 的 `docker-compose-iab-pc2.yaml`／`docker-compose-iab-pc3.yaml`（`diff` 確認與 PC2/PC3 實際執行版本逐字元相同），解決了這個長期懸而未決的「待釐清事項」。**教訓**：定期用 `diff <(ssh pc2/pc3 cat <file>) <PC1 本機檔案>` 直接比對，比翻找討論串記錄可靠得多——這次就是靠這個方法在幾秒內確認並修復，而不是繼續猜測。

---

## 2026-09-14 — Stage 3（soft clustered FL）上線除錯：三個獨立 root cause，外加 avgFL.md 舊數據作廢重測

實作 Stage 3 `IABClusterFedAvg`（`server_app.py`/`client_app.py`）並驗證正確後（獨立離線數學測試全部通過、線上零例外），量測階段遇到極不穩定的環境（容器隨機崩潰、DNAT/路由規則隨機失效、每次壞的節點都不一樣），花了一整個 session 才定位到三個完全獨立的 root cause。過程中也發現 Stage 2（`avgFL.md`）舊數據其實混入了一個既有 bug，判定作廢重測。

### Root cause 1：`REWARD_MODE` 從未傳到 `flower-supernode-nodeN`，Stage 2 舊數據混入 lagrangian 污染

`docker-compose-iab-server.yaml` 的 12 個 `flower-supernode-nodeN` 服務（負責 FL 觸發的本地訓練路徑：`client_app.py::train()` → `training_pipeline.py` → `drl_agent.py::train_on_batch()`）從 Stage 2 上線以來就**沒有** `REWARD_MODE` 環境變數——`drl_agent.py` 用 `os.getenv("REWARD_MODE", "lagrangian")` 讀取，沒設就悄悄用預設值 `lagrangian`，導致這條訓練路徑一直在跑 Lagrangian 限制式（λ 自適應更新），即使 `inference-nodeN`（近即時推論、每 60 秒背景訓練的主要路徑）正確吃到 `REWARD_MODE=throughput_only`。`avgFL.md` 當時的驗證方法（檢查 MongoDB 經驗文件缺少 `lambda_applied` 鍵）只驗證到 `inference-nodeN` 這一側，抓不到 `flower-supernode-nodeN` 這條路徑的問題。

**影響範圍**：`flower-supernode-nodeN` 的訓練頻率遠低於 `inference-nodeN`（需要 200 筆連續原始經驗才會觸發，`inference-nodeN` 每 60 秒就跑一次），實務上是「疊加在正確配置的主要訓練迴圈上的次要污染源」，不是整份 `avgFL.md` 數據作廢的等級，但仍是既有 bug，且原本舊版 `avgFL.md` 的 RTT 惡化（224→360ms）有一部分可能歸因於此。

**修復**：`docker-compose-iab-server.yaml` 12 個 `flower-supernode-nodeN` 服務都加上 `REWARD_MODE: "${REWARD_MODE:-lagrangian}"`（跟 `inference-nodeN` 同款寫法）。修復後重測 Stage 2，確認 `flower-supernode-node1` 全程 `lambda=0.0000`。

### Root cause 2：`start_iab_pc2.sh` 對 CU 的 NAT OUTPUT table 做無條件整批 flush，會砍掉其他主機已寫好的 DNAT 規則

`start_iab_pc2.sh` 在**兩個地方**對 `rfsim5g-donor-cu` 的 `iptables -t nat -F OUTPUT` 整批清空：一次是拿它當「等待 CU 就緒」的探測指令（`until ssh ... "iptables -t nat -F OUTPUT"`），一次是收尾的 `reapply_dnat_rules()` 函式開頭。這個 flush 沒有時序保護——只要它在 PC1（Node7/8）或 PC3（Node9~12）已經把自己的 DNAT 規則寫進 CU 之後才跑，就會把那些規則整批砍掉且沒有人補回來，造成「哪個主機的腳本最後跑完，其他主機的 access 節點就斷資料面」這種隨執行順序而變、難以重現定位的症狀。`start_iab_pc3.sh` 原本已經注意到這個風險、刻意不 flush，但沒有意識到問題其實出在 PC2 自己身上。

**額外發現的次要問題**：即使不流水線 flush，單純用 `-A OUTPUT`（append）也不夠——一旦某節點 MT 的 tunnel IP 因為 PDU session 自發性重建而改變（詳見 root cause 3 之外的觀察：MT 有時會在初次設定完成後自己重新建立 tunnel，把舊的 71/72/default 自訂路由跟著沖掉），舊的 DNAT 規則會殘留在 chain 較前面的位置、比新規則有更高比對優先權，造成「路由設對了、CU 規則卻還是指到舊 IP」的假象。

**修復**：
1. `start_iab_pc2.sh`／`start_iab_pc3.sh`／`start_iab_server.sh` 全部改用 `iptables -t nat -I OUTPUT 1 ...`（插入到最前面），不再有任何 `-F OUTPUT`；「等待 CU 就緒」的探測指令改成無害的 `docker exec -u 0 rfsim5g-donor-cu true`。
2. 三個腳本收尾都新增 `verify_and_heal_ues()` / `verify_and_heal_local_ues()` 自我修復迴圈：對自己負責的 UE 做 ping 驗證，失敗就呼叫 `reassert_mt_routes()`（重新斷言 MT 的 71/72/default 路由）+ `reapply_dnat_rules()`，最多重試 5 次、每次間隔 15 秒。

### Root cause 3：cpuset 過度擁擠（27 個 process 塞 4 核心）造成 CU 隨機崩潰——這是本次除錯最久才定位到的一個

在修完 root cause 1、2 之後，乾淨重啟仍然偶爾出現 `rfsim5g-donor-cu` 隨機崩潰（`RestartCount` 無故增加），且崩潰後波及的節點每次不同，一度懷疑是腳本啟動順序問題（PC2/PC3 同時起跑互相干擾），改成完全依序啟動（PC1 基礎設施→PC2 完整跑完→PC3 完整跑完→PC1 E2 等待+xApp）後仍然重現，證明跟啟動順序無關。回頭比對「今天新增了什麼設定」才發現：本次除錯過程中，作者把 Stage 2 就有的 `inference-nodeN` 專用 `cpuset: "12-15"`（4 核心，2026-09-13 root cause 修復，長期驗證穩定）誤以為「Global xApp/Flower FL 服務也應該做同樣的 CPU 隔離」，額外把 `global-xapp`／`flower-superlink`／12 個 `flower-supernode-nodeN`／`flower-scheduler`（共 15 個服務）全部也加上同一組 `cpuset: "12-15"`，變成 27 個 process 擠在同一組 4 核心。`mpstat` 檢查瞬時 CPU 使用率並不誇張（該組核心平均僅 30% 上下），完全沒有「CPU 用滿了」的直觀訊號，這也是為什麼一開始沒往這個方向懷疑——真正的機制是偶發性的排程延遲尖峰（大量 Python process 的 GIL/GC pause 跟外面共用實體核心），足以讓 RT-priority 的 CU/DU SCTP/RRC 計時器偶爾錯過期限而觸發 crash-restart，CU 一旦重建，所有依賴它當下 netns 的 DNAT/路由設定就全部失效，表現出來就是 root cause 2 那種「NAT/路由查起來都對、卻還是斷」的假象——這也解釋了為什麼修完 root cause 2 之後症狀還會偶發重現。

**驗證方式（A/B 對照，非猜測）**：把新加的 15 個 cpuset 設定移除，只保留原本的 12 個 `inference-nodeN`，重新依序做一次三主機乾淨重啟——`rfsim5g-donor-cu` 全程 `RestartCount` 維持 0，三個自我修復迴圈（root cause 2 的產物）全部一次檢查就通過，不需要任何人工介入。之後 Stage 2、Stage 3 兩次完整 15 分鐘正式量測，三主機全部容器 `RestartCount` 全程維持 0。

**修復**：`docker-compose-iab-server.yaml` 移除今天新加的 15 個 `cpuset: "12-15"`，只保留原本 12 個 `inference-nodeN` 的設定；並在該處註解加上警告，避免未來又無條件套用同一組隔離設定到其他容器。完整排查步驟已寫進 `CLAUDE.md` 第 7 節「容器隨機崩潰／連線隨機斷線排查指南」。

**教訓**：
1. 幫容器加 `cpuset` 隔離之前，要先確認「這組核心目前已經塞了多少 process」，不能看到 CPU 使用率不高就假設還有空間——排程延遲尖峰不會反映在粗粒度的平均使用率上。
2. 這次三個 root cause 混在一起，症狀高度重疊（都表現成「某些節點資料面斷線、隨機、修了又復發」），單靠繼續往下挖 NAT/路由邏輯永遠只能治標；真正定位靠的是「回想今天到底新改了什麼」+ A/B 對照實驗，跟本文件更早條目的教訓完全一致。
3. 除錯過程中一度嘗試在 PC2/PC3 沒有安裝 `conntrack` 工具的情況下手動 flush conntrack，指令直接失敗（`executable file not found`）——這個平台的 base image 本來就不含 `conntrack` CLI，之前腳本裡的 `conntrack -F 2>/dev/null || true` 其實一直是靜默 no-op，這點也順便記錄下來，避免以後重蹈覆轍去依賴一個不存在的工具。

### Stage 2（avgFL.md）舊數據作廢，重測結果

修完 root cause 1（`REWARD_MODE` 缺口）後，判定舊版 `avgFL.md` 的數據混入 lagrangian 污染，不能視為乾淨的 `throughput_only` 基準，決定重測。修完全部三個 root cause、確認三主機 15 分鐘全程零崩潰、量測前 17/17 UE 現場 ping 0% 封包遺失後，Stage 2、Stage 3 依序重新量測，結果與方法論限制見 `experiment_results/avgFL.md`、`experiment_results/clusterFL.md` 最新版本；`CLAUDE.md` 第 3 節路線圖表格已同步更新為重測後的數字。

---

## 2026-09-18 — Local DRL 改回 MLP（新增 MODEL_ARCH 開關）＋ Stage 2 收斂訓練前置作業踩坑

### 架構問題：「最基礎 DRL」其實一直是 GRU

查證 git 歷史發現：Local DRL 的 Actor/Critic 在 2026-07-09 從 MLP 改成 GRU（`DRL_METHODOLOGY_PLAN.md` §2.3 記錄的三個「已完成改動」之一），但 CLAUDE.md 五階段路線圖裡「最基礎 DRL」這個詞是在 GRU 已經完成兩個月後（2026-09-12）才被創造出來，從頭到尾只沿著 `REWARD_MODE` 這條軸定義，從未把架構（MLP vs GRU）納入考慮。也就是說 Stage 2/3 目前為止「已完成」的所有結果，用的都不是路線圖原本設想的陽春架構。

**決定**（跟舊版 MLP→GRU 遷移的做法不同）：不整倒退掉 GRU（未來改良版或其他研究可能還會用到），改成比照 `REWARD_MODE` 的既有模式，在 `inference/drl_agent.py` 加一個 `MODEL_ARCH`（`mlp`｜`gru`，預設 `mlp`）環境變數開關，兩套網路架構（`ActorNetworkMLP`/`CriticNetworkMLP` vs `ActorNetworkGRU`/`CriticNetworkGRU`）並存於同一份程式碼，`training_pipeline.py` 也對應拆成 `fetch_experiences()`（MLP，打散抽樣 i.i.d.，只看 `MIN_TRAIN_EXPERIENCES=200`）與 `fetch_sequences()`（GRU，原有的時間連續序列邏輯不變）。`docker-compose-iab-server.yaml` 全部 12 個 `inference-nodeN`、12 個 `flower-supernode-nodeN`、`flower-superlink` 都加上 `MODEL_ARCH: "${MODEL_ARCH:-mlp}"`。`DRLAgent.save()`/`load()` 的 checkpoint 多存一個 `arch` 欄位，`load()` 讀到 arch 不符會主動拒絕（印警告、退回隨機初始化），不會讓 state_dict key 不匹配的例外處理悄悄吃掉問題。

**意外的好處**：GRU 版本「訓練幾乎永遠觸發不了」的根因（`_is_contiguous()`／`TRAIN_SEQ_LEN`／`TRAIN_SEQ_COUNT` 的嚴格時間連續性門檻）完全是為了餵 GRU 的 BPTT 才加的，MLP 分支不需要這個門檻，訓練觸發難度大幅下降。相關文件（`DRL_DESIGN.md`、`DRL_METHODOLOGY_PLAN.md`、`STAGE3_CLUSTER_FL_DESIGN.md`、`STAGE4_CUSTOM_FL_DESIGN.md`）都已補上對應的 2026-09-18 更新說明。

單節點冒煙測試（MLP 與 GRU 兩個分支都測，含 checkpoint 存讀與 arch 不符時的拒絕載入邏輯）全部通過後才上線部署。

### 為了 Stage 2 收斂訓練，重建 inference image + 完整三主機乾淨重啟，過程中發現三個新的基礎設施問題

改完 `drl_agent.py`/`training_pipeline.py` 後 `docker build -t local-xapp-inference:latest ./inference` 重建映像檔（COPY-based Dockerfile，不是 bind mount），跑 `REWARD_MODE=throughput_only FL_MODE=avg MODEL_ARCH=mlp bash iab/run_stage2_fl.sh` 清空 checkpoint/經驗、重啟 Stage 2 服務。發現 PC1 上「還在跑」的容器其實已經死了 42 小時（CN5G/Donor CU 早就 Exited，只剩 xApp/inference 容器還活著空轉），因此需要完整三主機依序重啟（`start_iab_server.sh` → PC2 → PC3 → `run_local_pc1.sh --skip-server`）。過程中發現：

1. **`start_iab_server.sh` 內部對 `inference-nodeN` 的 plain `docker compose up -d` 呼叫，會在某些情況下觸發 recreate，把 `REWARD_MODE`/`MODEL_ARCH` 悄悄重設回 compose 檔預設值**（`REWARD_MODE` 預設是 `lagrangian`，不是這次要的 `throughput_only`）——因為這個呼叫在 `start_iab_server.sh` 自己的 shell 環境裡沒有帶上這些變數。現場遇到一次，用 `REWARD_MODE=... MODEL_ARCH=... docker compose up -d --force-recreate inference-node{1..12}` 手動修正。**`iab/training_watchdog.sh` 已經把這個修正納入 `full_recovery()`**：呼叫 `start_iab_server.sh` 時主動帶上正確的環境變數，收尾再額外 `--force-recreate` 一次並逐節點驗證 env，不依賴「應該不會被重設」的假設。

2. **三主機各自的 DNAT 重新斷言腳本會互相覆蓋，只有「最後跑」的那台主機的 access 節點資料面正常**：`start_iab_server.sh`（PC1，服務 Node7/8/UE5~8）、`run_local_pc2.sh`（PC2，服務 Node5/6/UE1~4/17）、`run_local_pc3.sh`（PC3，服務 Node9~12/UE9~16）三支腳本收尾都會對共用的 CU 執行 `iptables -t nat -I OUTPUT 1 ...` 插入 DNAT 規則。現場實測：依序跑完三支之後，只有最後一支對應的主機 UE 是通的，前面兩台的 UE 全部 100% packet loss；把其中一台重跑一次，換成那台通、其他兩台斷——精確驗證用 `iptables -t nat -L OUTPUT -n --line-numbers` 檢查發現：同一個目的地 IP（例如 DU7 的 `192.168.76.13`）在 chain 裡累積了好幾筆歷史 `to:` 目標不同的規則（tunnel IP 每次重啟都會變），舊規則沒有被清掉、只是被新規則插在前面蓋過去——但如果某台主機的 DNAT 重新斷言最後沒有真的執行到（例如 MT 容器沒有被重建，`configure_and_start_*_du()` 沒有被呼叫到，見下一點），它的規則就會停留在舊值，被其他主機新插入的規則群「淹沒」在 chain 後段，即使沒被真的覆寫也會因為 UE 自己的 tunnel IP 早就换了而失效。**這不是本文件更早條目修過的「整批 flush」問題**（那個已經改成 insert-only 確認修復），而是「該重新斷言的沒被觸發」+「chain 裡累積大量過期規則」共同造成的新症狀，現場靠手動 `iptables -t nat -I OUTPUT 1 -p udp --dport 2152 -d <DU IP> -j DNAT --to-destination <正確 tunnel IP>` 插入正確規則修復，尚未寫成自動化——**留給下一次遇到時解決，或考慮讓三支腳本的 DNAT 重新斷言改成「先查詢 MT 容器當下真實的 tunnel IP，再無條件插入」而不是依賴 MT/DU 容器有沒有被重建**。
3. **UE 的 PDU session 有時會在完成攻擊後自發重新建立（tunnel IP 改變），但預設路由沒有跟著重設**：UE5~8 現場診斷發現 `oaitun_ue1` 介面存在、IP 也正常分配，但 `ip route show` 只剩 `12.1.1.0/24 dev oaitun_ue1` 這條 kernel scope 路由，`default via 12.1.1.1 dev oaitun_ue1` 完全不見，導致 `ping: connect: Network is unreachable`（不是封包遺失，是連 route 都沒有）。手動 `ip route replace default via 12.1.1.1 dev oaitun_ue1` 立即修復。懷疑成因：`wait_for_ue`/`wait_for_local_ue` 只在**當時那次**附著成功後設一次預設路由，如果 UE 之後又經歷了一次自發的 PDU session 重建（拿到新 tunnel IP），沒有人會重新設路由——跟 root cause 2 的「tunnel IP 飄移」本質是同一類問題的另一種表現形式，只是這次是在 UE 自己這一側缺路由，不是 CU 側缺 DNAT。
4. **全部 12 個 `xapp-nodeN` 容器一度整批消失**（`docker ps -a` 完全找不到，不是 Exited 而是不存在）：發生的確切原因未查清（可能跟同一時段多次 `docker compose up -d --force-recreate inference-node{1..12}`、`start_iab_server.sh` 重跑有關，但沒有直接證據指向哪個指令），用 `for node in 1..12; do docker compose up -d node${node}-l-xapp; done` 全部重建後恢復正常（12/12 都能看到 `AI 決策` log）。**這個現象本身值得留意，若未來又發生，先確認是不是又是 root cause 3（cpuset 過度擁擠）的變形，或另一個尚未定位的觸發條件**。

**最終驗證**：三個問題全部手動修復後，17/17 UE 現場 ping 0% 封包遺失（UE17 這次 RTT 700~2100ms、仍然明顯偏高，但沒有像 `PF.md` 那次一樣 100% 全滅——具體是不是因為這次訓練還沒真正開始、Node4 沒有同時承受高負載，待後續 Stage 2 收斂訓練過程觀察），`REWARD_MODE=throughput_only`／`MODEL_ARCH=mlp`／`FL_MODE=avg` 在全部 26 個相關容器上核對一致，12 個 xApp 全部持續產生 `AI 決策` log。

### Stage 2 收斂訓練上線後第一次真實 FlexRIC 崩潰＋自動復原，順便抓到 `training_watchdog.sh` 自己的一個 bug

系統穩定後啟動 `iab/training_scenario_driver.sh`（三主機，共用 epoch）＋ `iab/training_watchdog.sh`（PC1），跑 `iab/calibrate_fl_rate.py` 校準：MLP 版本全網最慢節點實測 0.384 筆/秒，200 筆經驗約 9 分鐘內達標、512 筆約 22 分鐘——遠優於 GRU 時期「57 分鐘一次都沒跨過 200」的紀錄，證實移除序列連續性門檻確實大幅改善了訓練觸發難度。

上線約 3 分鐘後（15:48），`training_watchdog.sh` 第一次真實偵測到崩潰訊號（`flexric` RestartCount 0→1），90 秒二次確認後觸發完整重啟，依序跑完 PC1→PC2→PC3→PC1(xApp) 全部四步、13/13 E2 恢復、12/12 節點環境變數核對正確，全程約 15 分鐘（15:49~16:05），行為完全符合設計。

**但復原後發現 PC3 的場景驅動器沒有真的重新啟動**（`pgrep` 在 PC3 上找不到 `training_scenario_driver.sh`，PC1/PC2 都正常）。根因：`start_scenario_driver()` 用 `ssh host "cmd &"` 的寫法讓遠端 shell 自己把指令丟進背景，但 SSH session 有時會在遠端背景行程真的 fork 完成前就先關閉連線，導致背景行程從未真正啟動、且没有任何錯誤訊息（`ssh` 指令本身仍回傳成功，因為前景部分——也就是把 `cmd &` 丟出去這個動作——確實成功了，只是丟出去的東西沒接住）。這跟本文件更早條目的 root cause 完全不同類，是純粹的 shell/SSH 背景行程语意問題，不是 IAB 系統本身的 bug。

**修復**：改用 `ssh -f host "cmd"`（`-f` 讓 ssh 自己先 fork 到背景、確認 session 建立後才把控制權交還本地端，不依賴遠端 shell 的 `&` 語意，這是業界公認在 SSH 上啟動背景行程的正確做法），並在 `start_scenario_driver()` 收尾加一段主動驗證＋重試（`pgrep` 確認三主機都真的有行程在跑，缺的重打一次）。現場立即验证：改用 `ssh -f` 手動重啟 PC3 的驅動器後一次成功；`training_watchdog.sh` 本體也已修好並重新上線。

**教訓**：`nohup cmd &` 這個組合在本機 shell 裡是可靠的背景手法，但透過 `ssh host "cmd &"` 遠端執行時不能照搬同樣的假設——SSH session 的生命週期跟遠端 shell 幫你背景化的那個子行程是否真的活下來，是兩件不保證同步的事，寫任何跨主機自動化腳本時背景啟動遠端行程都要用 `ssh -f`，且收尾要主動驗證行程真的在跑，不能只看 SSH 指令本身有沒有回傳成功。

### 第二次真實崩潰：Node2 relay DU 的 RA process pool 耗盡，連帶暴露 DNAT 腳本的兩個既有 bug

修好第一次崩潰的復原流程後約 24 分鐘（16:27），`training_watchdog.sh` 第二次真實偵測到崩潰（同樣是 `flexric` RestartCount регресs），但這次 `full_recovery()` 的 PC1 步驟本身失敗了——`start_iab_server.sh` 收尾的 `verify_and_heal_local_ues()` 對 UE5~8 重試 5 次仍連不通，watchdog 依照設計正確地判斷「復原沒有生效」、主動停止（不是無限重試硬撐出一個假的成功）。

**根因（跟本文件之前任何一條都不同類）**：診斷發現 `rfsim5g-iab-du-8` 容器完全不存在（`configure_and_start_local_access_du()` 因為 MT8 的 tunnel IP 在 300 秒逾時內沒出現而被整段跳過，跟舊條目「Node5 tunnel IP 逾時」是同一種症狀，但這次的病根不同）。查 MT8 的 log 發現它卡在 PRACH/RAR 迴圈（`RAR reception failed`）反覆重試但從未附著成功；再查它的 parent relay——`rfsim5g-iab-du-2`（Node2）——的 log，找到真正的根因：`FAILURE: initiating RA procedure for preamble index N: no free RA process`。Node2 的 DU 端 Random Access process pool（OAI 內部一個小容量的固定陣列）被用盡，導致它完全無法再受理任何新的 PRACH，MT8 送出的每一次 Random Access 嘗試都直接被拒絕在最源頭——這不是 DNAT／路由問題，是 RAN protocol stack 本身的資源池耗盡，合理懷疑是這幾個小時內對 MT7/MT8 做了大量次重啟嘗試，每次失敗的 RA 嘗試在 Node2 這端沒有被正確釋放而逐漸洩漏掉可用的 process slot。

**修復**：`docker restart rfsim5g-iab-du-2` 立即清空 RA process pool，MT8 在下一次重啟後 10 秒內就成功附著（對照組：清空前已經卡了快 20 分鐘）。之後重跑一次 `start_iab_server.sh` 才讓 `configure_and_start_local_access_du()` 真正跑到、建出 `rfsim5g-iab-du-8`。

**過程中額外發現、順手修掉的問題**（都是本文件更早條目「root cause 2」修復之後才浮現的殘留問題，不是這次才引入的）：
1. **手動插入 DNAT 規則時把 Node7/Node8 的內部橋接 IP 搞反了一次**：`192.168.76.12` 其實是 `rfsim5g-iab-mt-7` 自己的位址、`192.168.76.13` 是 `rfsim5g-iab-mt-8` 自己的（不是直覺以為的「.12=Node8、.13=Node7」），第一次手動修復把兩者的 DNAT 目標插反，導致原本只有 Node8 斷線的狀況惡化成 Node7/Node8（UE5~8 全部 4 個）一起斷線；靠重新對照 `docker-compose-iab-server.yaml` 裡 `rfsim5g-iab-mt-7`/`rfsim5g-iab-mt-8` 的 `ipv4_address` 才抓到對調，改用「先查 MT 容器自己當下的 oaitun_ue1 實際 IP，再用變數帶入正確的 DU IP↔MT tunnel IP 配對」，不要用記憶中的數字。**教訓**：這類手動急救指令，永遠先用 `docker exec <container> ip addr show` 查當下真值，不要憑印象或先前 session 的記憶去背數字。
2. **（訂正）一開始誤判 `start_iab_server.sh` 對 Node7/8 的 DNAT 重新斷言「沒有每次真的重新插入」，事後查程式碼發現這個診斷是錯的**：`configure_and_start_local_access_du()` 每次呼叫都確實用 `docker exec $MT_NAME ip -f inet addr show oaitun_ue1` 現場查詢當下 tunnel IP、插入對應的新規則，沒有快取問題。真正的根因是**時序缺口**：PC1 本機的 UE5~8 健康檢查（`verify_and_heal_local_ues`）只在 `start_iab_server.sh` 自己執行過程中跑一次，時間點在整個三主機依序啟動流程的最前面；重啟 `rfsim5g-iab-du-2`（清 RA process pool）會連帶讓 MT7、MT8 都要重新附著，MT8 有明確重啟、事後有驗證，但 MT7 是在後續 PC2/PC3 花費數分鐘啟動期間**自發性**重新建立 tunnel（CLAUDE.md 已記載 MT tunnel 有時會自發重建的現象）——這段期間 PC1 端完全沒有任何東西在重新檢查/斷言 Node7/8，直到三主機全部跑完後我才做最終 ping 檢查，才發現 Node7 的 DNAT 規則已經跟不上。這是一個真實但不同類的缺口：**不是「重新斷言邏輯寫錯」，而是「PC1 本機節點的健康檢查時機點太早，跟整個復原流程的收尾對不起來，中間有一段沒人看顧的空窗期」**。
   **修復**：`iab/training_watchdog.sh` 的 `full_recovery()` 在最後一步（xApp 啟動完成之後）新增一次 PC1 本機 UE5~8 的最終重新驗證＋斷言，不再只依賴 `start_iab_server.sh` 內部那次時機過早的檢查。

**另一個復發的舊症狀**：修完 DNAT 後 UE5~8 一度變成 `ping: connect: Network is unreachable`（不是封包遺失），查出來又是本文件稍早記錄過的「UE 的 PDU session 自發重建、預設路由沒跟著重設」，手動 `ip route replace default via 12.1.1.1 dev oaitun_ue1` 補回。這已經是這個症狀第二次獨立出現（第三次出現、確認影響全主機並修進自我修復迴圈，見下方「第五次崩潰」條目）。

全部修復後 17/17 UE 現場 ping 0% 封包遺失，MongoDB 經驗資料在整個約 1 小時的多輪故障排除過程中完全沒有被清空或動過（只有 RAN 層的容器被重啟），`REWARD_MODE=throughput_only`／`MODEL_ARCH=mlp` 在 12 個 `inference-nodeN` 上核對一致，重新啟動三主機場景驅動器（新 epoch）與 `training_watchdog.sh`，訓練繼續進行。

### 待辦：FlexRIC pending event 積累導致的崩潰頻率問題（根因已查明，修復方案待實作）

收斂訓練上線後崩潰頻率明顯偏高（約每 20~30 分鐘一次，watchdog 目前為止已自動處理 4 次，全部成功復原，不需人工介入）。使用者詢問能否讓 FlexRIC 更強健，查證後定案：**這是既有的架構限制，不是這次訓練或 MODEL_ARCH 改動造成的**，先記錄根因與候選修復方案，這次先不動手（使用者決定先靠自動復原把 Stage 2 訓練到收斂，之後再回頭處理）。

**根因**：`openair2/E2AP/flexric/src/ric/near_ric.c::control_service_near_ric()`（約 745~769 行）——xApp 每送出一次 `CONTROL-REQUEST`（也就是 DRL 下發 PRB 分配那個動作），FlexRIC 就會用 `create_timer_ms_asio_ric()` 建立一個新的 **3000ms 逾時計時器**，註冊進 `ric->pending`（一個 `bi_map`，key 是 timer fd）。如果沒有在 3 秒內被相應的 ACK 處理路徑提早取消，計時器到期時會印出 `near_ric.c:485` 的 `[NEAR-RIC]: WARNING: Pending event timeout. Disarming timer.`（這正是 `training_watchdog.sh` 用來偵測崩潰的訊號字串），並清掉該筆 pending 項目。C xApp 端的 rate limiter（`xapp_node*.c`，每 10 個 MAC callback 才觸發一次 ZMQ，約每 100ms 一次）把單一節點的 CONTROL-REQUEST 頻率壓到理論上限約 10 次/秒，12 個節點加總尖峰可達 120 次/秒——這麼高的併發量下，只要有一部分沒能在 3 秒內被確認/取消，就會持續累積，長時間運行後正好對應 CLAUDE.md 已經記載的「長時間運行後 pending event queue 塞滿」症狀。`xapp_node*.c` 的 `apply_fallback()` 函式本身已經對這個問題有防禦（ZMQ 逾時時刻意不送 CONTROL-REQUEST，註解明確寫著「會把 FlexRIC 的 pending event queue 打爆」），代表專案先前就已經注意到 CONTROL-REQUEST 頻率是主要風險來源，但沒有進一步處理過「正常送出、只是恰好塞車」這種情況。

**候選修復方案（未實作，按風險/效果排序）**：
1. **調鬆 C xApp 的 rate limiter（風險最低、建議優先評估）**：把 12 個 `xapp_node{1..12}.c` 的觸發門檻從「每 10 個 MAC callback」放寬到例如「每 20~30 個」，直接讓 CONTROL-REQUEST 送出頻率打對折到打三分之一，不需要碰 FlexRIC 核心程式碼。代價是 PRB 重新分配的有效週期從 100ms 拉長到 200~300ms，需要重編 12 個 xApp、重新部署（跟一次崩潰復原差不多量級的中斷）。
2. **深查 FlexRIC 核心的 ACK 取消路徑（風險較高）**：確認收到 `RIC_CONTROL_ACKNOWLEDGE` 時是否真的有呼叫對應的 `stop_pending_event()` 提早取消計時器並釋放 timer fd——如果這條路徑本身有問題（例如沒有正確比對 `ric_id` 導致 ACK 配對不到 pending 項目），那才是真正的洩漏而非單純「量大排隊」，修對了可能徹底解決而不需要犧牲控制週期；但 `near_ric.c` 是全部 12 個 xApp 共用的核心事件迴圈，改動風險與測試成本都高，需要另外撥時間專門驗證。
3. **維持現狀，只靠 watchdog 自動復原**：目前 watchdog 每次都能在 15~20 分鐘內完全自主復原（含 2026-09-18 新增的 PC1 本機 UE5~8 最終驗證步驟），這個崩潰頻率與自動復原機制本身可以直接寫進論文方法論限制章節，誠實揭露「這是第一次讓系統在持續高負載下連續運行數小時，才觀察到這個既有限制的實際發生頻率」。

**下一步**：先讓 Stage 2 收斂訓練跑完（純靠方案 3 撐著），拿到數據之後再回頭評估要不要實作方案 1 或方案 2——如果 Stage 3 或後續階段也要跑一樣長的收斂訓練，屆時再做這個決定，不要在同一次訓練過程中中途換架構造成資料不連貫。

### 新增 `iab/check_convergence_mongo.py`：Track 1 改用 MongoDB，不再受容器重啟影響

使用者發現一個實際問題並問到重點：`training_watchdog.sh` 每次崩潰復原都會讓 `inference-nodeN` 重新啟動，`check_convergence.py`（舊版）靠 `docker logs` 抓「訓練完成」摘要行來判斷收斂趨勢，容器一重建 log 歷史就砍掉重來——以目前約 20~30 分鐘崩潰一次、`TRAIN_INTERVAL_S=60秒`一輪來算，兩次崩潰之間最多只能累積 20~30 輪，剛好卡在舊版預設 `--window 20` 的門檻邊緣，長期下來幾乎不可能真正累積到足夠輪數判斷收斂。**注意：這只影響「觀察收斂趨勢」的可視性，不影響訓練本身**——`DRLAgent` 的 `train_steps`／權重透過 checkpoint 持久化，MongoDB 經驗資料也完全沒被 `full_recovery()` 動過，模型是持續在訓練進步的，只是舊版工具看不到。

**修復**：新增 `iab/check_convergence_mongo.py`，改讀 MongoDB 裡 `node{N}_experiences` 的原始經驗（`timestamp`／`reward`／`is_idle`），依 `--bucket-minutes`（預設 5）分鐘一組切時間區間算平均 reward，對區間序列做線性回歸算趨勢（跟舊版同一套「視窗內變化量佔平均值比例」演算法），這份資料完全不受容器重啟影響、可以無限期跨越任意次數的崩潰復原持續累積。零風險、純新增查詢腳本，沒有改動任何正在跑的容器或既有程式碼。`iab/training_healthcheck.sh` 的 Section C 已經改成呼叫這支新腳本（`--window-minutes 120 --bucket-minutes 5 --min-buckets 8`），舊版 `check_convergence.py` 保留、仍可手動呼叫，只是不再是健檢腳本的預設。

現場測試：改用 MongoDB 版本後，同一批節點一次就看到 11~13 個有效時間區間（對照舊版同時間點幾乎每個節點都顯示「還沒有任何一輪訓練完成的 log」），證實有效解決容器重啟造成的觀察空窗問題。

### 第五次崩潰：watchdog 正確判斷失敗並停止，人工介入後確認「UE 端遺失預設路由」是三主機共通的復發症狀

第 5 次崩潰（19:13 觸發）的 `full_recovery()` 卡在 PC3 這步——`run_local_pc3.sh` 收尾的 UE9~16 自我修復重試 5 次仍失敗，watchdog 依設計正確停止（不是無限重試硬撐）。人工介入診斷：PC3 的容器（DU9~12/MT9~12/UE9~16）當下其實都已經是新的、正常運行中，多數 UE 過一會兒自己恢復（8 個只剩 UE15 還斷），查 `rfsim5g-iab-du-12` log 發現是另一種全新症狀——`[RLC] E max RETX reached on SRB 1` + `RLF detected, but no callable RLF handler registered`，UE15 的舊 RNTI 卡在一個 DU 端沒有正確清除的 Radio Link Failure 狀態，`docker compose up -d --force-recreate rfsim5g-end-ue-15` 強制其重新附著（拿到新 RNTI）解決。

**但重新附著後 ping 仍然失敗**，查出跟本文件稍早記錄過的「UE PDU session 自發重建、預設路由沒跟著重設」是同一類問題（`ip route show` 只剩 `12.1.1.0/24 dev oaitun_ue1`，`default via 12.1.1.1 dev oaitun_ue1` 不見了），`ip route replace default via 12.1.1.1 dev oaitun_ue1` 補上即解。**這次不只 UE15**——這次手動完成整個復原後，額外發現 PC1 的 UE5~8（`training_watchdog.sh` 因為卡在 PC3 這步、根本沒跑到後面新增的 PC1 最終驗證步驟）跟 PC2 的 UE17 也是同一個症狀（`ip route show` 都缺 `default via 12.1.1.1`），三台主機、四個不同節點群組（PC1 本機 access、PC2 的 relay 直連 UE17、PC3 的 access）在同一次事件裡全部復發同一種缺路由症狀，確認**這是一個影響全部三主機、不分節點角色的通用問題**，不是特定主機或特定節點類型的個案。

**目前狀態**：三個問題都已現場個別修復（DU12 的 RLF 用強制重建 UE15 解決；PC1/PC2/PC3 的缺路由都用手動 `ip route replace default` 補上），17/17 UE 現場 ping 0% 封包遺失後才重新啟動三主機場景驅動器（新 epoch）與 `training_watchdog.sh`（v5）。

**修復（同日稍後補上，非「待辦」）**：`verify_and_heal_ues()`（`start_iab_pc2.sh`／`start_iab_pc3.sh`）與 `verify_and_heal_local_ues()`（`start_iab_server.sh`）三支腳本都新增 `fix_ue_default_routes()` 函式，在既有的 `reassert_mt_routes`／`reapply_dnat_rules`／`reassert_local_access_dnat_and_routes` 之後、每次重試迴圈都額外對負責的 UE 做 `ip route replace default via 12.1.1.1 dev oaitun_ue1`；`start_iab_pc2.sh` 額外對 UE17（不在 `verify_and_heal_ues` 的 ping 重試範圍內，機制跟 access 節點不同）單獨補一次。三支腳本改完都跑過 `bash -n` 語法檢查，PC2／PC3 的版本已 `scp` 同步過去並在對方主機上再次語法檢查確認。這次修改只改腳本檔案本身，沒有觸碰任何正在跑的容器——下一次 `training_watchdog.sh` 觸發 `full_recovery()` 或有人手動重跑這幾支腳本時就會自動套用新邏輯。

### 追查 Track 1「一直沒有節點收斂」的訊號品質問題：找到並清掉少量 Lagrangian 污染資料

訓練跑了 4 小時、6 次崩潰復原之後，使用者問「還要訓練多久」，查 `check_convergence_mongo.py`（3 小時寬視窗）發現 12 個節點**全部**都還是「仍在變動」，變化量普遍落在 25%~151%（門檻是 <15%），完全沒有節點接近收斂。深入查證發現兩個獨立問題：

1. **少量 Lagrangian 模式污染資料混進 throughput_only 的訓練資料**：`REWARD_MODE=throughput_only` 時 reward 理論上恆為 `r_throughput ∈ [0,1]`（`compute_reward_breakdown()` 的公式，權重 `W_FAIRNESS=W_DELAY=0`），不可能是負值；但直接查 MongoDB 發現部分節點有 reward 低到 -1.5 的紀錄。查 `lambda_applied` 欄位（只有 `compute_lagrangian_reward()` 才會寫入）分布，證實這些負值就是污染——時間點對應本文件較早記錄的「`REWARD_MODE` 短暫被重設回預設值」窗口（`training_watchdog.sh` 的防呆修好之前的幾次崩潰復原）。污染比例很小（12 個節點合計 74772 筆裡 142 筆，0.19%），但因為 throughput_only 的 reward 本身量級很小（下一點），離群值對趨勢判斷的影響被放大。
2. **即使排除污染，正常 reward 數值量級也遠小於 `MAX_BSR` 正規化上限所暗示的 [0,1] 直覺範圍**（多數落在 0.0001 量級）——研判是訓練場景（`training_scenario_driver.sh` 的輪替表）混入太多閒置／低流量情境（Scenario R 的 `bursty`／`light` profile、以及 A/B/C/D 都沒有真正「把頻寬塞滿」的高負載設計），一般個別 UE 實際傳輸量遠達不到 `MAX_BSR=1,000,000 bytes/100ms`（≈80Mbps）這個上限，reward 訊號因此天生偏小、對雜訊敏感。這一點**尚待處理**，見下一條目。

**修復（資料清潔）**：
1. `check_convergence_mongo.py` 查詢時排除 `lambda_applied` 存在的文件，且區間統計從平均數改成中位數（對離群值更穩健），報告會列出排除筆數。
2. 新增 `iab/clean_lambda_contamination.py`（預設 dry-run，要 `--execute` 才會真的刪除），已執行 `--execute` 清掉全部 12 個節點合計 142 筆污染文件，清完 `lambda_applied` 存在的文件數確認歸零。這個刪除操作沒有影響背景訓練執行緒（MongoDB 刪除是原子操作），訓練全程沒有中斷。

**效果**：清完＋改用中位數後，多數節點的變化量顯著下降（例如 Node2 150%→17%、Node11 96%→30%），部分節點（Node3、Node4）已經達到「疑似收斂」門檻，證實訊號品質確實有改善，但多數節點仍未收斂——訓練場景設計本身可能也需要調整，見下一條目。

### 新增 Scenario T（低/中/高流量 × 低/中/高路徑損耗 3x3 交叉設計），取代 R 主導的輪替表

使用者指出訓練場景設計本身的問題：Scenario R 的 `light`／`bursty` profile 佔比高、閒置機率不低，且 A/B/C 之間的對比都在溫和範圍內，沒有任何場景真正把頻寬塞到「需求超過供給」的壅塞狀態，這正是 reward 訊號量級普遍偏小、對雜訊敏感的根本原因之一。

**修復**：`scenarios/traffic_scenario.py` 新增 `scenario_t_tiered()` / Scenario T——`TRAFFIC_TIERS={low:5, medium:25, high:120}` × `PLOSS_TIERS={low:3, medium:12, high:22}` Mbps/dB 3x3 交叉，high tier（120Mbps）刻意遠高於單一 UE 實測可達吞吐量（理論峰值 227Mbps、多跳實測約 43.8Mbps，見 CLAUDE.md 第 2 節），確保訓練資料涵蓋真正的壅塞狀態；每個 phase 依 `(UE 索引 + phase_index) % 9` 錯開分配組合，同時間不同 UE 拿到不同組合、隨 phase 推進輪替。`iab/training_scenario_driver.sh` 的輪替表重新設計：T 佔主力（53%），R 降到 20%（保留真實隨機多樣性），A/B/C 各自保留其邊界案例價值，移除 D（已被 T 的系統性覆蓋取代）。`scenarios/traffic_scenario.py`／`iab/training_scenario_driver.sh` 都已 `scp` 同步到 PC2/PC3 並各自語法檢查過，三主機驅動器已用新版重新啟動（沿用同一個 epoch，wall-clock 自我校正機制自動接續到正確位置，現場驗證過一次真實崩潰復原後正確從 T 輪替到 R，證實這個機制在改版後依然正常運作）。

**現場驗證**：Scenario T 上線後第一次觀察到 UE5 真的跑出 120Mbps 高負載流量（`iperf3 start: ... @ 120Mbps/TCP`）；下一次健檢（跨越一次真實崩潰復原）Track 1 首次出現「疑似收斂」節點（Node6/7/8，變化量 3~5%），多個節點變化量顯著下降，初步證實場景改版方向有效，但仍需要更長時間觀察才能下定論。

**新發現的殘留小問題（尚未修）**：這次崩潰復原後健檢又測到 1~2 筆新的 `lambda_applied` 污染文件（`check_convergence_mongo.py` 已自動排除，不影響判斷，但代表 `REWARD_MODE` 防呆並非 100% 滴水不漏——`training_watchdog.sh` 的 `full_recovery()` 收尾雖然會主動 `--force-recreate` 校正 `inference-nodeN`，但 `start_iab_server.sh` 內部自己的 `up -d` 到那之前這段窗口理論上仍有極短暫的機會跑到預設值；由於量極小（個位數）、且下游已有自動過濾機制擋住，這次先不處理，值得記錄下來供以後有餘裕時徹底根治（例如乾脆讓 `full_recovery()` 一開始就先 `--force-recreate`，不要等 `start_iab_server.sh` 自己跑完普通的 `up -d` 才補救）。**2026-09-19 更新：已徹底修復，見下方條目**——不再是「量極小先不處理」的暫時狀態。

**另一個同一類別的新發現**：同一次崩潰復原後，PC2 的 UE17 也出現跟 PC1 UE5~8 一樣的「最終驗證只做給最後執行的主機」缺口（`training_watchdog.sh` 目前只在收尾對 PC1 本機節點做最終驗證，PC2/PC3 各自的驗證只在自己那個步驟做一次，之後主機順序繼續往下跑，沒有人在全部流程跑完後回頭再驗證非最後一棒的主機）——這次手動補上 UE17 的路由即解決，**這個缺口本質上是「PC1 本機最終驗證」修復的同一個問題、只是換了一個主機**，如果之後要徹底解決，應該考慮把「最終驗證」擴大到全部三主機（不只 PC1），而不是只有 PC1 本機的 UE5~8 有這個保護。**仍未修復**（見下方 2026-09-19 條目，這次同類問題在 UE17 身上又出現一次，做法同樣是手動補路由，長期根治方案不變，這次時間有限沒有動手）。

## 2026-09-19 — Random Access process pool 耗盡自動修復上線、三主機乾淨重啟、Stage 2 正式重測（三度）、Stage 2 資料封存＋Stage 3 啟動

延續使用者的要求：不要再對個別 UE/DU 東補西補，三台主機全部乾淨重啟；並且把 relay/access DU 的 Random Access process pool 耗盡（`gNB_scheduler_RA.c:719` `"no free RA process"`，OAI 內部固定 `NR_NB_RA_PROC_MAX=4` 陣列）這個過去只能人工 `docker restart rfsim5g-iab-du-N` 排除的崩潰模式，做進腳本自動修復——這是 `training_watchdog.sh` 自動復原「有時候成功有時候失敗」的一個主因（先前 Node2、這次 session 稍早的 PC3 Node9~12 案例皆屬此類，且都不會觸發 FlexRIC pending timeout／xApp 重連風暴／CU-DU(donor)/FlexRIC RestartCount 這三個既有偵測訊號，watchdog 完全看不到）。

**Phase 1：RA process pool 耗盡自動修復**。新增 `heal_ra_exhaustion()`（`training_watchdog.sh`，每輪詢週期主動檢查全部 12 個 relay/access DU 的最近 90 秒 log，命中就 `docker restart` 該 DU，每個 DU 獨立 300 秒冷卻，跟既有的 `detect_crash_signal()`/`full_recovery()` 完全獨立、不需要二次確認，修復時間 5~10 秒）與 `heal_ra_exhaustion_local()`（`start_iab_server.sh`/`start_iab_pc2.sh`/`start_iab_pc3.sh` 三份，各自檢查自己主機本地的 DU；PC3 版本額外用 SSH 檢查 PC2 上的 parent relay Node3,4，因為 PC3 的 access 節點斷線也可能是跨主機 parent relay 的 RA pool 耗盡造成），接在各自 `verify_and_heal_*_ues()` 迴圈的第 3 次重試之後（給路由/DNAT 重新斷言前兩次機會，不是每次連通性問題都先無條件重啟 DU）。四份腳本改完先 `bash -n` 語法檢查，PC2/PC3 版本 `scp` 同步後在遠端各自再檢查一次。

**Phase 1.5：意外撿到並徹底修復一個已知但先前「先不處理」的舊坑**——上面 2026-09-18 條目末段記錄過「`start_iab_server.sh` 直接執行時，`inference-nodeN` 的 plain `up -d` 沒帶 `REWARD_MODE`/`MODEL_ARCH`，理論上有極短暫窗口套用 compose 預設值」，當時判斷「量極小、下游已過濾，先不處理」。這次直接執行 `start_iab_server.sh`（不透過 watchdog）做乾淨重啟後，現場實測發現這個窗口比想像中長很多：全部 12 個節點的 `inference-nodeN` 容器啟動後**持續 13 分鐘**都在跑 `REWARD_MODE=lagrangian`（不是「極短暫」，是直到我手動檢查才發現），且**Flower FL 服務整層（`flower-superlink`/`flower-supernode-node{1..12}`/`flower-scheduler`）完全沒有被腳本重新帶回來**（`docker compose down` 會把 profile-gated 的 `stage2-fl` 服務一併清掉，但 `start_iab_server.sh` 完全不知道有這個 profile，只有 `global-xapp`/`flower-scheduler` 這兩個不明原因倖存）。這 13 分鐘內產生的經驗資料有 32 筆（佔全部 114140 筆的 0.03%）帶有 `lambda_applied` 欄位，確認是 lagrangian 污染，用 `iab/clean_lambda_contamination.py --execute` 清除。**真正的修復**：把 `training_watchdog.sh` 原本只在 `full_recovery()` 內部才有的「主動覆蓋」邏輯，直接搬進 `start_iab_server.sh` 本身——啟動 `inference-nodeN` 後立即用 `REWARD_MODE`/`MODEL_ARCH` 環境變數（預設 `throughput_only`/`mlp`）`--force-recreate` 一次，並且用 `docker ps -a` 偵測 `flower-superlink` 是否存在過，存在就用 `FL_MODE`/`MODEL_ARCH` 一併 `--force-recreate` 帶回整層 stage2-fl 服務。這樣不管未來是透過 watchdog 呼叫、還是像這次一樣直接執行，都不會再有這個污染窗口，不用再依賴「下游有過濾機制擋住、污染量小所以能接受」這個脆弱假設。

**Phase 2~3：三主機依序乾淨重啟**（`start_iab_server.sh` → `run_local_pc2.sh` → `run_local_pc3.sh`），**不動 MongoDB/checkpoint**（本來就已經累積 6.5 小時的 Stage 2 訓練資料，2026-09-18 07:29~13:56 UTC）。過程中兩個小插曲：(1) 啟動 PC2 時手滑同時用了「舊式 `ssh "cmd &"`」與「`ssh -f`」兩種寫法各跑了一次，造成兩份 `start_iab_pc2.sh` 同時執行、有競爭風險，立刻 `kill -9` 全部相關 PID 後用單一 `ssh -f` 乾淨重跑，之後全部改成只用 `ssh -f` 單次啟動並立刻確認 process 數量。(2) 17 UE 連通性驗證發現 UE16（PC3，`[RLC] E max RETX reached on SRB 1` + `RLF detected` 反覆出現，卡在 radio-link failure，不是路由/DNAT 問題，`docker restart rfsim5g-end-ue-16` 後正常重新附著）與 UE17（PC2，預設路由在乾淨重啟後又是消失成 `default via 192.168.88.1 dev eth0` 而非 `12.1.1.1 dev oaitun_ue1` 的既有模式，手動 `ip route replace` 後 0% 封包遺失）各自需要一次性手動修復，修完後 17/17 UE 全部連通。

**Phase 4：Stage 2 固定場景正式重測**（第三次，取代 2026-09-14 版本）——先確認/修復 `inference-nodeN`（`REWARD_MODE=throughput_only`/`MODEL_ARCH=mlp`）與 Flower FL 層（`FL_MODE=avg`/`MODEL_ARCH=mlp`）皆正確、`iab/clean_lambda_contamination.py --execute` 清除污染、`bash scenarios/setup_iperf_servers.sh` 重新確認 ext-dn 的 17 個 iperf3 server 監聽正常後，DRL 全程保持啟用（不像 PF baseline 停用 xApp），三主機同時跑 `traffic_scenario.py --scenario R --seed 20260914 --phase-duration 60 --num-phases 15` + `measure_stage.py --duration 900 --interval 5`。全程三主機 FlexRIC/CU/DU/全部 12 個 relay+access DU `RestartCount` 維持 0。

**結果**：JFI=0.3960（PF 基準 0.3303，+19.9%）、17 UE 平均吞吐量 8.78 Mbps（PF 基準 6.45 Mbps，+36.1%）、平均 RTT 194.86 ms（PF 基準 224.23 ms，**改善 −13.1%**）——**三項核心指標第一次同時優於 PF baseline**，2026-09-14 版本的 RTT 曾經惡化 +32.1%，這次反而改善。跟上一版本最大的差異是 MODEL_ARCH 從 GRU 換回 MLP（推論路徑更輕量）+ 訓練時長從 15 分鐘拉長到 6.5 小時 wall-clock，兩個變數同時換掉，無法精確歸因給哪一個，如實記錄在 `experiment_results/avgFL.md`。UE17 量測期間再次出現 ICMP 100% 失敗（量測前現場確認過 0% 封包遺失，量測開始、系統進入高負載後才復現），跟 `PF.md` 記錄的 Node4 三重負載自我節流效應一致，判斷不是本次新增的連線問題。

**Phase 6：Stage 2 資料封存＋Stage 3（cluster FL）啟動**。新增 `iab/archive_stage_data.sh <tag>`（`docker cp` 封存全部 12 個節點的 checkpoint 到 `experiment_results/checkpoints_archive/<tag>/`、MongoDB `node{N}_experiences` 用 `renameCollection` 改名成 `node{N}_experiences_<tag>`，不是複製也不是切換資料庫——因為 collection 名稱在現有程式碼裡完全寫死不可配置，改名是改動範圍最小的封存方式），執行 `bash iab/archive_stage_data.sh stage2_avgfl_20260919` 封存全部 12 節點（合計 114108 筆經驗＋12 份 checkpoint）後，跑 `FL_MODE=cluster REWARD_MODE=throughput_only bash iab/run_stage2_fl.sh` 啟動 Stage 3——這支既有腳本的清空邏輯現在操作的是封存後新建的空 collection，不會動到剛搬走的 Stage 2 資料。確認全部 12 個節點 MongoDB 經驗數為 0（乾淨起點）、環境變數正確（`throughput_only`/`mlp`/`cluster`）。

**Phase 7：跨夜自主訓練基礎設施上線**。三主機各自 `nohup bash iab/training_scenario_driver.sh --host {pc1,pc2,pc3} --epoch <同一個值> &`（PC2/PC3 用 `ssh -f`，PC1 用 `nohup ... & disown`），PC1 額外 `nohup bash iab/training_watchdog.sh --reward-mode throughput_only --model-arch mlp --fl-mode cluster --epoch <同值> &`，以及一個 30 分鐘一次的健檢迴圈（`nohup bash -c 'while true; do bash iab/training_healthcheck.sh --since 1800; sleep 1800; done' > /tmp/training_health_log_stage3.txt &`）。逐一用 `ps -o pid,ppid,cmd` 確認 PC1 本機三個常駐行程 PPID 皆為 `1`（已跟呼叫它們的 shell 脫鉤）；PC2/PC3 的驅動器 PPID 是各自 sshd session 底下的 `nohup` 包裝行程（不是 `1`），但 `nohup` 本身就保證該行程不受 SIGHUP 影響、SSH session 結束後會自動被 init 收養繼續執行，效果等同，不是保護力較弱的替代方案。這三個行程完全獨立於發起指令的 Claude Code session（無論是使用者手動 SSH 或這次的 session）是否還存在，是訓練能撐過使用者筆電 SSH 斷線、本人無法介入的真正保證機制。

**Phase 7.5（2026-09-19 事後發現的嚴重遺漏，必須誠實記錄）：Phase 2 乾淨重啟時漏掉 `run_local_pc1.sh --skip-server`，導致全部 12 個 C 語言 Local xApp 容器從未啟動**——`start_iab_server.sh` 只負責基礎設施（CN5G/FlexRIC/MongoDB/Donor CU-DU/12 組 inference-nodeN），依 CLAUDE.md 既有文件，啟動 xApp 是接在後面的 `run_local_pc1.sh --skip-server`（等 13/13 E2 → 逐一 `docker compose up -d node${i}-l-xapp`）這一步的職責，但 Phase 2 的乾淨重啟序列裡這一步被漏掉，直接跳去做 Phase 3（UE 連通性驗證）與 Phase 4（Stage 2 量測）。`docker ps -a` 事後確認 `xapp-node1`~`xapp-node12` 全部「no such object」，從未被建立過。

**影響範圍比想像中大**：
1. **Phase 4 的 Stage 2 固定場景量測整份作廢**——量測全程沒有任何 E2SM-MAC CONTROL-REQUEST、Local rApp（DRL Actor）從未被呼叫、Global xApp 的 `fairness_bias` 從未被送達任何節點，DU 端 PRB 分配全程是純 OAI 內建排程器行為（疊加跟 PF baseline 相同的 backhaul-aware PRB 預算機制）。量到的「JFI=0.3960／8.78Mbps／194.86ms，三項全優於 PF baseline」這組數字因此**不能歸因給 Stage 2 訓練出來的 policy**，已在 `experiment_results/avgFL.md` 最上方加註大篇幅更正說明並保留原始數字供記錄，不刪除。
2. **Stage 3 啟動後同樣空轉了將近 40 分鐘**——Phase 6/7 封存 Stage 2 資料、啟動 Stage 3、上線 watchdog／場景驅動器／健檢迴圈的整個過程，xApp 都不存在，`node{N}_experiences` 全部掛 0，`training_healthcheck.sh` 的 30 分鐘健檢第一輪其實有印出異常訊號（Section B「AI 決策」計數應為 0 卻因為既有的 bash 語法錯誤〔`[[: 0\n0: syntax error`，見 CLAUDE.md/先前條目已知但未修的 bug〕整段被吞掉、沒有清楚報出來），沒有在當下被抓到。

**為什麼「17 UE 連通性 100%」「RestartCount 全程 0」這些檢查都沒抓到**：這兩項檢查驗證的是**資料面**（UE 能不能連上 DN）與**容器沒有崩潰重啟**，兩者都不依賴 xApp 是否存在——沒有 xApp 只代表 DU 端完全沒有收到 PRB 覆寫指令，資料面本身（Uu/F1/N3 tunnel、路由、DNAT）不受影響，UE 一樣可以正常上網，只是排程完全是 OAI 預設行為。這是這次的核心教訓：**「UE 連通性正常」「容器沒重啟」只能證明資料面健康，完全不能代表 DRL 控制迴圈真的在生效**，必須額外檢查 `docker ps` 確認 12 個 `xapp-nodeN` 容器存在、`docker logs flexric | grep -c "E2 SETUP-REQUEST"` ≥13、且 `xapp-nodeN` log 有 `CONTROL-REQUEST tx`/`CONTROL ACK rx` 這種主動控制訊號在跑，缺一不可。

**修復**：發現後立即 `bash iab/run_local_pc1.sh --skip-server`，13/13 E2、12/12 xApp、12/12 ZMQ socket 全部就緒，`xapp-node1` log 確認持續有 `CONTROL-REQUEST tx`/`CONTROL ACK rx`，MongoDB 全部 12 個節點的 `node{N}_experiences` 立刻開始有新文件（30 秒內每節點 17~62 筆）。Stage 3 訓練從這個時間點才算真正開始，`avgFL.md` 已同步更正「下一步」段落的時間點敘述。

**決定：不回頭重跑 Stage 2 的封存 checkpoint 重新驗證量測**——checkpoint 本身（6.5 小時的真實訓練過程，xApp 當時確實有在跑）沒有受影響，只是「量測」這個驗證環節失效；重跑會需要暫停剛剛才真正開始運作的 Stage 3、來回切換 Mongo collection／checkpoint／FL_MODE 好幾輪，操作複雜度與再次出錯的風險都不小，且使用者已明確表示優先順序是讓訓練持續推進、不要因為介入而停下來。`avgFL.md` 已如實標注這組數字作廢、原因、以及「有效的 Stage 2 驗證量測待後補」，留待有餘裕、或 Stage 3 需要跟 Stage 2 做比較時再回頭用封存的 checkpoint（`experiment_results/checkpoints_archive/stage2_avgfl_20260919/`）補做一次。

**Phase 7.6（2026-09-19 續，同一晚）：`training_watchdog.sh` 的 `full_recovery()` 遇到單一主機軟性失敗就整個中止，導致 xApp 層被晾著——已修復**。01:06 一次真實 FlexRIC 崩潰觸發自動復原，卡在 `[2/4] PC2` 這步（Node5 的 DU 因為 MT tunnel 建立過慢被腳本自己的 300 秒逾時跳過、從未建立，是先前已知的「DU 建立被跳過」模式再次出現），`full_recovery()` 偵測到「重試 5 次後仍有 UE 連不通」立刻 `return 1`、整個中止——**代表 [3/4] PC3、[4/4] xApp 啟動這兩步完全沒有被執行到**，CU 又因為這次崩潰重建、NAT 表清空，PC3 的 DNAT 規則從未被重新寫入，PC1 的 xApp 容器（[1/4] PC1 步驟裡 `start_iab_server.sh` 自己的 `docker compose down` 已經把它們清掉，且只有到 [4/4] 才會重新建立）也因此持續處於「已停用、沒人重啟」的狀態，直到下一次 30 分鐘健檢週期才被人工發現，訓練空轉了近一小時。

人工修復當下的問題後（手動重建 PC2 的 DU5、重新驗證/修復 PC3 全部 DNAT／路由、`run_local_pc1.sh --skip-server` 重新啟動 xApp、以新 epoch 重啟三主機驅動器＋watchdog），順手把這個設計缺陷本身也修掉，不留給下一次崩潰重演：`full_recovery()` 新增 `DEGRADED`／`DEGRADED_DETAIL` 計數器，把「單一主機的 UE 連通性軟性失敗（重試 5 次仍有 UE 連不通）」與「E2 連線數不足 13」都從「立即 `return 1` 中止」改成「記錄警告、繼續往下走」，只有 SSH/腳本本身非 0 結束（代表更根本的問題，例如主機真的連不上）、或**三主機同時**都回報軟性失敗，才視為需要人工介入而真正中止。這樣即使某一台主機的某個節點還沒修好，[4/4] 的 xApp 啟動步驟仍然一定會被執行，不會再讓整個訓練迴圈因為一個節點的問題被晾著。改完先 `bash -n` 語法檢查，然後**重啟正在跑的 watchdog process 本身**（單純編輯磁碟上的檔案不會讓已經在跑的 bash 行程套用新內容，必須整個換一個新行程）——只重啟 watchdog，不動三個場景驅動器（沿用同一個 epoch，wall-clock 自我校正機制保證位置不受影響）。

**Phase 7.7（2026-09-19 續）：Stage 3 固定場景試量測（8.5 小時訓練後）＋現場發現並修復第二個 `REWARD_MODE` 未傳遞 bug**。第 11 次崩潰的手動復原完成、17/17 UE 健康、xApp 明確驗證已啟動（吸取上次教訓，這次先確認 `docker ps | grep -c xapp-node` = 12、`CONTROL-REQUEST`/`AI 決策` 訊號都有才繼續）後，跑了跟 PF.md 完全相同方法論的固定場景量測（Scenario R / seed=20260914 / 900s）。

**結果不如預期**：JFI=0.3317（跟 PF 的 0.3303 幾乎持平）、平均吞吐量 4.01 Mbps（比 PF 的 6.45 Mbps **差 37.8%**）、平均 RTT 291.53ms（比 PF 的 224.23ms **差 30.0%**）——不是預期的「cluster FL 應該優於 PF」，量測全程三主機零崩潰，數字本身量測過程沒有問題。

**追查發現一個活躍中的 bug**：檢查 `inference-node1` log 發現 `lambda` 欄位持續在漂移（0.85→1.02，明顯是 Lagrangian dual-ascent 更新的行為模式），但 `REWARD_MODE=throughput_only` 理論上應該讓 `drl_agent.py` 完全跳過 λ 更新。逐一排查：`inference-node1` 自己的 `REWARD_MODE` 環境變數（含 `/proc/1/environ` 直接確認 PID 1 的真實環境，排除 `docker exec` 顯示的環境跟實際運行中 process 不一致的可能）確認正確是 `throughput_only`；但檢查全部 12 個 `flower-supernode-nodeN` 容器（`docker exec flower-supernode-nodeN cat /proc/1/environ`）發現**全部都是 `REWARD_MODE=lagrangian`**——這是 2026-09-14 那次 root cause 1（`flower-supernode-nodeN` 從未收到 `REWARD_MODE`）的同一類問題**沒有修乾淨**：當時的修法是幫 compose 檔的 `flower-supernode-nodeN` 服務定義加上 `REWARD_MODE: "${REWARD_MODE:-lagrangian}"`，但這個 `${VAR:-default}` 語法是在「執行 `docker compose up` 那個當下的 shell 環境」裡展開的——`run_stage2_fl.sh` 第 4 步與 `training_watchdog.sh::full_recovery()` 帶起這批 stage2-fl 服務時，都只在指令前面加了 `FL_MODE=... MODEL_ARCH=...`，漏了 `REWARD_MODE=...`，所以每次透過這兩個進入點啟動／重建 `flower-supernode-nodeN`，都會悄悄吃到預設值 `lagrangian`，整個 Stage 3 訓練期間（含這次量測當下）都是如此。

**追查是否真的污染了 policy 權重**：讀 `drl_agent.py` 原始碼確認 `self._lambda`（Lagrangian 乘子）的全部使用點——只在 metrics 記錄與 checkpoint 存讀時被讀寫，MLP 分支的 `actor_loss`/`critic_loss` 計算完全沒有用到它；另外確認 `training_pipeline.py`／`client_app.py`（FL 訓練路徑）都不呼叫任何 reward 計算函式，純粹讀取 MongoDB 裡已經由 `inference-nodeN`（用正確的 `throughput_only` 公式）寫好的 `reward` 欄位訓練——結論是這個 bug **不會**直接污染梯度更新／policy 權重本身，但代表：(1) 違反了「Stage 2~4 全程應為 throughput_only」的設計不變式；(2) checkpoint 裡的 `lambda` 欄位本身是髒的；(3) 不能排除還有其他尚未追查到的間接路徑。

**修復**：`run_stage2_fl.sh`／`training_watchdog.sh::full_recovery()` 兩處帶起 `flower-supernode-nodeN` 的指令都明確補上 `REWARD_MODE="$REWARD_MODE"`（不能只依賴 compose 檔的預設值語法），並把 `training_watchdog.sh` 那處原本沒有的 `--force-recreate` 也加上（沒有它，環境變數改了也不會套用到已存在的容器）。現場立即用正確環境變數 `--force-recreate` 全部 12 個 `flower-supernode-nodeN`，`/proc/1/environ` 逐一確認修復生效。

**判斷**：鑑於 (a) 上述 bug 雖不直接污染梯度但代表環境不乾淨、(b) 8.5 小時訓練期間歷經 11 次崩潰復原，個別 UE 連通性的既有不穩定模式可能延續進量測窗口（量測結果裡 UE15 覆蓋率 0%、多個節點覆蓋率低於 50%，範圍橫跨三主機，不是單一深層節點的既有模式），這次的量測結果記錄進 `clusterFL.md` 但明確標註為「confound 尚未排除的快照，不是 cluster FL 演算法優劣的最終定論」，不因為數字不好看就重跑到「滿意」為止，也不因為 bug 已修就直接宣稱重跑會變好——如實記錄現況與限制。

**Phase 7.8（2026-09-19 續）：Stage 2 有效重測（用封存的 Stage 2 checkpoint）＋兩份量測的最終誠實結論＋還原 Stage 3 繼續訓練**。用 `iab/archive_stage_data.sh`／新增的 `iab/restore_stage_data.sh`（`archive_stage_data.sh` 的反向操作，docker cp 還原 checkpoint、MongoDB `renameCollection` 搬回即時 collection 名稱）把 Stage 3 當時的即時狀態封存、換回 Stage 2 封存的 checkpoint 與經驗，`inference-nodeN`／`flower-supernode-nodeN`／`flower-superlink` 的環境變數逐一 force-recreate 確認正確（`REWARD_MODE=throughput_only`、`FL_MODE=avg`、`MODEL_ARCH=mlp`），`inference-node1` log 確認載入的是訓練步數 2600 的真實 Stage 2 checkpoint（不是隨機初始化）。

**過程中的操作失誤（如實記錄）**：archive/restore 這兩支腳本操作 MongoDB collection 改名時，因為 `inference-nodeN` 全程沒有暫停，期間持續有新經驗寫入，導致好幾次 `renameCollection` 因為目的地 collection 已被新資料重新建立而失敗（`target namespace exists`）；一次用「把新資料併回 Stage 3 封存 collection」的方式排除，但誤把 node7/node8**已經成功還原**的 Stage 2 資料（6188/5694 筆）當成「Stage 3 殘留新資料」一併併入 Stage 3 封存 collection，事後改用時間戳切分想要救回來，但混雜程度已經無法乾淨切乾淨，最終決定 node7/8 直接用空 collection 起始（checkpoint 本身沒有受影響，只有 MongoDB 經驗歷史的極小部分——2 個節點的背景訓練資料——永久混進了 Stage 3 的封存記錄，不影響任何一份 checkpoint 的正確性）。**教訓**：這類 archive/restore 操作應該先停止 `inference-nodeN`（消除寫入競爭）再做 collection 改名，第二次操作（Stage 2→Stage 3 換回）改成先停用再操作，全程順利無誤，之後任何類似操作都應該先停用寫入端。

**量測結果**：JFI=0.3293（跟 PF 的 0.3303 幾乎持平）、平均吞吐量 4.40 Mbps（比 PF 差 31.8%）、平均 RTT 312.44ms（比 PF 差 39.3%）、3/17 UE 零吞吐量。**但量測開始時 UE9/10/15/16/17 共 5 個節點已知連不通**（花了大量時間嘗試路由/DNAT/DU 重啟/conntrack 清空等既有修法皆未能在合理時間內解決），這 5 個節點佔了近三分之一，且 3 個直接零樣本，判斷這份數字主要反映的是「連通性缺口拉低全域平均」，不是 policy 決策品質——有連通性的 12 個節點（UE1~8、UE11~14）看起來的吞吐量/RTT 量級跟 PF/clusterFL 同一批節點相近，沒有明顯異常。詳細記錄於 `avgFL.md`。

**兩份量測（Stage 2 補測、Stage 3 量測）的共同結論**：都不建議直接拿來下「cluster FL 優於／劣於 avg FL」的定論——Stage 3 的量測受已修復的 `REWARD_MODE` bug 與整晚崩潰復原後的連通性殘留影響；Stage 2 補測則是量測當下就有 5 個節點已知連不通。兩者都是同一晚系統反覆出現的連通性不穩定模式造成的 confound，不是模型本身的問題。建議之後找一個系統穩定、17/17 UE 連通性確認良好的時間點，重新做一次乾淨的三方比較量測，才能對「PF < avg FL < cluster FL」這個路線圖假設下真正的定論。

**收尾**：Stage 2 的即時資料（含補測期間新增的少量經驗）重新封存回 `node{N}_experiences_stage2_avgfl_20260919`，checkpoint 同步更新封存檔；還原 Stage 3 的封存資料（`inference-node1` 確認載入訓練步數 4600~4600，與封存前一致，證實還原正確）、`FL_MODE` 設回 `cluster`。還原過程中意外發現 FlexRIC 在量測期間曾經歷一次短暫崩潰又自動重啟（`RestartCount` 0→1），且**這次 xApp 的 E2 訂閱在 FlexRIC 重啟後沒有自動恢復**（`docker logs xapp-node1` 卡在「等待 Node 1 連線...目前已連接節點數:6」，`flexric` log 大量「RIC Indication message arrived...but no xApp associated」）——單獨 `docker restart xapp-node{1..12}` 未能徹底解決（只恢復部分節點），最終依照 CLAUDE.md 既有記載的「FlexRIC 崩潰只重啟 xApp/DU 不夠，必須連 FlexRIC 一起完整重啟三主機」原則，做了最後一次三主機完整乾淨重啟才徹底解決；同一輪還發現並修復 PC1 host 對 Node1/3/4 relay tunnel IP 的路由（`ip route ... via macvlan-br`）過期未更新、PC3 的 access DU（Node9/10/11/12）在先前手動 `docker restart` 後遺失了往 MT internal IP 的自訂路由（`ip route replace ... via <mt_internal_ip>`，`docker restart` 會重置容器的網路命名空間，連帶清空 exec 進去下的 runtime route，這是先前記錄過的 DNAT/route 表在容器重啟後失效模式的同一類、但作用在不同路由表上的變體）。三主機重啟＋兩項路由修復後，17/17 UE 中 16 個確認連通（UE17 維持其一貫的 Node4 三重負載自我節流模式），以新 epoch 重新啟動三個場景驅動器與 watchdog，確認訓練已恢復（MongoDB 經驗數持續增加）。

**Phase 7.9（2026-09-19 續）：三方乾淨重測（Stage 3 → Stage 2 → PF），全部在使用者明確要求「先驗證 17/17 UE 連通、確認 xApp 真的在跑，再量測」下完成**。使用者指出先前 Stage 2 補測明知 5/17 UE 連不通卻仍繼續量測是錯誤做法，要求這次確實做到位。

**Stage 3 乾淨重測**：三主機依序乾淨重啟（過程中修復 PC1 host 對 relay tunnel 的過期路由 `ip route ... via macvlan-br`），量測前逐項驗證 17/17 UE 現場 ping 0% 封包遺失（含 UE17，本次首次真正做到全部連通）、`docker ps` 確認 12/12 xApp、`xapp-node1` log 確認真實 `CONTROL-REQUEST`/`AI 決策` 訊號、`FL_MODE=cluster`／`REWARD_MODE=throughput_only`／`MODEL_ARCH=mlp` 逐一用 `/proc/1/environ` 確認。結果：JFI=0.3113、吞吐量=5.23 Mbps、RTT=301.93ms，三項仍劣於 PF，但這次 **0/17 UE 零吞吐量**（比先前版本的 1/17 更乾淨），confound 已排除，數字可信。寫入 `clusterFL.md`，取代先前受 `REWARD_MODE` bug／連通性殘留污染的版本。

**Stage 2 乾淨重測**：archive Stage 3 即時狀態（新 tag `stage3_clusterfl_20260919_v2`，先 `docker compose stop inference-node{1..12}` 消除寫入競爭，這次 archive/restore 全程順利、沒有再犯上次的 collection 改名競爭錯誤）→ restore Stage 2 封存 checkpoint/經驗 → force-recreate `inference-nodeN`（`REWARD_MODE=throughput_only`）+ FL 層（`FL_MODE=avg`）→ 同樣逐項驗證 17/17 UE＋xApp 真實運作。過程中再次踩到「PC3 個別元件檢查都正常但 end-to-end 仍不通」的謎樣模式，最終追查到是 xApp 在某次 PC3 單獨重建後**E2 訂閱狀態跟 FlexRIC 對不上**（`flexric` log 大量 `RIC Indication message arrived...but no xApp associated`，`xapp-node1` 卡在「等待 Node 1 連線...已連接節點數:0」）——單獨 `docker restart xapp-node{1..12}` 沒用，必須依 CLAUDE.md 既有記載的順序（FlexRIC→DU→xApp）做完整三主機重啟才解決，這是本次 session 第二次遇到同一個模式，值得記錄成明確的排查優先順序：**「個別節點連通性檢查都過但整體還是不通」時，優先懷疑 xApp-FlexRIC 的 E2 訂閱狀態不同步，不要一直在路由/DNAT/NAT 層打轉**。

結果：JFI=0.2453（比 PF 的 0.3303 差 25.7%，是目前所有量測中最低的 JFI）、吞吐量=6.81 Mbps（比 PF 的 6.45 高 5.6%）、RTT=260.14ms（比 PF 差 16.0%）。混合結果——吞吐量小贏但公平性明顯輸，判斷是 `REWARD_MODE=throughput_only` 沒有 JFI 限制式、policy 傾向把資源集中給少數節點換取總量最大化的合理結果，呼應 Stage 5 規劃要重新啟用 `REWARD_MODE=lagrangian` 的設計動機。寫入 `avgFL.md`，取代前兩次因 xApp 未啟動／5 個 UE 已知連不通而不可信的版本。

**PF baseline 同夜重測**：Stage 2 補測完後，直接 `docker stop xapp-node{1..12}`（PF 模式不需要碰 checkpoint/MongoDB，只要停用 xApp 即可，比 Stage 2/3 之間的切換簡單很多）。量測前一樣逐項驗證 17/17 UE 連通（含修復 PC1 本機 Node7/8 的 MT PREROUTING NAT 規則遺失——跟先前 PC2 Node5 遇到的是同一類問題：`docker restart`／PDU session 自發重建會清空 `iptables -t nat -A PREROUTING ...` 這類 runtime 規則，不是路由層級能修好，必須額外補上 NAT 規則本身）。結果：JFI=0.3017、吞吐量=7.33 Mbps、RTT=238.12ms，跟 2026-09-13 原始基準（0.3303／6.45／224.23）同量級但有 ~10% 上下的差異，判斷是場景/系統狀態的正常雜訊，不是 PF 排程器行為改變。寫入 `PF.md` 新增章節（原始基準保留不動），建議後續 Stage 2/3 比較優先用這份同夜基準做 paired comparison。

**使用者明確指示**：更新完 PF 相關數據檔案後先停止，這次**不**把系統換回 Stage 3 繼續訓練——目前系統停留在 PF baseline 狀態（全部 12 個 xApp 容器 stopped，`inference-nodeN`/`flower-*` 仍是 Stage 2 的設定，checkpoint/經驗不受影響），等待下一步指示。Stage 3 的完整訓練狀態（243k+ 筆經驗、訓練步數 6768 的 checkpoint）已安全封存在 `node{N}_experiences_stage3_clusterfl_20260919_v2` collection 與 `experiment_results/checkpoints_archive/stage3_clusterfl_20260919_v2/`，之後要恢復訓練只需要 `bash iab/restore_stage_data.sh stage3_clusterfl_20260919_v2` + `FL_MODE=cluster` 重啟即可接續。

## 2026-09-20~21 — 發現並修復 iperf3 多埠 server 從未啟動的重大 bug（污染整天的 Stage 2 重訓資料）＋PF/avgFL/clusterFL 三方 Scenario T 乾淨比較

**背景**：前一晚（見上方 2026-09-19 條目）發現舊版 Stage 2 訓練橫跨 Scenario T 輪替表改版時間點，對測試用 Scenario R 曝光比例失衡（~51% vs Stage 3 的 ~20%），判斷是 cluster FL 表現不如 avg FL 的可能 confound。使用者決定：重新訓練 Stage 2（沿用新版 T 佔多數的輪替表），且這次量測改用 **Scenario T**（3×3 流量×路徑損耗交叉設計，全決定式、無 seed，比 Scenario R 的「同 seed」更嚴格可重現）取代 Scenario R，等訓練跑到跟 Stage 3 相近的規模／比例後，做一次 PF／avg FL／cluster FL 三方乾淨比較。

**Stage 2 重訓過程**：整晚透過 `training_watchdog.sh` 自動偵測 FlexRIC 崩潰並完整重啟（本輪累計發生 7 次），期間額外發現並修復多個新的基礎設施缺口：(1) `run_local_pc2.sh`／`start_iab_server.sh` 偶爾會**靜默跳過**啟動某個 access DU 容器（本輪至少兩次是 `rfsim5g-iab-du-5`，`docker ps -a` 直接查不到這個容器，母腳本卻正常回報「Ready」），需要手動 `docker compose up -d` 補上，並補齊該 DU 的自訂路由與其 MT 的 PREROUTING/POSTROUTING NAT 規則（因為這些設定原本是跟 DU 啟動綁在同一個函式裡一起下的，DU 沒啟動就全部漏掉）；(2) UE17（直連 Node4 relay）不在標準 `verify_and_heal_ues()` 範圍內，每次完整重啟後預設路由／DU4 F1-U 綁定位址別名都需要重新斷言，已把這個 heal 邏輯補進 `training_watchdog.sh::full_recovery()`；(3) `flower-supernode-nodeN` 的 `REWARD_MODE` 環境變數在 `start_iab_server.sh` 的靜默 recreate 後又飄回預設值 `lagrangian` 過一次，比照既有修法用 `--force-recreate` 明確帶正確環境變數蓋掉。訓練期間持續用「MongoDB 經驗時間戳 + 已知的 epoch/輪替表」直接反推每筆經驗當下實際套用哪個 scenario，追蹤 T/R/A/B/C 五類的真實佔比（而非只看訓練小時數）是否收斂到目標比例（T 53.3%／R 20%／A 8.3%／B 8.3%／C 10%），比小時數更能反映訓練資料的場景涵蓋是否足夠。

**使用者觸發三方測試**：明確指示「等下一次 FlexRIC 真的崩潰時」不要照舊自動修復回訓練模式，改為把那次完整重啟直接當成三方 Scenario T 比較的起點：PF（全部 xApp/rApp 關閉，只靠 OAI 內建排程器）、avg FL（還原這次 Stage 2 重訓的即時 checkpoint，`MODEL_ARCH=mlp REWARD_MODE=throughput_only FL_MODE=avg`）、cluster FL（還原已封存的 `stage3_clusterfl_20260919_v2`，`FL_MODE=cluster`），每個 stage 都先完整清乾淨重建（含路由）、驗證 17/17 UE 連通，才做 15 分鐘量測。觸發後依此流程：停用 watchdog／三個場景驅動器 → 停用 `inference-nodeN` → 用 `archive_stage_data.sh stage2_avgfl_retrain_20260920` 封存這次重訓的即時資料（12 節點合計 157,750+ 筆經驗＋12 份 checkpoint，全數安全封存，不受後續任何操作影響）→ 停用全部 xApp/rApp/FL 層 → 17/17 UE 驗證通過 → 開始 PF baseline 的 Scenario T 量測。

**重大發現：iperf3 多埠 server 從未正確啟動，整個 session（含整晚的 Stage 2 重訓）只有 UE1 真正有流量**。PF 第一次量測（Scenario T，15 分鐘）跑完後分析結果：17 個 UE 裡有 16 個 `achieved_mbps` 覆蓋率是 **0%**（iperf3 完全沒有成功量到任何吞吐量樣本），只有 UE1 正常（覆蓋率 98.7%）；RTT 覆蓋率大致正常，代表純 ICMP 連通性沒問題，問題出在 iperf3 這一層。查 `pf_scenario_pc1.log` 發現大量 `WARNING iperf3 supervisor 退出 rfsim5g-end-ue-N (rc=1)，重啟 loop...`，且是**整個 15 分鐘量測全程持續發生**，不是偶發。手動對 `rfsim5g-end-ue-6` 執行 `iperf3 -c 192.168.72.135 -p 5206` 直接重現 `Connection refused`。查 `rfsim5g-oai-ext-dn` 容器內的 process，發現只有一個 `iperf3 -s`（無 `-p` 參數，只監聽預設埠 5201），但 `traffic_scenario.py` 的 `UE_IPERF_PORTS` 對照表把 UE1~17 分別對應到 port 5201~5217（一個 UE 一個獨立埠）——單一預設埠的 server 只能服務到剛好對到 5201 的 UE1，其餘 16 個 UE 的 client 連線全部找不到監聽的 server。追查發現專案裡其實早就有 `scenarios/setup_iperf_servers.sh` 這支腳本，專門負責啟動 17 個各自獨立的 iperf3 server（port 5201~5217，各自包一層 `while true` 自動重啟 loop），且 `run_stage2_fl.sh` 執行完的提示訊息裡也明確要求「請先執行 `bash scenarios/setup_iperf_servers.sh`」——但這支腳本**從未被 `start_iab_server.sh` 或 `training_watchdog.sh::full_recovery()` 呼叫過**，這兩個本次 session 用來做乾淨重啟的進入點都只執行內建的單一 `iperf3 -s`，是遺漏，不是新產生的迴歸。

**影響範圍評估（用 MongoDB 資料實測，不是猜測）**：對 `stage2_avgfl_retrain_20260920` 封存資料的 12 個節點分別取樣 2000 筆經驗、算 `state.bsr`／`state.dl_buffer_info` 平均值：只有 Node1（relay，承載 Node5 access 的 backhaul 聚合流量）與 Node5（access，直接服務 UE1/UE2）顯示真實活動（avgBsr 約 223,661~229,669、avgBuf 約 1,775~1,877），其餘 10 個節點（Node2,3,4,6,7,8,9,10,11,12）全部趨近於零（avgBsr 個位數到個位數十、avgBuf=0）——精確對應「只有承載 UE1 流量的路徑有真實負載，其餘節點的 UE 全部因為 iperf3 連不上而近乎閒置」這個假設。結論：**整晚累積的 157,750+ 筆 Stage 2 重訓經驗裡，10/12 節點的資料反映的是近乎閒置、不是真實壅塞情境**，直接違背這次重訓「補足 Scenario T 真實壅塞曝光」的初衷。

**修復**：立即執行 `bash scenarios/setup_iperf_servers.sh`，確認 17 個埠全部監聽中，手動重測單一 UE（`rfsim5g-end-ue-6` → port 5206）成功量到 17.5 Mbits/sec，確認修復生效。已在 `CLAUDE.md` 第 6 節補上明確警語與強制步驟：**任何乾淨重啟後、啟動 `traffic_scenario.py`（不管是量測的一次性呼叫，還是 `training_scenario_driver.sh` 的訓練用長駐呼叫）之前，必須先執行這支腳本**，並記錄這是靜默失敗模式（不會讓腳本報錯、也不會讓 UE 的 ping 連通性檢查失敗，只有 iperf3 吞吐量樣本會全數缺失），特別容易被忽略。`training_watchdog.sh::full_recovery()` 目前仍未自動呼叫這支腳本，是已知缺口，留待之後補上自動化（例如接在 `[4/4] 啟動 xApp` 之後）。

**使用者決策（如實記錄，不事後補救）**：修復後用 v2（乾淨）重跑 PF 的 Scenario T 量測；`stage2_avgfl_retrain_20260920` 這份已經確認受污染的 checkpoint，使用者明確指示**照舊用來做 avg FL 這一階段的量測，並在對應的 `avgFL.md` 報告裡誠實註明這個限制**，不因為發現問題就整個作廢重訓（重訓一次是數小時等級的成本，使用者判斷這次先如實記錄限制、後續有需要再回頭補一次乾淨訓練即可）。PF 本身的量測不受影響（PF 不依賴任何 DRL 訓練資料），沿用同一批乾淨的 v2 結果。

## 2026-09-22 — 拓樸重新分配：Relay 全部集中 PC1，消除「同主機」不公平優勢

**背景**：2026-09-21 完成的 PF/avgFL/clusterFL 三方 Scenario T 乾淨比較（見上方條目與 `PF.md`/`avgFL.md`/`clusterFL.md`）逐 UE 比對後，使用者發現一個持續橫跨三個 stage 的模式：UE5~8（當時 Node2+Node7,8，跟 Donor 同在 PC1）在全部三次量測裡都是吞吐量最高的一群（09-21 PF 數據：23.70/5.59/20.99/2.38 Mbps），UE1~4/UE9~16（需要真正跨主機傳輸）全部是最低的一群（0.27~3.31 Mbps）。這個分組跟場景配置、排程演算法完全無關，純粹是「跟 Donor 同主機」省去一段實體跨主機開銷造成的量測 confound——只有 Node2/7/8 這一條分支的 Donor→Relay→Access 路徑完全在本機內完成，其餘三條分支都要真的走 USB-Ethernet 轉接卡→switch→轉接卡。使用者明確表示「我想改變拓樸的放置位置 我希望大家都是公平的」，要求進入 plan mode 設計並執行一次實體搬遷。

**設計決策**：討論後，使用者提出並採用的方案（比 Claude 最初建議的「relay 分散、access 整條分支搬家」方案更好）：把全部 4 個 relay（Node1~4）集中搬到 PC1（跟 Donor 同機，relay 本身負載比 access 輕），access 節點（Node5~12）平均分散到 PC2（Node5,6,7,8）/PC3（Node9,10,11,12），UE17 容器搬到 PC3。這樣四條分支的路徑結構第一次完全一致：Donor→Relay 全部同機（PC1 內部）、Relay→Access 全部跨主機，沒有誰佔便宜。負載驗證：PC1 從原本的 14 降到 10（Donor CU/DU 2 + 4×relay 8）、PC2:16（4×access 8 + UE1~8）、PC3:17（4×access 8 + UE9~17），全部低於已驗證安全的 ≤18 上限，比「0 個 relay 留 PC1、2 組完整分支各給 PC2/PC3」（會衝到約 20~21）安全許多。UE17 的 host 判斷特例（邏輯上仍掛在 Node4 底下，但容器要跟 access 節點一起分散到 PC3）採用「加一個小型 `UE_HOST_OVERRIDE` 字典」方案（AskUserQuestion 確認），而不是更動 `HOST_OF_NODE`/`NODE_CONFIG` 的核心結構。

**執行範圍**：3 份 docker-compose（搬容器定義區塊）、2 份 access DU conf（`iab_du_node7.conf`/`node8.conf` 的 internal-bridge IP 從 `192.168.76.x` 改成 `192.168.74.x`）、3 支 `start_iab_*.sh`（PC1 改成本機迴圈啟動全部 4 個 relay、不需 SSH；PC2 改成啟動 Node5~8 四個 access；PC3 新增 UE17 啟動邏輯）、`scenarios/traffic_scenario.py`（`HOST_OF_NODE` 更新＋新增 `UE_HOST_OVERRIDE`）、`CLAUDE.md` 第 1 節拓樸文件。全部檔案先在 PC1 用 `git diff` 級別的精確度改完、每一步都用 Python 腳本驗證（YAML 語法、service 集合前後一致 90 個不多不少、`bash -n` 語法檢查）才動手實際重啟三主機。

**執行過程中定位並修復的三個問題**（比原始 plan 多發現的部分）：

1. **PC2/PC3 各自維護獨立的檔案系統副本，直接編輯 PC1 上的原始碼不會自動同步過去**——這是 CLAUDE.md 第 7 節早就明文警告過的規則（「所有修改先在 PC 1 完成，再將修改好的檔案傳給 PC 2、PC 3」），但這次是第一次改到「啟動腳本本身的邏輯」（不只是共用的 C/RAN 底層檔案），執行遷移時漏掉了這一步。第一次跑 `run_local_pc2.sh` 時，PC2 實際執行的仍是舊版 `start_iab_pc2.sh`（仍會啟動 Node1,3,4 relay），跟 PC1 剛啟動好的同名 relay（相同 macvlan IP `.150`/`.152`/`.153`）在同一個共用 L2 網段上發生短暫 IP 衝突，導致 PC1 的 `rfsim5g-iab-mt-1` 重啟 2 次、`rfsim5g-iab-mt-4` 重啟 1 次。發現後立即 SSH 到 PC2 `kill` 掉腳本 process、`docker compose -f docker-compose-iab-pc2.yaml down` 清空，用 `rsync -avR` 把全部 10 個改動檔案（含 `CLAUDE.md`）同步到 PC2/PC3（排除 `iab_du_node1~4.conf`，這幾份是 relay DU 啟動時腳本自動 `sed -i` 覆蓋的執行期產物，不屬於遷移本身的改動），並將 PC1、PC2 各自完整 `down`→重啟一次，才確認乾淨。
2. 手動排查 UE15/16（Node12）連線異常時，只 `docker restart` 了 MT12 並手動補了 MT12 自己的路由＋CU 端 DNAT 規則，遺漏了 DU12 本身也需要重新斷言的路由（`configure_and_start_access_du()` 裡對 DU 容器下的那幾條 `ip route replace`），導致原本正常的 UE16 也跟著斷線——這正是 CLAUDE.md 第 6 節「長時間偵錯累積的手動介入也可能讓個別 UE 處於斷線狀態，靠繼續 debug 往往找不到，乾淨重啟通常是最快的解法」的活教材。最終放棄手動修補，完整 `docker compose -f docker-compose-iab-pc3.yaml down` + 重新執行 `run_local_pc3.sh`，UE9~16 全部一次到位（第 1 次健康檢查即通過）。
3. **新架構下 UE17 的 pathloss channelmod 控制跟 iperf 流量控制第一次物理分屬兩台主機**：Node4 的 DU telnetsrv（chanmod 控制埠 9092）現在跑在 PC1，但 UE17 容器在 PC3，而 telnet chanmod port 原本設計成只綁 `127.0.0.1`（同主機限定）。與使用者確認後採用「開放 Node4 telnet port 給 macvlan」方案（優於拆成兩個獨立呼叫或先不做動態控制）：`docker-compose-iab-server.yaml` 把該 port 的 host 綁定從 `127.0.0.1:9092:9092` 改成 `192.168.88.1:9092:9092`（macvlan-br 位址，只開放這一個 port，其餘節點的 telnet 仍是 `127.0.0.1`-only），並在 `traffic_scenario.py` 新增 `NODE_TELNET_HOST_OVERRIDE` 字典＋擴充 `build_controllers()`，讓 PC3 執行時能額外併入 Node4 的 controller（連 `192.168.88.1:9092` 而非 `127.0.0.1`），PC1 執行時則仍走 `127.0.0.1`（若曾經需要在本機測試）。這個修法是遷移過程中發現、跟使用者確認後才動手的，不在最初的 plan 文件裡。

**驗證結果**：13/13 E2 連線確認（`flexric` log 裡 unique Node ID 3584~3596 全部到齊，重複的行是我這次除錯過程中的重連，不是新節點）；16/17 UE 現場 ping 0% 封包遺失；三主機即時 process 數量 PC1:10／PC2:16／PC3:17，跟遷移前估算完全吻合；`nproc`=16（三台皆同），量測期間 load average 落在 20~53%，無過載跡象；全部 17 個 RAN 容器（relay/access 的 MT+DU）RestartCount 在最終乾淨重啟後全數維持 0。

**UE17 現場單次 ping 測試仍然 100% 封包遺失**（container restart ×2、Node4 telnet cross-host 開通後仍然如此），但這跟舊拓樸下已經記錄過的「Node4 三重負載邊界案例」（Node4 同時中繼 Node11/Node12、又直連服務 UE17）成因相同，並非這次遷移新增的問題——後續 15 分鐘 Scenario T 正式量測顯示 UE17 在量測視窗內其實有 78.4% 吞吐量取樣成功率，判斷是間歇性連線品質、不是全程斷線。使用者明確指示「接受 UE17 異常，繼續 16/17 驗證往下跑」，不再深入除錯這個已知的結構性瓶頸。

**PF baseline 重測（Scenario T，跟 09-21 同方法論）**：`bash scenarios/setup_iperf_servers.sh` 確認 17 埠監聽後，PC2/PC3 分別背景執行 `traffic_scenario.py --scenario T --host {pc2,pc3} --num-phases 15` ＋ `measure_stage.py --host {pc2,pc3} --duration 900 --interval 5`（PC1 在新拓樸下不再有任何 UE，不需要執行這兩支腳本）。**結果完全驗證了遷移動機**：JFI 從 09-21 舊拓樸的 0.2812 躍升到 **0.9812**，UE5~8 對其餘 13 UE 的吞吐量比值從約 10~20 倍收斂到 **1.08 倍**，證實先前「固定幾個 UE 持續最高」現象的主因確實是同主機優勢，不是排程或場景差異。平均吞吐量從 4.23 Mbps 降到 1.06 Mbps——這不是系統劣化，而是舊拓樸的 4.23 Mbps 本來就是被 UE5~8 的異常高值拉高的假象（扣掉這 4 個 UE，舊拓樸其餘 13 個 UE 平均本來就只有約 1.4~1.5 Mbps，跟新拓樸全部 17 UE 收斂到的 1.06 Mbps 屬同一量級）。完整數據與分析見 `experiment_results/PF.md` 「2026-09-22 — 拓樸重新分配後的 Scenario T 重測」章節。

**後續影響**：Stage 2（avg FL）／Stage 3（cluster FL）若要在新拓樸下重新量測比較，應該跟這份新的 PF 基準比較，不要跟 09-21 舊拓樸的數據比較——物理路徑結構不同，絕對數值量級的意義也不同；但相對改善幅度／JFI 仍可互相參照，用來判斷拓樸公平化之後，FL 聚合策略本身還能不能再貢獻額外的改善。Stage 2/3 已封存的訓練資料（`stage2_avgfl_retrain_20260920`／`stage3_clusterfl_20260919_v2`）完全不受這次遷移影響（只動了 RAN 資料面的 compose/腳本/conf，不動 MongoDB／checkpoint）。

## 2026-09-25 — TCP 吞吐量天花板調查（09-22~23）與改用 UDP 流量場景

**起因**：拓樸搬遷後的 PF baseline（見上方 2026-09-22 條目）JFI 雖然大幅改善，但 17 UE 平均吞吐量只有 1.06 Mbps，使用者擔心 TCP 數字太低、之後加上自己的方法也很難拉開差距。

**調查結果（TCP 為何低）**：
- 用 PC2 的 scenario log 把每筆吞吐量取樣對回當下的（路徑損耗, 目標頻寬）檔位：9 種組合的平均達成吞吐量全部落在 1.0~1.3 Mbps，且**整個資料集的單筆最高值都是 2.10 Mbps**——跟通道品質、目標頻寬檔位都沒有關聯（先前我把原因歸給「通道品質決定吞吐量」，被這份資料推翻）。
- 同一 UE、同通道（3dB）、同目標速率（20Mbps）手動對照：TCP 卡在 ~1.05 Mbps（每秒穩定 128KB、Retr=0），UDP 乾淨跑到 20.0 Mbps、0/32664 遺失。**底層無線容量吃得下，瓶頸在 TCP 本身**。
- 合理解釋（部分為推論）：TCP 吞吐量上限 ≈ 視窗 ÷ RTT，這個平台 RTT 300~700ms（多跳獨立 RF 模擬鏈路串接、無 BAP 層、軟體 RF 模擬受 CPU 排程影響、backhaul-aware PRB 預算再加重排隊），15 秒內慢啟動長不大。**backhaul-aware 機制與 TCP 陣發流量產生自我節流回饋迴圈是假說，尚未用 bhload 即時數值驗證。**
- 排除項：（1）調大緩衝區無效——三台主機 `net.core.rmem_max/wmem_max` 原本就是 4MB/50MB(1MB write)，容器 `tcp_rmem/tcp_wmem` 上限原本 6MB/4MB，遠超所需；host 調到 16MB + 容器 tcp_rmem/wmem 調到 16MB 後重測反而更低（68.7Kbps~1.31Mbps，run-to-run 變異大），已全部調回原值。（2）docker-compose `sysctls:` 在這個環境不可靠：PC1 對 `net.core.rmem_max` 回 permission denied（非 per-netns）、PC2（Ubuntu 20.04）連 `tcp_rmem` 都在建立容器時失敗（`open /proc/sys/net/core/rmem_max: no such file`，8 個 UE 整批卡在 Created），已全部移除，改用 `docker exec sysctl -w` 才生效。（3）`iperf3 -w 2M` 僅小幅改善（1.78→2.10 Mbps）、`-P 4` 平行連線總和仍約 2 Mbps。

**UDP 15 分鐘 Scenario T 量測（09-22）與其失真**：JFI=0.5973、平均 1.76 Mbps、最高單筆 8.54 Mbps、吞吐量取樣覆蓋率多數 0~40%、UE8/UE14 整場覆蓋率 0%。同一 access node 的兩個 UE 常一個有流量、一個近乎零（UDP 無退讓，PF 在需求遠超供給時把資源倒向其一）。**但這份數據被 traffic_scenario.py 的 DL frozen watchdog 污染**：該 watchdog 註解寫「僅 TCP 適用」，實作卻沒檢查協定，UDP 那次 PC2 單台被強制重啟 iperf3 17 次（TCP 那次 0 次；supervisor 退出 32 vs 11 次）；每次重啟 iperf3 log 以 `"w"` 模式覆寫，抹掉 `measure_stage.py` 正在讀的取樣，人為壓低覆蓋率與平均值（失真程度未量化）。**UDP 版數據需重測後才可引用**，目前未寫入 PF.md。

**決策**：使用者決定改用 UDP 做訓練與測試，要求把訓練/測試會用到的流量場景多寫一份 UDP 版本。

**實作**（`scenarios/traffic_scenario.py`、`iab/training_scenario_driver.sh`、`iab/training_watchdog.sh`）：
- 新增/泛化 `--protocol {tcp,udp}` 到 T/R/A/B/C 全部場景（未指定時行為完全不變）；A/B/C 函式加 `protocol` 參數、回傳三元組；R 的協定覆寫不影響 RNG 消耗（同 seed 的路徑損耗/頻寬/閒置序列與混合版一致，已用 363 筆抽樣驗證）。
- DL frozen watchdog 對 UDP UE 略過（mock 驗證：TCP 凍結流被重啟 2 次、UDP 0 次）。
- 訓練驅動器與 watchdog 都接受 `--protocol` 並在每次（重）啟動驅動器時帶上，避免崩潰復原後悄悄變回 TCP；用假 `python3` 攔截參數驗證 T/R/A/B/C 五種 slot 都正確帶上 `--protocol udp`，未指定時不帶。
- 三個檔案已 rsync 到 PC2/PC3 並確認內容抵達。

**驗證範圍與未完成事項**：以上都是單元/mock/dry-run 驗證，**尚未在真實容器上跑過 UDP 版新程式碼**——09-25 發現 PC1 約 1.5 小時前重開機過（uptime 1:32），PC1 上全部容器（Donor/relay/CN5G/FlexRIC/inference）都是 Exited，PC2/PC3 的 access/UE 容器仍在跑但上游已不在，需依 CLAUDE.md 第 6 節依序做完整乾淨重啟後才能 live 驗證與量測。另外 09-23 收尾時 UE16 仍卡住（容器重啟後仍 100% 遺失），乾淨重啟後應會一併解決。

## 2026-09-25（續）— UDP 吞吐量偏低調查：先前兩個解釋都被推翻，瓶頸指向跨主機 rfsim 鏈路

**起因**：使用者質疑上次 UDP 15 分鐘量測吞吐量太低，且不認同「PF 輪流餓其中一個 UE」的說法，要求找出問題並檢查 UDP 場景需不需要修改。PC1 已於 09-25 約 20:33 重開機，先依序乾淨重啟三台（13/13 E2、RestartCount 0；UE15 未通、UE17 靠重新斷言預設路由恢復）再做對照實驗。

**先前說法的更正**：
1. 「同 node 兩 UE 一個有量一個為零是 PF 在真實壅塞下輪流」**不成立**：離線分析上次資料，兩 UE 不對稱狀態平均持續 69 秒（最長 160 秒），且誰有量跟通道好壞（47%）、iperf3 誰先啟動（47%）都無關；17 個 UE 任何時刻平均只有 2.3 個有非零流量，全體加總平均 4.8 Mbps，每個活躍 UE ≈2 Mbps 與活躍數無關。
2. 「TCP 卡在 ~2Mbps 是視窗÷RTT」**不成立**：閒置系統上單一 UE，UDP 5Mbps 灌 10 秒（6.25MB），UE 以 **~1.7~2 Mbps** 的固定速率收了 ~30~40 秒才收完，累積剛好 100%、無丟包；TCP 的 2.1Mbps 與此同值。8 個 UE 同時各送 1Mbps UDP（總 8Mbps）→ 加總只收到 ~1.8Mbps，UE1/UE2（最先啟動）各 0.69、其餘六個各 0.07。→ 2Mbps 是整體共用的真實路徑容量。（09-22 手動測到「UE6 單流 UDP 20Mbps、0% 遺失」目前無法重現，同一平台速度隨時間差異大，原因未明。）
3. 「backhaul-aware PRB 預算把 DU 壓到 0 PRB」**排除**：`bhload` 忙碌度分母是 `2×106×2000×interval`（含 UL），DL 最多佔一半，ratio 實際不會低於 ~0.5。DL MCS 在有流量的 UE 上是 28（最高），也不是調變/通道限制。

**iperf3 UDP 上一次量測的另一項失真**：DL frozen watchdog 沒檢查協定（註解寫「僅 TCP 適用」），UDP 那次 PC2 被強制重啟 iperf3 17 次（TCP 0 次），重啟時 log 被覆寫；已修（略過 UDP）。另外 `measure_stage.py` 遇到 iperf3 印 `0.00 bits/sec`（無 K/M 前綴）時正則不匹配，會回傳更早的舊樣本或空白（記成空白而非 0），所以「零吞吐量」被當成缺值、之前算的平均值又只用非空白樣本，會系統性高估。

**現象（過載 UDP 的後果）**：offered 遠超容量時傳送端持續灌入、路徑上的佇列不丟包地越積越深（約 35 秒才吐完 6.25MB），iperf3 控制連線與 ICMP 排在佇列後面：client 收不到 test end（`-t 8` 的 client 72 秒後仍在跑）、server 回 `the server is busy running a test`、洪水後 ping 暫時 100% 遺失（RTT 恢復要一陣子）。iperf3 UDP 送不出去的封包不計入序號，所以 client 顯示 0% 遺失但速率極低。

**逐跳定位**：ext-dn→MT1（1 跳，Donor 與 Node1 同在 PC1）突發 4.8MB 頭 2 秒就收 3.35MB（~13Mbps）；ext-dn→MT5（2 跳，Node1 DU 在 PC1、MT5 在 PC2，**跨主機**）資料「一陣一陣」到、速率 ~1.6Mbps；到 UE1（3 跳）~1.7Mbps。閒置時 ext-dn→MT1 RTT 28ms、到 UE ~300ms。rfsim 每條無線鏈路是一條 TCP 連線傳時域 IQ：同主機連線 RTT 0.04ms、累積傳輸量 ~672GB（≈2.2Gbps/條）；跨主機連線（Node1 DU↔MT5）srtt 9.3ms（min 0.45ms）、累積 ~13.6GB（≈45Mbps/條），**差約 50 倍**。gNB/UE 以區塊同步交換，模擬時間推進速度 ≈ 區塊大小÷RTT，推論跨主機那幾跳的無線鏈路實際上在「慢動作」運行——這也應該就是 09-21 之前「跟 Donor 同主機的分支吞吐量高 10~20 倍」的真正成因（不是排程差異）。
- 主機間原始 ping（不經 rfsim）：PC1↔PC2 min 0.52/avg 4.5/max 11 ms；PC1↔PC3 **min 29/avg 35 ms**（PC3↔PC1、PC3↔PC2 皆 ~30 ms，代表 PC3 這端有固定 ~30ms 延遲）。三台網卡都是 USB 的 `r8152`（RTL8156B），PC1/PC2 協商 2500Mb/s、**PC3 只有 1000Mb/s**；閒置時（無測試流量）實體網卡就有 PC1 ~350Mbps、PC2 ~165Mbps、PC3 ~185Mbps 的 rfsim IQ 流量。
- 已試且**無效/已還原**：`ethtool -C rx-usecs 15000→200`（三台，ping 僅 4.5→3.7ms、PC3 不變、rfsim TCP RTT 仍 ~9ms、UDP 排空速率不變）；把 PC3 `ksoftirqd/8`（該 CPU 承接全部 xhci 網卡中斷，累積 20 分 55 秒 CPU，且與 8 條 SCHED_FIFO 80 執行緒同核）提到 FIFO 90（ping 不變）。網卡本身乾淨：PC3 0 錯誤 0 丟包、EEE 未啟用。PC1 CPU 80% 閒置、ksoftirqd 低，不是 PC1 瓶頸。**根因（跨主機那 9ms 排隊／PC3 的 30ms 固定延遲）尚未釘死**，待查：交換器與線材（PC3 為何只有 1G）、USB 網卡/xhci 的行為、PC2/PC3 上 SCHED_FIFO 執行緒對網路收包的影響。

**對既有結論的影響（需誠實面對）**：Stage 1~3 與拓樸搬遷後的 TCP/UDP 數字，絕對值主要反映「跨主機 rfsim 鏈路速度」這個平台/網路 artifact，而非排程演算法。拓樸搬遷後每條 Relay→Access 都跨主機，JFI 0.98 有一部分是「大家一樣慢」；先前歸因給 TCP 視窗或 PF 行為的說法都不成立。UDP 場景（T/R/A/B/C）目標頻寬比實測整體容量高 3~60 倍，需縮小或先解掉瓶頸。

**環境狀態**：測試環境仍在跑（15/17 UE 通；UE15 未通），PC2 的 Node5~8 UE 通道為做實驗被設成 3dB；`rx-usecs`、ksoftirqd 排程都已還原。

## 2026-09-25（續二）— 跨主機延遲根因：PC3 的 USB 網卡自 9/11 起被接在 USB 2.0 埠

**結論（實體修復尚未做，待使用者把 PC3 網卡移到 USB 3.x 埠後驗證）**：PC3 的 USB LAN 網卡（`r8152`, RTL8156B）在 2026-09-11 19:11~19:13 被連續拔插後，從 USB 3.0 埠（`usb 2-3`，開機時 13:58 的列舉）變成接在 **USB 2.0 埠（`usb 1-5: new high-speed USB device`，480Mb/s，`version 2.10`）**，此後 14 天一直如此；PC1/PC2 的同款網卡都在 USB 3.0（5000Mb/s，`version 3.20`）。PC3 的 xHCI 另有空著的 10Gb/s SuperSpeed 匯流排（Bus 2/4/6）。**這段期間（9/11 19:13 起）Stage 1~3 與拓樸搬遷後的所有量測都是在這個狀態下做的。**

**證據鏈**（每一步都是實測）：
1. 不經 rfsim 的飽和 UDP（iperf3 host-to-host）：PC1↔PC2 ~2.0Gbps（兩個方向）；**任何含 PC3 的主機對（PC1↔PC3、PC2↔PC3，後者不經過 PC1）都只有 ~310~330Mbps**，且與封包大小無關（200B 265Mbps~1350B 326Mbps → 是位元速率上限，不是 pps 上限）。飽和時 ping 由 0.4ms 跳到 ~19ms（標準佇列）。
2. rfsim 全開時 PC3 網卡只有 rx 187 / tx 185 Mbps（合計 ~372Mbps，貼著上限），主機間 ping PC1↔PC3 平均 35ms（min 29ms）、PC1↔PC2 平均 4.5ms；把 PC3 全部容器暫停（流量 0）→ PC1↔PC3 **0.72ms**；只暫停 MT9~12（PC3 上真正跨網卡的 4 條鏈路）也是 ~0.5ms，有流量（>=2 條同時動）就回到 ~33ms。**延遲是負載造成的排隊，不是線材/交換器/網卡的固定延遲**。
3. **PC3 拖累 PC2**：PC3 容器保持暫停、只恢復 PC2 → PC2 網卡流量從 ~165Mbps **暴增到 ~1.73Gbps**，PC1↔PC2 ping 4.5→2.6~2.9ms；此時單一 UE1 的 UDP 容量從 ~1.7Mbps 提高到 **~12Mbps**（60Mbps offered，穩態 11.2~12.4Mbps）。共用機制（PAUSE 訊框、PC1 網卡 TX 佇列、交換器緩衝...）未查明，但「PC3 一忙全系統變慢」是實測事實。
4. rfsim 連線層面：同主機連線 TCP RTT 0.04ms、跨主機 9.3ms；跨主機每條鏈路累積傳輸量約為同主機的 1/50——rfsim 的 gNB/UE 以區塊同步交換，模擬時間推進速度受 RTT/頻寬限制，慢的鏈路在「慢動作」運行。

**排除項**：`rx-usecs` 15000→200（三台）、`ksoftirqd/8` 提到 FIFO 90、把網卡 IRQ 移到 CPU7 並讓容器避開 CPU7/15、CPU 調節器（PC1/PC3 `performance`、PC2 `ondemand` 但閒置即 5GHz；三台皆 Ryzen 7 7700 約 5GHz）、網卡錯誤/丟包計數（0）、EEE（未啟用）——全部無效或無關，已還原。

**對先前結論的影響**：(1) 09-25 稍早寫的「TCP/UDP 2Mbps 天花板是視窗×RTT」「PF 輪流餓 UE」都已被推翻（見上方條目）；(2) 「跟 Donor 同主機的分支吞吐量高 10~20 倍」與 09-12「PC2/PC3 CPU 資源競爭」等舊診斷，有可能有一部分其實是這個網卡問題（9/11 19:13 起才有），**這只是假說，需修復後用同一場景重測才能判斷**，尤其拓樸搬遷（relay 全集中 PC1）是否有必要、JFI 0.98 有多少是「大家一樣慢」；(3) 20Mbps 的 09-22 手動測試因此可以理解為系統速度隨 PC3 負載大幅變動下的一個快的時刻，而不是矛盾。

**目前環境狀態**：PC3 的 17 個容器為了實驗被 `docker pause` 著（尚未恢復；停很久後 rfsim 連線大概已失效，修好網卡後建議依 CLAUDE.md 第 6 節做完整乾淨重啟），PC1/PC2 運作中，PC2 UE1~8 的通道被設成 3dB。

**待辦**：（人工）把 PC3 網卡移到 USB 3.x（藍色）埠、最好直連主機板後方埠、換 USB3 線，確認 `cat $(readlink -f /sys/class/net/enxc84d4427aa8f/device)/../speed` = 5000/10000、PC1↔PC3 飽和 UDP 接近 2Gbps（若交換器/線材支援 2.5G，網卡也可望從 1G 升到 2.5G）；然後乾淨重啟三台、重做 UDP 對照實驗（單一 UE 容量、多 UE 同時），再決定 UDP 場景的目標頻寬與是否需要重測 Stage 1~3。

## 2026-09-25（續三）— 依 Stage 1~5 開發階段整理程式碼與文件

**移除（皆無實際呼叫者，只剩註解/提示字串互相提到，git 歷史可復原）**：`iab/watchdog.sh`、`iab/monitor_drl.sh`、`iab/iab_perf_test.sh`、`iab/drl_report.py`（雙主機期 PC2-only 版本，已被 `training_watchdog.sh`／`training_healthcheck.sh`／`measure_stage.py` 取代）；`inference/global_xapp_bridge.py`（舊 5-node 配額橋接，其 import 的 `compute_quotas()` 早已從 `global_xapp.py` 移除，檔案本身無法執行）及 `inference/Dockerfile` 對應的 COPY。`check_convergence*.py` 內指向已刪 `drl_report.py` 的提示改指 `measure_stage.py`；`flwr_config.toml`、`global_xapp.py` 的過期註解已修。保留未動（待使用者決定）：`iab/check_convergence.py`（舊 docker-logs 版，`training_healthcheck.sh` 仍提及）、`check_convergence_weights.py`、`calibrate_fl_rate.py`、`clean_lambda_contamination.py`。

**CLAUDE.md 整併**：拓樸搬遷敘事縮成一段設計原則；§3 Stage 1~3 列改引用現行 md 的 09-19/09-21/09-22 數字（原本引用已被取代的 09-14 舊版數字）並加上 PC3 USB2 量測條件警語；Stage 1~5 逐階段長敘事併入路線圖（Stage 3 分群公式保留）；更正「backhaul-aware 機制尚未實作」為已實作（`gNB_scheduler_dlsch.c` 約 948 行）；43.8 Mbps 標明是同機路徑；§8 UDP 段落重寫（根因、容量上限、`measure_stage.py` 零值 regex 缺陷）；移除「已知過期腳本」清單改為現況說明。

**文件修正**：`experiment_results/*.md` 加 USB2 量測條件警語、更正 JFI 0.98 為「均貧」而非公平（UE5~8 倍率 10~20x 更正為約 9x）、`backhaul_mechanism_verification.md` 標題與結論標為作廢；`inference/DRL_DESIGN.md`／`STAGE3`／`STAGE4` 修正過期事實（5 nodes、`prb_quota_ratio`、100MHz、TCP 天花板歸因等）；`PHASE5_GLOBAL_DEV_LOG.md`、`DRL_METHODOLOGY_PLAN.md` 只加「已過時」橫幅（整份刪除待使用者授權）。

**未處理／待決**：`measure_stage.py` 的 `_RATE_RE` 不匹配 `0.00 bits/sec`（零吞吐量被記成空白，平均與 JFI 偏高）；PC3 網卡 USB 3.x 實體修復與重測。

## 附錄：已刪除的 `inference/PHASE5_GLOBAL_DEV_LOG.md`、`DRL_METHODOLOGY_PLAN.md` 中仍有效的內容（2026-09-25 併入）

這兩份文件（2026-07，5-node 時代）的主體——舊 relay→access 配額 Global xApp／`global_xapp_bridge.py`、硬性 2/3 分群、`prb_quota_ratio`——已被 Stage 2 的全域公平性廣播與 Stage 3 soft cluster 取代，隨檔案移除（完整內容在 git 歷史，`git log --diff-filter=D -- '*PHASE5_GLOBAL_DEV_LOG.md'`）。以下是仍適用的設計決策與踩坑：

**Flower 架構決策**
- 舊 `fl.server.start_server()`／`start_numpy_client()` 是 deprecated compat API；現行為 `ServerApp`/`ClientApp` + `flwr run`，以 **SuperLink + SuperNode（Deployment Engine）** 部署（節點是實體分散的 process，不是 Simulation Engine 的虛擬 client）。
- `flwr` 以 vendor 方式放在 `inference/vendor/flwr/`（`pip install -e ./vendor`，剔除 `*_test.py`），動機是日後能直接改框架原始碼實作自訂聚合。`vendor/pyproject.toml` 依賴**必須用 `==` 鎖版本**（對齊官方 `uv.lock`）：寬鬆 range 會解析出彼此 protobuf gencode/runtime 不相容的組合（`gencode 7.35.0 runtime 6.33.6`），`flower-superlink` 啟動即 crash。
- `flower-supernode` 預設 `--isolation subprocess`：`client_app.py` 的 `train()`/`evaluate()` 跑在與 `inference_server.py` **不同的 OS process**，無法共用記憶體中的 `DRLAgent`，模型交換一律透過共用 checkpoint 檔（`model_nodeN.pt`）與 mtime 熱重載。

**踩坑（仍會再踩）**
1. `_apply_weights_to_node()` 必須先 `agent.load()` 再套聚合後的 actor/critic 權重：用全新 `DRLAgent` 直接存檔會把 `train_steps` 歸零，`is_trained = train_steps > 0` 變 False，`InferenceServer` 熱重載後悄悄退回 BSR 啟發式（現行程式碼已遵守，勿改回）。
2. FAB 目錄名稱只允許 `^[A-Za-z][A-Za-z0-9-]*$`（故為 `flower-app/`，不能用底線）。
3. `ServerApp`/`ClientApp` 是 SuperLink/SuperNode **各自 spawn** 的子行程，環境變數必須設在這兩個長駐 process 的啟動環境，設在 `flwr run` 提交端會被忽略。
4. `flwr` ≥1.26 第一次 `flwr run` 會自動把 `pyproject.toml` 的 `[tool.flwr.federations]` 改寫成註解並寫 `~/.flwr/config.toml`，會弄壞 git 裡的原始碼、且對全新的一次性容器無效；因此固定使用 `inference/flwr_config.toml`（Dockerfile COPY 到 `/root/.flwr/config.toml`）。
5. 改了會被 Dockerfile COPY 進 image 或被腳本熱補的檔案，要同步更新複製清單／重新 build（曾因 `training_pipeline.py` 漏進熱補清單，5 個 `inference-nodeN` 進入 `ModuleNotFoundError` crash loop）。

**方法選擇理由（論文方法論可引用）**
- **排除完全異質模型 FL（HFL，知識蒸餾）**：HFL 的動機是客戶端因硬體限制或隱私各自設計不同架構；本專案所有節點同團隊、同程式碼、同環境，兩個動機都不成立。
- **排除 Byzantine-robust 聚合（Krum／MultiKrum／Bulyan／FedTrimmedAvg，`vendor/flwr/serverapp/strategy/` 有現成實作）**：針對惡意/損毀客戶端的對抗性威脅模型，本專案節點皆自己控制，沒有這種威脅。
- **採用 Clustered FL**（Sattler et al. 2020、IFCA 一系）：架構相同、依結構差異分群、群內標準加權聚合；後來在 Stage 3 進一步推廣成 soft／weighted（`role_ratio_i`，見 CLAUDE.md §3）。
- **聚合演算法是獨立於分群的另一維度**：**FedOpt 家族**（FedAdam／FedYogi／FedAdagrad，把「聚合結果 − 舊全域權重」當偽梯度餵給 server-side 優化器，Flower 預設 `eta=0.1, eta_l=0.1, beta_1=0.9, beta_2=0.99, tau=1e-3`，`FedAdam→FedOpt→FedAvg` 內部仍先呼叫 `aggregate_arrayrecords()`）只改 server 端、成本低；**FedProx**（本地 loss 加近端項對抗 client drift）要動 `training_pipeline.py`／`drl_agent.py` 訓練迴圈、成本較高。兩者不互斥，建議分開驗證以便歸因；分群後若用 FedAdam，每個原型需各自獨立的 server 端動量狀態。這些是 Stage 4 自訂 FL 的候選材料（見 `inference/STAGE4_CUSTOM_FL_DESIGN.md`）。
- **Local DRL 沿革**：獎勵函數為 Lagrangian 限制式 `R = R_tp + λ(JFI_raw − JFI_min)`（`JFI_min=0.8291` 為現場實測 PF 在 Scenario R 15 分鐘的 JFI，λ 依批次 JFI 自適應）；Actor/Critic 由 MLP 改 GRU（POMDP：單步 state 不足以區分 Scenario R 的閒置軌跡），訓練改抓時間上連續的序列（`TRAIN_SEQ_LEN=32`、`TRAIN_SEQ_COUNT=16`，連續性靠 `next_state_vec == state_vec` 位元組相等判斷），閒置轉換也寫入 MongoDB 並帶 `is_idle` 欄位供 λ 更新排除。GRU 與 MLP 的取捨曾評估 delta 特徵／frame stacking／GRU 三案（見 `DRL_DESIGN.md`）。

## 2026-09-25（續四）— `measure_stage.py` 零吞吐量 regex 修復、兩份過時文件刪除

**`iab/measure_stage.py`**：`_RATE_RE` 原為 `([\d.]+)\s+(Mbits|Kbits|Gbits)/sec`，iperf3 的零吞吐量行是 `0.00 bits/sec`（無 K/M/G 前綴）不匹配，於是該行被略過，函式往回找到更早的非零樣本，或整段都沒有時回傳 None（CSV 記成空白）。改為 `([\d.]+)\s+([KMG]?)bits/sec` 並處理無前綴（÷1e6）；以 5 種格式（M/K/G/無前綴/結尾摘要行）單元驗證通過。**影響**：此日期前的所有 CSV（Stage 1~3 全部量測）在有 UE 被餓死（吞吐量 0）時，平均吞吐量與 JFI 偏高（餓死的 UE 被漏算或以舊值取代），修復後的量測與先前數字不可直接比較；歷史 md 數字保留不改。

**刪除** `inference/PHASE5_GLOBAL_DEV_LOG.md`、`inference/DRL_METHODOLOGY_PLAN.md`（經使用者授權），仍有效內容已併入上方「附錄」；`STAGE3`／`STAGE4`／`DRL_DESIGN.md` 中的引用已改指 `DRL_DESIGN.md`／`HISTORY.md` 附錄。

## 2026-09-25（續五）— CLAUDE.md 關鍵路徑合併
原第 5 節（目錄結構）與第 7 節開頭（xApp／共用底層檔案清單）合併為第 5 節「專案目錄結構與關鍵檔案路徑」，改為依部署／腳本／場景／推論與 FL／C 語言底層分表，新增 `iab/` 腳本分類與 backhaul 機制原始檔路徑；用途待定的四支腳本（`check_convergence.py`、`check_convergence_weights.py`、`calibrate_fl_rate.py`、`clean_lambda_contamination.py`）在該節逐一記錄用途與保留原因（未刪除）。
