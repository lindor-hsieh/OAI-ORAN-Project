# 碩士論文實驗流程與環境配置配置指南 (O-RAN IAB 架構)

> **文件慣例**：本檔案只保留「現在式」的架構事實與可執行指令。任何帶日期的踩坑過程、除錯敘事、歷史量測數據表，一律寫進 `HISTORY.md`（同層目錄），不要寫回這裡。

## 1. 實驗實體環境與網路拓撲設置

採用**三主機 (Tri-host) 實體部署**，模擬真實 O-RAN IAB 網路中的實體隔離與傳輸延遲，拓樸為 **1 donor + 4 relay + 8 access + 17 UE** 的對稱四層樹狀結構。底層 IAB 架構採用 **MT + DU 串接模式**（無 BAP 層），所有跨節點傳輸皆透過標準 5G Uu 介面與 Linux IP Routing 進行轉發。三台主機透過各自的 USB3.0→RJ45 轉接卡共接同一台 switch，組成單一 `192.168.88.0/24` L2 網段（見 `iab/setup_lab_net.sh`）。

### 拓樸與節點編號

```
Donor (PC1)
├── Node1 (relay, PC1) ── Node5 (access, PC2) ── UE1, UE2
│                     └── Node6 (access, PC2) ── UE3, UE4
├── Node2 (relay, PC1) ── Node7 (access, PC2) ── UE5, UE6
│                     └── Node8 (access, PC2) ── UE7, UE8
├── Node3 (relay, PC1) ── Node9  (access, PC3) ── UE9,  UE10
│                     └── Node10 (access, PC3) ── UE11, UE12
└── Node4 (relay, PC1) ── Node11 (access, PC3) ── UE13, UE14
                      ├── Node12 (access, PC3) ── UE15, UE16
                      └── UE17（直接掛在 Node4 的 DU，不經過 access 層；Node4 在 PC1、UE17 容器在 PC3）
```

Donor→Relay→Access→UE 為 3-hop；UE17→Node4 為 2-hop。**全部 4 個 relay（Node1~4）現在都跟 Donor 同機（PC1）**，Donor→Relay 這一段變成本機內部通訊；Relay→Access 這一段（全部 8 個 access 節點）則全部是跨主機連線（PC1→PC2/PC3），透過三台共用的 macvlan L2 網段，不需要額外設定。

> **2026-09-22 節點重分配**：把全部 4 個 relay（Node1~4）集中搬到 PC1（跟 Donor 同機），access 節點（Node5~12）平均分散到 PC2（Node5,6,7,8）/PC3（Node9,10,11,12）；UE17 容器搬到 PC3（macvlan IP 位置透明、不需改值，見下方 IP 表）。
>
> 原因：2026-09-21 完成的 PF/avgFL/clusterFL 三方量測，逐 UE 比對後發現 UE5~8（當時 Node2+Node7,8，跟 Donor 同在 PC1）在三次量測中全部是吞吐量最高的一群，UE1~4/UE9~16（需要真正跨主機傳輸）全部是最低的一群——這個分組跟場景流量、排程策略都無關，純粹是「跟 Donor 同主機」省去一段實體跨主機開銷造成的量測 confound。改成「全部 relay 集中同機、全部 access 一律跨主機」後，四條分支的路徑結構完全一致（Donor→Relay 全部同機、Relay→Access 全部跨主機），理論上不應該再有任何一條分支系統性領先。
>
> 這個分布也比先前考慮過的「relay 分散、access 整條分支搬家」方案更省資源：relay 本身負載比 access 輕，PC1 集中 4 個 relay（8 個即時 process）比先前的 Node2（2 個）還少；PC2/PC3 各自 4 個 access（8 個 process）+ 8~9 個 UE，落在 PC2:16、PC3:17，均低於已驗證安全的 ≤18 上限。目前三主機即時 process 數量約為 **PC1:10、PC2:16、PC3:17**（乾淨重啟後的實測結果見 `experiment_results/PF.md` 對應章節）。
>
> UE17 的 pathloss channelmod 控制（Node4 的 DU telnetsrv）跟它的 iperf 流量控制（容器）現在物理上分屬兩台主機（PC1/PC3），已把 Node4 的 chanmod telnet port 從只綁 `127.0.0.1` 改成額外綁 macvlan-br 位址（`192.168.88.1:9092`），讓 PC3 的 `traffic_scenario.py` 能跨主機連過去（見 `scenarios/traffic_scenario.py` 的 `NODE_TELNET_HOST_OVERRIDE`）。

### 硬體與節點配置表

| 實體主機 | 部署元件 | 網路角色 | 備註說明 |
| :--- | :--- | :--- | :--- |
| **PC 1** (192.168.88.1, lindor) | CN5G、FlexRIC Server、MongoDB、Donor CU/DU、**全部 12 組** `xapp-nodeN`+`inference-nodeN` 容器、**Node1,2,3,4 (relay，Node4 含直連 UE17 的 DU)** | 核心網與全域控制中心 + 全部 relay 層 | xApp(C)+inference(Python) 刻意集中在 PC1（見下方理由），MongoDB 對外監聽 `27017`。4 個 relay 的 CU 端指令（路由）都是本機直接執行，不需要 SSH（見 `iab/start_iab_server.sh` 的 `configure_and_start_local_relay`）。 |
| **PC 2** (192.168.88.2, **mcalab**) | Node5,6,7,8 (access) + UE1~8 | RAN 資料面 | 純資料面，無 xApp/inference 容器。Ubuntu 20.04。全部 4 個 access 節點的 parent relay（Node1,2,3,4）都在 PC1，跨主機連線。 |
| **PC 3** (192.168.88.3, lindor) | Node9,10,11,12 (access) + UE9~17 | RAN 資料面 | 純資料面，無 xApp/inference 容器。Ubuntu 24.04。這 4 個 access 節點的 parent relay（Node3,4）在 PC1，跨主機連線；UE17 容器也在這裡（邏輯上仍掛在 Node4 底下，見上方拓樸圖說明）。 |

**xApp/inference 集中在 PC1 的理由**：xApp(C) 與 inference(Python) 之間走 `ipc://` Unix domain socket（見第 2 節），兩者必須同一台主機；若要「真正分散到 PC2/PC3」，ZMQ 要改走 TCP，5ms timeout 預算要多扛一段跨主機網路延遲，風險換來的好處不大——經評估後決定維持集中，`MONGO_URI` 因此不需要因為主機數增加而修改（所有 inference 容器仍是 `mongodb://localhost:27017`，因為它們仍跟 MongoDB 同一台主機）。RAN 節點（MT/DU/UE）則物理分散到 PC1（全部 relay）/PC2/PC3（access+UE），relay 與其 access 子節點必定跨主機（2026-09-22 起，見上方拓樸圖）。

