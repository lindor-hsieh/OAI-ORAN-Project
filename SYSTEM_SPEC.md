# IAB + O-RAN 系統規格（SYSTEM_SPEC）

> **用途**：這份文件定義實驗系統「應該是什麼樣子」，是之後所有平台設定、實驗設計、論文第 3 章的唯一依據。
> 先逐項定案（第 11 節的決策清單），再照第 10 節逐項驗證平台，最後才量基準、開始訓練。
> CLAUDE.md 描述「平台現在怎麼運作、怎麼操作」；HISTORY.md 記錄過程；本文件記錄「設計決定與依據」。
>
> **狀態標記**：✅ 已定案（有依據）　⚠️ 現況存在但定位待確認　❓ 待決定
> **依據標記**：〔實測〕平台量測（附位置）　〔程式〕程式碼或設定檔　〔文獻〕已讀原文確認　〔未驗證〕只有轉述或推論
>
> 起草：2026-10-01。所有數字的來源在括號內，查不到依據的地方明確標為未驗證。

---

## 1. 研究問題（草稿，待確認）

1. 在多跳 IAB 網路中，部署在 O-RAN Near-RT RIC 的 DRL xApp，能否在 PF 之上找到更好的資源分配？
2. 聯邦學習能否讓分散在各 IAB 節點的 agent 學得更快、更穩，並在節點間持久異質時仍然有效？
3. （待第 2.3 節決定後確認）多跳結構（backhaul 匯聚）對可改進空間與學習的影響為何？

---

## 2. IAB 架構

### 2.1 拓樸 ✅

1 donor + 4 relay + 8 access + 24 UE；每個 relay 帶 2 個 access 與 2 個直連 UE（UE17~24，2 跳，2026-10-01 起），每個 access 帶 2 個 UE（UE1~16，3 跳）。relay 直連 UE 的場景角色待定（目前閒置）。
〔程式〕CLAUDE.md §1。三主機部署：PC1＝donor、4 個 relay、全部 xApp／inference；PC2／PC3＝各 4 個 access 與其 UE。
拓樸已定案、不再更動（relay 全部與 donor 同機是公平性必要條件，見 HISTORY.md 2026-09-22）。

### 2.2 節點架構 ✅

- 每個 IAB 節點＝MT（以 UE 身分附著 parent 的 DU）＋DU（服務子節點），MT 與 DU 共用同一個 network namespace。
- **沒有 BAP 層**：節點間以 Linux IP routing 轉送；每個 MT 只有一個 PDU session／一個 DRB → parent DU 對每個子節點只有一條 RLC 佇列。
- donor＝CU＋DU，有線接核心網。
〔程式〕CLAUDE.md §1、§2 的標準差異表。

### 2.3 頻段／雙工模式 ✅ out-of-band（決策 D1，2026-10-01 定案）

**現況事實**：
- 〔程式〕所有 DU（donor、relay、access）設定同一個載波：n78、`absoluteFrequencySSB=621312`、`dl_absoluteFrequencyPointA=620040`、
  30 kHz SCS、106 PRB（40 MHz）。`conf/donor_du.conf`、`conf/iab_du_node*.conf`。
- 〔程式〕rfsimulator 讓每條鏈路是獨立的 socket 連線：**沒有同頻干擾**，MT 與 DU 是不同行程、各自連線，**可以同時收送（等同全雙工）**。
- 結論：**名義上是 in-band 設定，實際行為等同 out-of-band**（各鏈路資源互不排擠）。

**為什麼重要**：IAB 資源受限的主因是 in-band 半雙工（MT 與 DU 必須切分同一份時間資源）與多跳重複傳輸佔用頻譜
〔文獻〕Ericsson Technology Review「first backhaul hop must carry the backhaul bandwidth ... for all other IAB nodes further down」；
Polese et al. 2018（arXiv:1808.00376）指出 donor 要承載所有下游流量是 IAB 主要瓶頸之一。平台目前兩者都沒有，所以 backhaul 從不受限
（〔實測〕relay DU 只用 15～18% 的 RB、子節點佇列 p99 10～39 KB；HISTORY.md 續四十三之後的討論）。

| 選項 | 內容 | 依據 | 平台改動 | 風險 |
|---|---|---|---|---|
| **A. 宣告為 out-of-band** | backhaul 與 access 用不同頻段，MT／DU 可同時運作；backhaul 受限來自「匯聚流量 vs backhaul 頻寬」 | 〔文獻〕Topcu et al.（論文庫 Read/12）：out-of-band 為正式選項，使用分開的頻率資源 | 小：改 donor／relay DU 的頻寬（見 3.2），處理 MT–DU 耦合代理（見 3.3） | 中：DU 換頻寬後 MT 接續與穩定性需驗證 |
| B. 模擬 in-band 半雙工 | 每個節點的時間切成「MT 收」與「DU 送」互斥時段（H/S/NA 時域配置），parent 只在對應時段排程 MT | 〔未驗證〕3GPP TR 38.874 的半雙工限制（只見轉述，未讀規格原文） | 大：改 OAI 排程器（節點層級時段劃分＋parent 配合），三台重編 | 高：新排程機制在 rfsim 下的穩定性未知 |

