# 碩士論文實驗流程與環境配置配置指南 (O-RAN IAB 架構)

> **文件慣例**：本檔案只保留「現在式」的架構事實與可執行指令。任何帶日期的踩坑過程、除錯敘事、歷史量測數據表，一律寫進 `HISTORY.md`（同層目錄），不要寫回這裡。

## 1. 實驗實體環境與網路拓撲設置

採用**三主機 (Tri-host) 實體部署**，模擬真實 O-RAN IAB 網路中的實體隔離與傳輸延遲，拓樸為 **1 donor + 4 relay + 8 access + 17 UE** 的對稱四層樹狀結構。底層 IAB 架構採用 **MT + DU 串接模式**（無 BAP 層），所有跨節點傳輸皆透過標準 5G Uu 介面與 Linux IP Routing 進行轉發。三台主機透過各自的 USB3.0→RJ45 轉接卡共接同一台 switch，組成單一 `192.168.88.0/24` L2 網段（見 `iab/setup_lab_net.sh`）。

### 拓樸與節點編號

```
Donor (PC1)
├── Node1 (relay, PC2) ── Node5 (access, PC2) ── UE1, UE2
│                     └── Node6 (access, PC2) ── UE3, UE4
├── Node2 (relay, PC1) ── Node7 (access, PC1) ── UE5, UE6
│                     └── Node8 (access, PC1) ── UE7, UE8
├── Node3 (relay, PC2) ── Node9  (access, PC3) ── UE9,  UE10
│                     └── Node10 (access, PC3) ── UE11, UE12
└── Node4 (relay, PC2) ── Node11 (access, PC3) ── UE13, UE14
                      ├── Node12 (access, PC3) ── UE15, UE16
                      └── UE17（直接掛在 Node4 的 DU，不經過 access 層；Node4 在 PC2）
```

Donor→Relay→Access→UE 為 3-hop；UE17→Node4 為 2-hop。**Node9~12（access）的 parent relay Node3,4 現在跑在 PC2，是跨主機連線**（跟 relay 跨主機連 Donor 的機制完全相同，都是透過三台共用的 macvlan L2 網段，不需要額外設定）。

> **2026-09-12 節點重分配（分兩輪）**：
> 1. Node2 + 其兩個 access 子節點 Node7,8（含 UE5~8）從 PC2 搬到 PC1。
> 2. Node3,4（relay，含直連 UE17）從 PC3 搬到 PC2；Node9~12（access）留在 PC3 不動，變成跨主機連到 PC2 的 relay DU。
>
> 原因：PC2/PC3 各自同時跑太多組 relay+access 的 MT+DU（每組 2 個即時 RF 模擬 process）在 16 核心主機上過於擁擠，會造成 CPU 排程延遲使 UE 端誤判為 PHY 失步（RRC Reestablishment cause=otherFailure），連鎖觸發 CU 端 `no AMF for CU UE ID`、tunnel IP 不斷變動、DU 反覆重啟（診斷過程見 `HISTORY.md`）。第二輪特意不把 Node3,4 整組（含 Node9~12）都塞給 PC1，是為了保留 PC1 給未來 FL/Global xApp 階段的餘裕；也沒有全部塞給 PC2 避免重現 PC2 原本的問題，所以採用「relay 留在原本較輕的一側、access 留在原地變成跨主機」的折衷分法。目前三主機即時 process 數量約為 **PC1:14、PC2:18、PC3:11**。

### 硬體與節點配置表

