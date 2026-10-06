# 碩士論文實驗流程與環境配置配置指南 (O-RAN IAB 架構)

> **文件慣例**：本檔案只保留「現在式」的架構事實與可執行指令。任何帶日期的踩坑過程、除錯敘事、歷史量測數據表，一律寫進 `HISTORY.md`（同層目錄），不要寫回這裡。
>
> **系統設計決定與依據**（頻段模式、backhaul 頻寬、agent 範圍、控制動作、基準場景、評估規則等）以 `SYSTEM_SPEC.md` 為唯一依據；2026-10-01 起草，第 11 節決策清單待逐項定案。

## 1. 實驗實體環境與網路拓撲設置

採用**三主機 (Tri-host) 實體部署**，模擬真實 O-RAN IAB 網路中的實體隔離與傳輸延遲，拓樸為 **1 donor + 4 relay + 8 access + 24 UE** 的對稱樹狀結構：UE1~16 掛在 access 節點（3 跳），**UE17~24 直接掛在 relay（2 跳，每個 relay 2 個，2026-10-01 起）**。底層 IAB 架構採用 **MT + DU 串接模式**（無 BAP 層），所有跨節點傳輸皆透過標準 5G Uu 介面與 Linux IP Routing 進行轉發。三台主機透過各自的 USB3.0→RJ45 轉接卡共接同一台 switch，組成單一 `192.168.88.0/24` L2 網段（見 `iab/setup_lab_net.sh`）。

### 拓樸與節點編號

```
Donor (PC1)
├── Node1 (relay, PC1) ── Node5 (access, PC2) ── UE1, UE2
│                     ├── Node6 (access, PC2) ── UE3, UE4
│                     └── UE17, UE18（relay 直連，PC1）
├── Node2 (relay, PC1) ── Node7 (access, PC2) ── UE5, UE6
│                     ├── Node8 (access, PC2) ── UE7, UE8
│                     └── UE19, UE20（relay 直連，PC1）
├── Node3 (relay, PC1) ── Node9  (access, PC3) ── UE9,  UE10
│                     ├── Node10 (access, PC3) ── UE11, UE12
│                     └── UE21, UE22（relay 直連，PC1）
└── Node4 (relay, PC1) ── Node11 (access, PC3) ── UE13, UE14
                      ├── Node12 (access, PC3) ── UE15, UE16
                      └── UE23, UE24（relay 直連，PC1）
```

Donor→Relay→Access→UE 為 3-hop，Donor→Relay→UE（relay 直連 UE17~24）為 2-hop。**全部 4 個 relay（Node1~4）現在都跟 Donor 同機（PC1）**，Donor→Relay 這一段變成本機內部通訊；Relay→Access 這一段（全部 8 個 access 節點）則全部是跨主機連線（PC1→PC2/PC3），透過三台共用的 macvlan L2 網段，不需要額外設定。

> **拓樸設計原則**：全部 4 個 relay 集中在 PC1（跟 Donor 同機），access 節點平均分散到 PC2/PC3，讓四條分支的路徑結構完全一致（Donor→Relay 全部同機、Relay→Access 全部跨主機），避免「跟 Donor 同主機的分支吞吐量系統性領先」的量測 confound（2026-09-22 由舊分布搬遷而來，動機、逐 UE 證據與搬遷過程見 `HISTORY.md`、`experiment_results/PF.md`）。即時 RF process 數約 **PC1:18（含 8 個 relay 直連 UE）、PC2:16、PC3:16**；relay 直連 UE 放 PC1 是因為 PC2/PC3 已是 CPU 瓶頸（2026-10-01 承載試驗：24 UE 全負載三台 CPU 最低閒置約 56／72／71%，所有 DU 維持設定速度，見 `HISTORY.md` 續五十二～五十三）。
>
> **relay 直連 UE（UE17~24，2026-10-01 起）**：讓 relay 的 DU 同時服務子節點 MT（backhaul）與自己的 UE（access），relay 也有排程決策可學（`SYSTEM_SPEC.md` §6.4）。容器在 PC1、compose profile `relay-ue`，由 `iab/start_relay_ues.sh` 在 13/13 E2 之後依序啟動（`run_local_pc1.sh` Step 2.5；relay DU 上 MT 先連＝ue_id 0,1、UE＝2,3）；`RELAY_UES=0` 可回到 16 UE 拓樸（啟動腳本、量測工具、watchdog 都支援）。**relay UE 的場景角色**：主場景 HS／HSH／G（`scenario_hs()`）同時決定 access UE 與 relay UE 的需求與下行通道（relay 層與 access 層同時壅塞）；其他舊場景（T／TH／TM 等）經 `relay_ue_configs()` 讓 relay UE 閒置（L=10），PB／PU 為 relay 機制試驗的固定配置。2026-10-01 之前的量測都是 16 UE（2026-09-30 前為 17 UE），不可與之後直接比較。

### 硬體與節點配置表

| 實體主機 | 部署元件 | 網路角色 | 備註說明 |
| :--- | :--- | :--- | :--- |
| **PC 1** (192.168.88.1, lindor) | CN5G、FlexRIC Server、MongoDB、Donor CU/DU、**全部 12 組** `xapp-nodeN`+`inference-nodeN` 容器、**Node1,2,3,4 (relay)**、**relay 直連 UE17~24** | 核心網與全域控制中心 + 全部 relay 層 | xApp(C)+inference(Python) 刻意集中在 PC1（見下方理由），MongoDB 對外監聽 `27017`。4 個 relay 的 CU 端指令（路由）都是本機直接執行，不需要 SSH（見 `iab/start_iab_server.sh` 的 `configure_and_start_local_relay`）。 |
| **PC 2** (192.168.88.2, **mcalab**) | Node5,6,7,8 (access) + UE1~8 | RAN 資料面 | 純資料面，無 xApp/inference 容器。Ubuntu 20.04。全部 4 個 access 節點的 parent relay（Node1,2,3,4）都在 PC1，跨主機連線。 |
| **PC 3** (192.168.88.3, lindor) | Node9,10,11,12 (access) + UE9~16 | RAN 資料面 | 純資料面，無 xApp/inference 容器。Ubuntu 24.04。這 4 個 access 節點的 parent relay（Node3,4）在 PC1，跨主機連線。 |

**xApp/inference 集中在 PC1 的理由**：xApp(C) 與 inference(Python) 之間走 `ipc://` Unix domain socket（見第 2 節），兩者必須同一台主機；若要「真正分散到 PC2/PC3」，ZMQ 要改走 TCP，5ms timeout 預算要多扛一段跨主機網路延遲，風險換來的好處不大——經評估後決定維持集中，`MONGO_URI` 因此不需要因為主機數增加而修改（所有 inference 容器仍是 `mongodb://localhost:27017`，因為它們仍跟 MongoDB 同一台主機）。RAN 節點（MT/DU/UE）則物理分散到 PC1（全部 relay）/PC2/PC3（access+UE），relay 與其 access 子節點必定跨主機（2026-09-22 起，見上方拓樸圖）。

### IP / ID 配置表

| 項目 | Donor | Node1~4（relay） | Node5~12（access） | UE1~24 |
|---|---|---|---|---|
| `gNB_ID`/`gNB_DU_ID` | `0xe00` | `0xe01`~`0xe04` | `0xe05`~`0xe0c` | — |
| `nr_cellid` | `12345678` | `12345679`~`12345682` | `12345683`~`12345690` | — |
| `physCellId` | `0` | `1`~`4` | `5`~`12` | — |
| E2 `TARGET_NODE_ID`（xApp .c，= gNB_ID 十進位） | — | `3585`~`3588` | `3589`~`3596` | — |
| `rfsimulator.serverport` | `4043` | `4044`~`4047` | `4048`~`4055` | — |
| FlexRIC telnet debug port（chanmod 通道控制） | — | `9089`~`9092`（本機用 `127.0.0.1`） | `9093`~`9100` | — |
| IMSI (`208990100001xxx`) | — | 尾碼 `100`~`103` | 尾碼 `104`~`111` | 尾碼 `200`~`223`（UE1~16＝200~215、relay 直連 UE17~24＝216~223；跟 MT 區段刻意拉開） |
| macvlan IP | `.144`（DU）| Node1=`.150`,Node2=`.151`,Node3=`.152`,Node4=`.153`（全部在 PC1）—— MT/DU 共用同一 netns、同一 IP，這個位址不因實際跑在哪台主機而改變（三台共用同一個 macvlan L2 網段） | Node5=`.160/.161`,Node6=`.162/.163`,Node7=`.164/.165`,Node8=`.166/.167`（PC2）; Node9=`.168/.169`,Node10=`.170/.171`,Node11=`.172/.173`,Node12=`.174/.175`（PC3） | tunnel IP 動態（`12.1.1.0/24` SMF pool）；relay 直連 UE17~24 的 macvlan IP＝`.176`~`.183` |
| internal bridge IP | — | 不需要（relay 的 MT/DU 共用 netns，沒有獨立位址） | **PC2 用 `192.168.74.0/24`**：Node5=`.10/.20`,Node6=`.11/.21`,Node7=`.12/.22`,Node8=`.13/.23`；**PC3 用 `192.168.75.0/24`**：Node9=`.10/.20`,Node10=`.11/.21`,Node11=`.12/.22`,Node12=`.13/.23` | — |

> **各主機 internal bridge 子網刻意不同**（`.74.0/24`／`.75.0/24`）：PC1 的路由表對同一個子網只能指到一個 next-hop，若多台主機共用同一子網，PC1 就無法同時正確路由到不同主機的 access node internal IP。internal bridge 網路是 access 節點自己的 host-local 網路，只跟「access 節點實際跑在哪」有關，跟它的 parent relay 跑在哪台主機無關。PC1 只有 relay（MT/DU 共用 netns），不需要 internal bridge 網路。

CN5G（`.131`~`.134`）、FlexRIC（`.141`）全部在 PC1。

新增 IMSI 若落在既有 `100`~`223` 範圍以外，記得同步在 `oai_db.sql`／執行中的 `rfsim5g-mysql` 補 `INSERT INTO users`，否則 UE/MT 會收到 `FGS_REGISTRATION_REJECT`（過程見 `HISTORY.md`）。

---

## 2. 軟體架構與 O-RAN 職責映射

本系統跨越 C 語言的底層通訊與 Python 的 AI 推論，構築實體隔離的「Local / Global 雙層階層式控制平面」。

* **底層協議棧 (C 語言)**：使用 OAI (OpenAirInterface) 實作 DU/CU 與 UE。
* **Near-RT RIC (C 語言)**：使用 FlexRIC 作為 E2 代理伺服器與 xApp 框架。
* **Non-RT RIC & AI (Python)**：規劃透過 Flower Framework 進行階層式聯邦學習 (Hierarchical FL)，並透過 ZeroMQ 建立跨語言 IPC 通訊。

> **目前實作現況（2026-09-30）**：舊版（GRU/MLP＋連續 Dirichlet 動作空間＋FedAvg/CAPA-Fed）從未
> 真正贏過 PF baseline，2026-09-29 決定全面重新設計。**Local DRL v2（`MODEL_ARCH=mlp`）程式碼已實作、
> 離線單元測試通過，尚未在平台上收資料/訓練**（設計見 `inference/LOCAL_DRL_V2_DESIGN.md`：PF 初始化＋
> 5 檔離散上限＋全部 UE 共用 Actor 頭＋PF-shadow 資料預訓練 Critic＋relational state）；
> `MODEL_ARCH=gru` 仍是舊版 Dirichlet 設計。Stage 3/4 Global FL（`STAGE4_CUSTOM_FL_DESIGN.md`
> §10／§11）**尚未實作**，Stage 2 的 Global 層（FedAvg）維持不變。舊版 Stage 2/3 的量測結果（`avgFL.md`／`stage3_capaFed.md`）與對應 MongoDB 經驗/
> checkpoint 已全部清空刪除（過程見 `HISTORY.md` 續三十六）。Global 層服務（`global-xapp`／
> `flower-superlink`／`flower-supernode-node{1..12}`／`flower-scheduler`）在
> `docker-compose-iab-server.yaml` 掛 `profiles: ["stage2-fl"]`，Stage 1 PF 重跑時不受影響。
> 舊版 5-node relay→access 配額裁切設計、舊版 Stage 3 soft cluster FL、舊版 Stage 4 AW-FedAvg、
> 舊版 CAPA-Fed／ERA-Fed 皆已整段移除或移至附錄（細節見 `HISTORY.md`），不是需要復原的架構。

### 核心控制元件定義