**定案：A（out-of-band）**。依據：論文庫中與本研究最相近的兩篇都採用 out-of-band——〔文獻〕Read/13 OpenIAB（OAI 實作的 O-RAN IAB 平台）：
「IAB-Nodes are naturally deployed over two separate machines, hosting the gNB and the UE, and connected out-of-band」；Read/12 Topcu et al.：
「a holistic, multi-objective optimisation framework for out-of-band IAB in O-RAN」。以數學模型／模擬為主的論文（Read/06、Read/10、Unread/05）
多採 in-band 半雙工＋TDMA。論文需寫明「系統為 out-of-band IAB；in-band 半雙工列為未來工作」。

**out-of-band 下做排程的文獻（2026-10-01 網路查證）**：
- 〔文獻〕Topcu, Zaidi, Lawey, IEEE OJCOMS 2025（DOI 10.1109/OJCOMS.2025.3618006）：O-RAN＋out-of-band IAB，在 DU 層做使用者間的
  RB／功率／numerology 分配（MILP＋基因演算法，模擬）——與本論文「DU 對子節點分 RB」最接近。
- 〔文獻〕Hu, Liu, Yan, Blough, WNGW 2019「End-to-end Simulation of mmWave Out-of-band Backhaul Networks in ns-3」：在 ns-3 做 out-of-band
  backhaul 模組並改寫排程器，最佳排程比預設多 40% 吞吐量（某些情境）。
- 〔文獻〕Moro et al. 2023 OpenIAB（arXiv:2305.06048）：OAI＋O-RAN 的 IAB 實驗平台，out-of-band 部署（平台論文，非排程演算法）。
- 〔文獻〕Zhang, Kishk, Alouini 2021 IAB 綜述（Frontiers / arXiv:2101.01286）：3GPP 定義 in-band 與 out-of-band 兩種 relaying。
- 注意：以 DRL 做 IAB 排程的論文多數假設 in-band；out-of-band＋學習式 UE 排程的直接前例沒有找到，論文需說明本研究的資源競爭
  來自 DU 對多個子節點的排程（access 層），而非 MT／DU 互斥。

**B 的離線評估（2026-10-01，〔模型〕`/home/lindor/pf16_run_20260930/bh_model/inband.py`、`run_inband2.py`）**：
每個節點 MT 佔 β 比例的下行 slot、DU 用其餘時間，同一 106 PRB 載波；流體近似，未含同頻干擾、上行半雙工、保護時間。
「PF 固定」＝PF 排程＋全場景最佳的固定 β（relay 0.25～0.35、access 0.15）。壅塞相位：

| 場景／協定 | OOB PF | in-band PF 固定 | ＋逐相位調 β（PF 排程） | ＋最佳排程（β 固定） | 聯合上限 |
|---|---|---|---|---|---|
| TM TCP | 78.2 | 69.0 | +0.0% | +6.6% | +11.6% |
| TM UDP | 78.2 | 70.3 | +0.0% | +4.7% | +9.7% |
| T TCP | — | 84.2 | +0.2% | +3.5% | +4.9% |
| T UDP | — | 82.3 | +0.5% | +5.9% | +7.3% |

聯合上限裡把 β 拆開放：**只放開 relay 的 β：+0.0%（TM、T 皆然）；只放開 access 的 β：TM +4.7%、T +1.3%**。
結論：改成 in-band 後 relay 仍然沒有可學的決策——relay 的上下游都是高效率 backhaul（91.3 B/RB），只需少量時間，
瓶頸永遠在 access DU→UE（L22～24 只有 9～19 B/RB）。β 的價值只出現在 access 節點、且要搭配 UE 排程一起最佳化；
代價是 TM 壅塞相位 PF 吞吐量從 78.2 降到 69.0（−12%）。B 不能解決「relay 沒東西學」，維持 A。
relay 要有決策，需要 relay 本身也是瓶頸：例如 relay 也直接服務 UE（一般 IAB 文獻每個節點都服務 UE），或 backhaul 鏈路品質變差／會變動。

### 2.4 鏈路分類 ✅（由拓樸決定）

| DU | 服務對象 | 鏈路類別 |
|---|---|---|
| donor DU | 4 個 relay 的 MT | backhaul（第一跳） |
| relay DU | 2 個 access 的 MT | backhaul（第二跳） |
| access DU | 2 個 UE | access |

---

## 3. 無線資源

### 3.1 數值參數 ✅