| 實體主機 | 部署元件 | 網路角色 | 備註說明 |
| :--- | :--- | :--- | :--- |
| **PC 1** (192.168.88.1, lindor) | CN5G、FlexRIC Server、MongoDB、Donor CU/DU、**全部 12 組** `xapp-nodeN`+`inference-nodeN` 容器、**Node2 (relay) + Node7,8 (access) + UE5~8** | 核心網與全域控制中心 + 分擔一組 RAN 子樹 | xApp(C)+inference(Python) 刻意集中在 PC1（見下方理由），MongoDB 對外監聽 `27017`。Node2/7/8 的 CU 端指令（DNAT、路由）都是本機直接執行，不需要 SSH（見 `iab/start_iab_server.sh` 的 `configure_and_start_local_relay`/`configure_and_start_local_access_du`）。 |
| **PC 2** (192.168.88.2, **mcalab**) | Node1,3,4 (relay，Node4 含直連 UE17) + Node5,6 (access) + UE1~4,17 | RAN 資料面 | 純資料面，無 xApp/inference 容器。Ubuntu 20.04。Node3,4 的 DU/MT 是 2026-09-12 從 PC3 搬過來的。 |
| **PC 3** (192.168.88.3, lindor) | Node9,10,11,12 (access) + UE9~16 | RAN 資料面 | 純資料面，無 xApp/inference 容器。Ubuntu 24.04。這 4 個 access 節點的 parent relay（Node3,4）現在跑在 PC2，跨主機連線。 |

**xApp/inference 集中在 PC1 的理由**：xApp(C) 與 inference(Python) 之間走 `ipc://` Unix domain socket（見第 2 節），兩者必須同一台主機；若要「真正分散到 PC2/PC3」，ZMQ 要改走 TCP，5ms timeout 預算要多扛一段跨主機網路延遲，風險換來的好處不大——經評估後決定維持集中，`MONGO_URI` 因此不需要因為主機數增加而修改（所有 inference 容器仍是 `mongodb://localhost:27017`，因為它們仍跟 MongoDB 同一台主機）。RAN 節點（MT/DU/UE）則物理分散到 PC1（一部分）/PC2/PC3，且 relay 與其 access 子節點不一定同主機（見上方拓樸圖 Node3,4/Node9~12 的跨主機關係）。

### IP / ID 配置表

| 項目 | Donor | Node1~4（relay） | Node5~12（access） | UE1~17 |
|---|---|---|---|---|
| `gNB_ID`/`gNB_DU_ID` | `0xe00` | `0xe01`~`0xe04` | `0xe05`~`0xe0c` | — |
| `nr_cellid` | `12345678` | `12345679`~`12345682` | `12345683`~`12345690` | — |
| `physCellId` | `0` | `1`~`4` | `5`~`12` | — |
| E2 `TARGET_NODE_ID`（xApp .c，= gNB_ID 十進位） | — | `3585`~`3588` | `3589`~`3596` | — |
| `rfsimulator.serverport` | `4043` | `4044`~`4047` | `4048`~`4055` | — |
| FlexRIC telnet debug port（chanmod 通道控制） | — | `9089`~`9092` | `9093`~`9100` | — |
| IMSI (`208990100001xxx`) | — | 尾碼 `100`~`103` | 尾碼 `104`~`111` | 尾碼 `200`~`216`（跟 MT 區段刻意拉開） |
| macvlan IP | `.144`（DU）| Node1=`.150`（PC2）,Node2=`.151`（PC1）,Node3=`.152`,Node4=`.153`（PC2）—— MT/DU 共用同一 netns、同一 IP，這個位址不因實際跑在哪台主機而改變（三台共用同一個 macvlan L2 網段） | Node5=`.160/.161`,Node6=`.162/.163`,Node7=`.164/.165`,Node8=`.166/.167`（PC1/PC2 混合，依上表); Node9=`.168/.169`,Node10=`.170/.171`,Node11=`.172/.173`,Node12=`.174/.175`（PC3） | 動態，共用 `12.1.1.0/24` SMF pool；UE17 額外占用 macvlan `.176`（直連 relay，需要自己的 macvlan IP） |
| internal bridge IP | — | 不需要（relay 的 MT/DU 共用 netns，沒有獨立位址） | **PC1 用 `192.168.76.0/24`**：Node7=`.12/.22`,Node8=`.13/.23`；**PC2 用 `192.168.74.0/24`**：Node5=`.10/.20`,Node6=`.11/.21`；**PC3 用 `192.168.75.0/24`**（Node3,4 搬走後不變，因為 internal bridge 是 access 節點自己的，不受 parent relay 位置影響）：Node9=`.10/.20`,Node10=`.11/.21`,Node11=`.12/.22`,Node12=`.13/.23` | — |