### IP / ID 配置表

| 項目 | Donor | Node1~4（relay） | Node5~12（access） | UE1~17 |
|---|---|---|---|---|
| `gNB_ID`/`gNB_DU_ID` | `0xe00` | `0xe01`~`0xe04` | `0xe05`~`0xe0c` | — |
| `nr_cellid` | `12345678` | `12345679`~`12345682` | `12345683`~`12345690` | — |
| `physCellId` | `0` | `1`~`4` | `5`~`12` | — |
| E2 `TARGET_NODE_ID`（xApp .c，= gNB_ID 十進位） | — | `3585`~`3588` | `3589`~`3596` | — |
| `rfsimulator.serverport` | `4043` | `4044`~`4047` | `4048`~`4055` | — |
| FlexRIC telnet debug port（chanmod 通道控制） | — | `9089`~`9092`（Node4=`9092` 額外開放 macvlan-br 位址 `192.168.88.1`，供 PC3 跨主機控制 UE17，其餘仍 `127.0.0.1`-only） | `9093`~`9100` | — |
| IMSI (`208990100001xxx`) | — | 尾碼 `100`~`103` | 尾碼 `104`~`111` | 尾碼 `200`~`216`（跟 MT 區段刻意拉開） |
| macvlan IP | `.144`（DU）| Node1=`.150`,Node2=`.151`,Node3=`.152`,Node4=`.153`（全部在 PC1）—— MT/DU 共用同一 netns、同一 IP，這個位址不因實際跑在哪台主機而改變（三台共用同一個 macvlan L2 網段） | Node5=`.160/.161`,Node6=`.162/.163`,Node7=`.164/.165`,Node8=`.166/.167`（PC2）; Node9=`.168/.169`,Node10=`.170/.171`,Node11=`.172/.173`,Node12=`.174/.175`（PC3） | 動態，共用 `12.1.1.0/24` SMF pool；UE17 額外占用 macvlan `.176`（直連 relay，需要自己的 macvlan IP；容器在 PC3，位址不變） |
| internal bridge IP | — | 不需要（relay 的 MT/DU 共用 netns，沒有獨立位址） | **PC2 用 `192.168.74.0/24`**：Node5=`.10/.20`,Node6=`.11/.21`,Node7=`.12/.22`,Node8=`.13/.23`；**PC3 用 `192.168.75.0/24`**：Node9=`.10/.20`,Node10=`.11/.21`,Node11=`.12/.22`,Node12=`.13/.23` | — |

> **各主機 internal bridge 子網刻意不同**（`.74.0/24`／`.75.0/24`）：PC1 的路由表對同一個子網只能指到一個 next-hop，若多台主機共用同一子網，PC1 就無法同時正確路由到不同主機的 access node internal IP。internal bridge 網路是 access 節點自己的 host-local 網路，只跟「access 節點實際跑在哪」有關，跟它的 parent relay 跑在哪台主機無關。PC1 2026-09-22 後不再有任何 access 節點，不再需要 internal bridge 網路（原本 Node7,8 用的 `iab_internal_net_pc1`/`192.168.76.0/24` 已整段移除）。

CN5G（`.131`~`.134`）、FlexRIC（`.141`）全部在 PC1。

新增 IMSI 若落在既有 `100`~`216` 範圍以外，記得同步在 `oai_db.sql`／執行中的 `rfsim5g-mysql` 補 `INSERT INTO users`，否則 UE/MT 會收到 `FGS_REGISTRATION_REJECT`（過程見 `HISTORY.md`）。

---

## 2. 軟體架構與 O-RAN 職責映射

本系統跨越 C 語言的底層通訊與 Python 的 AI 推論，構築實體隔離的「Local / Global 雙層階層式控制平面」。

* **底層協議棧 (C 語言)**：使用 OAI (OpenAirInterface) 實作 DU/CU 與 UE。
* **Near-RT RIC (C 語言)**：使用 FlexRIC 作為 E2 代理伺服器與 xApp 框架。
* **Non-RT RIC & AI (Python)**：規劃透過 Flower Framework 進行階層式聯邦學習 (Hierarchical FL)，並透過 ZeroMQ 建立跨語言 IPC 通訊。

> **目前實作現況**（2026-09-14 更新）：Local xApp + Local rApp + Global xApp + Global rApp 四個元件皆已針對 12-node 拓樸重建完成並上線，Stage 2（avg FL，見 `experiment_results/avgFL.md`）與 Stage 3（soft cluster FL，見 `experiment_results/clusterFL.md`）皆已完成量測。**Global xApp 已改版為全域公平性軟性廣播機制**（非舊版 5-node 的 relay→access 配額裁切設計，見下方元件定義），`docker-compose-iab-server.yaml` 已新增 `global-xapp`／`flower-superlink`／`flower-supernode-node{1..12}`／`flower-scheduler` 服務定義，全部掛在 `profiles: ["stage2-fl"]` 底下（Stage 1 PF 重跑時不受影響）。舊版 5-node 配額廣播機制的設計細節保留在 `HISTORY.md` 供歷史參考，但**不再是需要復原的架構**——新設計已確認優於舊版（見下方元件定義的理由說明）。Stage 2 的數據於 2026-09-14 重測過一次（見 `HISTORY.md` 對應條目），目前 `avgFL.md`／`clusterFL.md` 皆為重測後的乾淨版本。

### 核心控制元件定義

1. **Local xApp**：
   * **實作**：純 C 語言 FlexRIC 程式，每個 Node 各自獨立（`xapp_node1.c` ~ `xapp_node12.c`）。
   * **職責**：局部控制迴圈（實際 100ms，C xApp 設有 Rate Limiter：每 10 個 10ms MAC callback 才觸發一次 ZMQ，避免 FlexRIC pending event queue 滿載崩潰）。只負責 PRB 分配這一個動作：透過 E2SM-MAC 擷取所屬 Node 的 **Δ DL TBS、MCS、DL Buffer Occupancy** 作為狀態（`dl_aggr_tbs` 差分值為吞吐量代理、`dl_mcs1` 為通道品質代理、`dl_buffer_info` 為不受排程與否影響的需求代理——注意 OAI RF Simulator 的真 3GPP `wb_cqi` 恆為 0，是模擬器本身不計算真實通道傳播的限制，`dl_buffer_info` 則沒有這個限制），將 JSON 狀態透過 ZeroMQ REQ 送給 Local rApp Python 端，收回 PRB 權重陣列後立即寫回 OAI MAC 層。C 語言端不含任何 AI 邏輯。