〔程式〕30 kHz SCS（μ=1，slot 0.5 ms）；TDD 週期 5 ms＝7 DL slot＋1 special（6 DL／4 UL symbol）＋2 UL slot；`min_rxtxtime=6`；
SISO；最高 256QAM。MAC 層峰值（TS 38.306，全符號下行）≈227 Mbps，扣 TDD 後下行約 159～169 Mbps（CLAUDE.md §2）。

### 3.2 backhaul 與 access 頻寬 ❓（決策 D2，依 D1）

- 現況：全部 106 PRB。
- 若 D1＝A：backhaul 頻寬是獨立的系統參數。要讓 backhaul 在壅塞時成為瓶頸、正常時不是，需依匯聚流量決定頻寬。
  **重要限制**：donor 承載全部 UE 流量，backhaul 一縮窄第一跳最先受限；donor 目前不是 agent（見 6.4），瓶頸落在 donor 只會壓低吞吐量、
  不會給 agent 決策空間。donor 與 relay 的 backhaul 頻寬可能需要不同。
- 〔實測，估計〕TM 場景下 relay DU 用約 10,000～12,000 RB/s（容量約 67,800）；donor 約 40,000～48,000 RB/s（4 個 branch 加總的**推算值，未直接量測**）。
- **離線評估結果（2026-10-01，`/home/lindor/pf16_run_20260930/bh_model/d2.py`；不含 MT–DU 耦合；模型在 106 PRB 時算出 relay 使用率 17%，與實測 15～18% 一致）**：
  - **TM 場景**（96 個壅塞相位）：relay 106／51 PRB 時 PF 總量都是 78.2、全網上限 83.1，relay 使用率 17%／35%（78、38 PRB 的離線結果相同，但 38 不是合法頻寬），relay 不是瓶頸。
    relay 24 PRB 時 PF 與上限都降到 74.4：卡在每個 MT 每秒約 430 次的排程上限（24×430 RB/s ≈ 17.5 模擬 Mbps／access 節點），
    **access 層的空間被吃掉、relay 層也沒有新增空間** → TM 這種「瓶頸在 access 層」的流量，縮 relay 頻寬沒有用。
  - **壓力 branch**（一個 access 節點好通道高需求、另一個壞通道且自己就卡住）：relay 層的空間**只在 UDP 出現**——
    **51 PRB（20 MHz）**：branch 需求 ≥70 時 UDP +19～25%（需求 50 時 0%）；24 PRB（10 MHz）：UDP +8%；**TCP 一律 0%**
    （TCP 依端到端瓶頸自己降速，relay 的 PF 已是最佳）。38 PRB 經使用者確認不是合法頻寬，不列入。
    51 PRB 時 TM 不受影響（PF 78.2、上限 83.1，與 106 PRB 相同）；24 PRB 會吃掉 access 層空間。
  - **建議 D2：donor 106 PRB、relay 51 PRB**（待驗證）。壓力 branch（需求約 70）不能與 TM 的 3 個 M 類 branch 同時出現（會超過平台上限約 100），
    場景要分成「access 壅塞相位」與「backhaul 壅塞相位」兩種（見 8.1、D6）。
  - 結論：多跳的 relay 決策只在「backhaul 吃緊＋非彈性流量（UDP）」時有價值；donor 維持 106 PRB（第一跳約 64%，縮窄只會壓低全網、donor 沒有 agent）。
  - 〔未驗證〕MT 的每秒 430 次上限沿用 UE 的實測值（MT 也是 nrUE，推論相同）；donor→relay 效率假設同 relay→MT；
    relay DU 改為 51 PRB 後 MT 的接續與穩定性尚未驗證；relay 層用時域遮罩是否有效未經平台驗證。
- 〔未驗證〕文獻中典型的 backhaul 使用率或頻寬比例：尚未找到可引用的數字。

### 3.3 MT–DU 耦合（backhaul-aware PRB 預算）✅ 停用（決策 D3，2026-10-01 定案）

- 〔程式〕`gNB_scheduler_dlsch.c`：`n_rb_sched = bw × backhaul_prb_ratio`；`ratio = 1 − EWMA(busy)`，
  `busy = ΔRB(DL+UL) / (2·106·2000·interval)`（`nr_mac_gNB_backhaul_poll.c`）。對 PF 與 DRL 一視同仁。
- 〔實測〕可用 PRB 比例在 T 與 TM 下都是 0.94～1.00（中位數 0.97～0.99），實際只壓掉 2～6%。
- 這個機制是在模擬 in-band 半雙工（MT 忙則 DU 少用）。**若 D1＝A（out-of-band），它與設定矛盾。**

| 選項 | 影響 |
|---|---|
| 拿掉（out-of-band 下最一致） | 平台行為改變（影響很小，2～6%），PF 基準要重量 |
| 保留，改稱「共用硬體／基頻處理的殘餘耦合」 | 不改平台，但論述較弱 |
| 若 D1＝B | 由真正的時段劃分取代 |