1. **Local xApp**：
   * **實作**：純 C 語言 FlexRIC 程式，每個 Node 各自獨立（`xapp_node1.c` ~ `xapp_node12.c`）。
   * **職責**：局部控制迴圈（**實際週期 1 秒**：xApp 向 E2 訂閱 E2SM-MAC 回報的週期是 100ms（`xapp_nodeN.c` 的 `"100_ms"`），Rate Limiter 每 10 次回報才觸發一次 ZMQ，所以每筆狀態的 delta 是 ~1 秒累積、實測 MongoDB 每節點 1 筆/秒；2026-09-26 前文件誤寫成 100ms/10ms，避免 FlexRIC pending event queue 滿載崩潰）。只負責 PRB 分配這一個動作：透過 E2SM-MAC 擷取所屬 Node 的 **Δ DL TBS、MCS、DL Buffer Occupancy** 作為狀態（`dl_aggr_tbs` 差分值為吞吐量代理、`dl_mcs1` 為通道品質代理、`dl_buffer_info` 為不受排程與否影響的需求代理——注意 OAI RF Simulator 的真 3GPP `wb_cqi` 恆為 0，是模擬器本身不計算真實通道傳播的限制，`dl_buffer_info` 則沒有這個限制），將 JSON 狀態透過 ZeroMQ REQ 送給 Local rApp Python 端，收回 PRB 權重陣列後立即寫回 OAI MAC 層。C 語言端不含任何 AI 邏輯。 **控制下發只發生在 ZMQ 週期（每 1 秒一次）**：OAI MAC 會保存最後一次控制（`nrmac->xapp_2d_ctrl`，無過期機制），所以其餘 9 個 callback 不重送（2026-09-26 移除，原本每個 xApp ~10 個 CONTROL-REQUEST/秒（1 次真實＋9 次重送）、12 個合計 ~120/秒；現在 1 次/秒、合計 ~12/秒（實測 xapp-nodeN 近 60 秒 60 筆；先前誤記為 100/1200 倍），每個請求在 FlexRIC 建一個 3 秒逾時計時器）；**ZMQ 失敗進入 fallback 時只送一次「解除上限」控制（`prb_quota=1.0`）**，讓 MAC 真正退回 PF（否則最後一次的 DRL 上限會永遠生效）。**動作語意（2026-10-01 起預設為時域遮罩）**：JSON 每個 UE 帶 `prb_abs`（PRB 上限）與選填的 `slot_mask`（16 位元，第 k 位元＝frame 內 slot%16==k 可排程，xApp 讀取後經 E2 寫入 `nrmac->xapp_2d_ctrl.slot_mask`）。**這個平台上頻域 PRB 上限無法重新分配資源**：OAI 每個 UE 每秒最多約 430 次傳輸（短 PUCCH 每 occasion 最多 2 個 ACK 位元，約 2/3 的下行 slot），限制一個 UE 的 PRB 後它仍每個 slot 搶排程，其他 UE 撞到自己的次數上限、省下的 RB 閒置（場景 P 實測 −10%）；時域遮罩讓其他 UE 在被讓出的 slot 獨佔整個頻寬才有效（+4~13%，見 `HISTORY.md` 續四十六～四十九）。
2. **Local rApp**：
   * **實作**：Python ZeroMQ REP 伺服器（`inference_server.py`，部署於 12 個獨立容器，全部在 PC1）。
   * **職責（雙重角色）**：
     * **Near-RT 推論（毫秒級）**：接收 Local xApp 的 ZeroMQ 請求，執行 DRL Actor 網路 forward pass，回傳 PRB 權重陣列，並將 State/Action/Reward 非同步寫入 MongoDB。
     * **Non-RT Fine-tuning（秒/分鐘級）**：從 MongoDB 讀取歷史資料，執行本地模型微調（`inference_server.py` 背景訓練執行緒 `_train_worker`，每 60 秒一輪），每輪讀取 MongoDB **最新**的 `TRAIN_FETCH_LIMIT=5000` 筆經驗（每節點 ~1 筆/秒 → ≈83 分鐘）做 10 次梯度更新；Actor 學習率 `DRL_LR_ACTOR`=3e-4（6 小時只有 ~3600 步）；存檔前先檢查磁碟 checkpoint 是否已被 FL 更新，若是則改載入 FL 權重、不覆寫（`_save_unless_superseded()`）。
     * **MLP 分支的信用分配（2026-09-26，`drl_agent.py`）**：γ=0（`DRL_GAMMA_MLP`）；advantage = clip((r−V(s))/獎勵滾動標準差, ±3)，不做每批 z-score；**只用「壅塞」樣本更新 Actor**（**決策當下**任一活躍 UE 的 RLC 佇列 `dl_buffer_info` ≥ `CONTENDED_BUF_BYTES`=100000 bytes 且 ≥2 個活躍 UE；2026-09-30 起不再看結果狀態 s′——s′ 受動作影響，用它篩選會讓 policy gradient 有選擇偏差；批次內壅塞樣本 <8 筆就跳過 Actor 更新），非壅塞樣本只訓練 Critic。理由：不壅塞時獎勵與 PRB 分配無關，舊版 z-score 會把純雜訊放大成隨機遊走。門檻 2026-09-26 用真實 1 秒視窗資料校準（門檻 100k：正常相位誤判 13.7%、壅塞相位命中 65%；50k 為 24%/74%、200k 為 8.7%/54%）；訓練日誌 `contended=xx%` 應約 35~45%，偏離很多時以環境變數調整。GRU 分支維持舊算法。 **離策略校正（PPO 式比例裁剪）**：推論時把當時的 log π(a|s) 存進經驗（`behavior_logp`），訓練時 ρ=exp(logπ新−logπ舊) 裁在 [1−ε,1+ε]（`DRL_PPO_CLIP_EPS`=0.2）；沒有 `behavior_logp` 的經驗（BSR 啟發式階段）只訓練 Critic、不更新 Actor，FedAvg 權重（壅塞樣本數）也同樣只計有 `behavior_logp` 者。 **Local DRL v2（`MODEL_ARCH=mlp`，2026-09-30）**：動作改為每 UE 從 5 檔選一檔（per-UE Categorical，存 `action_tiers`）；**2026-10-01 起 5 檔是時域遮罩**（`DRL_ACTION_DOMAIN=mask`，`MASK_TIERS=(0x0101,0x1111,0x9292,0xab98,0xFFFF)`，每檔都有平台實測、被限制 UE 的吞吐量隨檔位單調；最後一檔全開＝PF；`DRL_ACTION_DOMAIN=prb` 退回舊版 PRB 上限），Actor 是全部 UE 共用的小 MLP 頭、初始化≈PF，Critic 是排列不變的 DeepSets 架構，**從第一步就用 DRL 推論、不再有 BSR 啟發式暖身**，推論失敗退回 PF（不設上限）；上面的壅塞遮罩/PPO 裁剪/固定尺度 advantage 沿用，Dirichlet 相關描述只剩 GRU 分支適用，細節見 `inference/LOCAL_DRL_V2_DESIGN.md`。 **切換門檻（GRU 舊分支）**：訓練步數（含 FL 客戶端）≥`DRL_MIN_TRAIN_STEPS`=100 才從 BSR 啟發式切到 DRL；**執行緒**：每個 process 限 1 個 torch/BLAS 執行緒（`TORCH_NUM_THREADS`、映像 `OMP_NUM_THREADS=1`），12 個節點訓練起始依 node_id 錯開 ~5 秒（12 個 inference＋12 個 FL ClientApp 共用 cpuset 12-15，不限執行緒會超額訂閱、拉長推論延遲到超過 xApp 5ms 逾時）。**FedAvg 權重**：FL 客戶端回報的 `num-examples` = 可更新 Actor 的壅塞訓練樣本數（≥2 個活躍 UE 且佇列達門檻），不是全部訓練樣本數；單 UE 節點與無壅塞節點權重為 0（仍會收到聚合後的權重）。
3. **Global xApp（全域公平性軟性廣播，Stage 2~5 全程固定存在）**：
   * **實作**：獨立 Python process（`global_xapp.py`），每 `GLOBAL_XAPP_INTERVAL_S`（預設 2 秒）讀一次 MongoDB 全部 12 個節點最近 `GLOBAL_XAPP_LOOKBACK`（預設 300）筆經驗（每節點 1 筆/秒 → ≈5 分鐘），算出每個節點的平均吞吐量與**同角色**（relay=Node1~4、access=Node5~12）平均的落差，換算成 `fairness_bias = clip(role_mean / (node_mean + eps), 0.5, 2.0)`（2026-09-26 起同角色內比較，視窗 300 筆≈5 分鐘；舊版 relay/access 混算會使 relay 恆 <1、access 恆 >1，只是節點身分），透過 ZMQ PUB 廣播給全部 12 個 Local rApp（topic `nodeN`）。
   * **與「Backhaul-aware 動態 PRB 預算」機制（C 層，2026-10-01 起停用，見第 3 節）的分工**（停用前的設計說明）：兩者刻意作用在不同軸，不會衝突——C 層依「該節點自己 MT 的真實忙碌度」硬性縮小 DU 可用 PRB 池上限（單節點局部視角，PF 排程器也吃得到，是排程器的輸入約束）；Global xApp 依「全域相對落後程度」提供**純軟性 state 特徵**給 DRL Actor（單節點看不到的全域視角，只有跑 DRL 的階段才吃得到，不做任何硬性 PRB 裁切）。舊版 5-node 設計讓 relay 對子節點做配額裁切，跟 C 層機制做同一件事（雙重節流），因此已捨棄。
   * **常數**：`state_vec[48]` = active_ratio，`state_vec[49]` = 正規化後的 `fairness_bias`（`(clip(bias,0.5,2.0)-0.5)/1.5`），**`state_vec[50]` = `bh_ratio`（Backhaul-aware 可用 PRB 比例 ∈[0,1]，2026-09-26 起，`STATE_DIM=51`）**：由 DU 的 E2SM-MAC 回報（`mac_ind_msg_t.backhaul_prb_ratio`，線上格式在 tstamp 後多 4 bytes）經 xApp JSON 的 `bh_ratio` 帶進 Python。動作是每 UE PRB 上限，池子大小（106×此值）決定上限是否綁得住。**改 E2 訊息格式時 RIC／xApp／gNB 三側的 `libmac_sm.so` 與 `nr-softmodem` 必須同版本，三台主機都要重編並 `sudo ninja install`**。
4. **Global rApp（Flower ServerApp/ClientApp，聚合策略隨 `FL_MODE` 切換）**：
   * **實作**：`inference/flower-app/iab_fl/`，`server_app.py`（`FL_NUM_NODES=12`）+ `client_app.py`，透過 Flower SuperLink（`flower-superlink`）+ 12 個 SuperNode（`flower-supernode-node{1..12}`，`--clientappio-api-address` 分別綁定 `9101~9112`）部署，`flower-scheduler` 每 `FL_ROUND_INTERVAL_S`（預設 180 秒）觸發一輪 `flwr run`。
   * **`FL_MODE=avg`（Stage 2，預設值）**：`IABFedAvg`，標準 FedAvg，全部 12 節點一起聚合，聚合後的權重寫回共用的 `model_nodeN.pt` checkpoint，Local rApp 背景執行緒偵測到 mtime 變化即熱重載。已加上 `ZeroDivisionError` 防呆（全部節點 `num-examples=0` 時跳過本輪聚合，不崩潰、不誤把空 ArrayRecord 廣播出去覆蓋掉有效權重）。
   * **`FL_MODE=capa`／`FL_MODE=elastic`（`IABCapaFedAvg`／`IABElasticFedAvg`，2026-09-29 全面重新設計後已是舊版/附錄設計，不再是現行 Stage 3/4）**：程式碼仍留在 `server_app.py`（未刪除），但對應的設計（Critic 全域池化＋Actor 信心加權個人化／Actor+Critic 皆彈性拉扯）已移至 `inference/STAGE4_CUSTOM_FL_DESIGN.md` 附錄 A／B，量測結果 `stage3_capaFed.md` 已刪除。**現行 Stage 3 設計**（Hierarchical FedAvg+Hedge＋分歧節點保留自身模型，定義以論文第 3 章為準）尚未實作；**Stage 4 待重新設計**（原 HiRA-Fed 見 `inference/STAGE4_CUSTOM_FL_DESIGN.md` §11，僅供參考）。實作完成後會是新的 `FL_MODE` 值（取代 `capa`／`elastic`）。各 strategy class 並存於同一份 `server_app.py`，`main()` 依環境變數決定 instantiate 哪一個，`_seed_initial_arrays()`／checkpoint 廣播底層寫入邏輯（`_apply_weights_to_node()`）共用，新設計預期沿用這套既有機制。**舊版 `FL_MODE=cluster`（`IABClusterFedAvg`）與 `FL_MODE=custom`（`IABCustomFedAvg`，AW-FedAvg）已於 2026-09-29 路線圖重新定案後整段移除（程式碼、封存資料、設計文件皆已刪除），不是需要復原的架構，過程見 `HISTORY.md`**。
   * **Stage 5**：只再換 Local reward 機制，Global 聚合維持跟 Stage 4 一樣，見第 3 節路線圖。

### 與 3GPP IAB / O-RAN 標準規格的差異（誠實揭露，供論文方法論限制章節引用）

這個測試平台**不是**完整落地 3GPP IAB 規格與完整 O-RAN RIC 階層的系統，而是「用標準相容的底層元件（OAI 的 3GPP-compliant PHY/MAC/RRC/NAS 協議棧、FlexRIC 這個符合 O-RAN E2AP 規格的 Near-RT RIC 框架）搭建、但在 IAB backhaul 資源共享機制與 RIC 階層上做了論文範疇內合理簡化」的研究平台。差異點如下：