> **各主機 internal bridge 子網刻意不同**（`.74.0/24`／`.75.0/24`／`.76.0/24`）：PC1 的路由表對同一個子網只能指到一個 next-hop，若多台主機共用同一子網，PC1 就無法同時正確路由到不同主機的 access node internal IP。internal bridge 網路是 access 節點自己的 host-local 網路，只跟「access 節點實際跑在哪」有關，跟它的 parent relay 跑在哪台主機無關（Node9~12 的 internal bridge 一直都在 PC3，即使 parent Node3,4 搬到 PC2 也不用改）。

CN5G（`.131`~`.134`）、FlexRIC（`.141`）全部在 PC1。

新增 IMSI 若落在既有 `100`~`216` 範圍以外，記得同步在 `oai_db.sql`／執行中的 `rfsim5g-mysql` 補 `INSERT INTO users`，否則 UE/MT 會收到 `FGS_REGISTRATION_REJECT`（過程見 `HISTORY.md`）。

---

## 2. 軟體架構與 O-RAN 職責映射

本系統跨越 C 語言的底層通訊與 Python 的 AI 推論，構築實體隔離的「Local / Global 雙層階層式控制平面」。

* **底層協議棧 (C 語言)**：使用 OAI (OpenAirInterface) 實作 DU/CU 與 UE。
* **Near-RT RIC (C 語言)**：使用 FlexRIC 作為 E2 代理伺服器與 xApp 框架。
* **Non-RT RIC & AI (Python)**：規劃透過 Flower Framework 進行階層式聯邦學習 (Hierarchical FL)，並透過 ZeroMQ 建立跨語言 IPC 通訊。

> **目前實作現況**：只做到 Local xApp + Local rApp 這一層（純 Local-only DRL）。**Global xApp 配額機制與 Flower 聯邦學習尚未針對 12-node 拓樸重建**——`global_xapp.py`／`global_xapp_bridge.py`／`inference/flower-app/` 仍是舊 5-node 拓樸的版本，`docker-compose-iab-server.yaml` 目前不含 `global-xapp-bridge`／`flower-*` 服務定義。舊 5-node 版本的完整設計細節（PUB/SUB 配額機制、Flower SuperLink/SuperNode 部署、client/server app 邏輯）保留在 `HISTORY.md` 供未來重新設計 cluster FL 時參考架構（見第 3 節五階段實驗路線圖）。

### 核心控制元件定義

1. **Local xApp**：
   * **實作**：純 C 語言 FlexRIC 程式，每個 Node 各自獨立（`xapp_node1.c` ~ `xapp_node12.c`）。
   * **職責**：局部控制迴圈（實際 100ms，C xApp 設有 Rate Limiter：每 10 個 10ms MAC callback 才觸發一次 ZMQ，避免 FlexRIC pending event queue 滿載崩潰）。只負責 PRB 分配這一個動作：透過 E2SM-MAC 擷取所屬 Node 的 **Δ DL TBS、MCS、DL Buffer Occupancy** 作為狀態（`dl_aggr_tbs` 差分值為吞吐量代理、`dl_mcs1` 為通道品質代理、`dl_buffer_info` 為不受排程與否影響的需求代理——注意 OAI RF Simulator 的真 3GPP `wb_cqi` 恆為 0，是模擬器本身不計算真實通道傳播的限制，`dl_buffer_info` 則沒有這個限制），將 JSON 狀態透過 ZeroMQ REQ 送給 Local rApp Python 端，收回 PRB 權重陣列後立即寫回 OAI MAC 層。C 語言端不含任何 AI 邏輯。
2. **Local rApp**：
   * **實作**：Python ZeroMQ REP 伺服器（`inference_server.py`，部署於 12 個獨立容器，全部在 PC1）。
   * **職責（雙重角色）**：
     * **Near-RT 推論（毫秒級）**：接收 Local xApp 的 ZeroMQ 請求，執行 DRL Actor 網路 forward pass，回傳 PRB 權重陣列，並將 State/Action/Reward 非同步寫入 MongoDB。
     * **Non-RT Fine-tuning（秒/分鐘級）**：從 MongoDB 讀取歷史資料，執行本地模型微調（`inference_server.py` 背景訓練執行緒 `_train_worker`，每 60 秒一輪）。