**定案：拿掉**（out-of-band 下 MT 與 DU 互不排擠）。做法：三份 compose 的 relay／access DU 都改成 `--backhaul-mt-telnet-port 0`
（`start_backhaul_poll_thread()` 在 port≤0 時不啟動輪詢，`backhaul_prb_ratio` 維持初始值 1.0），不需重編；C 程式碼保留。
要恢復時把 port 改回 9188＋節點編號（Node1＝9189 … Node12＝9200）。E2 回報的 `bh_ratio` 恆為 1.0，state 維度不變（Local DRL 不改）。
影響：PF 基準要重量；Critic 預訓練資料的 `bh_ratio` 是 0.94～1.0，與現在的恆 1.0 有小幅分布差異，重收 PF-shadow 資料時一併處理。

### 3.4 每個 UE 的排程次數上限 ✅（平台特性，影響控制設計）

- 〔實測〕每個 UE 每秒最多約 430 次 DL 傳輸（約 2/3 的下行 slot）；單獨佔用 DU 時最多約 45,700 RB/s。
  DU MAC 統計與 PF-shadow 資料（HISTORY.md 續四十六）。
- 〔程式〕短 PUCCH 每個上行 occasion 最多 2 個 ACK 位元（`gNB_scheduler_uci.c::nr_acknack_scheduling`，`dai_c==2` 即滿）。
  〔未驗證〕這是次數上限的成因（推論，未逐 slot 追蹤）。
- **影響**：頻域 PRB 上限無法重新分配資源（一個 UE 少用的 RB 別人用不到），見 6.3。

### 3.5 平台速度與容量 ✅

- 〔程式〕速度調節 S=0.3（2026-10-01 由 0.4 改，`cmake_targets/ran_build/build/rfsim_speed.txt`，三台一致）；模擬時間＝牆鐘×S。所有 softmodem 帶 `-E`（3/4 取樣率）。
- 〔實測〕全系統容量約 100 模擬 Mbps，是 PC2／PC3 的 **CPU 限制**，與 UE 數、協定無關（CLAUDE.md §8）。
  這是「匯聚流量無法提高」的根本原因。

---

## 4. 通道 ✅（D4 待確認）

- 〔程式〕rfsim 通道模型只作用於接收端；下行惡化改 UE 端 `rfsimu_channel_enB0`；ploss 正值是增益、負值才是衰減。
- 場景損耗指標 L（0～25）→ 沿 `DEGRADE_PATH` 內插 UE 端 (ploss, noise)。
- 〔實測〕L 與每 RB 效率（PF-shadow，HISTORY.md 續四十三）：L=3/10 → 91 B/RB（MCS 28）；18 → 32（MCS 13）；22 → 19（MCS 8）；
  23 → 12.5（MCS 5）；24 → 8.8（MCS 3）。L=25 接近斷線，不用。
- **只有 access 鏈路（UE 下行）會惡化；backhaul 鏈路（MT）一律 MCS 28。**
- **D4（backhaul 通道要不要惡化）**：建議**不惡化**。〔文獻〕backhaul 通常假設為視距、架高（Ericsson：理想位置是與 macro 站視距的電線桿；
  Read/06 假設 backhaul 為 LoS）。backhaul 受限改由頻寬決定（3.2）。

---

## 5. 流量模型 ✅

- 需求受限（demand-limited）：每個 UE 一條 iperf3 下行流，目標速率由場景給定（模擬時間 Mbps，啟動時乘 S 換成牆鐘）。
- TCP 與 UDP 都量：TCP 由端到端壅塞控制降速（超過容量的需求不進網路）；UDP 依目標速率送，超量在有限 RLC buffer 被丟。
- 不用 full-buffer：會讓最佳策略退化成 max-C/I、無法區分正常／壅塞狀態、RTT 無意義（HISTORY.md 09-30 討論）。
- 〔文獻〕Polese et al. 2018 以固定速率 UDP（28／224 Mbps）製造不同壅塞程度，與本平台「以需求量控制壅塞」的做法一致。

---

## 6. O-RAN 控制

### 6.1 RIC 元件 ✅

| 元件 | 實作 | 對應 O-RAN |
|---|---|---|
| Local xApp | 12 個 C 程式（`xapp_nodeN.c`），E2SM-MAC | Near-RT RIC xApp |
| Local rApp | Python 推論＋背景訓練（`inference_server.py`） | **自訂服務**，與 Near-RT RIC 同機（不是 Non-RT RIC 的 rApp） |
| Global xApp | 全域公平性偏差廣播（`global_xapp.py`） | Near-RT RIC xApp |
| Global rApp | Flower FL 伺服器 | **自訂服務** |