| 項目 | 標準規範怎麼定義 | 這個測試平台實際做法 |
|---|---|---|
| Backhaul 資源共享 | 3GPP TS 38.300 / 38.874 定義 **BAP 層**（Backhaul Adaptation Protocol），負責跳點路由、backhaul RLC channel 對應、QoS mapping | **完全沒有 BAP 層**——MT+DU 串接模式，靠 Linux IP Routing 透傳（見第 1 節），不是標準的 backhaul bearer 機制 |
| H/S/NA 資源分時 | 標準定義 Hard/Soft/Not-Available 資源，DU 跟 MT 在**真正的時域/頻域**上互斥使用資源 | 定為 **out-of-band IAB**（各鏈路資源互不排擠，`SYSTEM_SPEC.md` D1），沒有 H/S/NA 時域劃分；in-band 半雙工列為未來工作（原本的 MT 忙碌度容量代理已於 2026-10-01 停用） |
| 多跳無線資源競爭 | 真實 IAB 多跳共用同一段頻譜，節點之間會互相干擾、搶資源 | 每個 Node 各自跑獨立的 rfsimulator 載波，彼此完全不搶頻譜——這點對「模擬多跳拓樸的路由/backhaul 聚合排程問題」是合理簡化，但不對應真實電磁環境下的資源競爭 |
| O-RAN RIC 完整架構 | Non-RT RIC + A1 介面 + Near-RT RIC + SMO | **只有 Near-RT RIC**（FlexRIC + E2 + xApp），沒有 A1 介面、沒有 Non-RT RIC、沒有 SMO；「Global xApp/Global rApp」的 FedAvg/Cluster FL 設計是這篇論文自訂的機制，不是 O-RAN 標準定義的元件 |

**單一 UE、無競爭情況下的理論吞吐量上限**（供對照實測數據用）：以本平台目前的 PHY 參數代入 3GPP TS 38.306 peak data rate 公式——106 PRB @ 30kHz SCS（`donor_du.conf`：`dl_carrierBandwidth=106`, `subcarrierSpacing=1`，等效 40MHz）、SISO 1 層、256QAM（Qm=8）、R_max=948/1024、FR1 DL overhead=0.14：

$$\text{Data Rate} = v_{layers} \times Q_m \times R_{max} \times \frac{N_{PRB} \times 12}{T_s^\mu} \times (1-OH) \approx 227\ \text{Mbps（下行，MAC 層理論峰值）}$$

這是規格書定義的絕對上限（假設每個 RE 都排到最高 MCS、最大編碼率），**不是**實際 iperf3 會量到的數字。2026-09-12 機制驗證（`experiment_results/backhaul_mechanism_verification.md`，Node2+Node7 兩個節點都在 PC1 同機、單一 UE、旁邊 UE 閒置）量到 **43.8 Mbps**，比理論峰值低了約 5 倍，受 rfsimulator 軟體模擬與 iperf3/TCP goodput 損耗影響，各項貢獻未拆解驗證。**這個數字是同機路徑量到的，不含跨主機鏈路，不能類比現行「relay 同機、access 跨主機」拓樸的多跳吞吐量**（跨主機路徑的容量見第 8 節）。

**Backhaul-aware PRB 預算機制已停用（2026-10-01，`SYSTEM_SPEC.md` D3）**：系統定為 out-of-band IAB（D1），MT 與 DU 互不排擠，三份 compose 的 DU 都改成 `--backhaul-mt-telnet-port 0`（不啟動輪詢，`backhaul_prb_ratio` 恆為 1.0）。停用前的節流上限推導（c/(1+ε_DL)≈118~121 Mbps／節點）與實測（可用比例 0.94~1.00）見 `HISTORY.md` 續四十一、`SYSTEM_SPEC.md` §3.3。TDD（7D/1S/2U）扣除後的下行峰值約 159~169 Mbps。

### 相關研究定位（2026-09-29，供論文相關研究章節引用）

本論文的兩個核心技術選擇——「聯邦式 DRL 部署在 O-RAN Near-RT RIC 的 xApp/rApp 上做資源排程」與
「多智能體 RL 處理 IAB 多跳拓樸的資源分配」——都各自是活躍的既有研究方向，不是孤立的自訂設計：

**聯邦 DRL + O-RAN xApp 資源排程**：
- *Federated Deep Reinforcement Learning for Resource Allocation in O-RAN Slicing*（GLOBECOM
  2022）——在 Near-RT RIC 上跑 DRL xApp 做網路切片排程、用聯邦方式跨節點協調，是本論文
  Local xApp+Local rApp（Near-RT，單節點 DRL）＋Global xApp+Global rApp（跨節點聯邦聚合）
  這個雙層架構最直接對應的既有文獻
- *Federated Neuroevolution O-RAN (F-ONRL)*——聯邦式機制強化 O-RAN xApp 的 DRL 穩健性，另一個
  聯邦式 xApp 設計的角度
- *Meta-Hierarchical Reinforcement Learning for Scalable Resource Management in O-RAN*——
  O-RAN 資源管理的階層式 RL 設計，跟本論文 Local（單節點）／Global（跨節點）兩層控制迴圈的
  階層劃分精神上呼應，雖然階層的切分方式不同（該文獻階層對應 RIC 層級，本論文對應拓樸位置）

**多智能體 RL + IAB 多跳資源分配**：
- *Multi-Agent Reinforcement Learning for Network Routing in Integrated Access Backhaul
  Networks*（2023）——IAB＋多智能體 RL 路由，跟本論文 12 個節點各自獨立訓練 DRL agent 的
  架構高度吻合，是目前找到最直接對應「IAB 場景下的多智能體 RL 資源管理」的文獻
- *Multi Agent DeepRL based Joint Power and Subchannel Allocation in IAB networks*（2023）
- *Mobility-Aware Resource Allocation for mmWave IAB Networks: A Multi-Agent Reinforcement
  Learning Approach*（2022）
- *Routing and Resource Allocation for IAB Multi-Hop Network in 5G Advanced*（IEEE，2022）——
  3GPP 標準框架下的 IAB 多跳路由＋資源分配，可對照本論文「MT+DU 串接、無 BAP 層」的簡化設計
  差異（見上方差異揭露表）

**本論文與上述文獻的差異定位**：既有文獻多半聚焦單一維度（要嘛聯邦式 xApp、要嘛多智能體 IAB
路由，兩者少有同時處理），本論文把兩者結合在同一個系統裡（12 節點各自的 Local DRL 是多智能體
RL 的實例，Global 層的聯邦聚合策略演進（avg FL→Hierarchical FedAvg+Hedge→Stage 4 待重新設計）對應聯邦式
xApp 文獻的方向），
且額外處理了「節點間持久異質性」這個既有文獻較少直接檢驗的軸（T/TH 雙軌框架，見第 8 節）。
Stage 3／4 聚合機制本身借鑑的個人化聯邦學習文獻（pFedMe、APFL 等）見
`inference/STAGE4_CUSTOM_FL_DESIGN.md` §10／§11，是機制層級的引用，跟這裡系統層級的定位
互補、不重複。

---

## 3. 標準開發流程

開發採「由下而上 (Bottom-Up)」策略，逐步將控制權由 OAI 預設排程器移交給 AI。

### 已完成階段
* **底層資料平面**：MT+DU 串接架構、Linux IP Routing 轉發正常。
* **Local xApp C 語言控制權**：成功擷取 `mac_ind_data_t` 中的 UE 狀態，並下發 `MAC_CTRL_REQ` 驗證 OAI 確實套用 PRB 覆寫。
* **獨立 xApp 開發與跨語言 IPC**：每個 Node 各自獨立的 C 語言 FlexRIC xApp + ZeroMQ（`libzmq`/`libcjson`，5ms timeout）+ Python ZeroMQ REP 推論伺服器 + MongoDB 資料持久化。
* **Local 單節點 AI 閉環控制**：DRL Actor-Critic（GRU + Dirichlet Policy Gradient）+ Local rApp 微調迴圈已完成並在 12-node 拓樸上驗證過 ZMQ round trip 正常運作（13/13 E2 連線、12/12 xApp）。
* **三主機基礎設施擴容**：1 donor + 4 relay + 8 access + 17 UE 全部端點啟動並驗證（2026-09-30 起移除 UE17，現為 16 UE）（見 HISTORY.md 2026-09-11 條目）。

> 開發 xApp 規則：每開發完一個 xApp，先編譯 FlexRIC xApp 和 OAI RAN with E2 Agent，編譯過了才進下一個/才能審核（見第 6 節建置指令）。

### 五階段實驗路線圖（2026-09-29 全面重新設計中——Local DRL 與 Stage 3/4 Global FL 從文獻重新定案，見下方狀態說明）

目標：在同一組流量+路徑損耗場景下，依序驗證 5 個遞增複雜度的控制策略，**每一階的實驗數據（TCP-DL、Latency、UDP-DL/UL、Jain's Fairness Index 等）依分軌驗收規則贏過前一階（見下方「驗收規則」）**，最終逼近吞吐量理論上限。

> **2026-09-29 全面重新設計**：舊版 Local DRL（GRU/MLP＋連續 Dirichlet 動作空間）與舊版 Stage 3/4
> Global FL（CAPA-Fed／ERA-Fed）從未真正贏過 PF baseline 吞吐量，即使 Global 端換了三種聚合方式
> 也一樣。決定停止在同一個未經文獻驗證的 Local DRL 基礎上持續更換 Global FL 聚合方式，改成：
> Local DRL 重新設計（目標吞吐量贏 PF，設計見 `inference/LOCAL_DRL_V2_DESIGN.md`）；Stage 2
> Global FL 維持標準 FedAvg（已有直接文獻對應，不變）；Stage 3/4 Global FL 從 2022 年後 IAB/O-RAN
> 文獻重新設計（設計見 `inference/STAGE4_CUSTOM_FL_DESIGN.md` §10／§11）；每個階段都採 T/TH
> 雙軌訓練+量測（見下方）。舊資料/模型/MongoDB 經驗已全部清空，CAPA-Fed／ERA-Fed 的設計與診斷
> 過程移至該文件附錄保留當論文方法論失敗案例記錄。決策過程、4 輪文獻研究＋2 輪引用真偽驗證的
> 完整記錄見 `HISTORY.md` 續三十六。**以下表格只反映目前的設計/實作狀態，不是最終驗收數字**
> （舊版已刪除的量測結果不再列出）。