3. **Global xApp / Global rApp（Flower Server）**：待針對 12-node 拓樸重建，見第 3 節。

---

## 3. 標準開發流程

開發採「由下而上 (Bottom-Up)」策略，逐步將控制權由 OAI 預設排程器移交給 AI。

### 已完成階段
* **底層資料平面**：MT+DU 串接架構、Linux IP Routing 轉發正常。
* **Local xApp C 語言控制權**：成功擷取 `mac_ind_data_t` 中的 UE 狀態，並下發 `MAC_CTRL_REQ` 驗證 OAI 確實套用 PRB 覆寫。
* **獨立 xApp 開發與跨語言 IPC**：每個 Node 各自獨立的 C 語言 FlexRIC xApp + ZeroMQ（`libzmq`/`libcjson`，5ms timeout）+ Python ZeroMQ REP 推論伺服器 + MongoDB 資料持久化。
* **Local 單節點 AI 閉環控制**：DRL Actor-Critic（GRU + Dirichlet Policy Gradient）+ Local rApp 微調迴圈已完成並在 12-node 拓樸上驗證過 ZMQ round trip 正常運作（13/13 E2 連線、12/12 xApp）。
* **三主機基礎設施擴容**：1 donor + 4 relay + 8 access + 17 UE 全部端點啟動並驗證（見 HISTORY.md 2026-09-11 條目）。

> 開發 xApp 規則：每開發完一個 xApp，先編譯 FlexRIC xApp 和 OAI RAN with E2 Agent，編譯過了才進下一個/才能審核（見第 6 節建置指令）。

### 五階段實驗路線圖（規劃中，尚未實作）

目標：在同一組流量+路徑損耗場景下，依序驗證 5 個遞增複雜度的控制策略，**每一階的實驗數據（TCP-DL、Latency、UDP-DL/UL、Jain's Fairness Index 等）都必須贏過前一階**，最終逼近吞吐量理論上限。

| Stage | 策略 | Local DRL reward | FL 聚合方式 |
|---|---|---|---|
| 1 | PF baseline | 無（無 AI） | 無 |
| 2 | avg FL | 陽春版（`REWARD_MODE=throughput_only`：只有 throughput，無 Lagrangian／無限制式） | 標準 FedAvg，全部 12 節點一起聚合 |
| 3 | cluster FL | 陽春版（同 Stage 2） | 依角色分兩群聚合：relay cluster（Node1~4）、access cluster（Node5~12）各自獨立 FedAvg |
| 4 | 自訂 FL | 陽春版（同 Stage 2） | 使用者自行設計的聚合演算法（介面待設計） |
| 5 | 自訂 FL + 改進版 DRL | 改進版（`REWARD_MODE=lagrangian`：重新啟用 `drl_agent.py` 已寫好但目前未使用的 Lagrangian JFI 限制機制） | 同 Stage 4 |

**單調遞增要求**：PF < avg FL < cluster FL < 自訂 FL < 自訂FL+改進版DRL。

**關鍵設計決定（避免混淆）**：
* Stage 2~4 底層用的是**同一個**陽春 DRL（只差 FL 聚合方式），不是三種不同的模型。
* 「改進版 DRL」= 重新啟用現有**已經寫好但目前沒被呼叫**的 Lagrangian 機制。目前 `inference_server.py` 唯一呼叫的是 `reward_calculator.py::compute_lagrangian_reward()`（`R = R_tp + λ·(JFI_raw − JFI_MIN)`，λ 由 `drl_agent.py::train_on_batch()` 自適應更新）——**這已經是有 Lagrangian 項的版本**，不是本檔案曾經誤植的純加權和版本。要做到 Stage 2~4 的「陽春」reward，需要新增一個 `REWARD_MODE` 開關（`throughput_only` 時改呼叫 `compute_reward`/`compute_reward_breakdown`，`W_THROUGHPUT=1.0, W_FAIRNESS=0, W_DELAY=0`，並讓 λ 固定在 0），這是待實作項目。
* **不寫死、隨時可單獨跑任一 stage**：每個 stage 要能透過環境變數/CLI flag 獨立選擇（例如 `FL_MODE=none|avg|cluster|custom` + `REWARD_MODE=throughput_only|lagrangian`），不是「一定要照順序、前面沒做完後面就不能跑」的線性相依關係——不論開發進度到哪，都要能重跑任何一個 stage 的數據。
* Stage 3 的 cluster 分群邏輯可參考 `inference/DRL_METHODOLOGY_PLAN.md`（舊 5-node 版本的 relay/access 分群設計，需要重新推導成 4/8 分群）。
* Global xApp／Flower FL 的完整舊版部署細節（SuperLink/SuperNode 拓樸、client/server app 邏輯）在 `HISTORY.md`，復原時可直接參考架構，但節點數/角色分群需要重新推導成新的 12-node 拓樸。