平台只有 Near-RT RIC（FlexRIC＋E2），沒有 A1、Non-RT RIC、SMO（論文第 3 章 Mapping 一節已揭露）。

### 6.2 控制週期 ✅

E2SM-MAC 回報 100 ms；xApp 每 10 次回報觸發一次推論與控制 → **控制週期 1 秒**；推論逾時 5 ms 退回 PF（CLAUDE.md §2）。

### 6.3 控制動作：時域 slot 遮罩 ✅

- 每個子節點從 5 檔遮罩選一檔（16 位元，第 k 位元＝frame 內 slot%16==k 可排程），PRB 不設上限。
  〔程式〕`drl_agent.py::MASK_TIERS`、12 個 `xapp_nodeN.c` 讀 JSON 的 `slot_mask`、OAI MAC 2D 控制。
- 〔實測〕場景 P（混合通道節點，TCP，混合節點合計，HISTORY.md 續四十九）：

| 檔位 | 遮罩 | 可用 | 混合節點合計 | 被限制 UE 合計 |
|---|---|---|---|---|
| 0 | 0x0101 | 2/16 | +3.0% | 4.5 |
| 1 | 0x1111 | 4/16 | **+11.5%**（UDP +13.3%） | 12.0 |
| 2 | 0x9292 | 6/16 | +5.1%（UDP +9.0%） | 14.6 |
| 3 | 0xab98 | 8/16 | +6.2% | 18.2 |
| 4 | 0xFFFF | 全開＝PF | 基準 | 23.4 |

- 〔實測〕頻域 PRB 上限（舊動作）：TCP −10.6%、UDP −11.8%（原因見 3.4）。
- 遮罩樣式與 TDD 週期交互作用，同比例不同樣式效果差很多（均勻 0x5555 無效），樣式只能實測決定。
- 〔未驗證〕DRL 推論路徑下遮罩到達 MAC：只做間接驗證（JSON 格式同規則模式，規則模式已用 DU MAC 統計驗證）。

### 6.4 agent 範圍 ❓（決策 D5，依 D1、D2）

- 〔實測〕relay 在現行平台沒有資源競爭：壅塞樣本比例 PF 下 0～0.2%、訓練試跑時 2～4%（多為探索造成），Actor 幾乎每輪跳過。
- 選項：(a) 只有 access 是 agent，relay／donor 固定 PF；(b) access＋relay；(c) access＋relay＋donor。
- 這取決於 D1／D2 後瓶頸落在哪一跳：若 backhaul 受限落在 relay，選 (b)；落在 donor 需要 (c)。
- **2026-10-01 使用者決定：平台拓樸不變（relay 不帶 UE）**；論文第 3 章的模型本來就是一般樹（任意深度、relay 可帶 UE、
  分支深度可不同，見 `chapter3-system-model.tex` Example「Testbed topology」），實驗是其中一個實例。
  論文須寫明：此實例中 relay 只有 IAB 子節點、backhaul 為高效率鏈路，relay 的 agent 沒有競爭（壅塞樣本≈0、FL 權重≈0），
  實驗結果不驗證 relay 的決策；下列離線評估可放在討論／未來工作。
- （未採用，留作依據）每個 relay 也直接服務 2 個 UE（2026-10-01 離線評估，〔模型〕`bh_model/relay_ue.py`、`relay_ue2.py`，out-of-band、各 DU 106 PRB、TM 壅塞相位）**：
  relay DU 要在「子節點 MT（91.3 B/RB）」與「自己的 UE（9～91 B/RB）」之間分 RB，是典型 IAB 的 backhaul／access 取捨。
  - 4 個 relay 都帶重負載 UE 時，donor（單一 106 PRB DU，上限約 115 sim Mbps）先飽和，總量固定、relay 層空間 ≈0%。
  - 只有 1～2 個 relay 帶重負載（混合通道）時：**relay 層 TCP +1.6～3.4%、UDP +2.7～9.9%**，全網上限 TCP +3～8%、UDP +6～10%；
    PF 總量 99～112 sim Mbps，已在平台 CPU 上限（約 100）附近。relay UE 只帶單一壞 UE 時 relay 層 ≈0%。
  - 〔未驗證〕平台承載：PC1 即時 RF 行程 10→18（PC1 有 4 核給 inference，其餘 12 核；訓練期間 PC1 閒置約 80%）；
    CPU 上限 100 是 PC2/PC3 量到的，relay UE 在 PC1 時總上限會怎麼變未知；IMSI 217～223 需補進 DB。需先做平台試驗。

### 6.5 狀態 ✅

53 維：每個子節點 3 維（上一秒送出 bytes、MCS、RLC 佇列，log 正規化）×16＋節點 5 維（活躍比例、公平性偏差、可用 PRB 比例、
parent 可用 PRB 趨勢 p̂、子節點總需求 ĉ）。〔程式〕`drl_agent.py::encode_state`、`inference_server.py` relational 執行緒。