2. **Local rApp**：
   * **實作**：Python ZeroMQ REP 伺服器（`inference_server.py`，部署於 12 個獨立容器，全部在 PC1）。
   * **職責（雙重角色）**：
     * **Near-RT 推論（毫秒級）**：接收 Local xApp 的 ZeroMQ 請求，執行 DRL Actor 網路 forward pass，回傳 PRB 權重陣列，並將 State/Action/Reward 非同步寫入 MongoDB。
     * **Non-RT Fine-tuning（秒/分鐘級）**：從 MongoDB 讀取歷史資料，執行本地模型微調（`inference_server.py` 背景訓練執行緒 `_train_worker`，每 60 秒一輪）。
3. **Global xApp（全域公平性軟性廣播，Stage 2~5 全程固定存在）**：
   * **實作**：獨立 Python process（`global_xapp.py`），每 `GLOBAL_XAPP_INTERVAL_S`（預設 2 秒）讀一次 MongoDB 全部 12 個節點最近 `GLOBAL_XAPP_LOOKBACK`（預設 50）筆經驗，算出每個節點的平均吞吐量與全域平均的落差，換算成 `fairness_bias = clip(global_mean / (node_mean + eps), 0.5, 2.0)`，透過 ZMQ PUB 廣播給全部 12 個 Local rApp（topic `nodeN`）。
   * **與 Stage 1 完成的「Backhaul-aware 動態 PRB 預算」機制（C 層，`gNB_scheduler_dlsch.c`）的分工**：兩者刻意作用在不同軸，不會衝突——C 層依「該節點自己 MT 的真實忙碌度」硬性縮小 DU 可用 PRB 池上限（單節點局部視角，PF 排程器也吃得到，是排程器的輸入約束）；Global xApp 依「全域相對落後程度」提供**純軟性 state 特徵**給 DRL Actor（單節點看不到的全域視角，只有跑 DRL 的階段才吃得到，不做任何硬性 PRB 裁切）。舊版 5-node 拓樸的「Global xApp」設計（relay 對自己的 access 子節點做局部配額裁切）已確認是錯誤方向——那本質上跟 C 層機制做同一件事（節流 DU 可用資源），只是一個用量測值、一個用父節點猜測值，同時上線會造成雙重節流；新設計改讓 Global xApp 專注做 C 層機制做不到的事（全域視角），完全消除了這個衝突。
   * **常數**：`state_vec[48]` = active_ratio，`state_vec[49]` = 正規化後的 `fairness_bias`（`(clip(bias,0.5,2.0)-0.5)/1.5`），`STATE_DIM=50` 維持不變（非破壞性變更，只是把原本恆為 1.0 的舊版 `prb_quota_ratio` 欄位換成有意義的語意）。
4. **Global rApp（Flower ServerApp/ClientApp，聚合策略隨 `FL_MODE` 切換）**：
   * **實作**：`inference/flower-app/iab_fl/`，`server_app.py`（`FL_NUM_NODES=12`）+ `client_app.py`，透過 Flower SuperLink（`flower-superlink`）+ 12 個 SuperNode（`flower-supernode-node{1..12}`，`--clientappio-api-address` 分別綁定 `9101~9112`）部署，`flower-scheduler` 每 `FL_ROUND_INTERVAL_S`（預設 180 秒）觸發一輪 `flwr run`。
   * **`FL_MODE=avg`（Stage 2，預設值）**：`IABFedAvg`，標準 FedAvg，全部 12 節點一起聚合，聚合後的權重寫回共用的 `model_nodeN.pt` checkpoint，Local rApp 背景執行緒偵測到 mtime 變化即熱重載。已加上 `ZeroDivisionError` 防呆（全部節點 `num-examples=0` 時跳過本輪聚合，不崩潰、不誤把空 ArrayRecord 廣播出去覆蓋掉有效權重）。
   * **`FL_MODE=cluster`（Stage 3）**：`IABClusterFedAvg`（繼承 `IABFedAvg`），依 `role_ratio_i` 加權聚合出 relay/access 兩個原型再依各節點自己的比例混合廣播，取代單一全域平均；`client_app.py::train()` 的回覆多帶一個 `node_id` 欄位供 server 端分組。兩個 strategy class 並存於同一份 `server_app.py`，`main()` 依環境變數決定 instantiate 哪一個，兩者的 `_seed_initial_arrays()`／checkpoint 廣播底層寫入邏輯（`_apply_weights_to_node()`）共用。完整設計、公式、量測結果見第 3 節路線圖與 `experiment_results/clusterFL.md`。
   * **Stage 4~5**：只再換 `server_app.py` 的聚合邏輯（自訂 FL），`client_app.py`／Global xApp／Local xApp+Local rApp 皆不變，見第 3 節路線圖。

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

### 五階段實驗路線圖（Stage 1~3 已完成，Stage 4~5 規劃中）

目標：在同一組流量+路徑損耗場景下，依序驗證 5 個遞增複雜度的控制策略，**每一階的實驗數據（TCP-DL、Latency、UDP-DL/UL、Jain's Fairness Index 等）都必須贏過前一階**，最終逼近吞吐量理論上限。