| Stage | 策略 | Global 層（配額協調/FL 聚合） | Local 層（單節點 DRL） | 狀態 |
|---|---|---|---|---|
| 1 | PF baseline | 無 | 無（OAI 內建 PF 排程器，全部 12 個 xApp 停止） | **已完成（2026-10-02，24 UE、HS／G／HSH、seed 20260930、TCP＋UDP）**，數據見 `experiment_results/PF.md`。壅塞相位滿足率（滿足率 JFI）：**HS** TCP 0.823（0.918）／UDP 0.880（0.970）；**HSH** TCP 0.838（0.926）／UDP 0.885（0.970）；**G** TCP 0.876（0.949）／UDP 0.927（0.986）。Stage 2~5 一律與這組比較（同場景、同 seed、同協定）；16 UE 時期的舊結果已刪除。 |
| 1.5 | 純 Local DRL（FL 對照組） | 無（`FL_MODE=none`，Global xApp 照常廣播） | Local xApp+Local rApp：與 Stage 2～4 逐行相同的 Local DRL（重新設計中，見下方「Local DRL 重新設計」） | **2026-10-03 加入**：論文定位為「Local DRL 是文獻既有方法、貢獻在 Global FL」，必須有不含 FL 的對照組，Stage 2～4 的進步才能歸因到 FL。v2.1 的單獨評估（下一列）屬開發紀錄，不是本階結果。 |
| 2 | avg FL + Local DRL v2 | Global xApp（全域公平性軟性廣播，Stage 2~5 全程固定）+ Global rApp：標準 FedAvg，全部 12 節點一起聚合（不變，`STAGE2_DESIGN.md` §3/§4） | Local xApp+Local rApp：**Local DRL v2**（PF 初始化＋5 檔離散上限＋全部 UE 共用 Actor 頭＋PF-shadow 資料預訓練 Critic＋relational state，reward 維持純吞吐量；取代舊版 GRU/MLP＋連續 Dirichlet，見 `inference/LOCAL_DRL_V2_DESIGN.md`） | **可以開始訓練（2026-10-01）**：動作已改為時域遮罩檔位；12 個節點的 Critic 已用 PF-shadow 資料（tm,t,th）預訓練並存入 `iab-xapp-model-nodeN`（備份在 `/home/lindor/drl_ckpt_pretrained_20261001/`，時間外推解釋比例 84~99%）；冒煙測試與 25 分鐘端到端訓練試跑皆通過（見 `HISTORY.md` 續五十），MongoDB 經驗已清空。基準場景已定案為 HS／HSH 雙軌＋G（`SYSTEM_SPEC.md` §8.1 D6）；24 UE 拓樸下的 Critic 預訓練與 PF 基準重做中（舊 53 維 checkpoint 與 16 UE 時期的 PF-shadow 資料已刪除）。舊版量測結果（`avgFL.md`）已刪除（未真正贏過 PF，基礎已改變不再適用）。實作順序：先驗證 Local DRL v2 單獨（`FL_MODE=none`）能否贏 PF，再接上 FedAvg 量測 Stage 2。 **v2.1 單獨評估（2026-10-03，HS TCP，`experiment_results/LocalDRLv2.md`）**：壅塞送達 +15.1%（80 對 70），但滿足率 −2.8%、滿足率 JFI −5.6%、壅塞 RTT +17%；G 不傷害。吞吐量贏、滿足率未贏，尚未達驗收規則；RL 訓練未超越 BC 起點。 |
| 3 | Hierarchical FedAvg+Hedge + Local DRL v2 | Global rApp：**兩層 Hierarchical FedAvg（branch 內樣本數加權，跨 branch 用 Hedge 線上自適應加權取代單純樣本數加權）＋ Sattler CFL 分歧 fallback**：被判定分歧的節點（leave-one-out cosine < 門檻）當輪不參與全域平均、也不被覆寫，保留自己的模型（單節點 cluster，每輪重新判定）；其餘節點整份覆寫。**2026-09-30 定案（`FL_MODE` 名稱待實作時定）：Stage 3 起就有個人化**（原設計把分歧節點自成一群但沿用 branch 的 Hedge 權重，展開後貢獻不變、機制無效，已修正）。論文定義見 `114368064_謝欣蓉_Master_s_thesis/Master_s_thesis/chapter/chapter3-system-model.tex`；`STAGE4_CUSTOM_FL_DESIGN.md` §10 為舊版，實作前需照論文版更新 | Local xApp+Local rApp：Local DRL v2（同 Stage 2，逐行相同） | **設計已定案，尚未實作**。舊版 CAPA-Fed 的完整訓練+量測結果、Dirichlet 退火根因診斷過程移至 `STAGE4_CUSTOM_FL_DESIGN.md` 附錄 A 保留參考。 |
| 4 | （待重新設計）+ Local DRL v2 | Global rApp：**2026-09-30 起待重新設計**（Stage 3 已提前做分歧節點個人化，原 HiRA-Fed 需重新定位；以下為舊版 HiRA-Fed 描述，僅供參考）——聚合計算完全複用 Stage 3（branch 內平均＋Hedge 跨 branch），廣播改用雙錨點個人化插值（Actor：branch 平均＋全域平均的角色相關混合）＋角色條件式彈性拉扯（Critic：relay 拉力比 access 強），修正 Stage 3 殘留的整份覆寫問題。完整設計見 `inference/STAGE4_CUSTOM_FL_DESIGN.md` §11 | Local xApp+Local rApp：Local DRL v2（同 Stage 2/3，逐行相同） | **待重新設計**。舊版 ERA-Fed 的彈性拉扯數學形式、角色分軌構想被直接沿用，設計移至 `STAGE4_CUSTOM_FL_DESIGN.md` 附錄 B 保留參考。 |
| 5 | Stage 4 聚合 + 改良版 DRL | Global xApp+Global rApp：同 Stage 4，不變 | Local xApp+Local rApp：改良版 DRL（`REWARD_MODE=lagrangian`：R = R_tp + λ·(JFI − JFI_MIN)，λ 投影對偶上升更新，JFI 為節點內各 UE 正規化吞吐量的 Jain 指數） | 未開始 |
| 6（選做，Stage 5 完成後有餘裕才做） | FedAdam/FedYogi 自適應聯邦優化 | Global rApp：`server_app.py` 改用 FedAdam／FedYogi（Reddi et al., *"Adaptive Federated Optimization"*, ICLR 2021）——伺服器端維持一個跨輪次的動量＋二階動量緩衝，對聚合後的 pseudo-gradient 做類 Adam 的自適應更新，取代單純加權平均；**只影響「這一輪聚合出的更新要怎麼套用到全域模型」（時間軸上的平滑/穩定），不影響「這一輪各節點的權重怎麼分配」**——relay 節點權重依然接近 0，只是讓 access 節點主導算出來的全域模型收斂更快更穩，直接服務「吞吐量優先」這個目標，不涉及公平性加權（q-FFL 等公平性導向方法因為會犧牲吞吐量換公平性，跟目前優先順序相反，故不採用）。實作只需改 `server_app.py`，不碰 Local 端或 `client_app.py`。 | Local xApp+Local rApp：不變 | 未開始（先寫下設計方向，Stage 5 做完且有餘裕再做；簡化版可先試 FedAvgM 伺服器端動量，風險/工作量更低） |

> **⚠️ 量測條件警語（2026-09-26）**：上表 Stage 1~3 全部數據都是在三個已修正的環境瑕疵下量得——(1) PC3 網卡曾在 USB 2.0；(2) rfsim 網路頻寬瓶頸（已加稀疏傳輸、通道融合讀取、速度調節器，見第 5、8 節）；(3) **場景的「路徑損耗」從未真正惡化過通道**：`ploss` 正值在 rfsim 是增益（負值才是衰減），且 DU 端通道只影響上行、UE 端原本沒有通道模型，下行完全沒被惡化。絕對值與「通道差異」的歸因不代表平台或演算法真實表現；**所有 Stage 需在修正後的同一版 `librfsimulator.so`、同一個速度 S、新的下行通道惡化下重測**。調查細節見 `HISTORY.md` 2026-09-25~26。

**量測方法**（Stage 1~5 沿用同一套以確保公平比較）：`iab/measure_stage.py` 與 `scenarios/traffic_scenario.py` 同時執行，併發取樣全部 UE（PC1 的 relay 直連 UE17~24 由 PC1 本機取樣）在同一組動態流量+路徑損耗場景下的即時吞吐量與 RTT；每個 stage 的完整數據記錄在 `experiment_results/<方法名>.md`。

**T/TH 雙軌訓練與量測（2026-09-29 定案，Stage 1~5 全部適用；2026-10-01 起兩軌改為 HS／HSH，見第 8 節，下文的 T／TH 對應 HS／HSH，G 兩軌都量作泛化檢查）**：Scenario T／TR 的節點間長期統計分布是均勻的（每個 UE 長期下來都會輪過全部檔位組合），異質性感知的聚合機制（例如 Stage 3 的 Hedge 加權、Stage 4 的角色相關個人化）在這種對稱環境下沒有真正的差異可以學、也沒有機會在量測時展現優勢。為了讓論文能回答「什麼場景條件該用哪個 Stage 的策略」而不只是單一排名，**每個 Stage 都要分別在 Scenario T（對稱，既有基準）與 Scenario TH（持久異質性，見第 8 節）下各訓練一次、各量測一次**（PF 不需訓練，直接兩種場景下各量一次）：
* T 訓練＋T 量測：延續既有方法論，維持跟歷史結果的可比性。
* TH 訓練＋TH 量測：驗證「給機制真正的異質性訊號後，優勢有沒有被放大」。
* 各 Stage 的預期角色（2026-10-01）：Stage 3 針對 branch 間持久負載異質，預期 HSH 勝過 Stage 2、HS 下持平；Stage 4 針對兩軌都存在的結構異質（relay 有 MT＋UE、access 只有 UE），預期兩軌都勝過前一階。
* （可選）交叉量測（T 訓練的模型拿去 TH 量測、反之）：看場景不匹配時的代價，資源許可再做。
* 兩種場景下量到的結果都要各自完整記錄（不能只挑好看的一份），寫進 `experiment_results/<方法名>.md` 時分開列出 T 與 TH 兩組表格。

**判讀指標（Stage 1~5 一律照這組比較，2026-09-26 起）**：
* **整段 JFI（每 UE 全程平均吞吐量的 Jain 指數）因時間平均天然偏高（~0.98），Stage 2~5 比較不能只看它。**
* 必須同時報告 **壅塞相位** 的：① **需求滿足率**（每 UE 達成÷目標，上限 1）平均；② **滿足率 JFI**（UE 間、逐相位計算後取平均，把目標不同的 UE 放在同一尺度）；③ **RTT**；並附各 UE 的個別吞吐量與壅塞相位滿足率（看誰被壓得最兇）。正常相位只當對照（滿足率應≈1）。
* 同時驗證場景確實壅塞：壅塞相位佔時間比例（設計 5/11=45.5%）、壅塞相位過載 UE 比例（目標≥3 且 達成/目標<0.85）、總送達 vs 總目標；正常相位過載應≈0%。沒有壅塞的量測沒有鑑別力，視為無效。
* 數字一律用模擬時間（吞吐量 ÷S、RTT ×S）。**開發／試驗階段只量 TCP；模型設計確認、訓練完成後的正式量測才 TCP 與 UDP 各量一份**（2026-10-02 使用者決定）、各自比較；兩者吞吐量相近，但 RTT（壅塞相位 TCP 約 116 ms／UDP 約 32 ms）、滿足率與 CPU 負載不同，論文驗收標準也同時列 TCP-DL 與 UDP。基準：PF（`experiment_results/PF.md`，2026-10-02 起 24 UE）：HS 壅塞相位滿足率 TCP 0.823／UDP 0.880，HSH 0.838／0.885，G 0.876／0.927。
* 工具：`iab/clean_env.sh`（重啟前清理）→ 依序重啟 → `iab/precheck_measure.sh`（13/13 E2、RestartCount、24 UE 連通、24 個 iperf3 server、三台 S 一致；Stage 2~5 設 `EXPECT_XAPP=12`）→ `OUT_DIR=<dir> [SCENARIO=T|TH] iab/run_stage_measure.sh {tcp,udp} <tag>` → `python3 iab/analyze_stage.py <dir> <tag>`（輸出上述全部指標；S 讀本機速度檔，分析舊資料用 `ANALYZE_S=<當時的 S>`；目標為 0 的 UE-相位不納入）。**TH 量測一律用固定 seed**（`MEASURE_SEED`，預設 20260930，兩台主機共用；2026-09-30 前沒傳 seed，每次流量分配都不同、無法配對比較）。`analyze_stage.py` 的每 UE 目標直接呼叫場景函數計算（2026-09-30 修正：之前 TH 的目標誤用 T 的輪替公式，TH 滿足率全錯）。

**⚠ 場景造成的設計限制（2026-10-02）**：HS／HSH 改為 PB 結構後，壅塞相位的改進空間全在 4 個 relay 節點，8 個 access 節點最好的動作永遠是「不介入」——Local DRL 只有 relay 學到決策；Stage 2 FedAvg 依決策點樣本數加權時 access 權重≈0（聚合≈4 個 relay 互相平均）；Stage 3／4 的分 branch／角色個人化設計須依此重新檢查。詳見 `SYSTEM_SPEC.md` 文末與 `inference/STAGE4_CUSTOM_FL_DESIGN.md` 文末。

**Local DRL v3（2026-10-03，取代 v2.1，Stage 1.5～4 共用；設計見 `inference/LOCAL_DRL_V2_DESIGN.md` 正文）**：照 IAB DRL 文獻——reward＝本節點子樹內終端 UE 的 α-fair 效用（`REWARD_MODE=alpha_fair`，relay＝自己直連 UE＋下游 access UE；α=1 即文獻的 log-sum rate，α 掃描顯示 0.3～0.4 時兩層最佳遮罩強度隨狀態改變，建議 0.4）；動作＝每個子節點從 21 檔時域遮罩選一檔（`DRL_MASK_TIERS=acktier`：依允許的 DL slot 數分級、位置平均分在兩個 HARQ-ACK 群組，含實測最好的 0x1111／0x9044），從 PF 起步、**不從規則做 BC、不設寫死的 MCS 門檻**，PPO actor-critic、γ=0.5、每輪 15 次更新、影子模型訓練（不再持鎖擋推論）、Critic 暖身。用時域遮罩而非 PRB 上限，是因為 OAI 的 HARQ-ACK 次數上限（短 PUCCH 每時機 2 位元）讓頻域重新分配無效（平台限制）。場景 HS／HSH 第四版每個壅塞相位同時有 relay 決策（PB 熱點）與 access 決策（一個混合節點）。**v3.2（10-04）**：α=0.2、兩段式動作（節點選強度＋每子節點是否套用）、6 檔選單。**v3.3（10-04）**：動作持續 5 秒（`DRL_ACTION_HOLD=5`，每秒重抽時 access 遮罩的好處在 reward 裡量不到）、訓練只用壅塞相位（`--scenario-family hsc`）、Stage 1.5 也開 Global xApp；FL 容器須與 inference 拿到相同的 DRL 設定（compose 已補），見 `STAGE2_DESIGN.md` §4.4、`HISTORY.md` 續六十九。**v3.4（10-04）**：3 步回報（`DRL_NSTEP=3`）——一步 TD 的 Critic 自舉把 relay 遮罩的立即好處蓋掉（把「MT 佇列變短」誤判為需求下降），離線驗證改 3 步後策略分化方向正確。**v3.5（10-04，現行）**：兩段式改 `DRL_FACTORED_MODE=apply_first`（每個子節點自己決定是否被遮、有遮才選強度）——node_on 讓 access「遮到好 UE −22%」的懲罰連帶壓低「遮壞 UE」，access 學成整個不開，合併資料也一樣（`HISTORY.md` 續七十一）。

