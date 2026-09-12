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

### 與 3GPP IAB / O-RAN 標準規格的差異（誠實揭露，供論文方法論限制章節引用）

這個測試平台**不是**完整落地 3GPP IAB 規格與完整 O-RAN RIC 階層的系統，而是「用標準相容的底層元件（OAI 的 3GPP-compliant PHY/MAC/RRC/NAS 協議棧、FlexRIC 這個符合 O-RAN E2AP 規格的 Near-RT RIC 框架）搭建、但在 IAB backhaul 資源共享機制與 RIC 階層上做了論文範疇內合理簡化」的研究平台。差異點如下：

| 項目 | 標準規範怎麼定義 | 這個測試平台實際做法 |
|---|---|---|
| Backhaul 資源共享 | 3GPP TS 38.300 / 38.874 定義 **BAP 層**（Backhaul Adaptation Protocol），負責跳點路由、backhaul RLC channel 對應、QoS mapping | **完全沒有 BAP 層**——MT+DU 串接模式，靠 Linux IP Routing 透傳（見第 1 節），不是標準的 backhaul bearer 機制 |
| H/S/NA 資源分時 | 標準定義 Hard/Soft/Not-Available 資源，DU 跟 MT 在**真正的時域/頻域**上互斥使用資源 | 「Backhaul-aware 動態 PRB 預算」機制（第 3 節）是**容量代理**（依 MT 忙碌程度縮放 DU 可用 PRB 池的大小），不是真正的雙工資源劃分 |
| 多跳無線資源競爭 | 真實 IAB 多跳共用同一段頻譜，節點之間會互相干擾、搶資源 | 每個 Node 各自跑獨立的 rfsimulator 載波，彼此完全不搶頻譜——這點對「模擬多跳拓樸的路由/backhaul 聚合排程問題」是合理簡化，但不對應真實電磁環境下的資源競爭 |
| O-RAN RIC 完整架構 | Non-RT RIC + A1 介面 + Near-RT RIC + SMO | **只有 Near-RT RIC**（FlexRIC + E2 + xApp），沒有 A1 介面、沒有 Non-RT RIC、沒有 SMO；「Global xApp/Global rApp」的 FedAvg/Cluster FL 設計是這篇論文自訂的機制，不是 O-RAN 標準定義的元件 |

**單一 UE、無競爭情況下的理論吞吐量上限**（供對照實測數據用）：以本平台目前的 PHY 參數代入 3GPP TS 38.306 peak data rate 公式——106 PRB @ 30kHz SCS（`donor_du.conf`：`dl_carrierBandwidth=106`, `subcarrierSpacing=1`，等效 40MHz）、SISO 1 層、256QAM（Qm=8）、R_max=948/1024、FR1 DL overhead=0.14：

$$\text{Data Rate} = v_{layers} \times Q_m \times R_{max} \times \frac{N_{PRB} \times 12}{T_s^\mu} \times (1-OH) \approx 227\ \text{Mbps（下行，MAC 層理論峰值）}$$

這是規格書定義的絕對上限（假設每個 RE 都排到最高 MCS、最大編碼率），**不是**實際 iperf3 會量到的數字。本平台用 rfsimulator（軟體模擬，非真實 RF，受 CPU 排程效率影響）+ 多跳 IP relay（每一跳都有處理開銷），Part 4 機制驗證量到的單一 UE（3-hop，旁邊 UE 閒置）實測吞吐量是 **43.8 Mbps**，比理論峰值低了一個數量級，落差主要來自「軟體模擬」與「多跳開銷」兩點，這是 IAB 多跳架構本身要付出的代價之一，不代表系統有問題。

**多跳鏈路（Donor→Relay→Access→UE）下，backhaul-aware PRB 預算機制本身造成的自我節流上限**：上面 227 Mbps 只考慮「每跳是獨立不競爭的模擬載波」這件事，沒有考慮這次上線的 backhaul-aware 動態 PRB 預算機制（第 3 節）本身在多跳鏈路上會形成一個自我節流的回饋迴圈——因為 UE 的下行資料要送達，必須先實際流過**該 UE 所屬 access 節點自己的 MT 無線鏈路**（DU 的 F1-U 資料是透過該節點 MT 的 tunnel relay 進來的），所以這個節點 MT 的忙碌度，正好就等於它正在 relay 給下游 UE 的那份流量本身，形成自己餵自己的迴圈；Relay 節點的 DU→Access-MT 這一段也是同樣的迴圈。

