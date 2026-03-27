# 碩士論文實驗流程與環境配置配置指南 (O-RAN IAB 架構)

## 1. 實驗實體環境與網路拓撲設置

本研究採用**雙主機 (Dual-host) 實體部署**，以模擬真實 O-RAN IAB 網路中的實體隔離與傳輸延遲。底層 IAB 架構採用 **MT + DU 串接模式** (無 BAP 層)，所有跨節點傳輸皆透過標準 5G Uu 介面與 Linux IP Routing 進行轉發。

### 硬體與節點配置表

| 實體主機 | 部署元件 | 網路角色 | 備註說明 |
| :--- | :--- | :--- | :--- |
| **PC 1** | 5G Core (CN5G)<br>FlexRIC Server<br>Donor Node<br>IAB Node 1, 2 | 核心網與全域控制中心<br>Relay Nodes (骨幹轉發) | FlexRIC 綁定實體 IP，接受跨主機 E2 連線。Node 1, 2 負責上下游流量調度。 |
| **PC 2** | IAB Node 3, 4, 5<br>UE 1 ~ 6 | Access Nodes (邊緣存取)<br>終端使用者 | 透過實體網路與 PC 1 連線。Node 3, 4, 5 各自獨立運行專屬的 Local xApp 容器。 |

> **開發進度註記**：目前已確認 PC 1 的 FlexRIC Server 能夠成功與所有 6 個節點 (Donor + 5 IAB Nodes) 建立 SCTP/E2AP 連線，並且驗證了 OAI MAC 層確實開放 PRB 控制權給 xApp 進行覆寫。
> **目前尚未開發 Node 1 到 Node 5 各自獨立的 Local xApp 程式碼。**

---

## 2. 軟體架構與 O-RAN 職責映射

本系統跨越 C 語言的底層通訊與 Python 的 AI 推論，並透過共享記憶體機制整合 Near-RT 與 Non-RT 迴圈。

* **底層協議棧 (C 語言)**：使用 OAI (OpenAirInterface) 實作 DU/CU 與 UE。
* **Near-RT RIC (C 語言)**：使用 FlexRIC 作為 E2 代理伺服器與 xApp 框架。
* **Non-RT RIC & AI (Python)**：使用 Flower Framework 進行階層式聯邦學習 (Hierarchical FL)，並透過 ZeroMQ 建立跨語言 IPC 通訊。

### 核心控制元件定義

1.  **Local xApp (PC 1 & PC 2)**：
    * **實作**：C 語言 FlexRIC 程式 + Python ZeroMQ 推論伺服器。
    * **職責**：毫秒級迴圈 (10ms)。透過 E2SM-MAC 擷取 BSR 與 CQI，交由 Python 神經網路進行瞬間推論，並將輸出的 PRB 權重分配陣列寫回 OAI MAC 層。
2.  **Local rApp / Flower Client (PC 1 & PC 2)**：
    * **實作**：Python 程式 (與推論伺服器運行於同一進程，共享模型)。
    * **職責**：秒/分鐘級迴圈。從 MongoDB 讀取歷史狀態與獎勵，執行本地模型微調 (Fine-tuning)，並與 Global rApp 同步權重。
3.  **Global rApp / Flower Server (PC 1)**：
    * **實作**：Python 程式 (部署於 Donor 端)。
    * **職責**：強制 5 個 IAB Nodes 參與聚合，收集全網吞吐量與延遲，計算 Jain's Fairness Index，並下發全域路由/頻寬配額策略。

---

## 3. 六階段標準開發流程

為確保系統穩定度，開發採「由下而上 (Bottom-Up)」策略，逐步將控制權由 OAI 預設排程器移交給 AI。