| Stage | 策略 | Global 層（配額協調/FL 聚合） | Local 層（單節點 DRL） | 狀態 |
|---|---|---|---|---|
| 1 | PF baseline | 無 | 無（OAI 內建 PF 排程器，全部 12 個 xApp 停止） | **已完成**（2026-09-13 三度重測，見 `experiment_results/PF.md`：併發 JFI=0.3303、17 UE 平均吞吐量約 6.45 Mbps、平均 RTT 224.23 ms，15 分鐘全程三主機零新增崩潰——**這份數據是 backhaul-aware 機制在三台主機全部真正生效後的正式基準**。前兩次量測皆已作廢：第一次忘記停用 xApp；第二次雖已停用 xApp，但事後發現 PC2/PC3 的 `librfsimulator.so` 忘記重新編譯（只重編了 nr-uesoftmodem/nr-softmodem/telnetsrv，見第 7 節新增的 rsync 後置檢查規則），導致只有 PC1 節點的機制真正生效，PC2/PC3 全部節點仍是 no-op；三主機皆確認 `bhload` 模組成功註冊後才產出本次數據。**UE17 現場複測 ICMP 100% 封包遺失、iperf3 完全無法建立傳輸**，是三主機機制全部真正介入後 Node4（UE17 直連 relay，同時中繼 Node11+Node12）三重負載疊加的極端案例，詳見 PF.md「UE17 特別說明」；是 Stage 2~5 的比較對象。**2026-09-22 節點重分配後已在 Scenario T 下重測**（見 `experiment_results/PF.md` 對應章節）：JFI 從 0.2812（09-21 舊拓樸）躍升到 0.9812，UE5~8 對其餘 13 UE 的吞吐量比值從約 10~20 倍降到 1.08 倍，證實先前「固定幾個 UE 持續最高」的現象主因是同主機優勢而非排程/場景差異；平均吞吐量下降（4.23→1.06 Mbps）是移除該優勢後的預期結果，非系統劣化，詳見該章節「結論：同主機優勢 confound 已消除」） |
| 2 | avg FL + 最基礎 DRL | Global xApp（全域公平性軟性廣播，Stage 2~5 全程固定）+ Global rApp：標準 FedAvg，全部 12 節點一起聚合 | Local xApp+Local rApp：最基礎 DRL（`REWARD_MODE=throughput_only`，無 Lagrangian／無限制式） | **已完成（2026-09-14 重測版，取代 2026-09-13 舊版本）**，見 `experiment_results/avgFL.md`：JFI=0.4779 對比 PF 的 0.3303（**改善 +44.7%**）、17 UE 平均吞吐量 8.40 Mbps 對比 PF 的 6.45 Mbps（**改善 +30.2%**）、平均 RTT 296.07 ms 對比 PF 的 224.23 ms（**惡化 +32.1%，尚未達成 RTT 單調遞增要求，判斷主因同舊版本——DRL Actor 推論延遲疊加進 MAC 排程週期**），15 分鐘全程三主機 FlexRIC/CU/DU/MT 零新增崩潰。**重測原因**：2026-09-13 舊版本的 `flower-supernode-nodeN` 訓練路徑其實一直缺 `REWARD_MODE` 環境變數、悄悄跑 lagrangian，加上另外兩個會導致資料面隨機斷線的基礎設施問題（CU NAT table 被無條件 flush、cpuset 過度擁擠造成 CU 隨機崩潰）修復後決定重測，三個 root cause 完整記錄見 `HISTORY.md` 2026-09-14 條目 |
| 3 | soft cluster FL + 最基礎 DRL | Global xApp+Global rApp：Soft/Weighted Clustered FL——依節點連續角色比例 `role_ratio_i`（見下方公式）加權聚合出 relay/access 兩個原型模型，每個節點依自己的 `role_ratio_i` 混合接收兩個原型；硬性二分群是 `role_ratio∈{0,1}` 時的特例（2026-09-13 討論定案，取代原本 relay/access 硬分兩群設計，動機見下方 Node4 說明） | Local xApp+Local rApp：最基礎 DRL（同 Stage 2，模型不變，只有 Global 聚合方式不同） | **已完成**（2026-09-14，見 `experiment_results/clusterFL.md`：JFI=0.4163、17 UE 平均吞吐量 6.30 Mbps、平均 RTT 372.57 ms，皆**低於** avg FL（Stage 2）的 0.4779／8.40 Mbps／296.07 ms，**尚未達成單調遞增要求**，15 分鐘全程三主機零新增崩潰。**重要方法論限制**：量測全程 FL 觸發訓練幾乎每輪都因經驗數量不足而跳過聚合，`IABFedAvg`／`IABClusterFedAvg` 在這 15 分鐘視窗內幾乎沒有機會做出有意義的非零權重聚合，Stage 2/3 觀察到的差異主要反映 12 個節點各自獨立訓練軌跡的隨機變異，不能直接歸因為聚合演算法本身有問題（聚合公式已經過離線數學驗證）；為何落後與後續調整方向待下一輪討論，詳見 clusterFL.md） |
| 4 | 自訂 FL + 最基礎 DRL | Global xApp+Global rApp：自訂聚合演算法（介面待設計） | Local xApp+Local rApp：最基礎 DRL（同 Stage 2/3） | 未開始 |
| 5 | 自訂 FL + 改良版 DRL | Global xApp+Global rApp：自訂聚合演算法（同 Stage 4，不變） | Local xApp+Local rApp：改良版 DRL（`REWARD_MODE=lagrangian`，重新啟用 Lagrangian JFI 限制機制） | 未開始 |

**Stage 1 量測方法**（後續 Stage 2~5 沿用同一套方法以確保公平比較）：`iab/measure_stage.py` 與 `scenarios/traffic_scenario.py` 同時執行，併發取樣全部 17 個 UE 在同一組動態流量+路徑損耗場景下的即時吞吐量與 RTT；每個 stage 的完整數據與量測日期記錄在 `experiment_results/<方法名>.md`（例如 `PF.md`、`avgFL.md`、`clusterFL.md`）。

**單調遞增要求**：PF < avg FL + 最基礎DRL < cluster FL + 最基礎DRL < 自訂FL + 最基礎DRL < 自訂FL + 改良版DRL。Stage 2→3→4 只換 Global 聚合方式、Local 模型不變，用來單獨驗證「聚合策略」的貢獻；Stage 4→5 只換 Local reward 機制、Global 聚合不變，用來單獨驗證「改良版 DRL（Lagrangian）」的貢獻——每次只換一個變數，才能把進步歸因到正確的地方。