**驗收規則（2026-10-01 定案，`SYSTEM_SPEC.md` D7；取代原本「兩軌每一階都要贏前一階」）**：兩軌（HS 對稱／HSH 持久異質）各自比較，「贏」＝壅塞相位吞吐量高於前一階超過雜訊（1～2%），「不輸」＝差距在雜訊內。**Stage 1.5～4 以吞吐量為準，滿足率／滿足率 JFI 只報告不列入驗收，輸 PF 也可以，留到 Stage 5 處理（使用者 2026-10-04 確認）**。
* Stage 1→1.5（PF → 純 Local DRL）：HS、HSH 都必須贏 PF（證明 Local DRL 本身有效）。
* Stage 1.5→2（純 Local DRL → avg FL）：HS、HSH 都必須贏純 Local DRL（FL 的貢獻；2026-10-03 起取代原本「Stage 1→2 贏 PF」）。
* 動態規則（`XAPP_MODE=rule RULE_KIND=dyn`）只列為「需專家依平台實測重調」的參考線，不在驗收鏈內，不要求任何 Stage 贏它。
* Stage 2→3（Hierarchical FedAvg＋Hedge）：**HSH 必須贏**（針對 branch 間持久負載異質）；**HS 不輸即可**（對稱時各 branch 長期統計相同，沒有分歧可利用，預期持平）。
* Stage 3→4（待重新設計，方向：依 relay／access 角色個人化，針對兩軌都存在的結構異質——relay 有 MT＋UE 子節點、access 只有 UE）：**HS、HSH 都必須贏**。
* Stage 4→5（Lagrangian）：兩軌都不輸，且滿足率 JFI 為全程最高。
* G（泛化）：每一階都只要求不輸 PF（改進空間約 1%，用來證明不會傷害）。不要求 HS 下的某 Stage 贏過 HSH 下的另一個 Stage。Stage 2→3→4 只換 Global 聚合方式、Local 模型不變，單獨驗證「聚合策略」的貢獻；Stage 4→5 只換 Local reward 機制、Global 聚合不變，單獨驗證「改良版 DRL（Lagrangian）」的貢獻——每次只換一個變數，才能把進步歸因到正確的地方。**Stage 6 是 Stage 5 完成後才考慮的選做項目，不在這個單調遞增鏈的驗收範圍內**，是否要做、做完要不要跟 Stage 5 比較，屆時再定。

**關鍵設計決定（避免混淆）**：
* Stage 2~4 底層用的是**同一個** Local DRL v2（只差 FL 聚合方式），見 `inference/LOCAL_DRL_V2_DESIGN.md`；Local 層 Stage 2~4 逐行相同是硬性規則，不因 Global 層設計而改動；Stage 5 是另一版 DRL（改良版，見下）。
* 「改進版 DRL」= Stage 5 重新啟用 Lagrangian 機制（`R = R_tp + λ·(JFI_raw − JFI_MIN)`）。Local DRL v2 的 reward 維持純吞吐量 `R_tp`（2026-09-30 審查拿掉反事實 PF reward：state-only 項會被 Critic baseline 完整吸收，見 `LOCAL_DRL_V2_DESIGN.md` §3），所以 Lagrangian 項照舊疊在 `R_tp` 上，不需要重新設計。切換 REWARD_MODE／Local DRL 版本前務必清空 MongoDB 經驗與模型 checkpoint，reward 語意改變不能混進同一批訓練資料；清空 docker volume 前一定先用 `docker volume ls`／`docker inspect` 確認實際名稱（compose 的 volume 有 `name:` 覆寫）。
* **不寫死、隨時可單獨跑任一 stage**：每個 stage 透過環境變數/CLI flag 獨立選擇 Global FL 聚合方式＋ `REWARD_MODE`，不論開發進度到哪都能重跑任何一個 stage（Stage 3/4 新設計實作時的 `FL_MODE` 具體命名待定，取代舊版 `capa`／`elastic`）。
* State 現為 69 維（2026-10-01 起，Stage 2~4 凍結；`LOCAL_DRL_V2_DESIGN.md` §8.x）：每 UE 4 維（吞吐量、MCS、佇列、`is_iab_child`＝MT 或 UE）×16、`active_ratio`、`fairness_bias`（全域視角，同角色內比較）、`bh_ratio`（backhaul 預算停用後恆為 1.0）、relational 特徵 $\hat p$＝parent DU 的 RLC 佇列總量（上游壅塞，無 parent＝0）與 $\hat c$＝children 聚合需求。全網 JFI 目前只做監控（`global_xapp.py` 印出 `global_jfi`，不寫回 Mongo、不納入聚合權重）。

### 環境層機制：Backhaul-aware 動態 PRB 預算（2026-10-01 起停用）

原設計在每個節點依自身 MT 的忙碌度縮小 DU 可用 PRB（`gNB_scheduler_dlsch.c`：`n_rb_sched = bw × backhaul_prb_ratio`，`ratio = 1 − EWMA(busy)`，`nr_mac_gNB_backhaul_poll.c` 經 `bhload query` 取 MT 的 RB 使用量），用來近似 in-band 半雙工。系統定為 out-of-band 後（`SYSTEM_SPEC.md` D1／D3）停用：compose 的 DU 一律 `--backhaul-mt-telnet-port 0`，C 程式碼保留；要恢復時把 port 改回 9188＋節點編號。E2 回報的 `bh_ratio` 恆為 1.0，state 維度不變。2026-10-01 之前的 PF 基準與各 Stage 量測都是在這個機制開啟下量的，PF 需重量。

**新 Stage 3（Hierarchical FedAvg+Hedge）／Stage 4（待重新設計）**：Stage 3 現行定義以論文第 3 章為準（2026-09-30 修正分歧節點處理，見上表），`inference/STAGE4_CUSTOM_FL_DESIGN.md` §10／§11 為原始數學推導（2026-09-29 全面重新設計，取代舊版 CAPA-Fed／ERA-Fed，兩者設計與診斷過程移至該文件附錄 A／B 保留參考，不是需要復原的架構）。

**Stage 3~5 注意事項**：
* Stage 3/4 開跑前（實作完成後）MongoDB/checkpoint 要重新清空，且需要先完成 `LOCAL_DRL_V2_DESIGN.md` §7 待辦清單（PF-shadow 資料收集、BC 預訓練）。
* Stage 5 **只換** `REWARD_MODE=lagrangian`（Local 層），Global 聚合維持跟 Stage 4 一樣——刻意設計，才能單獨歸因 Lagrangian 的貢獻；同樣必須清空 MongoDB/checkpoint。驗收標準是全部指標（TCP-DL、Latency、UDP-DL/UL、JFI）都優於前四階，尤其 JFI 應為全程最高。


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

以下路徑除特別註明外，都以 `~/openairinterface5g/` 為根（PC2/PC3 也是同一絕對路徑 `/home/lindor/openairinterface5g`，見第 7 節同步規則）。`R` = `ci-scripts/yaml_files/5g_rfsimulator`（docker 部署目錄，下文簡稱）。

### 部署（`R/`）

| 檔案 | 用途 |
|---|---|
| `R/docker-compose-iab-server.yaml` | PC1：CN5G、Donor CU/DU、FlexRIC、MongoDB、12 組 `xapp-nodeN`+`inference-nodeN`、**全部 4 個 relay（Node1~4）**、Global 層（`profiles: ["stage2-fl"]`）。由 `iab/run_local_pc1.sh` → `iab/start_iab_server.sh` 啟動 |
| `R/docker-compose-iab-pc2.yaml` | PC2：Node5~8 access + UE1~8（parent relay 在 PC1，跨主機）。`iab/run_local_pc2.sh` → `start_iab_pc2.sh` |
| `R/docker-compose-iab-pc3.yaml` | PC3：Node9~12 access + UE9~16（UE17 已移除，定義以註解保留）。`iab/run_local_pc3.sh` → `start_iab_pc3.sh` |
| `R/conf/` | `donor_cu.conf`、`donor_du.conf`、`iab_du_node{1..12}.conf`、`flexric.conf`、`nrue.uicc.conf`（MT 用）、`nrue.uicc.chanmod.conf`（16 個終端 UE 用：啟用 UE 端 DL 通道模型；IP/ID 對照見第 1 節；`iab_du_node*.conf` 的 `local_n_address` 等欄位會被啟動腳本在執行期覆寫，所以 git 上常顯示為已修改） |
| `R/experiment_results/` | 各 stage 量測結果（現存：`PF.md`、`backhaul_mechanism_verification.md`；`avgFL.md`／`stage3_capaFed.md` 已於 2026-09-29 全面重新設計時刪除，新版結果待新設計實作+量測後重新產出）與 `checkpoints_archive/` |
| `HISTORY.md`（專案根） | 歷史踩坑/量測紀錄（見文件開頭慣例） |

### 腳本（`R/iab/`）

| 類別 | 檔案 |
|---|---|
| 啟動 / 網路 | `run_local_pc{1,2,3}.sh`、`start_iab_{server,pc2,pc3}.sh`、`start_relay_ues.sh`（relay 直連 UE17~24 啟動與自我修復）、`start_xapp_node{1..12}.sh`（compose 使用）、`setup_lab_net.sh`（三主機網卡與 SSH）、`run_stage2_fl.sh`（帶起 Global 層並清空經驗/checkpoint） |
| 訓練（長時間收斂訓練） | `training_scenario_driver.sh`（場景輪替）、`training_watchdog.sh`（崩潰復原）、`training_healthcheck.sh` |
| 量測 | `measure_stage.py`（併發取樣該主機的 UE 吞吐量與 RTT，輸出 CSV；場景期間有容器崩潰會寫 `.invalid` 並 exit 2）、`clean_env.sh`（整套重啟前三台清理並驗證）、`precheck_measure.sh`（量測前檢查）、`run_stage_measure.sh`（兩狀態 Scenario T 量測，TCP/UDP）、`analyze_stage.py`（判讀指標分析，見第 3 節） |
| 資料封存 | `archive_stage_data.sh`、`restore_stage_data.sh`（MongoDB 經驗與 checkpoint 封存/還原到 `checkpoints_archive/`） |
| 收斂診斷（現行） | `check_convergence_mongo.py`（讀 MongoDB `node{N}_experiences` 的 reward 趨勢；`training_healthcheck.sh` 有呼叫，且不受容器重啟清 log 影響） |
| **保留但未被現行流程呼叫的工具（用途待定，未刪除）** | `check_convergence_weights.py`：2026-09-18 新增的權重穩定度版收斂判斷——因 reward 的視窗內變化量幾乎完全由「目前輪替到哪個訓練場景 slot」決定（方波），不反映 policy 學習進度，改直接量測 policy 權重是否停止變化；沒有任何腳本呼叫，需手動執行。`check_convergence.py`：舊版，解析 `docker logs inference-nodeN` 的訓練摘要（預設只看 Node1~5；容器每次崩潰復原重啟就會清掉 log，長訓練幾乎湊不齊輪數，已被上面兩支取代，`training_healthcheck.sh` 仍提到它）。`calibrate_fl_rate.py`：一次性校準探測，輪詢 MongoDB 量出各節點「可訓練經驗筆數」的實際成長曲線（用來估 FL 量測視窗長度；需搭配 RAN + `traffic_scenario.py` 同時在跑、MongoDB 剛清空）。`clean_lambda_contamination.py`：一次性資料清潔，刪除 MongoDB 裡混入的 Lagrangian 污染經驗（帶 `lambda_applied` 欄位者；預設 dry-run，`--execute` 才真刪；為 2026-09-18 `REWARD_MODE` 被 watchdog 短暫重設回 lagrangian 的事後補救） |

雙主機期的 `monitor_drl.sh`／`watchdog.sh`／`drl_report.py`／`iab_perf_test.sh` 已於 2026-09-25 移除（見 git 歷史）。

### 流量場景（`R/scenarios/`）
`traffic_scenario.py`（流量+路徑損耗場景控制器，見第 8 節）、`channelmod_ctrl.py`（telnet chanmod 控制）、`setup_iperf_servers.sh`（每次乾淨重啟後必跑，見第 6 節）。

### AI 推論與 FL（`R/inference/`）
| 檔案 | 用途 |
|---|---|
| `inference_server.py`、`main.py` | Local rApp：ZMQ REP 推論 + 背景訓練執行緒 |
| `drl_agent.py`、`reward_calculator.py`、`training_pipeline.py` | DRL Actor-Critic、reward（`REWARD_MODE`）、訓練資料抓取 |
| `global_xapp.py` | Global xApp（`fairness_bias` 廣播，port 5560） |
| `flower-app/iab_fl/{server_app,client_app}.py`、`flwr_config.toml`、`vendor/flwr/` | Global rApp（Flower，`FL_MODE=avg|capa|elastic`） |
| `Dockerfile` | inference/FL 共用 image；改了會被 COPY 的檔案必須重新 `docker build` |
| `STAGE2_DESIGN.md` | **Stage 2 現行設計＋完整數學公式**（Local xApp+Local rApp+Global xApp+Global rApp 四元件整合，含 `DRL_CAP_MODE`/`DRL_DETERMINISTIC`） |
| `STAGE4_CUSTOM_FL_DESIGN.md` | **新 Stage 3（Hierarchical FedAvg+Hedge，§10）／新 Stage 4（HiRA-Fed，§11）設計文件**（2026-09-29 全面重新設計）——完整數學推導、退化條件、風險與監控計畫；§1~9（AW-FedAvg）與附錄 A/B（CAPA-Fed／ERA-Fed）為已放棄的設計與失敗診斷過程，保留作論文方法論的失敗案例記錄 |
| `LOCAL_DRL_V2_DESIGN.md` | **現行 Local xApp/rApp 設計**（2026-09-29 全面重新設計，取代 `STAGE2_DESIGN.md` §1/§2）——離線 BC 預訓練、PF-shadow 模式、離散動作空間、反事實 PF reward、relational state，Stage 2~5 全程共用 |
| `DRL_DESIGN.md` | Local xApp/rApp 的歷史沿革與 GRU/Lagrangian 分支推導，非 Stage 2 現行路徑 |

