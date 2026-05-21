# 碩士論文實驗流程與環境配置配置指南 (O-RAN IAB 架構)

## 1. 實驗實體環境與網路拓撲設置

本研究採用**雙主機 (Dual-host) 實體部署**，以模擬真實 O-RAN IAB 網路中的實體隔離與傳輸延遲。底層 IAB 架構採用 **MT + DU 串接模式** (無 BAP 層)，所有跨節點傳輸皆透過標準 5G Uu 介面與 Linux IP Routing 進行轉發。

### 硬體與節點配置表

| 實體主機 | 部署元件 | 網路角色 | 備註說明 |
| :--- | :--- | :--- | :--- |
| **PC 1** | 5G Core (CN5G)<br>FlexRIC Server<br>Donor Node<br>IAB Node 1, 2 | 核心網與全域控制中心<br>Relay Nodes (骨幹轉發) | FlexRIC 綁定實體 IP，接受跨主機 E2 連線。Node 1, 2 負責上下游流量調度。 |
| **PC 2** | IAB Node 3, 4, 5<br>UE 1 ~ 6 | Access Nodes (邊緣存取)<br>終端使用者 | 透過實體網路與 PC 1 連線。Node 3, 4, 5 各自獨立運行專屬的 Local xApp 容器。 |

> **開發進度註記**：目前已確認 PC 1 的 FlexRIC Server 能夠成功與所有 6 個節點 (Donor + 5 IAB Nodes) 建立 SCTP/E2AP 連線，並且驗證了 OAI MAC 層確實開放 PRB 控制權給 xApp 進行覆寫，已開發 Node 1 到 Node 5 各自獨立的 Local xApp 程式碼。
> **Local rApp 已完成開發**：`inference_server.py` 同一 Python 進程中包含 Near-RT 推論（ZeroMQ REP）與 Non-RT Fine-tuning（背景訓練執行緒，每 60 秒從 MongoDB 讀取經驗執行 Offline A2C 更新）兩個功能，共享同一 DRLAgent 實例。

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
    * **實作**：Python ZeroMQ REP 伺服器（部署於 5 個獨立容器），與 Flower Client 運行於同一進程、共享 DRL 模型。
    * **職責（雙重角色）**：
      * **Near-RT 推論（毫秒級）**：接收 Local xApp 的 ZeroMQ 請求，執行 DRL Actor 網路 forward pass，回傳 PRB 權重陣列，並將 State/Action/Reward 非同步寫入 MongoDB。
      * **Non-RT Fine-tuning（秒/分鐘級）**：從 MongoDB 讀取歷史資料，執行本地模型微調，準備作為 Flower Client 參與聯邦學習聚合。
3.  **Global xApp (PC 1)**：
    * **實作**：獨立的 Python Docker 容器。
    * **職責**：毫秒級全域迴圈 (10ms)。具備「全域視野 (Global View)」，透過 ZeroMQ 接收全網 5 個 Node 的瞬時狀態，負責跨節點的巨觀調度。**核心機制（軟性回傳約束）**：RF Simulator 下每個 DU 各自有獨立 106 PRB，不符合 in-band IAB 頻譜共享；修改 OAI 底層代價太高，改以 Global xApp 模擬回傳瓶頸。具體邏輯：① 監控 Node1/Node2 xApp 對 MT3/MT4 各分配了多少 PRB（代表流量通過 Node1→Node3/4 或 Node2→Node4/5 的回傳容量）；② 將該 PRB 配額透過 ZeroMQ 下發給對應的 Node3/4/5 Local xApp；③ Node3/4/5 的 Local xApp 將可用 PRB 上限從 106 改為收到的配額值（`effective_prb = min(106, quota_from_global)`），以此在 xApp 層模擬 in-band IAB 的回傳瓶頸限制。
4.  **Global rApp / Flower Server (PC 1)**：
    * **實作**：Python 程式 (部署於 Donor 端)。
    * **職責**：小時級 Non-RT 迴圈。強制 5 個 IAB Nodes 參與聚合，收集全網吞吐量與延遲，以計算 Jain's Fairness Index 為優化目標，進行聯邦學習全域權重聚合 (Aggregation) 後下發，完成階層式 AI 的學習閉環。

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

### 第四階段：Local 單節點 AI 閉環控制 + Local rApp (當前進行中)
* **模型建構**：在 Python 端建立深度強化學習 (DRL) Actor 網路。✅ 已完成（Actor-Critic + Dirichlet Policy Gradient）
* **Reward 設計**：撰寫複合獎勵函數，結合 Throughput 最大化、Delay 懲罰與 Fairness 補償。✅ 已完成
* **Local rApp 開發**：與 Local xApp 推論伺服器運行於**同一 Python 進程、共享 DRL 模型**。從 MongoDB 讀取歷史 State/Action/Reward，執行本地模型 Fine-tuning，為第五階段 Flower Client 整合做準備。✅ 已完成（`inference_server.py` 背景訓練執行緒）
* **穩定度測試**：讓 AI 取代 Hardcode 邏輯，觀察系統在高併發流量下的穩定度與收斂情況。⬜ 進行中
* **DRL vs PF 吞吐量驗證**：使用 Scenario A/B/C（訓練用 D，測試用 A/B/C 驗證泛化能力）作為測試條件。流程：① PC2 先跑 `traffic_scenario.py --scenario A`（設好 CQI）② `drl_report.py --iters 3` 接管 iperf3 量測（CQI 設定保留在 DU）③ 停 xApp 後以相同 scenario CQI 重量 PF baseline，存入 `drl_report.py` 的 `PF_PER_UE`。**TCP-DL、UDP-DL 吞吐量與 Jain's Fairness Index 三項在 A/B/C 三個場景下須全部優於對應 PF Baseline 才算完成本階段**（延遲允許小幅劣化）。⬜ 待驗證

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
* **Flower Server 啟動**：在 PC 1 啟動 Flower Server，實作自定義的聚合策略以計算全網 Jain's Fairness Index 為優化目標。
* **Local rApp 擴充為 Flower Client**：在第四階段 Local rApp 基礎上，實作 `train()` 方法將本地 Fine-tuning 結果上傳，並驗證 TCP 控制封包能穩定穿透實體 Backhaul 鏈路完成權重同步。

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
* `/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator/docker-compose-iab-client.yaml`：for PC 1
* `/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator/docker-compose-iab-server.yaml`：for PC 2
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

獎勵權重（`reward_calculator.py`）：W_THROUGHPUT=**0.5**、W_FAIRNESS=**0.4**、W_DELAY=**0.1**（提高公平性權重至 0.4 以防止 2-UE policy monopoly collapse；吞吐量仍為主要目標）。

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