---

## 7. 學習

### 7.1 Local DRL v2 ✅

PF 初始化（PF 檔機率 0.8）、全部子節點共用的 per-child Actor、DeepSets Critic、PPO-clip（逐子節點比例，γ=0 近視近似）、
只用壅塞樣本更新 Actor、reward＝正規化吞吐量。〔程式〕`drl_agent.py`；設計見 `inference/LOCAL_DRL_V2_DESIGN.md`。
- Critic 已用 PF-shadow 資料（tm,t,th）預訓練，時間外推解釋比例 84～99%（HISTORY.md 續四十九）。**若平台設定改變（D1～D3），要評估是否重新收資料、重新預訓練。**
- 〔實測〕冒煙測試與 25 分鐘端到端訓練試跑通過（HISTORY.md 續五十）。

### 7.2 Global FL 各階段

| Stage | 內容 | 狀態 |
|---|---|---|
| 2 | FedAvg | ✅ 已實作 |
| 3 | 依 branch 的階層式 FedAvg＋Hedge 跨 branch 加權＋分歧節點保留自身模型 | ✅ 已定義（論文第 3 章），未實作 |
| 4 | 待設計（候選：Stage 3＋伺服器端動量＋個人頭；或利用 PF 錨定 critic 的 IAB 特有機制） | ❓ |
| 5 | Stage 4＋Lagrangian 公平性限制 | ✅ 已定義，未實作 |

Local 層在 Stage 2～4 逐行相同（硬性規則；Stage 5 是另一版 DRL）；Global 層不可離開 FL 範式（硬性規則）。

---

## 8. 評估

### 8.1 場景 ✅（決策 D6，2026-10-01 定案）

兩層設計：**主場景 HS／HSH**（結構化隨機，relay 層與 access 層同時壅塞）＋**泛化場景 G**（一般隨機）＋固定機制場景（只用來解釋增益來源）。
實作見 `scenarios/traffic_scenario.py::scenario_hs()`，相位 110 s、11 個相位一週期、壅塞相位與 T 相同（第 2/4/5/7/9 個）。

| 場景 | 特性 | 用途 | 依據 |
|---|---|---|---|
| **HS**（對稱） | 每個壅塞相位一個 PB 結構熱點 branch（依相位輪替）：relay 直連 UE 80% 邊緣 L24（該介入）、20% 好通道 L10（不該遮），下游極重＋中等 access 節點（全 L10），其他 branch 輕負載；壅塞總需求約 94（2026-10-02 改版） | T 軌：訓練（隨機 seed）＋量測（seed 20260930） | 〔實測〕平台驗證 PF 70／69 vs 動態規則 79／78（+12.9%，seed 1001／1002）；PB 固定配置 PF 74 vs 規則 83～84 |
| **HSH**（持久異質） | 同 HS，熱點 branch 依固定權重抽（實際約 [26,54,9,12]%），各 branch 的熱點頻率持久不同 | TH 軌：訓練＋量測（seed 20260930） | 同 HS 結構（未單獨驗證增益） |
| **G**（一般隨機） | 壅塞相位總需求 115、無結構 | 泛化／不傷害檢查：量測用，同時混入兩軌訓練（教 agent 何時不介入） | 〔模型〕上限約 +1% |
| PB／PU、TM、P | 固定配置 | 機制說明（PB：遮 relay 壞 UE，TCP +14.5%，約模型上限 86%） | 〔實測〕續五十四～五十五 |
| T／TH | 舊基準 | 不再作為主場景（壅塞相位同節點 UE 通道相近，上限只比 PF 高 1～3%） | 續四十三 |

- **T/TH 雙軌（沿用 2026-09-29 框架）**：每個 Stage 各訓練兩次——HS 軌（訓練家族 `hs`＝HS,HS,G,HS,HS,G）與 HSH 軌（`hsh`＝HSH,HSH,G,HSH,HSH,G），
  各自在同名場景量測；G 兩軌都量。PF 不需訓練，HS／HSH／G 各量一次（TCP＋UDP）。交叉量測（HS 訓練→HSH 量測）選做。
- **各 Stage 的預期角色**：Stage 3（Hierarchical FedAvg＋Hedge＋分歧節點保留）針對 branch 間的持久負載異質，預期在 HSH 勝過 Stage 2、
  HS 下與 Stage 2 持平（對稱時沒有分歧可利用）；Stage 4 針對兩軌都存在的結構異質（relay 節點有 MT＋UE 子節點、access 節點只有 UE），
  預期兩軌都勝過前一階。驗收規則見 8.2（D7）。
- **訓練／測試分離**：訓練 seed 為 HS 165000＋k、HSH 170000＋k、G 175000＋k，與量測 seed 20260930 不重疊。開發階段每軌量 1 個 seed；
  定案時量 3 個 seed 報平均與信賴區間。