### C 語言：xApp 與共用底層（修改須謹慎）

開發/修改 xApp 至少涉及以下檔案（**正確檔名是 `xapp_nodeN.c`**）：

**xApp 本體（每個 Node 各自獨立，共 12 份）**：`openair2/E2AP/flexric/examples/xApp/c/ctrl/xapp_node1.c` ~ `xapp_node12.c`（FlexRIC 專案在 `openair2/E2AP/flexric`，xApp 主要開發目錄 `openair2/E2AP/flexric/src/`）

**共用底層檔案（Node 1~12 共用）**
```
openair2/E2AP/flexric/src/sm/mac_sm/ie/mac_data_ie.c
openair2/E2AP/flexric/src/sm/mac_sm/ie/mac_data_ie.h
openair2/E2AP/flexric/src/sm/mac_sm/enc/mac_enc_plain.c
openair2/E2AP/flexric/src/sm/mac_sm/dec/mac_dec_plain.c
openair2/E2AP/flexric/src/sm/mac_sm/mac_sm_agent.c
openair2/E2AP/flexric/src/sm/mac_sm/mac_sm_ric.c
openair2/E2AP/flexric/src/xApp/sm_ran_function_def.c
openair2/E2AP/flexric/src/sm/mac_sm/test/main.c
openair2/E2AP/flexric/src/xApp/db/sqlite3/sqlite3_wrapper.c
openair2/E2AP/RAN_FUNCTION/CUSTOMIZED/ran_func_mac.c
openair2/LAYER2/NR_MAC_gNB/nr_mac_gNB.h
openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_dlsch.c
```

**Backhaul-aware PRB 預算機制（第 3 節）**：`openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_dlsch.c`（把 `backhaul_prb_ratio` 套進可用 RB）、`openair2/LAYER2/NR_MAC_gNB/nr_mac_gNB_backhaul_poll.{c,h}`（DU 端輪詢執行緒：連 MT 的 `bhload query` 取 RB 使用量、換算成 `backhaul_prb_ratio`）、`common/utils/telnetsrv/telnetsrv_bhload.{c,h}`（`bhload` telnet 模組，直接编進 `nr-softmodem`/`nr-uesoftmodem`，非獨立 `.so`）、`radio/rfsimulator/simulator.c`（`librfsimulator.so`，跨主機同步時要一起重編；含 2026-09-26 起的**稀疏傳輸** patch：只送每個 slot 第一到最後一個非零取樣＋1 取樣結尾標記，接收端本來就會補零，取樣內容不變只降低傳輸量；`RFSIM_SPARSE=0` 可關閉回到完整傳送；另有通道模型融合讀取（`RFSIM_CHAN_FAST=0` 關閉）與**速度調節器**：伺服器端（gNB/DU）依各主機 build 目錄的 `cmake_targets/ran_build/build/rfsim_speed.txt`（值 S，模擬時間=牆鐘×S，可不重啟調整；檔案不存在=不調節）節流，**目前設 0.3**（三台都要一致，見第 8 節；檔案屬 root，用 `echo 0.3 | sudo tee <檔案>` 修改）。**所有 Stage 必須用同一版 `.so`、同一個 S**）。所有 DU／MT／UE 的 softmodem 一律帶 **`-E`（3/4 取樣率，106 PRB 下 46.08 Msps）**：rfsim 每條鏈路的 IQ 流量少 25%，吞吐量不受影響（實測），兩端必須一致（2026-10-01 起）。

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

**重啟前先清理：每次整套重啟前先在 PC1 跑 `bash iab/clean_env.sh`**（三台一次清掉殘留容器/行程/遺留 iperf3/暫存檔並驗證，不乾淨會 exit 1，此時不可重啟；docker volume 不動）。

**建議做法（2026-09-14 驗證更穩定）：完全依序啟動，不要三台同時跑**——先讓 PC1 的基礎設施（不含 E2 等待、不含 xApp 啟動）單獨跑完，再依序（不要同時）跑 PC2、PC3，最後回 PC1 做 E2 等待＋啟動 xApp：

```bash
# 1. PC1 基礎設施（CN5G/FlexRIC/MongoDB/Donor CU-DU/Node1,2,3,4 relay），跑完才繼續下一步
bash ~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator/iab/start_iab_server.sh

# 2. PC2 完全跑完，才換 PC3（不要背景同時跑兩台）
bash ~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator/iab/run_local_pc2.sh
bash ~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator/iab/run_local_pc3.sh

# 3. 回 PC1：等待 13/13 E2、啟動 relay 直連 UE17~24（start_relay_ues.sh）、啟動全部 xApp
bash ~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator/iab/run_local_pc1.sh --skip-server
```

**舊式三台同時跑的寫法**（`run_local_pc1.sh`（完整版）+ PC2/PC3 同時執行）理論上也能動，且過去多次量測都是這樣起的，但 2026-09-14 debug 過程中觀察到三台完全同時起跑時偶爾會出現 CU/DU 隨機崩潰（根因見第 7 節「容器隨機崩潰排查指南」第 1、2 點，不是這個同時啟動的寫法本身的 bug），目前**沒有辦法**100%排除同時啟動時的競爭條件，所以正式量測前建議一律用上面的依序寫法。

驗證：`docker logs flexric 2>&1 | grep -c "E2 SETUP-REQUEST"` 應為 `13`（1 donor + 12 node）；`docker inspect --format '{{.RestartCount}}' rfsim5g-donor-cu` 應為啟動前的原值（沒有新增崩潰）。**每次啟動或切換 stage 後、跑 15 分鐘量測前，一定要先對全部 24 個 UE 做一次現場 `docker exec <container> ping -c 2 <ext-dn-IP>` 確認 0% 封包遺失**——這是低成本前置檢查，能在花 15 分鐘量測之前就抓到連線缺陷，長時間偵錯累積的手動介入也可能讓個別 UE 處於「容器存活但資料面斷線」的狀態，靠繼續 debug 往往找不到，乾淨重啟（依上面的依序寫法）通常是最快的解法；若同一個節點反覆發生一樣的連通性問題（乾淨重啟後還是壞），才需要深入排查是否有真正的程式碼或設定 bug（見第 7 節）。

**網路健檢（2026-09-25 新增，每次啟動後、量測前必做）**：三台的實體網卡都是 USB 的 `r8152`，**必須接在 USB 3.x 埠**——USB 2.0 的實際上限只有 ~320Mbps，會讓所有 rfsim 跨主機鏈路（連續傳 IQ 取樣）變成「慢動作」，吞吐量、RTT 全被拉壞，而且會連帶拖慢其他主機。
```bash
# 三台各自執行（IF: PC1=enxc84d44350030 / PC2=enxc84d44350008 / PC3=enxc84d4427aa8f），應為 5000 或 10000，不是 480
cat $(readlink -f /sys/class/net/<IF>/device)/../speed
# 主機間原始 ping（rfsim 全開時）應 <1ms；若是數 ms~30ms 代表網路路徑有問題
ping -c 30 -i 0.1 -q 192.168.88.2; ping -c 30 -i 0.1 -q 192.168.88.3
```

**⚠️ 每次乾淨重啟後、啟動任何 `traffic_scenario.py`（不管是量測用的一次性呼叫，還是 `training_scenario_driver.sh` 的訓練用長駐呼叫）之前，必須先在 PC1 執行 `bash scenarios/setup_iperf_servers.sh`**：這支腳本在 `rfsim5g-oai-ext-dn` 容器裡啟動 24 個各自獨立的 iperf3 server（port 5201~5224，一個 UE 一個 port；`RELAY_UES=0` 時用 `IPERF_LAST_PORT=5216`）。`start_iab_server.sh` 自己內建的 `docker exec -d rfsim5g-oai-ext-dn iperf3 -s`（無 `-p` 參數，只監聽預設的 5201）**不是這支腳本的替代品**——用預設埠的單一 server 只能服務到剛好對應 5201 的那個 UE（依現行對照即 UE1），其餘 15 個 UE 的 iperf3 client 會持續 `connection refused` / `rc=1` crash-loop，且是靜默失敗（scenario log 只會印 `WARNING iperf3 supervisor 退出...重啟 loop`，不會讓整個腳本報錯、也不會讓 UE 的 ping 連通性檢查失敗），非常容易在乾淨重啟時被忽略，讓訓練或量測在「看起來正常運作」的情況下，實際上只有 1/16 UE 真正產生流量、其餘節點的 MAC 層狀態近乎閒置——訓練跟量測都會失去意義（現場案例見 `HISTORY.md` 2026-09-20 條目）。`training_watchdog.sh` 的 `full_recovery()` 目前**沒有**自動呼叫這支腳本，是已知缺口，之後排查「崩潰復原後訓練資料看起來正常但品質不對」時應優先檢查這裡。

server 用 `iperf3 -s -1`（服務完一個連線才退出、迴圈重啟），**不可再加 `timeout N`**：16 個 server 同時啟動，會每 N+1 秒同步被砍一次，讓所有 UE 的 client 同一秒集體 rc=1。場景跑完後應在 PC2/PC3 檢查 `sudo journalctl -k --since "-10min" | grep segfault`：曾發生 `nr-uesoftmodem` 在 `init_RA`（UE MAC RA 初始化，空指標）segfault，與場景的 UE 端通道變更同一秒發生，被 docker 自動重啟後 IP 改變、留下失效的 F1-U 位址，導致 UPF 在閒置時仍被封包迴圈吃滿，整個 pc3 掉包；發生時只能乾淨重啟（見 `HISTORY.md` 2026-09-26 續八）。`traffic_scenario.py` 內建 **CrashGuard**：場景開始時記下本機 UE/MT/DU 容器的 RestartCount/StartedAt，每 10 秒與每次套完相位後比對，發現重啟就記 ERROR、寫 `/tmp/scenario_invalid_<hostname>.txt`；有限相位/固定場景（量測）預設 `--on-crash abort`（清理後 exit code 2，該次量測無效），無限訓練預設 `warn`。量測腳本要檢查場景 exit code 與該標記檔。

### 啟動 Stage 2 起的 Global 層（avg FL / cluster FL / 自訂 FL）
```bash
# 前置：三主機 RAN 基礎設施（run_local_pc{1,2,3}.sh）已就緒、13/13 E2、12/12 xApp
REWARD_MODE=throughput_only bash ~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator/iab/run_stage2_fl.sh
```
這支腳本會停止並清空 12 個 `inference-nodeN` 的 MongoDB 經驗與模型 checkpoint、用指定的 `REWARD_MODE` 重新啟動、並帶起 `global-xapp`／`flower-superlink`／`flower-supernode-node{1..12}`／`flower-scheduler`（`profiles: ["stage2-fl"]`，Stage 1 PF 重跑時不受影響）。Stage 3/4 只需改 `server_app.py` 聚合邏輯後直接重跑；Stage 5 改用 `REWARD_MODE=lagrangian`。

---

## 7. 注意事項

（xApp 與共用底層檔案的完整路徑清單見第 5 節。）

> **規則**：刪除整份檔案需經過授權。所有修改先在 PC 1 完成，再將修改好的檔案傳給 PC 2、PC 3（`rsync`，見 `setup_lab_net.sh` 建立的 SSH 免密碼登入：`ssh pc2`／`ssh pc3`）。 **用 `bash iab/sync_pc23.sh`（dry-run 比對，加 `--apply` 同步）**：以校驗碼（不是時間戳）比對整個 repo 原始碼與 PC2/PC3 的差異、同步、再比對，並檢查 `nr-softmodem`／`nr-uesoftmodem`／`librfsimulator.so` 是否比對應原始碼舊（舊就必須在該主機 `sudo ninja` 重編）；不同步 `conf/iab_du_node*.conf`（啟動腳本執行期覆寫的佔位值）。PC2/PC3 各自在本機重新編譯（`build_oai`／FlexRIC cmake），不是直接搬二進位檔——因為 docker-compose 用**絕對路徑** bind-mount 宿主機編譯產物（例如 `/home/lindor/openairinterface5g/cmake_targets/ran_build/build/...`），PC2 帳號是 `mcalab` 但仍在 `/home/lindor/openairinterface5g` 編譯（已手動建立 `/home/lindor` 目錄供其使用），確保路徑跟 compose 檔一致。
>
> **⚠️ `rsync` 完只是同步了原始碼，不代表 PC2/PC3 已經吃到修改**：一定要在 PC2、PC3 上各自重新執行編譯指令，且要重編**全部受這次修改影響的 build target**，不是只重編「看起來相關」的那一個——2026-09-13 debug session 曾經漏編 `radio/rfsimulator/simulator.c` 對應的 `rfsimulator` target（只重編了 `nr-uesoftmodem`/`nr-softmodem`/`telnetsrv`），導致 PC2、PC3 的 `librfsimulator.so` 停留在舊版本，三台主機表面上都跑著「同一份原始碼」，實際上只有 PC1 真正生效，另外兩台主機的行為跟修 bug 之前完全一樣，卻很容易被誤判成「已經全部修好」。修改共用檔案後，收工前務必在三台主機上都做這個檢查：
> ```bash
> stat -c '%Y %n' <改過的原始碼檔案>
> stat -c '%Y %n' <對應編譯產物，例如 librfsimulator.so / libtelnetsrv.so / nr-softmodem / nr-uesoftmodem>
> ```
> 確認每一台主機上編譯產物的時間戳都新於原始碼的時間戳，時間戳比原始碼舊就代表沒吃到這次修改，必須重編。哪個原始碼檔案對應哪個 build target，用 `grep -rn <檔名> --include=CMakeLists.txt .` 或 `grep -rn <檔名> CMakeLists.txt`（頂層）查，不要用猜的——這個專案裡不少檔案（例如 `telnetsrv_bhload.c`）是直接编進某個執行檔而非獨立成 `.so`，跟其他 telnetsrv 模組的慣例不同。