設穩態端到端吞吐量為 $T$，單一載波理論峰值為 227 Mbps。對 Access 這一跳：Access-MT 忙碌度 $\approx T/227$，故 Access-DU 可用容量 $=(1-T/227)\times227=227-T$，要撐住穩態流量需 $T \le 227-T$，即 $T \le 113.5$ Mbps；對 Relay 這一跳推導完全相同，也收斂到 $T \le 113.5$ Mbps。兩層剛好收斂到同一個不動點（不會疊乘複合往下掉，因為 Relay 節流後送出的 113.5 Mbps 到了 Access 這一跳，Access-MT 忙碌度變成 113.5/227≈0.5，Access-DU 可用容量也剛好是 113.5，供給等於自己的節流上限，是穩定不動點）；Donor-DU 本身沒有 MT、不受此機制限制，不構成瓶頸。

**結論**：三跳鏈路（Donor→Relay→Access→UE）在這個機制下的理論吞吐量上限 **≈113.5 Mbps**（約為不考慮此機制時 227 Mbps 的一半），這是機制設計本身對深層 UE 的內建懲罰，量化了「為什麼較深層節點吞吐量普遍偏低」除了跳數本身的處理開銷之外，還有這個機制額外貢獻的一份下降；上述推導為忽略 EWMA 平滑暫態、假設下行主導流量的理想化穩態近似，僅供對照量級參考。此上限仍遠高於 Part 4 實測的 43.8 Mbps（後者已經計入 rfsimulator 軟體模擬與 iperf3/TCP goodput 的額外損耗）。

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

| Stage | 策略 | Global 層（配額協調/FL 聚合） | Local 層（單節點 DRL） | 狀態 |
|---|---|---|---|---|
| 1 | PF baseline | 無 | 無（OAI 內建 PF 排程器，全部 12 個 xApp 停止） | **已完成**（2026-09-13 三度重測，見 `experiment_results/PF.md`：併發 JFI=0.3303、17 UE 平均吞吐量約 6.45 Mbps、平均 RTT 224.23 ms，15 分鐘全程三主機零新增崩潰——**這份數據是 backhaul-aware 機制在三台主機全部真正生效後的正式基準**。前兩次量測皆已作廢：第一次忘記停用 xApp；第二次雖已停用 xApp，但事後發現 PC2/PC3 的 `librfsimulator.so` 忘記重新編譯（只重編了 nr-uesoftmodem/nr-softmodem/telnetsrv，見第 7 節新增的 rsync 後置檢查規則），導致只有 PC1 節點的機制真正生效，PC2/PC3 全部節點仍是 no-op；三主機皆確認 `bhload` 模組成功註冊後才產出本次數據。**UE17 現場複測 ICMP 100% 封包遺失、iperf3 完全無法建立傳輸**，是三主機機制全部真正介入後 Node4（UE17 直連 relay，同時中繼 Node11+Node12）三重負載疊加的極端案例，詳見 PF.md「UE17 特別說明」；是 Stage 2~5 的比較對象） |
| 2 | avg FL + 最基礎 DRL | Global xApp+Global rApp：標準 FedAvg，全部 12 節點一起聚合 | Local xApp+Local rApp：最基礎 DRL（`REWARD_MODE=throughput_only`，無 Lagrangian／無限制式） | 未開始 |
| 3 | cluster FL + 最基礎 DRL | Global xApp+Global rApp：Cluster FL，依角色分兩群聚合：relay cluster（Node1~4）、access cluster（Node5~12）各自獨立 FedAvg | Local xApp+Local rApp：最基礎 DRL（同 Stage 2，模型不變，只有 Global 聚合方式不同） | 未開始 |
| 4 | 自訂 FL + 最基礎 DRL | Global xApp+Global rApp：自訂聚合演算法（介面待設計） | Local xApp+Local rApp：最基礎 DRL（同 Stage 2/3） | 未開始 |
| 5 | 自訂 FL + 改良版 DRL | Global xApp+Global rApp：自訂聚合演算法（同 Stage 4，不變） | Local xApp+Local rApp：改良版 DRL（`REWARD_MODE=lagrangian`，重新啟用 Lagrangian JFI 限制機制） | 未開始 |