### 待開發：實驗數據驗證與論文撰寫
* 設定動態干擾與高負載測試情境（見下方流量場景設計）。
* 對比 FL 雙層 AI 架構與 OAI 預設 PF 架構的效能差異（五階段路線圖）。
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
* `/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator`：docker 部署目錄
* `/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator/docker-compose-iab-server.yaml`：for PC 1（CN5G、Donor CU/DU、FlexRIC、MongoDB、全部 12 組 xapp-nodeN + inference-nodeN 容器、**Node2 relay + Node7,8 access + UE5~8**，由 `iab/run_local_pc1.sh` → `iab/start_iab_server.sh` 啟動）
* `/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator/docker-compose-iab-pc2.yaml`：for PC 2（Node1,3,4 relay（Node4 含直連 UE17）+ Node5,6 access + UE1~4,17，由 `iab/run_local_pc2.sh` → `iab/start_iab_pc2.sh` 啟動）
* `/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator/docker-compose-iab-pc3.yaml`：for PC 3（Node9,10,11,12 access + UE9~16，parent relay 跨主機在 PC2，由 `iab/run_local_pc3.sh` → `iab/start_iab_pc3.sh` 啟動）
* `/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator/scenarios/traffic_scenario.py`：流量+路徑損耗場景控制器（見第 8 節）
* `/openairinterface5g/HISTORY.md`：歷史踩坑/量測紀錄（見文件開頭慣例說明）

---

## 6. 常用建置與執行指令

### 編譯 FlexRIC xApp
```bash
cd ~/openairinterface5g/openair2/E2AP/flexric/build
cmake -G Ninja -DCMAKE_BUILD_TYPE=Release -DKPM_VERSION=KPM_V3_00 -DE2AP_VERSION=E2AP_V2 ..
ninja
sudo ninja install
```

### 編譯 OAI RAN with E2 Agent
```bash
cd ~/openairinterface5g/cmake_targets
sudo ./build_oai --gNB --nrUE --build-e2 --ninja -w USRP -C --cmake-opt -DE2AP_VERSION=E2AP_V2 --cmake-opt -DKPM_VERSION=KPM_V3_00 --cmake-opt -DCMAKE_BUILD_TYPE=Release
```

> **PC2（Ubuntu 20.04）額外需求**（三台主機唯一不是 24.04 的，除錯過程見 HISTORY.md）：`libuhd-dev`（focal 原生倉庫有）、`libyaml-cpp-dev` 需從源碼建置 0.8.0（原生只有 0.6.2，OAI CMake 需要新版 ALIAS target 支援）、CMake 需用 Kitware 倉庫裝到 3.28.3（原生 3.16.3 太舊）、GCC 需用 `ppa:ubuntu-toolchain-r/test` 裝 gcc-13/g++-13 並設為預設（原生 9.4.0 編譯 AVX512 SIMD 會報錯）。**執行期額外需求**：`nr-uesoftmodem`/`nr-softmodem` 連結 host 的 `libssl.so.1.1`，但容器基底只有 `libssl3`，需把 `/usr/lib/x86_64-linux-gnu/{libssl,libcrypto}.so.1.1` 複製進 `cmake_targets/ran_build/build/`（此檔案不在版控裡，`ran_build` 目錄重建後要重做）。

### 一鍵啟動三主機系統
```bash
# PC1（先啟動，CN5G/FlexRIC/MongoDB/Donor CU-DU/全部 xApp+inference）
bash ~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator/iab/run_local_pc1.sh

# PC2、PC3（等 PC1 完成後，兩台可同時執行）
bash ~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator/iab/run_local_pc2.sh
bash ~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator/iab/run_local_pc3.sh
```