### 狀態觀測窗口（1 秒）與正規化常數

xApp 訂閱 E2SM-MAC 的回報週期是 100ms，C xApp 的 Rate Limiter 每 10 次回報才觸發一次 ZMQ，因此每筆 State 的 `bsr` 欄位是 **~1 秒累積的 `delta_dl_aggr_tbs`（bytes/視窗）**（實測 MongoDB 文件間隔 1.00 秒；2026-09-26 之前文件與 `MAX_BSR` 都誤以為是 100ms）。對應的正規化常數：

```
MAX_BSR = 2,000,000 bytes/視窗（reward_calculator.py，環境變數 REWARD_MAX_BSR；S=0.4 時期單 UE 峰值約 2MB/視窗、實測 UE bsr p90 ~60~85 萬、max ~1.46M；一度誤改成 250,000 使 r_throughput 的 p90 頂到 1.0，已改回。若改 S 或控制週期須同步調整）
MAX_BSR = 2,000,000 bytes/視窗（drl_agent.py，state 編碼 log 正規化用，與上面同值）
MAX_BUF_INFO = 2,000,000 bytes（drl_agent.py，dl_buffer_info 正規化上限）
```

`reward_calculator.py` 中的所有吞吐量計算均以其 `MAX_BSR` 為分母；`drl_agent.py` 的 state 編碼另有一組獨立常數。若未來修改 Rate Limiter 的觸發間隔，這些常數必須同步調整。

**State vector 第 49 維（`fairness_bias`，Stage 2 起改版，取代舊版 2026-07-09 的 `prb_quota_ratio`）**：