### 第一階段：底層資料平面暢通與基準建立 (已完成)
* 建立 MT+DU 串接架構，確認 Linux IP Routing 規則轉發正常。
* 使用 `iperf3` 測量 Proportional Fairness (PF) 排程器下的基準效能 (Baseline)。
* **觀測結果**：確立了下行吞吐量不均 (Jain's Fairness 低落) 與長尾延遲 (Bufferbloat) 的優化靶心。

### 第二階段：Local xApp C 語言控制權驗證 [ 當前開發重點 ]
* 實作單節點的 C 語言 FlexRIC xApp。
* 成功擷取 `mac_ind_data_t` 中的 `ue_stats_len`、`bsr` 與 `wb_cqi`。
* 成功透過 Hardcode 寫死 PRB 分配陣列，並下發 `MAC_CTRL_REQ` 驗證 OAI 確實套用覆寫。

### 第三階段：獨立 xApp 開發與跨語言 IPC (當前進行中)
* **實體化 xApp**：為 Node 1 到 Node 5 撰寫各自獨立的 C 語言 FlexRIC xApp 程式碼與部署在 Docker 裡,每開發完一個xApp,請先編譯FlexRIC xApp和OAI RAN with E2 Agent,編譯過了我再審核。
* **ZeroMQ 整合**：在 C 語言 xApp 中引入 `libzmq` 與 `libcjson`，建立 REQ 模式並設定 5ms Timeout 防呆機制。
* **Python 對接**：建立 Python 端的 ZeroMQ REP 伺服器，解析 JSON 狀態並回傳推論結果。
* **資料持久化**：將每次的 State (狀態)、Action (決策) 寫入 MongoDB，為聯邦學習準備歷史資料集。

### 第四階段：Local 單節點 AI 閉環控制
* **模型建構**：在 Python 端建立深度強化學習 (DRL) Actor 網路。
* **Reward 設計**：撰寫複合獎勵函數，結合 Throughput 最大化、Delay 懲罰與 Fairness 補償。
* **穩定度測試**：讓 AI 取代 Hardcode 邏輯，觀察系統在高併發流量下的穩定度。

### 第五階段：Global rApp 與聯邦學習整合
* 在 PC 1 啟動 Flower Server，實作自定義的 `aggregate_kpm_metrics` 以計算全網 Jain's Fairness。
* 將 Local Python 程式擴充為 Flower Client，實作 `train()` 方法讀取 MongoDB 資料進行訓練。
* 驗證 TCP 控制封包能穩定穿透實體 Backhaul 鏈路完成權重同步。

### 第六階段：實驗數據驗證與論文撰寫
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

目前先從第三階段開始開發，依據我之前的經驗開發一個xApp至少要修改以下這些檔案
`~/openairinterface5g/openair2/E2AP/flexric/examples/xApp/c/ctrl/mac_ctrl.c`

`~/openairinterface5g/openair2/E2AP/flexric/src/sm/mac_sm/../mac_data_ie.c`

`~/openairinterface5g/openair2/E2AP/flexric/src/sm/mac_sm/../mac_data_ie.h`

`~/openairinterface5g/openair2/E2AP/flexric/src/sm/mac_sm/../mac_enc_plain.c`

`~/openairinterface5g/openair2/E2AP/flexric/src/sm/mac_sm/../mac_dec_plain.c`

`~/openairinterface5g/openair2/E2AP/flexric/src/sm/mac_sm/../mac_sm_agent.c`

`~/openairinterface5g/openair2/E2AP/flexric/src/sm/mac_sm/mac_sm_ric.c`

`~/openairinterface5g/openair2/E2AP/flexric/src/xApp/sm_ran_function_def.c`

`~/openairinterface5g/openair2/E2AP/flexric/test/sm/mac_sm/main.c`

`~/openairinterface5g/openair2/E2AP/RAN_FUNCTION/../ran_func_mac.c`

`~/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/nr_mac_gNB.h`

`~/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_dlsch.c`

`~/openairinterface5g/openair2/E2AP/flexric/src/xApp/db/sqlite3/sqlite3_wrapper.c`
我希望除了`~/openairinterface5g/openair2/E2AP/flexric/examples/xApp/c/ctrl/mac_ctrl.c`這個是 xApp 本身,能有五份程式碼給不同的 node ,其他都檔案能夠讓node 1~node 5的 xApp 共用,刪除整份檔案需要經過我的授權,所有修改都先在這個電腦(PC 1)完成就好,我會再將修改好的檔案傳給(PC 2)