**Stage 1 量測方法**（後續 Stage 2~5 沿用同一套方法以確保公平比較）：`iab/measure_stage.py` 與 `scenarios/traffic_scenario.py` 同時執行，併發取樣全部 17 個 UE 在同一組動態流量+路徑損耗場景下的即時吞吐量與 RTT；每個 stage 的完整數據與量測日期記錄在 `experiment_results/<方法名>.md`（例如 `PF.md`、`avgFL.md`、`clusterFL.md`）。

**單調遞增要求**：PF < avg FL + 最基礎DRL < cluster FL + 最基礎DRL < 自訂FL + 最基礎DRL < 自訂FL + 改良版DRL。Stage 2→3→4 只換 Global 聚合方式、Local 模型不變，用來單獨驗證「聚合策略」的貢獻；Stage 4→5 只換 Local reward 機制、Global 聚合不變，用來單獨驗證「改良版 DRL（Lagrangian）」的貢獻——每次只換一個變數，才能把進步歸因到正確的地方。

**關鍵設計決定（避免混淆）**：
* Stage 2~4 底層用的是**同一個**陽春 DRL（只差 FL 聚合方式），不是三種不同的模型。
* 「改進版 DRL」= 重新啟用現有的 Lagrangian 機制。**`REWARD_MODE` 開關已實作完成**（2026-09-12）：環境變數預設 `lagrangian`（維持現行行為，`inference_server.py` 呼叫 `reward_calculator.py::compute_lagrangian_reward()`，`R = R_tp + λ·(JFI_raw − JFI_MIN)`，λ 由 `drl_agent.py::train_on_batch()` 自適應更新）；設成 `throughput_only` 時改呼叫 `compute_reward_breakdown()`（純 throughput，`W_THROUGHPUT=1.0, W_FAIRNESS=0, W_DELAY=0`），且 `drl_agent.py` 會跳過 λ 更新（恆為 `LAMBDA_INIT=0.0`）。用法：`REWARD_MODE=throughput_only bash iab/run_local_pc1.sh`（12 個 `inference-nodeN` 服務的 `docker-compose-iab-server.yaml` 都已接上 `${REWARD_MODE:-lagrangian}`）。**切換 REWARD_MODE 前務必清空 MongoDB 經驗與模型 checkpoint**，reward 語意改變不能混在同一批訓練資料裡（沿用既有先例）。
* **不寫死、隨時可單獨跑任一 stage**：每個 stage 要能透過環境變數/CLI flag 獨立選擇（例如 `FL_MODE=none|avg|cluster|custom` + `REWARD_MODE=throughput_only|lagrangian`），不是「一定要照順序、前面沒做完後面就不能跑」的線性相依關係——不論開發進度到哪，都要能重跑任何一個 stage 的數據。
* Stage 3 的 cluster 分群邏輯可參考 `inference/DRL_METHODOLOGY_PLAN.md`（舊 5-node 版本的 relay/access 分群設計，需要重新推導成 4/8 分群；分群理由本來是「relay 中繼下游流量、access 直接面對 UE，結構不同」——這個理由在加入下方 backhaul-aware PRB 預算機制後仍然成立，因為 relay 的下游是「其他有 DU 的節點」、access 的下游是「純 UE」，性質確實不同，但兩者現在都同樣會被 backhaul 使用量壓縮 PRB 預算，不是只有 relay 才有這個約束）。
* Global xApp／Flower FL 的完整舊版部署細節（SuperLink/SuperNode 拓樸、client/server app 邏輯）在 `HISTORY.md`，復原時可直接參考架構，但節點數/角色分群需要重新推導成新的 12-node 拓樸。

### 環境層新增機制：Backhaul-aware 動態 PRB 預算（五階段共用，2026-09-12 設計討論）

**動機**（詳細討論過程見 `HISTORY.md`）：現行架構每個 Node 各自跑獨立的 rfsimulator 載波，彼此不搶頻譜，這本身是合理的 IAB 模擬簡化（對應 3GPP IAB 的 MT+DU 分裂架構、多 hop backhaul 聚合排程問題都有確實模擬到），但**每個節點自己的 MT（連上層 backhaul）跟 DU（服務下游）之間目前完全獨立，沒有互相牽制**——這跟真實 IAB 的 H/S/NA（Hard/Soft/Not-Available）資源分時機制不符：真實系統裡，一個節點的 DU 能不能用某段資源，取決於當下 MT 有沒有在用同一份資源做 backhaul。