```
FAIRNESS_BIAS_MIN = 0.5（drl_agent.py，Global xApp 廣播值域下限）
FAIRNESS_BIAS_MAX = 2.0（drl_agent.py，Global xApp 廣播值域上限）
GLOBAL_XAPP_INTERVAL_S = 2.0（global_xapp.py，每幾秒重算一次全部節點的 fairness_bias，預設值）
GLOBAL_XAPP_LOOKBACK = 300（global_xapp.py，計算節點平均吞吐量時往回看幾筆經驗，預設值；每節點 1 筆/秒 → ≈5 分鐘）
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

### 容器隨機崩潰／連線隨機斷線排查指南（2026-09-14 定案）

如果三主機乾淨重啟後，`rfsim5g-donor-cu`／DU／MT **隨機**崩潰或重啟（`docker inspect --format '{{.RestartCount}}'` 不斷增加，且每次壞掉的節點都不一樣、跟「哪個腳本最後跑完」看起來沒有固定關係），依照以下順序排查，不要一開始就假設是既有 RAN C code 的問題：

1. **先檢查是不是 `cpuset` 把太多 process 塞進同一組核心**：`docker-compose-iab-server.yaml` 目前只有全部 12 個 `inference-nodeN` 服務釘在 `cpuset: "12-15"`（4 核心，Stage 2 上線時就有的既有設計，長期驗證穩定）。**任何要新增 cpuset 隔離的容器（例如 Global xApp／Flower FL 服務），都不能無條件套用同一組 `"12-15"`**——2026-09-14 debug session 曾經把 `global-xapp`／`flower-superlink`／12 個 `flower-supernode-nodeN`／`flower-scheduler`（共 15 個服務）也全部加上 `cpuset: "12-15"`，變成同一組 4 核心要塞 27 個 process，即使瞬時 CPU 使用率看起來不誇張（`mpstat` 平均只有 30% 上下），仍然造成偶發性的排程延遲尖峰，讓 RT-priority 的 CU/DU SCTP/RRC 計時器偶爾錯過期限而 crash-restart，進而讓所有依賴 CU 當下 netns 的 DNAT/路由設定失效——這正是「不管怎麼修 NAT/路由、過一陣子又壞、每次壞的節點都不一樣」這種難以定位症狀的真正源頭。**排查方法**：`docker inspect --format '{{.HostConfig.CpusetCpus}}' <container>` 列出目前所有容器的 cpuset 分組，數一數同一組核心裡總共塞了多少 process，跟核心數（`nproc`）比較是否明顯失衡；懷疑是這個原因時，直接把新加的 cpuset 限制拿掉做 A/B 對照（拿掉後乾淨重啟，觀察 CU RestartCount 是否維持 0），比繼續往下挖 NAT/路由邏輯快得多。
2. **確認 CU 的 NAT OUTPUT table 沒有被意外整批清空**：`iab/start_iab_pc2.sh`／`start_iab_pc3.sh`／`start_iab_server.sh` 的 DNAT 規則寫入邏輯全部使用 `iptables -t nat -I OUTPUT 1 ...`（插入到最前面），**不會**、也**不能**對整條 `OUTPUT` chain 做 `-F` 全部清空——早期版本 PC2 的腳本會在自己的收尾步驟對 CU 的 NAT OUTPUT table 做無條件 `-F`，只要這個 flush 跑在其他主機已經寫好自己節點 DNAT 規則**之後**，就會把其他主機的規則整批砍掉，且沒有人會補回來，造成跟第 1 點類似的「隨機哪個節點斷線」症狀（但成因完全不同：這個是規則被砍，第 1 點是 CU 本身真的 crash）。三個腳本現在都已改成「不 flush、只在最前面插入」，若未來又看到類似症狀，先確認相關腳本有沒有被還原成舊版的 flush 寫法。
3. **確認三主機啟動順序**：即使上述兩點都排除，仍建議依序（不要三台完全同時）執行：先跑 `bash iab/start_iab_server.sh`（只做 PC1 基礎設施＋Node1,2,3,4 relay，不等 E2、不啟動 xApp）→ 等它完全跑完 → 依序（不要同時）跑 PC2、PC3 各自的 `run_local_pc{2,3}.sh` 並各自等到完全跑完 → 最後跑 `bash iab/run_local_pc1.sh --skip-server`（只做 E2 等待＋啟動 xApp）。這個順序讓三主機的 CU 操作完全不重疊，實測比三台同時起跑更穩定（2026-09-14 驗證：依序啟動後，15 分鐘正式量測全程 CU/DU/MT RestartCount 維持 0）。
4. **自我修復機制**：三個 `start_iab_*.sh` 腳本收尾都有 `verify_and_heal_ues()`（PC1 2026-09-22 後不再有任何 UE，改成 `heal_ra_exhaustion_local()` 只檢查 4 個 relay DU），會對自己負責的 UE 做 ping 驗證，失敗時自動重新斷言 MT 路由＋CU DNAT 規則，最多重試 5 次（每次間隔 15 秒）。若腳本印出「重試 5 次後仍有 UE 連不通，需要人工檢查」，通常代表當時 CU 或該節點的 DU/MT 剛好處於第 1、2 點描述的不穩定狀態，等它穩定後手動重跑一次 `docker exec -u 0 <mt> ip route replace ...`＋CU 端 `iptables -t nat -I OUTPUT 1 ...`（或直接重跑該主機的腳本）通常就會通。

---

## 8. 流量與路徑損耗場景設計

`scenarios/traffic_scenario.py` 提供多種流量場景，透過 `channelmod_ctrl.py` 對 OAI rfsimulator 的 telnet chanmod 介面（`channelmod modify <ue_id> ploss <val>`，即時生效不需重啟容器）動態調整每個 UE 的路徑損耗，模擬通道劣化環境。

**主場景（2026-10-01 定案、2026-10-02 改版並經平台驗證，`SYSTEM_SPEC.md` §8.1 D6）**：**HS**（對稱）每個壅塞相位一個「PB 結構」熱點 branch，依相位均勻輪替：熱點 relay 的兩個直連 UE 以 80% 機率在細胞邊緣 L24（該讓 slot 給 backhaul），否則好通道 L10（不該遮）；下游一個 access 節點極重（2×L10、每 UE 18～24）、一個中等（2×L10、每 UE 10～14）；relay UE 需求 6～10；其他三個 branch 輕負載（每 UE 0.3～1.0）。**第四版（2026-10-03）另在一個非熱點 branch 放一個混合 access 節點**（M 類 60%：好 UE L22 需求 20～24＋壞 UE L24 需求 5～7，該遮壞 UE；N 類 40%：好 UE 需求 7～9，不該遮），讓 relay 與 access 都有依狀態而定的決策；壅塞總需求平均約 115（第三版 94）。平台驗證：HS TCP（只跑壅塞相位）動態規則對 PF 87／82、88／81（seed 1001／1002，平均 +7.3%），access M 類節點遮壞 UE +36%（場景 PA 固定配置 +12～14%）、N 類遮了 −17～−25%，見 `HISTORY.md` 續五十九～六十。HS／HSH 的 PF 基準須依第四版重量（`PF.md` 現有 HS／HSH 數字屬第三版）。**HSH**（持久異質）同 HS，熱點 branch 依固定權重抽（實際分布約 [26,54,9,12]%）。**G**（一般隨機，壅塞總需求 115，泛化／不傷害檢查，未改）。相位 110 s、11 相位一週期、壅塞相位同 T。**平台驗證（2026-10-02）**：HS TCP 壅塞總送達 PF 70／69 對動態規則（`XAPP_MODE=rule RULE_KIND=dyn`）79／78（seed 1001／1002），+12.9%。舊版 HS（混合 access 節點結構）平台實測無增益（好 UE 在 PF 下已被滿足、relay MT 不積壓），已移除，過程見 `HISTORY.md` 續五十七。訓練家族 `hs`＝(HS,HS,G,HS,HS,G)、`hsh`＝(HSH,HSH,G,HSH,HSH,G)，TCP／UDP 交替；訓練 seed 從 HS 165000／HSH 170000／G 175000 起，量測 seed 20260930（場景驗證用 1001／1002）。CLI：`--scenario {HS,HSH,G} --seed <n> --host {pc1,pc2,pc3}`（三台都要跑，PC1 控制 relay UE）。

**場景清單（含舊場景）**：A（CQI 差異化）、B（流量不均）、C（最差公平性）、D（動態訓練，均勻隨機）、**R（真實隨機）**、**T（兩狀態壅塞，量測基準用，見下）**、**TH（T 的持久異質性版本，2026-09-29 新增，見下）**、**P（混合通道壅塞試驗用固定配置）**、**TM／TMR（混合通道壅塞候選基準與其訓練用隨機版，2026-10-01，見下）**。Scenario R：面積均勻抽樣模擬細胞邊緣 UE 較多（場景損耗指標 L 最高 24，不用 25）、每 UE 每相位依 **idle : burst : traffic = 1 : 2.5 : 6.5**（10%:25%:65%）抽三態（idle 整相位不傳、burst 高需求 8~16 sim Mbps、traffic 一般流量依持久化 profile heavy/light/bursty 為 1~8 sim Mbps；量級與 Scenario T 同為模擬時間 Mbps，期望每 UE offered ≈5.2、16 UE 總 offered 平均 ≈83）、TCP/UDP 混合（`P_UDP=0.25`），並支援 `--seed` 重現同一串隨機條件供 PF vs DRL 的 paired comparison 使用、`--phase-origin` 讓相位連號。R 沒有「壅塞/正常」狀態標籤，`analyze_stage.py` 只適用 Scenario T／TH（TH 重用 T 的相位標籤邏輯）。 **目前實驗（Stage 1~5 比較）以 HS／HSH 雙軌為主（見上方主場景）**，T／TH 為舊基準；R 已改好但未在真實平台驗證，需要時再跑短測。

**Scenario TH（T-Heterogeneous，2026-09-29 新增，2026-09-30 改版，`scenarios/traffic_scenario.py::scenario_th_heterogeneous()`）**：**每個相位、每台主機用的（流量×通道）組合跟 Scenario T 完全相同**（同一組 `NORMAL/CONGESTED_TRAFFIC_TIERS`／`PLOSS_TIERS`、同樣兩狀態壅塞週期，總負載與各主機負載都跟 T 逐相位相等），唯一差異是「哪個 UE 拿到哪一個組合」：先取 T 在這台主機、這個相位會用的那批組合由易到難排好，每個 UE 的排序分數＝所屬節點的 `BRANCH_HARDSHIP[node_id]`（逐節點由固定結構性種子 `TH_HARDSHIP_SEED=999999999` 導出、值域 [-1,1]、跟 `--seed` 無關）＋均勻隨機 ±`TH_RANK_NOISE`（0.5，由 `_rng_for(seed, global_id, phase_index)` 導出），分數低的拿較易的組合。離線驗證：2 主機 × 3000 相位，組合集合跟 T 0 個不一致；各節點長期平均需求 1.2~9.6 sim Mbps（節點間標準差 ≈2.96，99~4950 相位都穩定，不會平均掉），全體平均與 T 同為 5.85。**T 與 TH 只差異質性、不差總負載**（09-30 前的舊版把組合編號往難的方向平移 round(hardship×4) 格，節點難度平均偏正又集中在 PC2，總負載明顯比 T 重，T/TH 同時差兩件事，已改掉）。量測一律用固定 seed（`run_stage_measure.sh` 的 `MEASURE_SEED`，預設 20260930）。CLI 用法同 T／TR：`python3 scenarios/traffic_scenario.py --scenario TH --seed <n> --host <pc>`。設計動機見 `HISTORY.md` 2026-09-29、09-30 條目。

**Scenario TM／TMR（2026-10-01，16 UE 時期的候選基準，已由 HS／HSH 取代，保留作機制說明）**：現行 T／TH 的壅塞相位同節點兩 UE 通道相近，資源重新分配幾乎沒有空間（理論上限只比 PF 高 1~3%，見 `HISTORY.md` 續四十三）。TM（`scenario_tm_mixed()`）保留 T 的骨架（11 相位、壅塞相位 2/4/5/7/9、正常相位＝T），壅塞相位每個 branch 一個主動節點＋一個輕負載節點（2×L=10、需求 1）：4 個 branch 中 3 個是 M 類（好 UE L=22 需求 18~22＋壞 UE L=23/24 需求 6~8，該遮壞 UE）、1 個是 N 類（好 UE 需求 8、本來就吃得飽，遮了反而虧），全部隨 phase_index 輪替。PF 基準（`/home/lindor/tm_pf_20261001/`）：壅塞相位 TCP 送達 74／目標 102、滿足率 0.859；UDP 76／102、0.912。TMR（`scenario_tm_random()`，`--scenario TMR --seed`）是訓練用隨機版：壅塞機率同 TR、N 類 branch 數 0/1/2 隨機，訓練驅動器 `--scenario-family tm`＝(TMR,TMR,R,TMR,TMR,R)。場景 P（`scenario_p_pilot()`）是驗證控制方式用的固定配置。

**UDP 版本（2026-09-25 起，`--protocol {tcp,udp}`）**：`traffic_scenario.py` 的 T/TH/R/A/B/C 六個場景都支援 `--protocol udp`，把全部 UE 統一改用 UDP 跑，通道/頻寬設定不變、只換傳輸層，供 TCP/UDP 1:1 對照。未指定時各場景維持原行為（T/TH/A/B/C=tcp、R=TCP/UDP 混合）；R 指定 `--protocol` 時同 seed 的路徑損耗/頻寬/閒置抽樣序列與原本完全一致，只有協定被覆寫。UDP 模式下 DL frozen watchdog 會略過該 UE（UDP 沒有壅塞退讓，重啟 iperf3 不會有幫助，反而會覆寫 log 抹掉 `measure_stage.py` 的取樣）。

```bash
# 測試（UDP）
python3 scenarios/traffic_scenario.py --scenario T --protocol udp --host pc2 --num-phases 15
# 訓練：驅動器在 PC1（relay 直連 UE17~24）、PC2、PC3 各跑一份、用同一個 --epoch；不要帶 --protocol
# （帶了會覆寫全部 slot 的協定）。watchdog 在 PC1 跑一份，--epoch 同上（崩潰復原後也會在三台重啟驅動器）。
EPOCH=$(date +%s)
nohup bash iab/training_scenario_driver.sh --host pc1 --epoch $EPOCH > /tmp/driver_stage_pc1.log 2>&1 &
ssh -f pc2 "cd ~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator && nohup bash iab/training_scenario_driver.sh --host pc2 --epoch $EPOCH > /tmp/driver_stage_pc2.log 2>&1 < /dev/null"
ssh -f pc3 "cd ~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator && nohup bash iab/training_scenario_driver.sh --host pc3 --epoch $EPOCH > /tmp/driver_stage_pc3.log 2>&1 < /dev/null"
nohup bash iab/training_watchdog.sh --epoch $EPOCH > /tmp/training_watchdog.log 2>&1 &
```

**訓練用場景（`training_scenario_driver.sh`，2026-09-26 重寫）**：只輪替兩種、都已對齊新平台量級——**`TR`**（隨機化的兩狀態 T：同固定 T 的檔位與 ~45% 壅塞比例，但壅塞相位排列與每 UE 的組合由 `--seed` 打散、16 個 UE 各拿一份洗牌後的組合，跨主機由 (seed, phase_index) 決定式導出）與**新版 R**（idle:burst:traffic=1:2.5:6.5，內建 TCP/UDP 混合）；TR 的 slot 交替 TCP／UDP，一輪 13200 秒（≈3.7 小時）循環，每輪 seed 不同。**不含固定 Scenario T（測試基準，避免訓練=測試同分佈）與 A/B/C（流量仍是舊量級，17 UE 加總 400~850 sim Mbps，會壓垮平台）**。訓練時 CrashGuard 用 `--on-crash warn`，崩潰復原由 `training_watchdog.sh` 負責：復原前先 `clean_env.sh`（`CLEAN_ENV_KEEP_WATCHDOG=1` 不殺自己）、復原後 24 個 UE（含 PC1 的 relay 直連 UE）逐一驗證與修復、重建 24 個 iperf3 server、在三台重啟驅動器；另偵測三台 UE/MT/DU 容器的 RestartCount（UE 崩潰會造成整台掉包，原本只看 CU/DU/FlexRIC 抓不到）。**終端 UE 容器崩潰（OAI UE 端 assertion 退出，約每 10 分鐘可能一次）走「就地修復」、不做整套重啟**：docker 自動重啟後重新斷言 UE 預設路由（`ip route replace default via 12.1.1.1 dev oaitun_ue1`）並 ping ext-dn 驗證，每顆最多 8 次、第 5 次起重啟該 UE 容器；修不好、或修完後同主機 ≥3 個其他 UE 不通，才升級為完整重啟。MT/DU/CU/FlexRIC 崩潰仍走完整重啟（冷卻窗內再崩潰則停止並要求人工介入）。

**平台容量與量測注意事項（2026-09-26）**：
* **速度調節器與時間膨脹**：rfsim 預設「跑多快算多快」，網路不再是瓶頸後會吃光 PC2/PC3 CPU（17 個即時程序互搶→秒級延遲、掉包）。以速度檔設 **S=0.3**（2026-10-01 由 0.4 改為 0.3，使用者決定；原因：relay 直連 UE 時 PC1 USB 網卡被跨主機 IQ 吃滿、relay DU 被拖慢，S=0.3 加 `-E` 後全負載下所有 DU 穩定在設定速度，見 `HISTORY.md` 續五十三。以下為 0.4 時期的取捨數據：TCP 灌滿 4 UE 時牆鐘總量 42.6 Mbps＝~106 模擬 Mbps，標準差 4.7 為所有 S 最穩、CPU 閒置 ~55%）。S=0.5 為上限（PC2 負載下閒置僅 12.5%）、≥0.6 負載下 CPU 飽和。CPU 上限的模擬時間負載約 100 Mbps 不隨 S 變，**降低 S 是用時間換 CPU 餘裕**。**模擬時間 = 牆鐘 × S**：牆鐘量到的吞吐量 = 模擬時間吞吐量 × S，RAN 相關 RTT 牆鐘值 = 模擬值 ÷ S；所有 Stage 用同一個 S，論文方法論須說明。
* **容量（S=0.5 實測，牆鐘 Mbps；÷S 為模擬時間）**：單 UE TCP 下行 32~38；全系統總容量平台 ~54（~108 模擬），與 UE 數/協定無關，是 **CPU 限制**；CPU 餘裕 ≥20% 的最大總 offered 約 32~35 牆鐘。每條跨主機 rfsim 鏈路即時需 ~2 Gbps，網卡曾是瓶頸。
* **場景頻寬單位 = 模擬時間 Mbps**：`start_iperf_client()` 啟動時乘上 S 換成牆鐘 `-b`，場景設定與 S 無關；量到的牆鐘吞吐量要 ÷S。**Scenario T 為兩狀態設計**（`NORMAL_*`／`CONGESTED_*` 檔位，11 個相位一週期，第 2/4/5/7/9 個為壅塞相位＝45.5% 的時間；狀態內保留 3×3 流量×通道輪替）：正常相位 流量 1/4/8、L=3/10/18（總 offered ≈74，全送得完）；壅塞相位 流量 1/10/12、L=22/23/24（同節點兩 UE 合計容量 23/17.6/13，約 5/11 的 UE 過載，總送達 ≈95 對目標 ≈128）。量測用 `--phase-origin <epoch>`（兩台主機傳同一個值）使相位剛好 `--phase-duration` 秒且連號；未指定則相位會因套用通道耗時而跳號。容量曲線與設計推導見 `HISTORY.md` 2026-09-26（續十）。**不要用 L=25**（容量 5.4、近斷線邊緣）。
* **量測方法**：一律用 `-R`（下行）看**接收端**報告；UDP 傳送端永遠顯示送出速率與 0% 遺失；不要用 offered ≫ 容量估容量；TCP 下「冷 UE」（第一次傳的 UE）吞吐量偏低，成因未明。
* **協定/資料**：TCP 與 UDP 狀態分佈不同，切換協定前比照 `REWARD_MODE` 先清空 MongoDB 經驗與 checkpoint。
* **`measure_stage.py`**：2026-09-25 前的 regex 讀不到 `0.00 bits/sec`，該日前的 CSV（含 Stage 1~3）平均值與 JFI 偏高。

**通道惡化機制（2026-09-26 重寫，所有場景 A/B/C/D/R/T 共用）**：
* **rfsim 通道模型只作用於接收端**：DU 端的 `rfsimu_channel_ue*`（`ChannelModController`）只影響上行；下行要改 **UE 端** `rfsimu_channel_enB0`（終端 UE 用 `nrue.uicc.chanmod.conf`，telnet port 9301，`UEChannelController` 經 `docker exec` 連入）。
* **`ploss` 的符號：正值是增益、負值才是衰減**（`pow(10, ploss/20)`）。正值太大會 int16 削波；負值/雜訊太大會讓 UE 斷線且**不會自動恢復**（需乾淨重啟）。已知會斷線的點：UE 端 (ploss -15, noise -6)、ploss ≥ +36。
* **場景只惡化下行、上行維持正常通道**：場景損耗指標 L（0~25）→ s=L/25 → 沿 `DEGRADE_PATH` 內插 UE 端 (ploss, noise)：s=0→(0,-50)、0.25→(-5,-20)、0.5→(-5,-10)、0.75→(-10,-10)、1→(-10,-6)；實測下行 MCS 由 28 單調降到 3~4（T 的低/中/高檔位 = MCS ~28/~23/~8）。`--no-dl-degrade` 或 `SCENARIO_DL_DEGRADE=0` 關閉。`PATHLOSS_SAFE_MAX_DB=25.0` 只是 L 的上限（s=L/25 的分母），不是實際 dB；A/B/C/D 的 CQI 經 `cqi_to_pathloss` 對照表轉成 L（舊的 `--calibrate` 流程量的是上行增益，已失效，勿用）。校準數據見 `HISTORY.md` 2026-09-26（續四）。

**跨主機執行**：telnet chanmod port 原則上只在該主機本機（`127.0.0.1`）可連，`traffic_scenario.py` 用 `--host {pc2,pc3}` 各自在本地執行、只套用自己負責的 UE/Node 子集；兩台主機用同一個 `--seed`，每個 UE 每個 phase 的隨機值用 `(seed, ue_global_id, phase_index)` 三元組獨立導出（`phase_index` 以絕對時間換算），不需要跨主機即時通訊即可保持同步。PC1 用 `--host pc1` 控制 relay 直連 UE17~24（下行通道經 UE 端 chanmod，`docker exec` 本機容器）；PC1 只有 relay UE 時，`scenario_configs()` 會補印相位狀態 log，量測分析才能對齊相位。

```bash
# PC2：控制 UE1~8
python3 scenarios/traffic_scenario.py --scenario R --seed 42 --host pc2

# PC3：控制 UE9~16
python3 scenarios/traffic_scenario.py --scenario R --seed 42 --host pc3

# PC1：控制 relay 直連 UE17~24（目前所有場景對它們一律閒置，見 relay_ue_configs()）
python3 scenarios/traffic_scenario.py --scenario R --seed 42 --host pc1
```