- **改版紀錄**：2026-10-01 初版（熱點＋混合 access branch，離線 LP 上限 +10.5%）平台實測無增益（規則 +1%），10-02 第二版（兩個混合 branch、好 UE L3/L10）也無增益（好 UE 在 PF 下已被滿足、relay MT 不積壓），第三版改為 PB 結構並通過平台驗證。access 層在壅塞相位已無「該介入」的結構，學得到的決策集中在 relay 節點。
- **論文揭露**：HS 是依「改進空間所在」設計的結構化場景，須同時報告 G 結果與「不該介入」相位比例，證明 agent 學會不介入。

### 8.2 指標與判讀 ❓（決策 D7）

- 兩軸（提案）：**A 最終表現**（凍結模型量測：壅塞相位吞吐量、滿足率、滿足率 JFI、RTT，TCP＋UDP）；
  **B 學習效率**（以凍結的 PF 預訓練 critic 估計每小時相對 PF 的改善，算收斂時間與訓練期累積損失）；TMH 另看最差節點滿足率。
- 待決定：是否以此取代 CLAUDE.md 現行「每一階最終吞吐量都要贏前一階」的規則。

### 8.3 量測流程與雜訊 ✅

- 流程：`clean_env.sh` → 依序重啟 → `precheck_measure.sh` → `run_stage_measure.sh` → `analyze_stage.py`（CLAUDE.md §3）。
- 〔實測〕PF 同條件 3 次（T，TCP）：平均吞吐量 CV 0.4%、壅塞滿足率標準差 0.0006、滿足率 JFI 標準差 0.009、壅塞 RTT CV 5%
  （HISTORY.md 續四十五）。差距 >1～2%（吞吐量、滿足率）才視為真差異。UDP、TH 的雜訊未量。

---

## 9. 已知限制（論文須揭露）

1. 平台是 rfsimulator 軟體模擬，總容量受 CPU 限制（約 100 模擬 Mbps），以時間膨脹 S=0.3 換取穩定（PC1 網卡是跨主機 IQ 的瓶頸）。
2. 沒有 BAP、沒有同頻干擾；頻段模式依 D1 定義。
3. RIC 只有 Near-RT 層；Local／Global rApp 是自訂服務。
4. 每個 UE 的排程次數上限（3.4）使頻域控制無效，控制改為時域。
5. γ=0 近視近似、local reward 是全域目標的代理（論文第 3 章已寫）。

---

- 〔實測〕**F1 路徑（2026-10-01 修正）**：access DU 的 F1 原本全部（F1-U 上行與 F1-C）經主機乙太網路直送 PC1，上行完全繞過
  access MT 與 relay 的無線鏈路（下行有走）；access UE 的 RTT 因此只有 82 ms（應為 3 段上行）。已改為 F1-U 上行經同節點 MT 隧道
  （`start_iab_pc{2,3}.sh` 的 `assert_f1u_uplink_via_mt`），修正後 access UE RTT 217～235 ms（牆鐘）、MT 上行計數隨 UE 上行增加。
  **F1-C（SCTP）仍走乙太網路**（僅控制訊令，使用者決定論文不另提）。relay DU 與 MT 共用 netns，F1 原本就經 relay MT。
- 〔實測〕**E2 也不經 backhaul**：12 個 IAB DU 到 RIC（`192.168.88.141`）的 E2 由 macvlan 網卡直連 PC1（控制面、流量小）。
  2026-10-01 使用者決定不修。
- 〔程式〕其他與標準的差異（論文差異表應列）：無 BAP，backhaul 以 MT 的 PDU session 承載、F1 封包經 UPF（標準為 BAP over BH RLC
  channel，donor DU→donor CU 不經 UPF）；MT 為一般 UE（`nr-uesoftmodem`），無 IAB 節點整合程序與多 parent；E2SM-MAC 與時域遮罩
  控制為 FlexRIC 自訂，非 O-RAN 標準 E2SM（KPM／RC）；只有 Near-RT RIC，無 Non-RT RIC／A1／O1／SMO；backhaul 通道固定理想（MCS 28）。
- 〔實測＋程式〕**每段上行無線延遲偏高**：約 28 ms 模擬時間（下行每段僅數 ms）。**SR 週期不是主因**：`nr_radio_config.c`
  `set_SR_periodandoffset()` 在本 TDD（10 slot 週期、第一個 UL slot＝8）選 `sl10`＝5 ms，已是此 TDD 格式的最短值；
  依 `min_rxtxtime=6` 推算每段上行應約 7.5～12.5 ms，尚有約 15 ms 未解釋（候選：多輪 BSR／grant、跨隧道轉送處理）。
  待做：系統閒置時在 PC1 同時抓 relay MT1 隧道與 CU 端封包時間戳，量單段上行單向延遲。可調方向：DU 主動定期給 UL grant
  （`ulsch_max_frame_inactivity`）、改 TDD 格式（不建議）、調低 `min_rxtxtime`。皆屬系統設定決定，改了須重量。
  **2026-10-01 使用者決定：維持現狀，不再拆解或調整。**本文件與論文的 RTT、吞吐量一律以模擬時間表示（牆鐘 ×S、÷S）。