驗證：`docker logs flexric 2>&1 | grep -c "E2 SETUP-REQUEST"` 應為 `13`（1 donor + 12 node）。

---

## 7. 注意事項

開發/修改 xApp 至少要涉及以下這些檔案（**正確檔名是 `xapp_nodeN.c`**）：

**xApp 本體（每個 Node 各自獨立，共 12 份）**
`~/openairinterface5g/openair2/E2AP/flexric/examples/xApp/c/ctrl/xapp_node1.c` ~ `xapp_node12.c`

**共用底層檔案（Node 1~12 共用，修改須謹慎）**
```
~/openairinterface5g/openair2/E2AP/flexric/src/sm/mac_sm/ie/mac_data_ie.c
~/openairinterface5g/openair2/E2AP/flexric/src/sm/mac_sm/ie/mac_data_ie.h
~/openairinterface5g/openair2/E2AP/flexric/src/sm/mac_sm/enc/mac_enc_plain.c
~/openairinterface5g/openair2/E2AP/flexric/src/sm/mac_sm/dec/mac_dec_plain.c
~/openairinterface5g/openair2/E2AP/flexric/src/sm/mac_sm/mac_sm_agent.c
~/openairinterface5g/openair2/E2AP/flexric/src/sm/mac_sm/mac_sm_ric.c
~/openairinterface5g/openair2/E2AP/flexric/src/xApp/sm_ran_function_def.c
~/openairinterface5g/openair2/E2AP/flexric/src/sm/mac_sm/test/main.c
~/openairinterface5g/openair2/E2AP/RAN_FUNCTION/CUSTOMIZED/ran_func_mac.c
~/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/nr_mac_gNB.h
~/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_dlsch.c
~/openairinterface5g/openair2/E2AP/flexric/src/xApp/db/sqlite3/sqlite3_wrapper.c
```

> **規則**：刪除整份檔案需經過授權。所有修改先在 PC 1 完成，再將修改好的檔案傳給 PC 2、PC 3（`rsync`，見 `setup_lab_net.sh` 建立的 SSH 免密碼登入：`ssh pc2`／`ssh pc3`）。PC2/PC3 各自在本機重新編譯（`build_oai`／FlexRIC cmake），不是直接搬二進位檔——因為 docker-compose 用**絕對路徑** bind-mount 宿主機編譯產物（例如 `/home/lindor/openairinterface5g/cmake_targets/ran_build/build/...`），PC2 帳號是 `mcalab` 但仍在 `/home/lindor/openairinterface5g` 編譯（已手動建立 `/home/lindor` 目錄供其使用），確保路徑跟 compose 檔一致。

### 狀態觀測窗口（100ms）與正規化常數

C xApp 的 Rate Limiter 每 10 個 10ms MAC callback 才觸發一次 ZMQ，因此每筆 State 的 `bsr` 欄位實際上是 **100ms 累積的 `delta_dl_aggr_tbs`（bytes）**，而非單一 10ms 窗口值。對應的正規化常數：

```
MAX_BSR = 2,000,000 bytes/100ms（reward_calculator.py，實測高負載下 delta_tbs 可達 1~2.5M bytes）
MAX_BSR = 1,000,000 bytes/100ms（drl_agent.py，state 編碼用，刻意設較保守的上限，見 DRL_DESIGN.md §5.1）
MAX_BUF_INFO = 2,000,000 bytes（drl_agent.py，dl_buffer_info 正規化上限）
```

`reward_calculator.py` 中的所有吞吐量計算均以其 `MAX_BSR` 為分母；`drl_agent.py` 的 state 編碼另有一組獨立常數。若未來修改 Rate Limiter 的觸發間隔，這些常數必須同步調整。