**關鍵設計決定（避免混淆）**：
* Stage 2~4 底層用的是**同一個**陽春 DRL（只差 FL 聚合方式），不是三種不同的模型。
* 「改進版 DRL」= 重新啟用現有的 Lagrangian 機制。**`REWARD_MODE` 開關已實作完成**（2026-09-12）：環境變數預設 `lagrangian`（維持現行行為，`inference_server.py` 呼叫 `reward_calculator.py::compute_lagrangian_reward()`，`R = R_tp + λ·(JFI_raw − JFI_MIN)`，λ 由 `drl_agent.py::train_on_batch()` 自適應更新）；設成 `throughput_only` 時改呼叫 `compute_reward_breakdown()`（純 throughput，`W_THROUGHPUT=1.0, W_FAIRNESS=0, W_DELAY=0`），且 `drl_agent.py` 會跳過 λ 更新（恆為 `LAMBDA_INIT=0.0`）。用法：`REWARD_MODE=throughput_only bash iab/run_local_pc1.sh`（12 個 `inference-nodeN` 服務的 `docker-compose-iab-server.yaml` 都已接上 `${REWARD_MODE:-lagrangian}`）。**切換 REWARD_MODE 前務必清空 MongoDB 經驗與模型 checkpoint**，reward 語意改變不能混在同一批訓練資料裡（沿用既有先例）。
* **不寫死、隨時可單獨跑任一 stage**：每個 stage 要能透過環境變數/CLI flag 獨立選擇（例如 `FL_MODE=none|avg|cluster|custom` + `REWARD_MODE=throughput_only|lagrangian`），不是「一定要照順序、前面沒做完後面就不能跑」的線性相依關係——不論開發進度到哪，都要能重跑任何一個 stage 的數據。
* Stage 3 的分群邏輯**已從硬性 4/8 二分群改為連續角色比例加權**（2026-09-13 討論定案，理由見下方 Node4 說明）。`inference/DRL_METHODOLOGY_PLAN.md` 記錄的舊 5-node 硬分群設計僅供架構參考，**不再是 Stage 3 要重新推導的目標**——分群理由本來是「relay 中繼下游流量、access 直接面對 UE，結構不同」，這個理由本身仍成立（relay 的下游是「其他有 DU 的節點」、access 的下游是「純 UE」），但硬性二分在 Node4 這種混合角色節點上會強迫模型偏向其中一種下游型態、犧牲另一種下游的吞吐量，改用連續角色比例後兩種下游都能被正確反映，且對更大規模拓樸（未來可能出現更多混合角色節點）不需要重新手動定義分群邊界，可直接推廣。
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

* **Stage 2（avg FL + 最基礎 DRL）**：**已完成**（2026-09-14 重測版，取代 2026-09-13 舊版本，見 `experiment_results/avgFL.md`）。
  - Global xApp 改版為全域公平性軟性廣播（取代舊版 5-node relay→access 配額裁切設計，理由與新舊設計差異見第 2 節元件定義），`global_xapp.py` 已重寫，`inference/flower-app/` 已針對 12-node 拓樸驗證（`FL_NUM_NODES=12`），`global_xapp_bridge.py` 已標記為過期但保留在磁碟（未經授權不刪除檔案）。
  - 沒有加「這次排程週期實際可用 PRB 數量／106 的比例」這個 state 特徵——評估後判斷 Global xApp 的 `fairness_bias` 已經是全域視角的間接訊號，優先級較低，`STATE_DIM=50` 維持不變。若後續 Stage 發現 Local DRL 對 backhaul 緊繃程度不夠敏感，可以再評估加回。
  - 啟動前已執行 MongoDB 經驗與模型 checkpoint 清空（`REWARD_MODE` 從 lagrangian 切到 throughput_only）。**踩坑記錄**：docker-compose 的 volume 有 `name:` 覆寫（例如 `inference_models_node1` 這個 compose 內部 key 實際對應到 Docker volume `iab-xapp-model-node1`），第一次清空時誤用 compose key 名稱掛載，建立了一個全新的空白同名 volume，實際的 checkpoint volume 完全沒被清到——之後全部用 `docker volume ls` 確認過實際名稱才修正。清空 volume 一定要先用 `docker volume ls`／`docker inspect` 確認實際名稱，不能直接套用 compose 檔裡的 service-local key。
  - 全網 JFI 目前只做監控（`global_xapp.py` 計算並印出 `global_jfi`，不寫回 Mongo、不納入聚合權重），`avgFL.md` 的 JFI 改善主要來自 Local DRL 感知 `fairness_bias` 後的行為調整，不是 Global rApp 聚合階段做了額外處理。
  - **2026-09-14 重測**：發現 `flower-supernode-nodeN`（FL 觸發訓練路徑）從未拿到 `REWARD_MODE` 環境變數、一直悄悄跑 `lagrangian`，加上另兩個會導致資料面隨機斷線的基礎設施問題（見下方 Stage 3 條目的三個 root cause 說明），修復後判定舊數據作廢、重新量測。重測版 JFI/吞吐量皆優於舊版本，RTT 仍未達成單調遞增，詳見 `avgFL.md`。