**適用範圍**：Node1~Node12 全部 12 個節點都要套用（不是只有「relay」Node1~4）——因為 Node5~12（access）一樣有自己的 MT（連上層 relay）+ DU（服務底下的 UE 或再下一層 access），一樣存在 MT/DU 資源互搶的問題，只是它們的下游是純 UE、relay 的下游是其他節點，這點不影響「MT 用得多、DU 就該分得少」這個規則本身。

**機制設計（核心規則，符合真實 H/S/NA 語意）**：
1. 每個排程週期開始前，**先**根據該節點當下 MT backhaul 的即時使用量，算出「這個節點的 DU 這次排程週期實際可用的 PRB 數量」（<106，隨 MT 忙碌程度動態縮小）。
2. 這個縮小後的可用 PRB 數量，是排程器（不管是 OAI 內建 PF 演算法，還是 xApp/DRL 的權重陣列）的**輸入**，不是對輸出結果的事後裁切——真實系統的 H/S/NA 資源分類是排程器「看不到」被劃走的資源，不是「排完再砍」。
3. **必須實作在 OAI MAC 排程器的 C 語言層**（很可能是 `gNB_scheduler_dlsch.c`，已在第 7 節「共用底層檔案」清單中），讓這個縮小規則對 PF 跟 DRL 兩種排程來源都一視同仁地生效——因為 Stage 1 PF baseline 完全沒有 xApp 介入，如果這個機制只寫在 xApp 裡，PF baseline 就吃不到這個約束，會破壞跨 Stage 比較的公平性（每一階都必須面對「同一份被壓縮過的資源池」，差別只在池子裡怎麼分）。
4. 目前**尚未實作**，公式（MT 使用量怎麼換算成 DU 可用 PRB 上限的具體比例關係）也還沒設計，是下一步要確認的技術細節。

**各開發階段需要注意的事**：

* **Stage 1（PF baseline）**：**已完成**（最終定案於 2026-09-13）。歷經兩輪：
  - **2026-09-12**：回歸測試（13/13 E2、17/17 UE 附著、iperf3 sanity check）與正式 15 分鐘量測通過，過程中發現並根除三個 root cause bug（詳見 HISTORY.md 對應日期條目）：telnetsrv 的 `recv()` 錯誤值處理不完整導致的 buffer overflow 崩潰、CU UID 分配器耗盡時的整數溢位崩潰、CU 對同一 DU ID 的 F1 association 記錄在異常斷線後永久不清除導致的連線永久拒絕。但這輪的 `PF.md` **事後查出 backhaul-aware 機制其實整段是靜默 no-op**（`bhload` 命令命名撞上 telnetsrv 保留字導致 MT 端 SIGSEGV，見下方 HISTORY.md 條目），數據已作廢。
  - **2026-09-13**：修復 `bhload` 命名衝突（改名 `get`→`query`，並補上 `telnetsrv.c::setgetvar()` 的 NULL 防呆）、修復 PC2/PC3 漏編譯 `rfsimulator` target 導致機制只有三分之一節點真正生效的問題後，三度重測才產出真正有效的基準（`experiment_results/PF.md`：JFI=0.3303、平均吞吐量 6.45 Mbps、平均 RTT 224.23 ms）。PF 排程器本身不需要額外開發（它本來就不讀任何自訂 state），縮小可用 PRB 池這件事對 PF 排程器是透明的。