## 10. 驗證清單（各項決定後執行，有數據才算成立）

| # | 驗證項目 | 方法 | 狀態 |
|---|---|---|---|
| V1 | 頻寬設定後 MT 能穩定接上、全系統 13/13 E2、16 UE 連通 | 乾淨重啟＋precheck，連跑 1 小時無崩潰 | 待 D2 |
| V2 | 壅塞時瓶頸落在預期的那一跳 | PF 下各 DU 的 RB 使用率、佇列（DU MAC 統計＋shadow 資料） | 待 D2 |
| V3 | 控制動作在 DRL 路徑真的到達 MAC | DRL 模式下讀 DU MAC 每 UE 傳輸次數，與選擇的檔位對照 | 待做 |
| V4 | 場景長期統計符合定義（對稱／持久異質） | 離線模擬 | TM／TMH ✅；HS／HSH 跨主機一致性 ✅ |
| V5 | 場景有改進空間且大於雜訊 | 模型＋平台規則試驗 | access 層 ✅；backhaul 層待 D2 |
| V6 | 新設定下的雜訊大小 | PF 同條件重複 3 次 | 待做 |

---

## 11. 決策清單（依相依順序）

| # | 決策 | 選項 | 建議 | 依賴 |
|---|---|---|---|---|
| **D1** | 頻段／雙工模式 | A out-of-band／B 模擬 in-band 半雙工 | ✅ **A（2026-10-01 定案）** | — |
| **D2** | backhaul 頻寬（donor、relay 各自） | relay 106／51／24 PRB | ✅ **全部 106 PRB（2026-10-01 定案）** | D1 |
| **D3** | MT–DU 耦合代理 | 拿掉／保留並改稱 | ✅ **拿掉**（2026-10-01，compose 設 port 0） | D1 |
| D4 | backhaul 通道是否惡化 | 不惡化／惡化 | **不惡化** | — |
| **D5** | agent 範圍 | access／＋relay／＋donor | 依瓶頸位置 | D1、D2 |
| D6 | 基準場景 | HS／HSH 雙軌＋G 泛化＋固定機制場景 | ✅ **2026-10-01 定案**（8.1）；**10-03 第四版**：PB 熱點 branch＋一個混合 access 節點（M 類 60%／N 類 40%），relay 與 access 都有決策（CLAUDE.md §8、HISTORY 續五十九～六十） | D1～D5 |
| D7 | 評估規則 | 兩軸／原本的單調遞增 | ✅ **2026-10-01 定案、10-03 修訂**：分軌驗收（Stage 3 在 HSH 必須贏、HS 不輸；Stage 2、4 兩軌都必須贏；G 只要求不輸 PF）。10-03 加入 Stage 1.5（純 Local DRL，`FL_MODE=none`）：1.5 須贏 PF、Stage 2 改為須贏 1.5（FL 的貢獻）；動態規則只當專家參考線。全文見 CLAUDE.md §3 | — |
| D8 | Stage 4 設計 | 待設計 | 待 Stage 1.5 結果 | D5 |

> **⚠ 設計限制（2026-10-02，HS／HSH 改為 PB 結構後；Stage 2～4 FL 設計必須考慮）**
> 壅塞相位的改進空間全部集中在 4 個 relay 節點（relay 邊緣 UE 讓 slot 給 backhaul）；8 個 access 節點在壅塞時最好的動作永遠是「不介入」（舊版混合 access 結構平台實測無增益，已移除）。後果：
> 1. Local DRL 真正學到決策的只有 relay 節點，access 節點只學會「不要遮」。
> 2. Stage 2 FedAvg 以「決策點樣本數」（`count_contended()`＝starved 且有候選）加權，access 節點權重接近 0，聚合實際上幾乎是 4 個 relay 互相平均；access 節點只被動接收全域模型。
> 3. Stage 3（分 branch 聚合＋Hedge）與 Stage 4（角色個人化）的設計前提要重新檢查：branch 內平均時 access 節點幾乎沒有樣本、relay 主導；「relay／access 角色分軌」在 access 側沒有可學內容，個人化的收益只會出現在 relay 側。
> 4. 使用者 2026-10-02 決定先照此場景進行（唯一平台實測贏 PF 的結構）；若之後要讓 access 層也有可學決策，需先在平台上找到 access 層實測有增益的結構（P 原配置 4 節點同時壅塞僅 +4%）。