* **Stage 3（soft cluster FL + 最基礎 DRL）**：**已完成**（2026-09-14，見 `experiment_results/clusterFL.md`）。設計定案於 2026-09-13。
  - Local 模型架構跟 Stage 2 完全相同，只換 Global 聚合的分群方式；量測前已清空 MongoDB 經驗與模型 checkpoint。
  - **設計緣起（Node4 邊界案例）**：Node4 的 DU 同時中繼給 Node11/Node12（各帶 2 個 UE，共 4 個）、也直接服務 UE17，是唯一橫跨 relay/access 兩種下游型態的節點。若強制二選一硬分群，Node4 的模型會被拉去擬合單一下游型態、犧牲另一種下游的吞吐量表現，跟本階段「單調遞增總吞吐量」的驗收目標衝突；改用連續角色比例後不需要對混合節點做人工判定，機制本身也不寫死拓樸大小，可直接推廣到未來更大規模、更多混合角色節點的 IAB 樹。
  - **`role_ratio_i` 定義**（結構性，依拓樸算出、不需即時量測，可重現）：
    ```
    role_ratio_i = 直連 UE 數量 / (直連 UE 數量 + 透過下游 DU 節點間接服務的 UE 數量)
    ```
    現行 12-node 拓樸代入結果：Node1,2,3（純 relay，無直連 UE）= `0`；**Node4 = 1/5 = 0.2**（直連 UE17 共 1 個，加上經 Node11/12 服務的 UE13~16 共 4 個）；Node5~12（純 access）= `1`。已寫成 `server_app.py::ROLE_RATIO` 常數字典。
  - **聚合公式**（`n_i` 沿用既有 FedAvg 樣本數權重）：
    ```
    W_relay  = Σ_i (1 - role_ratio_i) · n_i · ΔW_i  /  Σ_i (1 - role_ratio_i) · n_i     (i ∈ 全部 12 節點)
    W_access = Σ_i    role_ratio_i    · n_i · ΔW_i  /  Σ_i    role_ratio_i    · n_i     (i ∈ 全部 12 節點)
    ```
  - **廣播公式**（每個節點依自己的 `role_ratio_i` 混合兩個原型，取代原本「整份模型歸某一群」的做法）：
    ```
    W_i(廣播回去) = (1 - role_ratio_i) · W_relay + role_ratio_i · W_access
    ```
    Node1~3 拿到近乎純 `W_relay`、Node5~12 拿到近乎純 `W_access`、Node4 拿到 `0.8·W_relay + 0.2·W_access` 的個人化混合，不需要二選一。硬性二分群是 `role_ratio_i∈{0,1}` 時的特例，論文方法論章節可註明本設計為 Clustered FL 的 soft/weighted 推廣版（類似 Multi-Center FL 的 soft assignment、或 APFL 的模型插值精神，插值對象換成 relay/access 兩個原型）。
  - **實作**：`server_app.py` 新增 `IABClusterFedAvg`（繼承 `IABFedAvg`，只覆寫 `aggregate_train()`，`aggregate_evaluate()` 沿用父類別），跟原本的 `IABFedAvg`（Stage 2）並存，用 `FL_MODE=avg|cluster` 環境變數切換（`main()` 依此決定 instantiate 哪個 strategy class），比照 `REWARD_MODE` 的既有模式；`min_train_nodes`/`min_evaluate_nodes` 等 Flower 參數維持對全部 12 節點的門檻（因為現在是全體節點都貢獻進兩個原型，不是子集分群，不需要拆成 4/8 兩組門檻）。
  - **`client_app.py` 有一個必要的最小改動，跟原本「完全不變」的假設不同**：`train()` 的回覆 metrics 多加一個 `node_id` 整數欄位——Server 端要依 `role_ratio_i` 分組加權，必須知道每筆回覆來自哪個實體節點，但 Flower 的 `Message` 內部 node id 是 SuperLink 指派的亂數、跟本專案的 `NODE_ID`（1~12）沒有已知對應關係，只能由 client 端自己在 metrics 帶出來。`evaluate()` 的回覆不受影響（跟分群無關）。
  - **正確性驗證**：獨立的純 Python 離線數學驗證腳本（不依賴 Docker/Flower，可直接測 `_weighted_average_flat()` 加權平均與混合廣播公式），涵蓋已知輸入下的正確性與邊界情況（某一側全部節點 `num-examples=0`、Node4 混合節點的廣播值介於兩原型之間且不等於任一純原型），全部通過。
  - **量測結果**：JFI=0.4163、17 UE 平均吞吐量 6.30 Mbps、平均 RTT 372.57 ms，三項皆**低於** avg FL（Stage 2）、尚未達成單調遞增要求；15 分鐘全程三主機零新增崩潰。**重要方法論限制**：量測全程 FL 觸發訓練幾乎每輪都因經驗數量不足（`training_pipeline.py` 需要 200 筆連續原始經驗）而跳過聚合，`IABFedAvg`／`IABClusterFedAvg` 在單次 15 分鐘視窗內幾乎沒有機會做出有意義的非零權重聚合——Stage 2/3 觀察到的差異主要反映 12 個節點各自獨立訓練軌跡的隨機變異，不能直接歸因為聚合演算法本身有問題（聚合公式已經過離線數學驗證）。為何落後、後續怎麼調整（拉長量測視窗／降低訓練門檻等）待下一輪討論，詳見 `clusterFL.md`。
  - **上線過程額外定位並修復三個獨立的基礎設施 root cause**（完整過程見 `HISTORY.md` 2026-09-14 條目，這裡只列結論，之後遇到「容器隨機崩潰／連線隨機斷線」都應先照第 7 節「容器隨機崩潰排查指南」排查，不要重新從頭摸索）：
    1. `flower-supernode-nodeN` 從未拿到 `REWARD_MODE` 環境變數，一直悄悄跑 `lagrangian`——已補上 `REWARD_MODE: "${REWARD_MODE:-lagrangian}"`（同 `inference-nodeN` 寫法）。
    2. `start_iab_pc2.sh` 對 CU 的 NAT OUTPUT table 做無條件整批 flush，若時序上晚於其他主機寫入自己的 DNAT 規則，會把那些規則整批砍掉——三個 `start_iab_*.sh` 腳本全部改用 `iptables -t nat -I OUTPUT 1 ...`（插入不清空），並加上 `verify_and_heal_ues()` 自我修復迴圈。
    3. 誤把 Global xApp/Flower FL 共 15 個服務也加上跟 `inference-nodeN` 相同的 `cpuset: "12-15"`，27 個 process 塞 4 核心造成 `rfsim5g-donor-cu` 隨機崩潰——已移除，只保留原本 12 個 `inference-nodeN` 的設定。

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
* `/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator/docker-compose-iab-server.yaml`：for PC 1（CN5G、Donor CU/DU、FlexRIC、MongoDB、全部 12 組 xapp-nodeN + inference-nodeN 容器、**全部 4 個 relay：Node1,2,3,4（Node4 含直連 UE17 的 DU）**，由 `iab/run_local_pc1.sh` → `iab/start_iab_server.sh` 啟動）
* `/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator/docker-compose-iab-pc2.yaml`：for PC 2（Node5,6,7,8 access + UE1~8，parent relay 跨主機在 PC1，由 `iab/run_local_pc2.sh` → `iab/start_iab_pc2.sh` 啟動）
* `/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator/docker-compose-iab-pc3.yaml`：for PC 3（Node9,10,11,12 access + UE9~17，parent relay 跨主機在 PC1，UE17 容器也在這裡，由 `iab/run_local_pc3.sh` → `iab/start_iab_pc3.sh` 啟動）
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

**建議做法（2026-09-14 驗證更穩定）：完全依序啟動，不要三台同時跑**——先讓 PC1 的基礎設施（不含 E2 等待、不含 xApp 啟動）單獨跑完，再依序（不要同時）跑 PC2、PC3，最後回 PC1 做 E2 等待＋啟動 xApp：

```bash
# 1. PC1 基礎設施（CN5G/FlexRIC/MongoDB/Donor CU-DU/Node1,2,3,4 relay），跑完才繼續下一步
bash ~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator/iab/start_iab_server.sh

# 2. PC2 完全跑完，才換 PC3（不要背景同時跑兩台）
bash ~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator/iab/run_local_pc2.sh
bash ~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator/iab/run_local_pc3.sh

# 3. 回 PC1：等待 13/13 E2、啟動全部 xApp
bash ~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator/iab/run_local_pc1.sh --skip-server
```

**舊式三台同時跑的寫法**（`run_local_pc1.sh`（完整版）+ PC2/PC3 同時執行）理論上也能動，且過去多次量測都是這樣起的，但 2026-09-14 debug 過程中觀察到三台完全同時起跑時偶爾會出現 CU/DU 隨機崩潰（根因見第 7 節「容器隨機崩潰排查指南」第 1、2 點，不是這個同時啟動的寫法本身的 bug），目前**沒有辦法**100%排除同時啟動時的競爭條件，所以正式量測前建議一律用上面的依序寫法。