* **Stage 2（avg FL + 最基礎 DRL）**：
  - 這是第一個要接上 Global xApp/Global rApp 的階段，`global_xapp.py`／`global_xapp_bridge.py`／`inference/flower-app/` 都要先針對 12-node 拓樸重建（目前是舊 5-node 版本，尚未重建）。
  - 建議評估是否要讓 Local DRL 的 state 多一個「這次排程週期實際可用 PRB 數量／106 的比例」特徵，讓 Actor 能感知 backhaul 緊繃程度、提前做出更聰明的決策（而不是每次都假設有滿的 106 可用，被動被縮減）——這是可選的優化，不是正確性必要條件（縮減是 C 語言層強制生效的，DRL 不知道這個特徵也不會違規，只是可能學得比較慢/比較不精準）。若要加，`STATE_DIM` 會變動，屬於破壞性變更。
  - 啟動這個 Stage 前，**MongoDB 經驗與模型 checkpoint 要重新清空**：一來 `REWARD_MODE` 從 lagrangian 切到 throughput_only（既有規則），二來如果上面那條也一起做了，環境本身的 state/action 動態都變了，舊經驗不能混用。
  - 全網 JFI（`compute_global_jfi()`，舊版已有但只做監控）如果要在這個階段就開始納入聚合權重或做為額外訊號，需要明確決定；如果沒有，`avgFL.md` 的全網 JFI 表現可能改善有限，屬於預期內、不是 bug。

* **Stage 3（cluster FL + 最基礎 DRL）**：
  - Local 模型架構跟 Stage 2 完全相同，只換 Global 聚合的分群方式，訓練資料/checkpoint 是否需要清空，取決於「換聚合方式」算不算破壞性變更——建議清空，避免 Stage 2 殘留的聚合結果污染 Stage 3 的量測（FL 聚合的效果評估需要乾淨的起點）。
  - `DRL_METHODOLOGY_PLAN.md` 提到的舊版分群邏輯要重新推導成 4/8 分群（relay 4 個、access 8 個），確認 `min_train_nodes`/`min_evaluate_nodes` 等 Flower 參數有沒有跟著調整。

* **Stage 4（自訂 FL + 最基礎 DRL）**：
  - 自訂聚合演算法介面要先設計出來（目前 CLAUDE.md 標註「待設計」），建議設計時就把「要不要把全網 JFI 或 backhaul 緊繃程度也當作聚合權重的輸入」一併考慮進去，呼應第 3 節開頭「Global 智慧分配」要解決的是全網協調問題，不能只是換一種模型平均方式。
  - 同樣建議 MongoDB/checkpoint 重新清空。

* **Stage 5（自訂 FL + 改良版 DRL）**：
  - 這階段**只換** `REWARD_MODE=lagrangian`（Local 層），Global 聚合方式維持跟 Stage 4 一樣不變——這是刻意設計，用來單獨驗證「加回 Lagrangian 限制式」的貢獻，不要在這階段順便改動 Global 聚合邏輯，否則進步幅度無法歸因。
  - 必須清空 MongoDB/checkpoint（既有規則，reward 語意改變）。
  - 這是五階段的終點，驗收標準是全部指標（TCP-DL、Latency、UDP-DL/UL、JFI）都優於前四階，尤其 JFI 應該是全程最高（Lagrangian 機制專門在管這件事）。

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
>
> **⚠️ `rsync` 完只是同步了原始碼，不代表 PC2/PC3 已經吃到修改**：一定要在 PC2、PC3 上各自重新執行編譯指令，且要重編**全部受這次修改影響的 build target**，不是只重編「看起來相關」的那一個——2026-09-13 debug session 曾經漏編 `radio/rfsimulator/simulator.c` 對應的 `rfsimulator` target（只重編了 `nr-uesoftmodem`/`nr-softmodem`/`telnetsrv`），導致 PC2、PC3 的 `librfsimulator.so` 停留在舊版本，三台主機表面上都跑著「同一份原始碼」，實際上只有 PC1 真正生效，另外兩台主機的行為跟修 bug 之前完全一樣，卻很容易被誤判成「已經全部修好」。修改共用檔案後，收工前務必在三台主機上都做這個檢查：
> ```bash
> stat -c '%Y %n' <改過的原始碼檔案>
> stat -c '%Y %n' <對應編譯產物，例如 librfsimulator.so / libtelnetsrv.so / nr-softmodem / nr-uesoftmodem>
> ```
> 確認每一台主機上編譯產物的時間戳都新於原始碼的時間戳，時間戳比原始碼舊就代表沒吃到這次修改，必須重編。哪個原始碼檔案對應哪個 build target，用 `grep -rn <檔名> --include=CMakeLists.txt .` 或 `grep -rn <檔名> CMakeLists.txt`（頂層）查，不要用猜的——這個專案裡不少檔案（例如 `telnetsrv_bhload.c`）是直接编進某個執行檔而非獨立成 `.so`，跟其他 telnetsrv 模組的慣例不同。

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