**目前 reward 現況**：`inference_server.py` 唯一呼叫 `reward_calculator.py::compute_lagrangian_reward()`（`R = R_tp + λ·(JFI_raw − JFI_MIN)`，`JFI_MIN=0.8291`，λ 由 `drl_agent.py` 自適應更新，範圍 `[0, LAMBDA_MAX=10.0]`）。同檔案裡的 `compute_reward`/`compute_reward_breakdown`（純加權和版本，`W_THROUGHPUT=1.0, W_FAIRNESS=0.0, W_DELAY=0.0`）目前**未被呼叫**，是五階段實驗路線圖 Stage 2~4「陽春 reward」的候選實作（見第 3 節）。

### FlexRIC 崩潰規律與重啟流程

**現象一（xApp 端）**：累積約 2000 筆 experience 後 FlexRIC 容器會崩潰（E2 connection 中斷），log 顯示 `[NEAR-RIC]: WARNING: Pending event timeout. Disarming timer.`，xApp 端持續 `Resending Setup Request after timeout`。長時間運行後 pending event queue 塞滿也會觸發同樣症狀。

**現象二（DU 端）**：現象一發生後若只單獨重啟 xApp/DU 而不動 FlexRIC 本身，DU 容器會以 `assoc_rb_tree_extract` assertion 反覆 SIGSEGV，每次都在同一點崩潰。必須連同 FlexRIC 一起做完整乾淨重啟才會恢復。

**恢復流程**：
1. 三台機器都要重新跑（不能只重啟其中一台），啟動順序強制要求 **FlexRIC → DU → xApp**：
   ```bash
   bash ~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator/iab/run_local_pc1.sh
   bash ~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator/iab/run_local_pc2.sh
   bash ~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator/iab/run_local_pc3.sh
   ```
2. `inference_server.py` 啟動時會自動從 `/app/models/model_nodeX.pt` 載入 checkpoint，訓練進度與 MongoDB experience 不會丟失。

**已知過期、待更新的輔助腳本**：`iab/monitor_drl.sh`、`iab/watchdog.sh`、`iab/drl_report.py`、`iab/check_convergence.py`、`iab/iab_perf_test.sh` 仍是雙主機期的 PC2-only（帳號/IP 已過期）、Node1~5／UE1~6 寫死版本，尚未針對三主機 12-node/17-UE 拓樸更新，跑五階段實驗路線圖前需要先處理。

---

## 8. 流量與路徑損耗場景設計

`scenarios/traffic_scenario.py` 提供多種流量場景，透過 `channelmod_ctrl.py` 對 OAI rfsimulator 的 telnet chanmod 介面（`channelmod modify <ue_id> ploss <val>`，即時生效不需重啟容器）動態調整每個 UE 的路徑損耗，模擬通道劣化環境。

**場景清單**：A（CQI 差異化）、B（流量不均）、C（最差公平性）、D（動態訓練，均勻隨機）、**R（真實隨機，推薦）**。Scenario R 用面積均勻抽樣模擬細胞邊緣 UE 較多、lognormal 重尾分佈模擬真實流量需求（多數適中、少數高需求）、間歇閒置機率模擬 burst→idle→burst 使用型態、持久化的每 UE 使用者 profile（heavy-streaming/light-browsing/bursty-iot）避免每個 phase 完全獨立同分布、TCP/UDP 協定混合，並支援 `--seed` 重現同一串隨機條件供 PF vs DRL 的 paired comparison 使用。

路徑損耗安全上限 `PATHLOSS_SAFE_MAX_DB=25.0`（超過會讓 UE 斷線，已現場驗證）。Scenario R 直接送連續 ploss 值，不需要 CQI 校正；Scenario A/B/C/D 需要（`--calibrate --node N`）。

**跨主機執行**：telnet chanmod port 只在該主機本機（`127.0.0.1`）可連，`traffic_scenario.py` 用 `--host {pc2,pc3}` 各自在本地執行、只套用自己負責的 UE/Node 子集；兩台主機用同一個 `--seed`，每個 UE 每個 phase 的隨機值用 `(seed, ue_global_id, phase_index)` 三元組獨立導出（`phase_index` 以絕對時間換算），不需要跨主機即時通訊即可保持同步。

```bash
# PC2：控制 UE1~8
python3 scenarios/traffic_scenario.py --scenario R --seed 42 --host pc2

# PC3：控制 UE9~17（含 UE17）
python3 scenarios/traffic_scenario.py --scenario R --seed 42 --host pc3
```