驗證：`docker logs flexric 2>&1 | grep -c "E2 SETUP-REQUEST"` 應為 `13`（1 donor + 12 node）；`docker inspect --format '{{.RestartCount}}' rfsim5g-donor-cu` 應為啟動前的原值（沒有新增崩潰）。**每次啟動或切換 stage 後、跑 15 分鐘量測前，一定要先對全部 17 個 UE 做一次現場 `docker exec <container> ping -c 2 <ext-dn-IP>` 確認 0% 封包遺失**——這是低成本前置檢查，能在花 15 分鐘量測之前就抓到連線缺陷，長時間偵錯累積的手動介入也可能讓個別 UE 處於「容器存活但資料面斷線」的狀態，靠繼續 debug 往往找不到，乾淨重啟（依上面的依序寫法）通常是最快的解法；若同一個節點反覆發生一樣的連通性問題（乾淨重啟後還是壞），才需要深入排查是否有真正的程式碼或設定 bug（見第 7 節）。

**⚠️ 每次乾淨重啟後、啟動任何 `traffic_scenario.py`（不管是量測用的一次性呼叫，還是 `training_scenario_driver.sh` 的訓練用長駐呼叫）之前，必須先在 PC1 執行 `bash scenarios/setup_iperf_servers.sh`**：這支腳本在 `rfsim5g-oai-ext-dn` 容器裡啟動 17 個各自獨立的 iperf3 server（port 5201~5217，一個 UE 一個 port）。`start_iab_server.sh` 自己內建的 `docker exec -d rfsim5g-oai-ext-dn iperf3 -s`（無 `-p` 參數，只監聽預設的 5201）**不是這支腳本的替代品**——用預設埠的單一 server 只能服務到剛好對應 5201 的那個 UE（依現行對照即 UE1），其餘 16 個 UE 的 iperf3 client 會持續 `connection refused` / `rc=1` crash-loop，且是靜默失敗（scenario log 只會印 `WARNING iperf3 supervisor 退出...重啟 loop`，不會讓整個腳本報錯、也不會讓 UE 的 ping 連通性檢查失敗），非常容易在乾淨重啟時被忽略，讓訓練或量測在「看起來正常運作」的情況下，實際上只有 1/17 UE 真正產生流量、其餘節點的 MAC 層狀態近乎閒置——訓練跟量測都會失去意義（現場案例見 `HISTORY.md` 2026-09-20 條目）。`training_watchdog.sh` 的 `full_recovery()` 目前**沒有**自動呼叫這支腳本，是已知缺口，之後排查「崩潰復原後訓練資料看起來正常但品質不對」時應優先檢查這裡。

### 啟動 Stage 2 起的 Global 層（avg FL / cluster FL / 自訂 FL）
```bash
# 前置：三主機 RAN 基礎設施（run_local_pc{1,2,3}.sh）已就緒、13/13 E2、12/12 xApp
REWARD_MODE=throughput_only bash ~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator/iab/run_stage2_fl.sh
```
這支腳本會停止並清空 12 個 `inference-nodeN` 的 MongoDB 經驗與模型 checkpoint、用指定的 `REWARD_MODE` 重新啟動、並帶起 `global-xapp`／`flower-superlink`／`flower-supernode-node{1..12}`／`flower-scheduler`（`profiles: ["stage2-fl"]`，Stage 1 PF 重跑時不受影響）。Stage 3/4 只需改 `server_app.py` 聚合邏輯後直接重跑；Stage 5 改用 `REWARD_MODE=lagrangian`。

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

**State vector 第 49 維（`fairness_bias`，Stage 2 起改版，取代舊版 2026-07-09 的 `prb_quota_ratio`）**：

```
FAIRNESS_BIAS_MIN = 0.5（drl_agent.py，Global xApp 廣播值域下限）
FAIRNESS_BIAS_MAX = 2.0（drl_agent.py，Global xApp 廣播值域上限）
GLOBAL_XAPP_INTERVAL_S = 2.0（global_xapp.py，每幾秒重算一次全部節點的 fairness_bias，預設值）
GLOBAL_XAPP_LOOKBACK = 50（global_xapp.py，計算節點平均吞吐量時往回看幾筆經驗，預設值）
```

`state_vec[49] = (clip(fairness_bias, FAIRNESS_BIAS_MIN, FAIRNESS_BIAS_MAX) - FAIRNESS_BIAS_MIN) / (FAIRNESS_BIAS_MAX - FAIRNESS_BIAS_MIN)`，正規化到 `[0, 1]`。`state_vec[48]` = active_ratio（`n / MAX_UE_COUNT`）不受影響。

**目前 reward 現況**：`REWARD_MODE` 環境變數控制（`docker-compose-iab-server.yaml` 全部 12 個 `inference-nodeN` 服務接上 `${REWARD_MODE:-lagrangian}`）。預設 `lagrangian`：`inference_server.py` 呼叫 `reward_calculator.py::compute_lagrangian_reward()`（`R = R_tp + λ·(JFI_raw − JFI_MIN)`，`JFI_MIN=0.8291`，λ 由 `drl_agent.py` 自適應更新，範圍 `[0, LAMBDA_MAX=10.0]`）。設成 `throughput_only`（Stage 2~4 使用）：改呼叫 `compute_reward_breakdown()`（純加權和版本，`W_THROUGHPUT=1.0, W_FAIRNESS=0.0, W_DELAY=0.0`），`drl_agent.py` 跳過 λ 更新（恆為 `LAMBDA_INIT=0.0`）；此路徑產生的 MongoDB 經驗文件**不含** `lambda_applied` 鍵（`_build_rl_experience()` 已改為條件式寫入，避免 `KeyError`）。

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

### 容器隨機崩潰／連線隨機斷線排查指南（2026-09-14 定案）

如果三主機乾淨重啟後，`rfsim5g-donor-cu`／DU／MT **隨機**崩潰或重啟（`docker inspect --format '{{.RestartCount}}'` 不斷增加，且每次壞掉的節點都不一樣、跟「哪個腳本最後跑完」看起來沒有固定關係），依照以下順序排查，不要一開始就假設是既有 RAN C code 的問題：

1. **先檢查是不是 `cpuset` 把太多 process 塞進同一組核心**：`docker-compose-iab-server.yaml` 目前只有全部 12 個 `inference-nodeN` 服務釘在 `cpuset: "12-15"`（4 核心，Stage 2 上線時就有的既有設計，長期驗證穩定）。**任何要新增 cpuset 隔離的容器（例如 Global xApp／Flower FL 服務），都不能無條件套用同一組 `"12-15"`**——2026-09-14 debug session 曾經把 `global-xapp`／`flower-superlink`／12 個 `flower-supernode-nodeN`／`flower-scheduler`（共 15 個服務）也全部加上 `cpuset: "12-15"`，變成同一組 4 核心要塞 27 個 process，即使瞬時 CPU 使用率看起來不誇張（`mpstat` 平均只有 30% 上下），仍然造成偶發性的排程延遲尖峰，讓 RT-priority 的 CU/DU SCTP/RRC 計時器偶爾錯過期限而 crash-restart，進而讓所有依賴 CU 當下 netns 的 DNAT/路由設定失效——這正是「不管怎麼修 NAT/路由、過一陣子又壞、每次壞的節點都不一樣」這種難以定位症狀的真正源頭。**排查方法**：`docker inspect --format '{{.HostConfig.CpusetCpus}}' <container>` 列出目前所有容器的 cpuset 分組，數一數同一組核心裡總共塞了多少 process，跟核心數（`nproc`）比較是否明顯失衡；懷疑是這個原因時，直接把新加的 cpuset 限制拿掉做 A/B 對照（拿掉後乾淨重啟，觀察 CU RestartCount 是否維持 0），比繼續往下挖 NAT/路由邏輯快得多。
2. **確認 CU 的 NAT OUTPUT table 沒有被意外整批清空**：`iab/start_iab_pc2.sh`／`start_iab_pc3.sh`／`start_iab_server.sh` 的 DNAT 規則寫入邏輯全部使用 `iptables -t nat -I OUTPUT 1 ...`（插入到最前面），**不會**、也**不能**對整條 `OUTPUT` chain 做 `-F` 全部清空——早期版本 PC2 的腳本會在自己的收尾步驟對 CU 的 NAT OUTPUT table 做無條件 `-F`，只要這個 flush 跑在其他主機已經寫好自己節點 DNAT 規則**之後**，就會把其他主機的規則整批砍掉，且沒有人會補回來，造成跟第 1 點類似的「隨機哪個節點斷線」症狀（但成因完全不同：這個是規則被砍，第 1 點是 CU 本身真的 crash）。三個腳本現在都已改成「不 flush、只在最前面插入」，若未來又看到類似症狀，先確認相關腳本有沒有被還原成舊版的 flush 寫法。
3. **確認三主機啟動順序**：即使上述兩點都排除，仍建議依序（不要三台完全同時）執行：先跑 `bash iab/start_iab_server.sh`（只做 PC1 基礎設施＋Node1,2,3,4 relay，不等 E2、不啟動 xApp）→ 等它完全跑完 → 依序（不要同時）跑 PC2、PC3 各自的 `run_local_pc{2,3}.sh` 並各自等到完全跑完 → 最後跑 `bash iab/run_local_pc1.sh --skip-server`（只做 E2 等待＋啟動 xApp）。這個順序讓三主機的 CU 操作完全不重疊，實測比三台同時起跑更穩定（2026-09-14 驗證：依序啟動後，15 分鐘正式量測全程 CU/DU/MT RestartCount 維持 0）。
4. **自我修復機制**：三個 `start_iab_*.sh` 腳本收尾都有 `verify_and_heal_ues()`（PC1 2026-09-22 後不再有任何 UE，改成 `heal_ra_exhaustion_local()` 只檢查 4 個 relay DU），會對自己負責的 UE 做 ping 驗證，失敗時自動重新斷言 MT 路由＋CU DNAT 規則，最多重試 5 次（每次間隔 15 秒）。若腳本印出「重試 5 次後仍有 UE 連不通，需要人工檢查」，通常代表當時 CU 或該節點的 DU/MT 剛好處於第 1、2 點描述的不穩定狀態，等它穩定後手動重跑一次 `docker exec -u 0 <mt> ip route replace ...`＋CU 端 `iptables -t nat -I OUTPUT 1 ...`（或直接重跑該主機的腳本）通常就會通。

---

## 8. 流量與路徑損耗場景設計

`scenarios/traffic_scenario.py` 提供多種流量場景，透過 `channelmod_ctrl.py` 對 OAI rfsimulator 的 telnet chanmod 介面（`channelmod modify <ue_id> ploss <val>`，即時生效不需重啟容器）動態調整每個 UE 的路徑損耗，模擬通道劣化環境。

**場景清單**：A（CQI 差異化）、B（流量不均）、C（最差公平性）、D（動態訓練，均勻隨機）、**R（真實隨機，推薦）**。Scenario R 用面積均勻抽樣模擬細胞邊緣 UE 較多、lognormal 重尾分佈模擬真實流量需求（多數適中、少數高需求）、間歇閒置機率模擬 burst→idle→burst 使用型態、持久化的每 UE 使用者 profile（heavy-streaming/light-browsing/bursty-iot）避免每個 phase 完全獨立同分布、TCP/UDP 協定混合，並支援 `--seed` 重現同一串隨機條件供 PF vs DRL 的 paired comparison 使用。

路徑損耗安全上限 `PATHLOSS_SAFE_MAX_DB=25.0`（超過會讓 UE 斷線，已現場驗證）。Scenario R 直接送連續 ploss 值，不需要 CQI 校正；Scenario A/B/C/D 需要（`--calibrate --node N`）。

**跨主機執行**：telnet chanmod port 原則上只在該主機本機（`127.0.0.1`）可連，`traffic_scenario.py` 用 `--host {pc2,pc3}` 各自在本地執行、只套用自己負責的 UE/Node 子集；兩台主機用同一個 `--seed`，每個 UE 每個 phase 的隨機值用 `(seed, ue_global_id, phase_index)` 三元組獨立導出（`phase_index` 以絕對時間換算），不需要跨主機即時通訊即可保持同步。**唯一例外是 UE17**（2026-09-22 起）：它的容器在 pc3、但邏輯上掛的 Node4 telnet 在 pc1，`build_controllers()` 用 `NODE_TELNET_HOST_OVERRIDE` 讓 pc3 這邊跨主機連 `192.168.88.1:9092`（見第 1 節）——PC1 不需要、也不會執行這支腳本（PC1 沒有任何 UE 容器）。

```bash
# PC2：控制 UE1~8
python3 scenarios/traffic_scenario.py --scenario R --seed 42 --host pc2

# PC3：控制 UE9~17（含 UE17）
python3 scenarios/traffic_scenario.py --scenario R --seed 42 --host pc3
```
