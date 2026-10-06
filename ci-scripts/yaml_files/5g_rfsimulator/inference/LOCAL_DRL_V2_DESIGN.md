# Local DRL 設計文件（現行：v3.5，2026-10-04）

> **文件定位**：Local 層（Local xApp＋Local rApp）的**現行設計**。Stage 1.5（純 Local DRL，`FL_MODE=none`）到 Stage 4
> 共用、逐行相同（硬性規則）；Stage 5 是另一版 DRL（Lagrangian，見 §9）。Global 層見 `STAGE4_CUSTOM_FL_DESIGN.md`。
> v2（2026-09-29～10-02）與 v2.1（10-02～10-03）的設計、實驗與失敗診斷保留在**附錄 A**，作論文方法論沿革；
> 檔名沿用 `LOCAL_DRL_V2_DESIGN.md`（其他文件引用此檔名）。逐日過程見 `HISTORY.md` 續五十六～六十四。
>
> **論文定位（2026-10-03 使用者決定）**：Local DRL 採用 IAB DRL 文獻的既有做法（每節點一個 agent、reward 把功勞算給
> 子樹的終端 UE），貢獻在 Global rApp 的 FL；動態規則只當「需要專家依平台實測重調」的參考線，不在驗收鏈內。
>
> **使用者的三個驗收條件**：①relay 與 access 都要學到策略；②吞吐量明顯贏 PF；③不能訓練完還跟規則一樣（v2.1 的問題）。

---

## 0. v3 設計摘要

| 項目 | 設計 | 程式／參數 |
|---|---|---|
| Agent | 每個 IAB 節點（Node1～12）一個；Donor 維持 PF | `inference_server.py`（12 個容器） |
| 控制對象 | DU 上每個子節點（relay：2 個子節點 MT＋2 個直連 UE；access：2 個 UE）的**時域份額** | xApp → E2 → `xapp_2d_ctrl.slot_mask` |
| 動作 | **兩段式**（v3.1）：節點先從 **6 檔**時域遮罩（v3.2：0x0101、0x1111、0x9044、0x9292、0xab98、全開）選一個強度，再對每個子節點決定是否套用（§2、§5） | `DRL_ACTION_SPACE=factored`、`DRL_MASK_TIERS=0x0101,...,0xffff` |
| State | 每子節點 5 維×16＋節點 5 維＝85 維（§3） | `drl_agent.encode_state` |
| Reward | **子樹 α-fair 效用** Σ U_α(x_u)，**α=0.2**（v3.2；v3／v3.1 用 0.4，訊號太小）（§4） | `REWARD_MODE=alpha_fair`、`DRL_ALPHA` |
| 演算法 | PPO actor-critic、γ=0.5（每個決策）、**3 步回報**（v3.4，§6.1）、從 PF 起步、**不從規則做 BC、沒有決策閘門** | `DRL_GAMMA_MLP=0.5`、`DRL_NSTEP=3` |
| 控制週期 | 1 秒（E2 每 100 ms 回報、每 10 次推論一次）；**決策週期 5 秒**（v3.3 動作持續：每 5 個控制週期才重選一次遮罩，§5.1） | xApp rate limiter、`DRL_ACTION_HOLD=5` |
| 訓練 | 每 60 秒讀最新 5000 筆逐秒經驗（併成約 1000 筆決策經驗）、15 次梯度更新；影子模型訓練；Critic 暖身 600 步 | `TRAIN_EPOCHS_PER_ROUND=15`、`DRL_ACTOR_WARMUP_STEPS` |
| 訓練場景 | `hsc`：HS 第四版＋G 交替、**只跑壅塞相位**（v3.3；正常相位最佳動作恆為不介入，不提供決策訊號） | `training_scenario_driver.sh --scenario-family hsc` |

---

## 1. 為什麼控制「時域遮罩」而不是 PRB 數量

### 1.1 頻域 PRB 上限在這個平台上無法重新分配資源
- **實測**（場景 P，2026-09-30，HISTORY 續四十六）：把壞通道 UE 的 PRB 上限壓到 30%，節點吞吐量 **−10%**（TCP／UDP 皆然）。
- **原因：HARQ-ACK 回報次數上限**。OAI 在本平台設定下，下行資料的 ACK 用短格式 PUCCH（format 0）回報，**每個 UE 每個上行時機最多 2 個 ACK 位元**
  （`gNB_scheduler_uci.c::nr_acknack_scheduling`，`dai_c == 2` 即視為已滿）。因此一個 UE 每個 frame 最多只能被排約 12 次
  （約 2/3 的下行 slot，實測每秒約 430 次〔牆鐘〕）。限制壞 UE 的 PRB 後，它仍每個 slot 都來搶排程；好 UE 在自己 ACK 用滿的 slot 不能傳，
  壞 UE 用剩的 PRB 就沒人用、只能閒置。
- 切片類 O-RAN 論文的頻域控制「有效」，是指上限被執行（隔離／保障），且每切片 UE 多、空出的 PRB 有人接手；本平台每節點只有 2～4 個子節點，
  又有 ACK 上限，頻域重新分配沒有接手的人。

### 1.2 時域遮罩有效
- 遮罩（16 位元，第 b 位元＝frame 內 slot%16==b 可排程）讓被限制的 UE 只能在部分 slot 被排程；**其他 UE 在被讓出的 slot 獨佔整個頻寬**，
  而這些 slot 落在它本來就能用（ACK 未滿）的位置。
- **實測**：場景 P 遮壞 UE（0x1111）混合節點 +11.5%；場景 PA（2026-10-03）M 類節點 +12～14%；PB（relay 熱點）+14.5%。

### 1.3 和 IAB 標準的關係
- 控制的對象——**parent DU 內 access UE 與子節點 MT（backhaul）之間的資源份額**——是標準的 IAB 排程問題（3GPP 未規定排程器，屬實作範圍）；
  PF 把背後有多個 UE 的 MT 當成 1 個使用者、分給 backhaul 太少，這是多跳 IAB＋PF 的通用現象，與 OAI 無關。
- 時域資源分配是 IAB 文獻的主流做法（in-band 的 TDM、3GPP Rel-16 的 H/S/NA；MARL 文獻如 arXiv 2205.06011 逐 slot 決定 access／backhaul）。
  差異：本平台定為 out-of-band，遮罩**不是**子節點 DU／MT 的半雙工切分，而是 parent DU 內的份額控制；且以每子節點、1 秒為單位（Near-RT RIC 時間尺度）。
- 選時域而非頻域，是 OAI ACK 上限造成的**平台限制**（論文須揭露）；方法本身（Local DRL 決定份額、FL 跨節點共享）不依賴控制介面。

---

## 2. 遮罩怎麼產生

### 2.1 強度：允許的下行 slot 數，不是位元數
TDD 為 5 ms 週期：slot 0～6 下行（DL）、slot 7 特殊（S：前 6 個符號下行、4 個保護、4 個上行）、slot 8～9 上行（UL）；一個 frame 20 個 slot
（DL＝0～6、10～16；S＝7、17；UL＝8、9、18、19）。遮罩以 slot%16 索引，所以**位元 0～3 各管兩個 slot**（0&16、1&17(S)、2&18(UL)、3&19(UL)），
**位元 8、9 只管 UL slot（遮了沒作用）**。遮罩的強度＝允許的 DL（＋S）slot 數。實測一致：0x1111（4 DL）+11.5%、0x9292（4 DL＋2 S）+5.1%、
0xab98（5 DL＋1 S）+6.2%、0x5555（8 DL）≈0。

### 2.2 位置也很重要：HARQ-ACK 群組
- **OAI 的 ACK 規則**：DL slot n 的 ACK 依序嘗試 k1＝6～13（`min_rxtxtime=6` 起連續 8 個，`nr_radio_config.c::set_dl_DataToUL_ACK`），
  放進**第一個還沒滿 2 位元**的上行時機；上行時機＝slot 7、8、9、17、18、19（S slot 有上行符號也算，`is_ul_slot`）。
- **推導出兩個 ACK 群組**，每群 8 個 DL／S slot 共用 3 個上行時機（每 UE 最多 6 個 ACK）：
  - 群組 A：DL slot 14、15、16、17(S)、0、1、2、3 → 上行時機 7、8、9
  - 群組 B：DL slot 4、5、6、7(S)、10、11、12、13 → 上行時機 17、18、19
  每個 UE 每群最多用 6 個 slot，**本來就有 2 個用不到**；被遮 UE 允許的 slot 若每群不超過 2 個，幾乎不會佔到其他 UE 能用的 slot。
- **位置敏感度分析**（場景 SW3，2026-10-03，HISTORY 續六十三）：同為 4 個 DL slot，

  | 遮罩 | 允許 DL slot | A／B 群 | relay 子樹 | access 子樹（相對 PF） |
  |---|---|---|---|---|
  | 全開（PF） | 全部 | — | 58.3 | 16.9 |
  | 0x1111 | 0、4、12、16 | 2／2 | 57.8 | 19.0（+12.8%） |
  | 0x9044 | 2、6、12、15 | 2／2 | 59.4 | 18.4（+8.9%） |
  | 0x0821 | 0、5、11、16 | 2／2 | 58.6 | 17.3（+2.5%） |
  | 0x3c00 | 10～13（集中） | 0／4 | 60.1 | 16.7（−1%） |

- **逐 slot 模擬**（`/home/lindor/mask_pos_20261003/sim/acksim.py`，精確 k1 規則＋PF＋遮罩）：重現「集中在同一群（0x3c00）無效」（模擬 −1.4%／實測 −1%），
  並重現 PF 下好／壞 UE 的比例；但預測三個 2／2 平衡樣式效果相同（+17%），**實測卻差到 2.5%～12.8%**——可能是單次量測（2 相位）雜訊，
  也可能是模擬未納入的因素（CSI 佔用上行時機、TCP 動態），未定論。

### 2.4 v3.2 選單（6 檔，2026-10-04 定案）
v3／v3.1 用下節的 21 檔，訓練 90 分鐘都停損：21 檔中真正有效的只有少數（0x1111 最好），隨機探索多數落在無效遮罩，關鍵樣本稀少。v3.2 依平台實測縮為 6 檔：
0x0101（2 DL）、0x1111（4 DL）、0x9044（4 DL，位置敏感度第二）、0x9292（4 DL＋2 S）、0xab98（5 DL＋1 S）、全開。強度涵蓋重遮到輕遮。
論文寫法：system model 只抽象定義有限遮罩集合 $\mathcal{M}$（含全開＝PF）；實驗設定列出這 6 檔，篩選依據（HARQ-ACK 時序排除集中樣式＋位置敏感度量測＋21 檔版本學不起來）放附錄；屬動作剪枝（action elimination，Zahavy 等 NeurIPS 2018）。

### 2.3 選單產生規則（`DRL_MASK_TIERS=acktier`，21 檔；v3／v3.1 使用，v3.2 起改 2.4）
1. 強度 k＝允許的 DL slot 數 2～13，各強度只考慮**不含 S slot、且 k 個 slot 平均分在 A／B 兩群**（差 ≤1）的樣式；
2. 同強度依「slot 之間的最小間距」由大到小排序（`/home/lindor/mask_pos_20261003/gen_menu.py`）；
3. k＝3、5、6 各取前 3 種；**k＝4 取敏感度分析實測最好的 0x1111、0x9044（同屬平衡樣式）＋規則第 1 種**；其餘 k 取 1 種；
4. 最後一檔全開（0xFFFF＝PF）。

| k（DL slot） | 遮罩 |
|---|---|
| 2 | 0x1004 |
| 3 | 0x8404、0x8408、0x8410 |
| 4 | **0x1111**、**0x9044**、0x0411 |
| 5 | 0x0449、0x0849、0x1049 |
| 6 | 0x2449、0x0455、0x0855 |
| 7～13 | 0x1455、0x5455、0x0c7d、0x4c7d、0x5c7d、0xdc7d、0xfc7d |
| 全開 | 0xffff |

**設計理由**：強度（份額）是主要變數，對應文獻常見的「資源比例」動作；位置以 HARQ-ACK 時序推導的平衡原則限制，
並在主要強度提供多種平衡位置讓 DRL 自己學（位置在平衡樣式之間的差異未定論）。未放 1 個 DL slot（0x0101 已遮太重，掃描中從未最佳）
與 0 個（整段斷流）。沿革：v2 的 5 檔（0x0101、0x1111、0x9292、0xab98、全開）→ dl14 17 檔（只看強度）→ 位置敏感度分析後改為 acktier。

---

## 3. State（每節點 85 維，`STATE_DIM`）
- **每個子節點 5 維**（最多 16 個位置，實際 relay 用 4 個、access 用 2 個，其餘補 0、以 mask 標記非活躍）：
  吞吐量（上一秒 Δ TBS，log 正規化）、MCS（/28）、RLC 佇列（log 正規化，需求代理）、是否為子節點 MT（依附著順序標記）、上一步是否被遮（`was_masked`）。
- **節點 5 維**：活躍子節點比例、`fairness_bias`（Global xApp，同角色比較）、`bh_ratio`（backhaul 預算停用後恆為 1）、
  $\hat p$（parent DU 的 RLC 佇列總量，上游壅塞）、$\hat c$（children 的佇列總量，下游需求）。
- 存進經驗的 state 與推論當下看到的 state 用同一份特徵快照編碼（PPO 比例才一致）。

## 4. Reward：子樹 α-fair 效用（`REWARD_MODE=alpha_fair`）
- $r_t = \sum_{u \in \mathcal{U}_n} U_\alpha(x_u)$，$U_\alpha(x)=x^{1-\alpha}/(1-\alpha)$（α=1 時 $\log(x+0.1)$）；$x_u$＝終端 UE u 在動作之後那一秒的下行吞吐量（模擬 Mbps）。
- $\mathcal{U}_n$：relay＝自己的 2 個直連 UE＋下游兩個 access 節點的 4 個 UE（不含 MT，MT 的流量就是下游 UE 的流量）；access＝自己的 2 個 UE。
  與 IAB DRL 文獻一致（UE log-sum rate：Lei 等 2020、arXiv 2309.00144；功勞算給下游：arXiv 2205.06011 的跳數加權）。
- 下游 UE 吞吐量在訓練讀取時從子節點經驗依時間戳對齊（±1.5 s，`training_pipeline.apply_alpha_fair_reward`）；對不到的經驗丟棄；
  本地訓練與 FL 客戶端都經 `fetch_experiences()`，定義一致。
- **為什麼用 α-fair 而不是純吞吐量或 Σlog**（HISTORY 續六十～六十一）：
  - α=0（純吞吐量，v2.1）：最佳動作恆為「全力遮」→ 學成常數、≈規則，滿足率輸 PF；
  - α=1（Σlog，PF 本身近似最大化的目標）：用 PB／P／PA 實測算，任何遮罩都比不遮差 → 學成 PF；
  - **α 掃描**（場景 SW1／SW2，5 種遮罩）：α=0.3～0.4 時 relay 與 access 的最佳強度都隨需求改變（例：access 好 UE 需求 22 時重遮、14 時不遮），
    且依狀態選擇的吞吐量與滿足率都勝過固定 0x1111；α=0.5 增益只剩 0～2%，接近雜訊。建議 α=0.4。

## 5. Action 與 Actor（v3.1 兩段式，2026-10-03）
- **為什麼改**：v3 第一版（`per_ue`）每個子節點各自從 21 檔獨立抽遮罩，第一次訓練 90 分鐘停損（壅塞送達 81，PF 82，四類都只學到「少遮」）。
  訓練資料顯示 relay 熱點的有效動作是**組合式**的：只遮 1 個邊緣 UE 沒有效果（另一個邊緣 UE 吃掉讓出的 slot），兩個都重遮才 +3.3%（規則驗證兩個同時遮 0x1111 為 +5.3%）；
  獨立抽樣下兩個同時重遮的機率約 1%，Actor 幾乎收不到正訊號（HISTORY 續六十五～六十六）。
- **動作**：$a_n=(\tau, \{g_v\}_{v})$：節點層級的遮罩強度 $\tau \in$ 21 檔（全部子節點共用）＋每個活躍子節點的「是否套用」$g_v \in \{0,1\}$；子節點實際遮罩＝$g_v$ ? $\tau$ : 全開。
  $\tau$＝全開時 $g_v$ 無意義、不計入 logp。log π(a|s)＝log π(τ)＋[τ≠全開]·Σ_v log π(g_v)（推論端存 `behavior_logp`，訓練端 `factored_logp_entropy` 同定義）。
- **Actor**（`ActorNetworkFactored`）：共用 per-子節點編碼器（輸入＝[自己 5 維｜節點 5 維｜同節點其他子節點的 mean 與 max]）→ 每子節點的套用 logit；
  活躍子節點編碼的 mean／max 池化＋節點特徵 → 強度 logits。
- **從 PF 起步**：強度選全開的機率 0.6（`DRL_NODE_PF_INIT_PROB`），非 MT 子節點套用機率 0.5（`DRL_APPLY_INIT_PROB`），MT 的套用 logit 另加可學偏置 −3.5（初始約 0.03，`DRL_MT_APPLY_BIAS`）。
  初始約 70% 的秒數完全不遮（等於 PF）；「兩個邊緣 UE 同時重遮（≤6 slot）」的探索機率由約 1.5% 提高到 6.4%。
- **離線驗證**：合成 reward（只有兩個邊緣 UE 同時重遮才給分）下訓練 300 步，兩段式從 6.4% 學到 17.1%；`per_ue` 只從 1.5% 到 2.2%。推論端與訓練端 logp 一致（差 0）。

### 5.0 apply_first（v3.5，2026-10-04，`DRL_FACTORED_MODE=apply_first`）
- **為什麼改**：node_on（v3.1～v3.4）的「開遮罩」是節點層級共用機率，開了之後每個子節點各自 50% 被套用。access 有一半機會遮到好 UE（實際 −22%），
  蓋過只遮壞 UE 的 +4～5%，Actor 學成整個不開；合併 12 節點資料（FL 的上限）也一樣（HISTORY 續七十一）。relay 兩個直連 UE 都是邊緣 UE，不受影響。
- **做法**：沒有節點層級的「全開」選項；每個活躍子節點 $v$ 自己 $g_v\sim\text{Bernoulli}(\sigma(\ell_v))$；$\sum_v g_v>0$ 時節點從 5 檔非全開強度選 $\tau$。
  $\log\pi(a|s)=\sum_v\log\pi(g_v)+\mathbb 1[\sum g>0]\log\pi(\tau|\neg\text{PF})$。遮好 UE 的懲罰只落在該子節點的 $g_v$ 上。
- 初始：非 MT 套用 0.3（`DRL_APPLY_FIRST_INIT_PROB`）、MT 偏置 −3.5；不遮任何子節點 49%、relay 兩個直連 UE 都遮約 9%、access 只遮壞 21%。

### 5.1 動作持續（v3.3，2026-10-04）
- **為什麼加**：v3.2（α=0.2、6 檔、每秒重選）訓練 70 分鐘的資料顯示，access-M「只遮壞 UE」的 Critic advantage 與「不遮」完全相同
  （+0.315 對 +0.314，n=114 對 802）——每秒重抽遮罩時，遮罩的好處在 1 秒的 reward 裡量不到；平台上同一動作維持 20 秒時 α=0.2 為 +8.2%
  （維持 5 秒的效果約為 20 秒的 3/4，HISTORY 續六十六、六十八）。relay「兩個邊緣 UE 都遮」方向正確但只有 +0.17σ。
  v3.2 的策略因此只學到「整體少遮」（四類一起由 10% 降到 6～7%，沒有分化；HISTORY 續六十九）。
- **做法**（action repeat／frame skip，DQN（Mnih 等 2015）以來 DRL 的標準做法）：每 $K$=5 個控制週期（5 秒牆鐘＝1.5 秒模擬時間）才由 Actor 重抽一次動作，
  中間沿用同一組遮罩；若子節點集合改變（附著／斷線）立即重抽。每秒仍各寫一筆經驗（`hold_id`、`hold_pos`），
  訓練讀取時 `training_pipeline.merge_action_holds()` 把同一決策的 $K$ 筆併成一筆：$s$＝決策當下、$a$＝該決策、
  $r$＝$K$ 秒子樹 α-fair 效用的平均、$s'$＝下一個決策當下；γ=0.5 改為「每個決策」的折扣。缺決策當下那一筆的群組整組捨棄。
- `DRL_ACTION_HOLD=1` 即退回每秒重選（v3.2 以前）；FL ClientApp 經同一個 `fetch_experiences()`，定義一致。
- 只有 1 個活躍子節點時直接全開、不記 logp。凍結評估用隨機採樣（`DRL_DETERMINISTIC=0`）。`per_ue`（v3 第一版）與 `macro`（v2.1）保留可選。

## 6. Critic 與訓練
- Critic：DeepSets（每子節點共用編碼器→mean／max pooling→節點特徵→V），對排列不變；**從零開始**（v3 不預訓練）。
- TD 目標 $r + \gamma V(s')$，γ=0.5（遮罩效果有 TCP 爬升與佇列消化的延遲）；advantage＝clip((target−V)/獎勵滾動標準差, ±3)。
- Actor：PPO 比例裁剪（ε=0.2）；**只用「有競爭」的樣本更新**（≥2 個活躍子節點且某子節點佇列 ≥100 KB，用決策前的 s 判定；只是挑訓練樣本，不限制推論）；
  批次內少於 8 筆就只更新 Critic。熵係數 0.01 起衰減，熵低於 0.1 時拉高到 0.05。
- **Critic 暖身**（`DRL_ACTOR_WARMUP_STEPS`）：前 N 步只更新 Critic（隨機初始化的 Critic 給出的 advantage 是雜訊），策略維持近 PF。建議 600 步（約 40 分鐘）。
- **每輪 15 次梯度更新**（`TRAIN_EPOCHS_PER_ROUND`）：每節點每分鐘只有約 60 筆新經驗，學習瓶頸在資料量；30 次時 PPO 裁剪比例到 53～67%。
- **影子模型訓練**（2026-10-03 修正）：舊版每次梯度更新都持有模型鎖，推論被擋住（離線重現：3 輪訓練期間推論只搶到 1 次、360 ms），造成 ZMQ 逾時、DRL 動作退回 PF。
  改為在複本上訓練、只在複製與換回權重時短暫持鎖；訓練期間若 FL 熱重載了磁碟權重，本輪結果作廢。訓練執行緒 nice 15。
  平台驗證：落在自己訓練期間的 >5 ms 延遲由 50% 降到 9%（與時間比例相當）；整體 ZMQ 逾時 175→147 次／15 分鐘（12 節點，約 1.4% 決策退回 PF；
  暫停訓練約 0.75%），剩餘差距來自訓練模式的 MongoDB 讀寫與 CPU 共用，列為平台限制。

### 6.1 n 步回報（v3.4，2026-10-04）
- **為什麼改**：v3.3 訓練 2 小時的資料（每節點約 1000 個決策）離線訓練，不論 12 節點合併或各節點自己，策略都往**錯的方向**分化
  （relay 該遮 7.9%／不該遮 8.7%）。拆解 TD 目標：relay 該遮狀態「兩個邊緣 UE 都遮」的立即 reward 48.2 對不遮 44.5（+8.3%），
  但 Critic 給遮罩後狀態的 V(s') 低 8.1，γV(s') 把好處整個蓋掉；實際資料中下一個決策的 reward 只低 1.3、再下一個持平，
  **三步實際總和 +1.9%（依強度：0x9044 +8.7%、0x9292 +6.2%、0xab98 +6.1%、0x0101 −6.9%）**。Critic 的偏差來自部分可觀測：
  它把「MT 佇列長」當成「這個 branch 需求高」的代理，遮罩把佇列消化後就誤判需求下降（HISTORY 續六十九）。
- **做法**：TD 目標改為 $G_k=\sum_{i=0}^{m-1}\gamma^i r_{k+i}+\gamma^m V(s_{k+m})$，$m\le n=3$（相鄰決策間隔 >9 秒或資料尾端就截斷），
  `training_pipeline.attach_nstep_returns()`；Critic 回歸 $G_k$、advantage＝$(G_k-V(s_k))/\sigma$。前 3 個決策（15 秒）用實際 reward，
  Critic 自舉只佔 $\gamma^3=1/8$ 的權重（A3C／PPO 的 n-step return，偏差–變異取捨的標準做法）。
- **離線驗證**（同一批 v3.3 資料、同樣 600＋1500 步）：一步 TD 合併 7.9%／8.7%（錯向）→ 3 步合併 10.1%／9.2%、各節點自己 10.7%／8.5%（正確方向）。
  access M「只遮壞 UE」在實際資料中也沒有好處（三步和 41.3 對 42.0；只有 0x1111 為 +3%，n=13），access 是否學得到取決於資料量。

## 7. 監控與驗收
- **策略診斷**（`iab/policy_diag.py`，每 20 分鐘）：依決策當下可觀測狀態，把「壞通道子節點」（MCS 比最好的低 ≥4、非 MT）分成
  relay-該遮（MT 積壓 ≥100 KB）／relay-不遮／access-M（最好通道的子節點積壓 ≥100 KB）／access-N，統計各類被遮的強度分布，以及 MT 被遮比例。分類只用於診斷、不參與訓練。
- **學習曲線**：每 90 分鐘暫停訓練（`/tmp/drl_pause`：不訓練、不寫經驗），HS TCP（seed 20260930、只跑 5 個壅塞相位）實測，對照同條件 PF 82、規則 86。
- **三個條件的判定**：①relay-該遮的介入率 > relay-不遮，且 access-M > access-N；②壅塞送達比 PF 高 5% 以上；③學習曲線從 PF 往上爬，且不同類別選的強度不同（不是單一常數）。
- **停損點（建議）**：90 分鐘實測低於 PF、或兩層都沒有分化 → 停下查原因；3 小時贏 PF 但未贏規則 → 延長到 6 小時；學成單一常數 → 停下討論。

## 8. 平台驗證紀錄（v3 定案依據，2026-10-03）
| 驗證 | 結果 | HISTORY |
|---|---|---|
| access 層決策（場景 PA） | M 類遮壞 UE +12～14%、N 類 −17～−25%；兩類只能靠「好 UE 佇列」分辨（MCS 相同） | 續五十九 |
| HS 第四版（PB 熱點＋混合 access 節點） | 動態規則對 PF +6.1%／+8.6%（seed 1001／1002）、滿足率也較高；access M 類相位 +36% | 續六十 |
| α 掃描（SW1／SW2） | α=0.3～0.4 兩層最佳強度隨狀態改變 | 續六十一 |
| 參照基準（seed 20260930） | PF 82、規則 86（+4.9%） | 續六十二 |
| 冒煙測試／延遲修正 | 設定生效、初始策略 85% 全開、MT 2.8%；影子模型訓練 | 續六十二～六十四 |
| 遮罩位置敏感度（SW3） | 同強度不同位置 access −1%～+12.8% | 續六十三 |

## 9. 已知限制與後續
- **平台**：時域控制源於 OAI ACK 上限（§1.1）；約 1% 決策因推論逾時退回 PF（§6）；子節點「是否為 MT」依附著順序標記（MT 未經整套重啟而重新附著時會錯，目前 MT 崩潰一律整套重啟）。
- **reward**：relay 的 reward 也受下游 access 自己的動作影響（共享 reward 的歸功雜訊，文獻常見）；剛啟動時約 12% relay 經驗對不到下游而丟棄。
- **滿足率**：α-fair 在「被遮 UE 需求低」時會讓滿足率略輸 PF（SW1）；Stage 1.5 驗收建議「吞吐量明顯贏、滿足率不明顯輸」，滿足率改善交給 Stage 5。
- **access 經驗稀少**：每個 access 節點約 3% 的時間是 M 類情況，純 Local DRL 可能學得慢——這正是 FL 跨節點共享經驗要解決的問題（Stage 2～4）。
  v3.3 以「只用壅塞相位訓練」把決策狀態的出現頻率提高約 2.2 倍，但每個決策 5 秒、樣本數變為 1/5；3 小時內每個 access 節點只會遇到數十次 M 類決策。
- **Stage 5（改良版 DRL）建議**：約束式 RL（Lagrangian，Tessler 等 ICLR 2019）：在「每 UE／整體滿足率不低於門檻」的約束下最大化 α-fair 效用，
  只改 reward，符合「一次只換一個變數」。備案：時間抽象（options）、記憶型網路（DRQN）、大型離散動作空間（Wolpertinger，可讓 DRL 直接選位置）。

## 10. 參數總表（v3）
| 參數 | 值 | 說明 |
|---|---|---|
| `REWARD_MODE` | `alpha_fair` | 子樹 α-fair |
| `DRL_ALPHA` | 0.2（v3.2 起） | α |
| `DRL_ACTION_SPACE` | `factored` | 兩段式（`per_ue`＝v3 第一版、`macro`＝v2.1） |
| `DRL_NODE_PF_INIT_PROB`／`DRL_APPLY_INIT_PROB`／`DRL_MT_APPLY_BIAS` | 0.6／0.5／−3.5 | 兩段式的初始化 |
| `DRL_MASK_TIERS` | `0x0101,0x1111,0x9044,0x9292,0xab98,0xffff`（v3.2 起） | 6 檔（`acktier`＝21 檔，v3／v3.1） |
| `DRL_ACTION_HOLD` | 5（v3.3） | 動作持續的控制週期數（1＝每秒重選） |
| `DRL_FACTORED_MODE` | `apply_first`（v3.5） | 兩段式動作的結構（`node_on`＝v3.1～v3.4）；`DRL_APPLY_FIRST_INIT_PROB`=0.3 |
| `DRL_NSTEP` | 3（v3.4） | n 步回報（1＝一步 TD）；`DRL_NSTEP_MAX_GAP_S`=9 |
| 訓練場景家族 | `hsc`（v3.3） | HS＋G、只跑壅塞相位（`hs`＝含正常相位，v3～v3.2） |
| `DRL_GAMMA_MLP` | 0.5 | 折扣因子 |
| `DRL_PF_INIT_PROB` | 0.8 | 初始全開機率 |
| `DRL_MT_PF_BIAS` | 2.0 | MT 全開 logit 偏置（可學） |
| `DRL_ACTOR_WARMUP_STEPS` | 600（建議） | Critic 暖身步數 |
| `TRAIN_EPOCHS_PER_ROUND` | 15 | 每 60 秒梯度更新次數 |
| `TRAIN_FETCH_LIMIT` | 5000 | 每輪讀最新經驗筆數 |
| `TRAIN_THREAD_NICE` | 15 | 訓練執行緒優先權 |
| `DRL_PPO_CLIP_EPS` | 0.2 | PPO 裁剪 |
| `CONTENDED_BUF_BYTES` | 100000 | Actor 樣本篩選門檻 |
| `RFSIM_SPEED` | 0.3 | reward 換算模擬時間 |

---

# 附錄 A：v2／v2.1 設計與實驗沿革（2026-09-29～10-03，已被 v3 取代，原文保留）

> v2：PF 初始化＋5 檔上限／遮罩＋共用 Actor 頭＋PF-shadow 預訓練 Critic＋純吞吐量 reward，凍結評估與 PF 持平。
> v2.1：從動態規則做 BC＋4 個宏動作＋決策閘門，HS 吞吐量 +15% 但 ≈ 規則、RL 未超越 BC、只有 relay 有決策。
> 以下各節為當時原文（標題降一級），數字與「現行」等用語皆指當時狀態。

### A.0. 為什麼要重新設計（診斷）

舊設計（GRU/MLP 雙分支、Dirichlet 連續動作空間、`throughput_only`/`lagrangian` 兩種 reward、
100% 線上從零訓練）從 Stage 2 到 Stage 3（CAPA-Fed，含 Dirichlet 退火時間常數修正重訓）全程
**從未真正贏過 PF baseline 吞吐量**，即使 Global 端換了三種聚合方式都一樣。2026-09-29 決定停止
在同一個未經文獻驗證的 Local DRL 基礎上持續更換 Global FL 聚合方式，改成先把 Local DRL 本身用
文獻立論紮實。診斷出的問題：

1. **100% 線上從零訓練，樣本嚴重不足**：每輪訓練只能負擔 ~600～2800 個環境步（多小時訓練預算＋
   每秒 1 筆觀測），遠少於典型 RL 論文。**在真實硬體上贏過 PF 的論文都不是從零開始學**（§1）。
2. **連續、高維度的動作空間**（16 維 Dirichlet 採樣）在這個樣本預算下太難學（§2）。
3. **State 沒有局部多跳關聯性**：只有全域角色平均的 `fairness_bias`，看不到自己 parent/children
   的即時狀態（§4）。

（原本第 4 點「裸吞吐量 reward 雜訊太大、要改反事實 reward」經 2026-09-30 審查確認是誤判，見 §3。）

---

### A.1. 從 PF 出發，而不是從零開始

#### 1.1 文獻依據

- **Residual policy learning**（Silver et al. 2018；Johannink et al. 2019）：RL 只學「在既有控制器
  之上的修正量」，修正量初始化為 0，初始行為精確等於既有控制器，RL 只在有好處的地方偏離。
- **Reinforcement Based User Scheduling for Cellular Communications**（Springer 2022，DOI
  10.1007/978-3-031-07689-3_15，已驗證）：在真實 4G eNodeB 上以 PF 為底、RL 只做前瞻修正，
  贏過原生 PF——「RL 當 PF 的修正層而非取代品」的直接實證。
- **Sever et al.**（`~/thesis_papers/Experimental/02+03`，arXiv:2501.05879）：**同一套 OAI 平台**
  的 DQN xApp，先用平台上的真實流量離線訓練，才部署進 near-RT 迴圈。**誠實揭露**：他們贏 PF
  是贏在 QoS 達成率，不是總吞吐量；引用的是「離線預訓練」這個機制，不是「贏 PF」的宣稱。
- **PandORA**（arXiv:2407.11747）、**ColO-RAN**（arXiv:2112.09559）：先離線/在數位分身大量訓練，
  再上線少量微調。

#### 1.2 為什麼用平台上的真實 PF 行為，而不自建簡化模擬器

使用者 2026-09-29 確認：**資料來源是平台上真的在跑的 PF，不另寫 Python 端 PF/MAC 分析模擬器**。
理由：①這個平台已被「紙上合理、實際不同」反覆咬過（`ploss` 符號、時間膨脹、USB2.0、cpuset、
Dirichlet 退火常數……），自建模擬器必然漏掉 TCP 與 RLC 佇列交互、`backhaul_prb_ratio` 回饋迴圈、
rfsim 雜訊等真實動態，又要跟 C 端 `pf_dl()` 保持同步；②Sever et al. 同平台先例也是用真實流量。

#### 1.3 PF-shadow 模式（資料收集基礎設施，已實作）

**問題**：PF baseline 量測時 12 個 xApp 都是關閉的，E2SM-MAC 的 state 從未被計算或寫入 MongoDB，
現有 PF log 只有吞吐量/RTT 時序，不能拿來訓練。

**設計**：`inference_server.py` 新增環境變數 `XAPP_MODE=shadow`（預設 `active`）。**C 端 xApp
不需要任何模式開關**——它照常訂閱 E2SM-MAC、組 state、送 ZMQ 請求、套用收到的控制；shadow 模式
全部在 Python 端：

- Local rApp 收到請求後一律回傳「全部 UE 上限 = 106（不設上限）」，MAC 排程器因此跑的就是純 PF
  ——**不是模擬 PF，是真的在跑 PF，只是同時記錄 state**。
- 每一步寫一筆到 `node{N}_pf_shadow`（不寫進 `node{N}_experiences`，避免跟 DRL on-policy 經驗
  混在一起），內容：跟線上完全相同的 `state_vec`（含 §4 的 relational 特徵）、原始 `ues`（含
  `bsr`，預訓練時用下一步的 `bsr` 算 reward）、`t_mono`（配對相鄰兩筆時判斷中斷）、
  `scenario_tag`（`PF_SHADOW_SCENARIO_TAG`，T/TH 分開）、`pf_actual_rbs`／`pf_rb_share`。
- shadow 模式略過 DRL 訓練、checkpoint 熱重載、Global xApp 公平性訂閱三個執行緒；`fairness_bias`
  在 shadow 資料裡恆為中性值 1.0（Global xApp 讀的是 `node{N}_experiences`，shadow 期間沒資料）。
- **PF 實際分配的 RB 數（`pf_actual_rbs`）不需要新增 E2SM-MAC 欄位**（2026-09-29 查證）：
  `mac_ue_stats_impl_t` 裡早就有 `uint32_t dl_aggr_prb`（`ran_func_mac.c:119`：
  `UE->mac_stats.dl.total_rbs`，累加計數器），而 `mac_enc_plain.c`／`mac_dec_plain.c` 對整個
  `ue_stats` 陣列是整包 `memcpy`，這個欄位本來就穿過 E2。只需在 12 個 `xapp_nodeN.c` 比照
  `compute_delta_tbs()` 新增 `compute_delta_prb()`，JSON 多帶 `pf_actual_rbs`
  （$\Delta RB_{i,t}=\text{dl\_aggr\_prb}_{i,t}-\text{dl\_aggr\_prb}_{i,t-1}$）。不需要改 E2 訊息格式、
  不需要三台主機重編 `libmac_sm.so`／`nr-softmodem`；xApp 只跑在 PC1，只需在 PC1 重編。
  2026-09-30 審查後這個欄位不再當 Actor 的 BC 標籤（§1.5），保留作為分析 PF 行為的資料。

#### 1.4 資料收集範圍（T/TH 雙軌分開）

比照 `CLAUDE.md` 第 3 節 T/TH 雙軌框架：`XAPP_MODE=shadow` 下分別跑 Scenario T、TH 各 2～3 小時
（涵蓋足夠多的壅塞/正常相位交替），用 `PF_SHADOW_SCENARIO_TAG=t`／`th` 標記，預訓練時依場景家族
各自產生一份初始 checkpoint。

#### 1.5 預訓練內容（2026-09-30 審查修正）

**Actor：residual 式 PF 初始化，不需要資料。** 在「每 UE PRB 上限」這個動作空間裡，PF 真正的
動作就是「全部 UE 不設上限」＝全部選最高檔 1.0。所以「對 PF 做 behavior cloning」跟「residual
零修正初始化」在這裡是同一件事，可以用輸出層閉式設定：最後一層權重縮到接近 0、bias 設成
$\log p_{PF}$（PF 檔）與 $\log\frac{1-p_{PF}}{4}$（其他 4 檔），$p_{PF}$=`DRL_PF_INIT_PROB`（預設
0.8）。初始策略以 80% 機率選 PF 檔、20% 均分到較低檔位當探索；`DRL_DETERMINISTIC=1` 評估時取
argmax＝PF 檔，**訓練前的評估結果就是 PF 本身**，是正確的起點。

> **為什麼不用原設計的「PF 實際 RB 份額」當 BC 標籤**：份額 $y_i=\Delta RB_i/\sum_j\Delta RB_j$
> 是分配結果，不是上限動作；把它換算成檔位（例如拿到 25% 的 UE 對應 0.3 檔 = 上限 32 RB），等於
> 把 UE 鎖在自己的**平均**份額，但 PF 在個別 slot 常給超過平均的量（份額是 1 秒內上千個 slot 的
> 加總），上限一鎖就把那些 slot 砍掉——BC 學出的初始策略會**比 PF 還差**，違反「從 PF 出發」的本意。

**Critic：用 PF-shadow 資料預訓練（`pretrain_critic.py`）。** 把相鄰兩筆 shadow 資料配對成
$(s_t, r_t)$，$r_t$ 用第 $t+1$ 筆的 UE 狀態、以線上相同的 `compute_reward_breakdown()` 算
（間隔 >5 秒或單調時鐘不連續就不配對，同線上 `STALE_PREV_UES_THRESHOLD_S`），做 MSE 迴歸
$V_\phi(s)\approx\mathbb{E}_{PF}[r\mid s]$（γ=0，同線上 contextual bandit 設定）。同時用資料的 reward
標準差初始化 advantage 的固定尺度 `_reward_std`。預訓練完存成該節點線上訓練前的初始 checkpoint
（Actor 為上述 PF 初始化、Critic 為預訓練結果）。腳本會印 holdout MSE 與「永遠猜平均」的對照 MSE，
前者沒有明顯低於後者代表沒學到 state 相關性，不應採用。

**效果**：線上訓練第一步起策略≈PF、$V(s)$≈PF 的期望表現，advantage $A=r-V(s)$ 天然代表「這次
比 PF 好多少」——這正是原設計想用反事實 reward 達到的效果（見 §3），但不需要額外的模型。

---

### A.2. 動作空間離散化

#### 2.1 文獻依據

- **Sever et al.**（同上）：離散化成 10 檔，200 episode 內收斂。
- **PandORA**（arXiv:2407.11747）：動作粒度要跟可負擔的訓練資料量匹配，資料少時粗粒度較好。

#### 2.2 設計：每 UE 獨立選一個上限檔位

每個活躍 UE 各自從 5 檔中選一檔（per-UE 獨立 Categorical，取代舊版單一 16 維 Dirichlet 聯合採樣）：

$$m \in \{0.3,\ 0.5,\ 0.7,\ 0.85,\ 1.0\},\qquad \text{cap}_i = \text{clip}(m_i \times 106,\ 5,\ 106)$$

- $m_i=1.0$ 精確等於不設上限，PF 是動作空間裡可**精確表達**的點（延續舊版 `relative` 模式已驗證
  的「PF 是恆等點」性質）。C 端換算不變（`clip(cap_i/106, 0, 1)`），不需要改 C 程式。
- 訓練：每 UE 獨立採樣，$\log\pi(a\mid s)=\sum_{i\in\text{active}}\log\pi_i(a_i\mid s)$（推論端存的
  `behavior_logp` 與訓練端 PPO 比例用同一個定義，已用單元測試驗證一致）。
- 評估（`DRL_DETERMINISTIC=1`）：每 UE 取 argmax，不採樣。
- **只有 1 個活躍 UE 時直接選 PF 檔**：單一 UE 設上限只會降低自己的吞吐量、不可能有好處，也沒有
  分配決策可學（訓練端的壅塞遮罩本來就要求 ≥2 個 UE 才更新 Actor）。
- 動作在 MongoDB 存成 `action_tiers`（每 UE 檔位 index，非活躍 -1）；舊版 `action_ratios` 只剩
  GRU 分支使用。
- 探索：entropy 正則化沿用舊版遞減係數；離散 entropy ≥0（上限 ln5≈1.61，初始約 0.78），低於
  `DRL_ENTROPY_FLOOR`（預設 0.1）時強制把係數拉到 0.05，避免策略過早塌成確定性。

#### 2.3 Actor 架構：全部 UE 共用一個小 MLP 頭（2026-09-30 審查修正）

原設計是「整個 state → `128→16×5=80`，每個 slot 各一組 5 檔 logits」。問題：slot 順序是 xApp 的
RNTI 表順序、本身沒有意義；現行拓樸每節點只有 1~3 個 UE，16 個 slot 大多是空的；每個 slot 要各自
學一套，在數千步的訓練預算下樣本效率很差。改成**每個 UE 都用同一個小 MLP 頭**：

$$\text{logits}_i = f_\theta\big([\,x_i,\ c,\ \text{mean}_{j\neq i}\,x_j,\ \max_{j\neq i}\,x_j\,]\big)\in\mathbb{R}^5$$

- $x_i$：UE $i$ 的 3 維特徵（norm_bsr, norm_mcs, norm_buf）；$c$：節點層級 5 維（active_ratio,
  fairness_bias, bh_ratio, $\hat p$, $\hat c$）；mean/max 是同節點**其他**活躍 UE 的特徵摘要，讓每個
  UE 的決策看得到競爭者。輸入共 14 維，$f_\theta$ 為 `14→64→64→5`（ReLU）。
- 每個活躍 UE 都是同一組權重的訓練樣本；對 UE 排列順序等變（單元測試驗證：交換 UE 順序，輸出
  跟著交換）。仍是 MLP——關聯資訊靠特徵串接而不是 GNN/attention，同 Yamin & Permuter（2023）
  Relational A2C 的做法（其「relational」是把鄰居特徵串進輸入，網路本身是一般前饋網路，已查證）。
- 推論延遲（PC1 單執行緒 CPU）：p50≈0.3ms、p99≈0.6ms，遠低於 xApp 的 5ms 逾時。
- **Critic 也改成排列不變的集合架構（DeepSets，2026-09-30 第二輪審查）**：每個活躍 UE 的
  [自己 3 維｜節點 5 維] 過共用編碼器 φ（`8→64→64`），對活躍 UE 做 mean／max pooling，接上節點
  5 維，經 ρ（`133→64→1`）輸出 V(s)。原本扁平的 `53→128→64→1` 會把不同 slot 的 UE 當成不同東西
  學，跟排列等變的 Actor 不一致；advantage 品質取決於 V 準不準，資料少時影響更大。

#### 2.4 Actor 的訓練樣本篩選（2026-09-30 第二輪審查修正）

只用「壅塞」樣本更新 Actor（非壅塞時 reward 跟 PRB 怎麼分無關）。判斷條件從「決策前 $s$ **或**
決策後 $s'$ 的最大 RLC 佇列 ≥ 100k」改成**只看決策前 $s$**（且 ≥2 個活躍 UE）：$s'$ 是動作造成
的結果——對某 UE 設上限會讓它的佇列變長、$s'$ 更容易過門檻——用 $s'$ 篩選等於「哪些樣本拿來學」
取決於「採取了什麼動作」，policy gradient 有選擇偏差（方向是把 Actor 拉回 PF，正好壓掉要找的
改進）。標準原則是篩選只能用決策前的資訊。代價是壅塞相位剛開始、佇列還沒堆起來的幾步不計入；
100k 門檻原本是用「$s$ 或 $s'$」校準的，收到真實資料後要確認訓練 log 的 `contended=xx%` 仍在合理
範圍（舊版約 35~45%），必要時用 `CONTENDED_BUF_BYTES` 調整。

---

#### 2.x 2026-10-01 修訂：動作改為時域遮罩檔位（`DRL_ACTION_DOMAIN=mask`，預設）

場景 P 試驗（HISTORY.md 續四十六）證實：**這個平台上頻域 PRB 上限無法重新分配資源**。OAI 每個 UE 每秒最多約 430 次傳輸
（短 PUCCH 每 occasion 最多 2 個 ACK 位元，約 2/3 的下行 slot），限制一個 UE 的 PRB 後它仍每個 slot 搶排程，別的 UE 撞到自己
的次數上限，省下的 RB 閒置（−10%）。時域 slot 遮罩讓好 UE 在被讓出的 slot 獨佔 106 RB，實測 +4%（兩次可重現）。

因此每個 UE 的 5 檔動作改成 5 個 16 位元遮罩（第 k 位元＝frame 內 slot%16==k 可排程），PRB 一律不設上限：
`MASK_TIERS = (0x0101, 0x1111, 0x9292, 0xab98, 0xFFFF)`（允許 2／4／6／8／16 個位元；最後一檔全開＝PF，PF 初始化與 PPO 等其餘
設計完全不變）。0x9292 平台實測、0x1111 平台重測中、0x0101／0xab98 由 `/home/lindor/pf16_run_20260930/slotsim/` 評估。遮罩
與 TDD 週期交互作用，同比例不同樣式效果差很多（均勻 0x5555 無效），所以樣式要先評估、不能取均勻間隔。同節點被限制的 UE 共用
同一組受限 slot（不做旋轉：旋轉會改變樣式與 TDD 的對齊、效果不可預期）。`DRL_ACTION_DOMAIN=prb` 可退回舊版 PRB 上限。
xApp（12 個 `xapp_nodeN.c`）已改為讀 JSON 的 `slot_mask`，OAI MAC 原本就支援。

### A.3. Reward：維持純吞吐量（2026-09-30 審查修正：拿掉反事實 PF reward）

原設計把 reward 改成 $R_i=\Delta TBS_i/B_{max}-\widehat R^{PF}_i(s)$（扣掉「PF 在同一 state 下的
預期表現」），理由是扣掉外生流量這個共同因子、提高訊噪比。**審查發現這在數學上是多餘的**：

- $\widehat R^{PF}(s)$ 只跟 state 有關。在 γ=0、advantage $A=r-V(s)$ 的設定下，從 reward 扣掉任何
  只跟 state 有關的 $b(s)$，Critic 會學成 $V'(s)=V(s)-b(s)$，advantage
  $(r-b(s))-V'(s)=r-V(s)$ 完全不變——**state-only 的 baseline 會被 Critic 完整吸收，policy
  gradient 的期望值不變**。Critic 本身就是「最好的 state-only baseline」，外生流量造成的變異原本就
  由它扣除。
- 反而多了一個要另外訓練的模型，而且它用 PF 資料訓練、卻套在 DRL 策略產生的 state 分布上，有分布
  偏移風險。
- 原設計真正想要的「advantage 代表比 PF 好多少」，改用 PF-shadow 資料預訓練 Critic 就能達到（§1.5）。

因此 reward 維持 Stage 2~4 既有的 `REWARD_MODE=throughput_only`（`compute_reward_breakdown()`，
$R=\frac1n\sum_i\min(\Delta TBS_i/B_{max},1)$），`reward_calculator.py` 不改。附帶好處：Stage 5 的
Lagrangian 限制式（`R=R_{tp}+\lambda(JFI-JFI_{min})`）照舊疊在 $R_{tp}$ 上，不需要重新設計。

MORPH（arXiv:2605.01128）的實際機制是「融合 iPerf 實測／MCS 分布／PHY 模擬三種吞吐量訊號降低
sim-to-real 偏差」，跟本系統直接在真實平台上量測的情境不同，不再引用為 reward 設計依據。原 §3.3
的 TailO-RAN CCDF 備案一併撤除。

---

### A.4. State：加入 relational 特徵（局部多跳 credit assignment）

#### 4.1 文獻依據

- **Yamin & Permuter**（arXiv:2305.16170，Ad Hoc Networks 2024，已驗證）：Relational Advantage
  Actor-Critic，每個節點的輸入串接鄰居節點的關聯資訊，不需要中心化訓練就能做隱式多跳 credit
  assignment——對應本系統「每節點各自獨立訓練」的限制。

#### 4.2 設計：新增兩維特徵，STATE_DIM 51 → 53

$$\hat p=\text{clip}\big(0.5+5\,(bh^{parent}_t-\overline{bh}^{parent}_{t-1}),\ 0,\ 1\big),\qquad
\overline{bh}_t=0.7\,\overline{bh}_{t-1}+0.3\,bh_t$$

$$\hat c=\text{clip}\Big(\log\big(1+\textstyle\sum_{j\in\text{children}}Q_j\big)\,/\,\log\big(1+Q_{max}\,|\text{children}|\big),\ 0,\ 1\Big)$$

- $\hat p$：parent 節點 `bh_ratio` 相對其平滑值的偏離（原設計的單步差分太吵，改用 EWMA 平滑）。
  上游 relay 的 backhaul 越來越忙 → 流進本節點的資料會變少。
- $\hat c$：children 節點的 RLC 佇列加總（下游壓力）。
- 拓樸靜態已知（`CLAUDE.md` 第 1 節）：access 的 parent 是對應 relay；relay 的 parent 是 Donor，
  **Donor 沒有 MT 也沒有 Local rApp，所以 relay 的 $\hat p$ 恆為中性值 0.5**——對 relay 而言這一維
  不帶資訊，relay 對上游的感知只靠自己的 `bh_ratio`。relay 的 children 是兩個 access 節點；access
  沒有 children，$\hat c=0$。（UE17 已於 2026-09-30 移除，relay 的 DU 底下只剩兩個 access 節點的 MT。）

**取得方式（2026-09-30 修正）**：原設計「推論時直接讀 MongoDB 裡 parent/children 的最新經驗」會
吃掉 xApp 的 5ms 預算，且 shadow 模式下各節點寫的是 `node{N}_pf_shadow`、不是 `_experiences`。改成：

- 主迴圈每步把自己的 `(bh_ratio, RLC 佇列總和)` 記在記憶體；背景執行緒 `_relational_worker`
  （active/shadow 兩種模式都跑）每秒把它 upsert 到共用 collection `node_status`，並讀 parent/children
  的最新一筆算好 $\hat p,\hat c$ 快取。**推論路徑只讀記憶體快取，不碰 MongoDB**。
- 對方超過 10 秒沒更新（容器掛掉/重啟中）→ 退回中性值，不拿過期數字當輸入。
- 每次請求一開始就把 `fairness_bias`／$\hat p$／$\hat c$ 取一份快照，推論與存進經驗的 `state_vec`
  用同一份——避免背景執行緒在兩次編碼之間更新快取，造成存下的 state 不是策略實際看到的 state、
  PPO 比例算錯（這個競態舊版的 `fairness_bias` 就有，一併修正）。

#### 4.3 跟現有 `fairness_bias` 的關係

$\hat f$（同角色平均的相對落後程度，全域視角）保留不變；$\hat p,\hat c$ 是只看直接 parent/children
的局部拓樸視角，兩者互補。

---

### A.5. 參數總表（新增/變更項，未列出的沿用 `STAGE2_DESIGN.md` §5）

| 參數 | 數值 | 位置 |
|---|---|---|
| State 維度 | 53（每 UE 3 維×16 + 節點 5 維） | `drl_agent.py` `STATE_DIM` |
| 上限檔位 | {0.3, 0.5, 0.7, 0.85, 1.0}，PF 檔 = 1.0 | `CAP_TIERS`／`PF_TIER` |
| Actor | 共用 per-UE 頭 `14→64→64→5` | `ActorNetworkMLP`／`build_per_ue_inputs()` |
| Actor 初始化 PF 檔機率 | 0.8 | `DRL_PF_INIT_PROB` |
| Entropy 下限 | 0.1（低於則係數拉到 0.05） | `DRL_ENTROPY_FLOOR` |
| Critic | DeepSets：φ `8→64→64`、mean/max pooling、ρ `133→64→1`；PF-shadow 預訓練 | `CriticNetworkMLP`／`pretrain_critic()` |
| Actor 更新樣本 | 決策前 $s$ 最大佇列 ≥ 100k 且 ≥2 UE | `_contended_mask()`／`CONTENDED_BUF_BYTES` |
| Reward | `throughput_only`（不變） | `reward_calculator.py` |
| Relational 更新週期／過期門檻 | 1 秒／10 秒 | `REL_POLL_S`／`REL_STALE_S` |
| $\hat p$ 平滑係數／增益 | 0.3／5.0 | `REL_TREND_EWMA`／`REL_TREND_GAIN` |
| MongoDB collections | `node{N}_pf_shadow`、`node_status` | `inference_server.py` |
| MongoDB 動作欄位 | `action_tiers`（MLP）；`action_ratios` 只剩 GRU | `_build_rl_experience()` |

---

### A.6. 已知限制（論文方法論須說明）

- **γ=0（contextual bandit）**：假設這一秒的上限只影響這一秒的吞吐量，忽略 TCP 壅塞視窗等跨秒
  延遲效應。沿用舊版設定，未改。
- **Critic 預訓練資料的 `fairness_bias` 恆為中性**：shadow 期間沒有 Global xApp 訊號；線上時這一維
  會變動，Critic 需要在線上訓練中補學這一維的影響。
- **relay 的 $\hat p$ 恆中性**（Donor 沒有 MT/rApp，見 §4.2）。
- **PPO 搭配回放緩衝區**：標準 PPO 是 on-policy（收一批、用完就丟）；這裡每 60 秒從最近 5000 筆
  經驗（約 83 分鐘、跨多個舊策略）抽樣更新，屬 off-policy 變體，靠 PPO 比例裁剪（截斷式 importance
  sampling）控制偏差。訓練 log 的 `ppo_clip_frac` 若長期 >30%，代表舊資料偏離目前策略太多，應縮小
  `TRAIN_FETCH_LIMIT`。
- **advantage 用固定尺度（獎勵滾動標準差）而非每批 z-score**：沿用 2026-09-26 的設計——非壅塞時
  reward 與動作無關，每批標準化會把雜訊放大。
- **GRU 分支未改**：`MODEL_ARCH=gru` 維持舊版 Dirichlet 設計，Local DRL v2 只適用 `MODEL_ARCH=mlp`。

---

### A.7. 與其他設計文件的關係

- `STAGE2_DESIGN.md`：§1、§2（Local 舊設計）已被本文件取代；§3/§4（Global xApp／Global rApp
  `FL_MODE=avg`）不變，仍是 Stage 2 現行設計。
- `DRL_DESIGN.md`：GRU／Lagrangian 分支的歷史推導；Stage 5 重新啟用 Lagrangian 時疊在本文件的
  純吞吐量 reward 上（§3）。
- `STAGE4_CUSTOM_FL_DESIGN.md`：Global 層（Stage 3／4），假設 Local 層是本文件的設計。

### A.8. 實作進度

1. **已完成（2026-09-29）**：`dl_aggr_prb` 查證（不需改 E2 格式）；12 個 `xapp_nodeN.c` 的
   `compute_delta_prb()`＋`pf_actual_rbs`（已編譯安裝，PC1）；`inference_server.py` 的
   `XAPP_MODE=shadow`。
2. **已完成（2026-09-30）**：`drl_agent.py`（STATE_DIM 53、離散檔位、共用 per-UE Actor 頭、PF 初始化、
   Categorical PPO、`pretrain_critic()`、`is_trained` 對 MLP 恆為 True——策略一開始就合法，不再需要
   BSR 啟發式暖身）；`inference_server.py`（relational 背景執行緒與 `node_status`、特徵快照、
   `action_tiers`、MLP 推論失敗改退回 PF 而非 BSR 啟發式、shadow 資料改存 `ues`/`t_mono`）；
   `training_pipeline.py`（projection 加 `action_tiers`）；新增 `pretrain_critic.py`（Dockerfile 已加入）。
   離線單元測試 13 項全過（初始化≈PF、排列等變、`behavior_logp` 與訓練端一致、合成環境下學習方向
   正確、Critic 預訓練、shadow 資料配對規則等），推論延遲 p99≈0.6ms。
   **第二輪審查（同日）**：Critic 改 DeepSets（§2.3）、Actor 樣本篩選只看決策前 $s$（§2.4）；
   單元測試增為 15 項全過（新增 Critic 排列不變、壅塞遮罩只看 $s$）。
3. **下一步**：重建 inference image → 三主機啟動 → `XAPP_MODE=shadow` 跑 Scenario T、TH 各 2~3 小時
   收集資料 → 12 節點 × 2 場景家族跑 `pretrain_critic.py` → 以 `FL_MODE=none` 驗證 Local DRL v2
   單獨能否贏 PF → 再接 Stage 2 FedAvg。

4. **收完 PF-shadow 資料後（使用者 2026-09-30 要求）：算各場景的吞吐量理論上限**，當論文裡 PF 與各 Stage 的參照。
   做法：用 shadow 資料的每 UE 每秒 `pf_actual_rbs` 與 `bsr`（送出 bytes）估每 UE 的「每 RB 效率」（依通道檔位分組），
   再對每個節點、每個相位把可用 RB 依效率由高到低分給 UE、每個 UE 以自己的需求為上限（有需求上限的貪婪分配），
   得到該場景可達的最大吞吐量。報告形式：「需求 100%／理論上限 X%／PF 84%（T）／各 Stage Y%」。注意：需求與 RB 效率
   要用 T、TH 各自的場景（同 seed）算；上限只反映排程可改進的空間，不含平台 CPU 上限的影響。

#### 8.x 拓樸改為 24 UE 後的待定修改（2026-10-01，Local DRL v2 單獨訓練前必須定案；之後 Stage 2～4 不得再改）

1. **同節點遮罩錯開**：同一節點的多個 UE 選到同一檔時，目前遮在同一批 slot 上，那些 slot 可能空著。應依 UE 在節點內的順序旋轉樣式（例如第 k 個 UE 的遮罩循環左移 k 個 slot），並在平台上驗證錯開後的增益。relay 節點有 4 個子節點（2 MT＋2 UE），問題更明顯。
2. **p̂ 已變常數**：backhaul 預算停用後 `bh_ratio` 恆為 1，p̂ 恆為 0.5、不帶資訊。改為有意義的上游訊號（例如 parent DU 的壅塞程度）或拿掉。
3. **子節點類型特徵**：relay 的子節點有 MT（backhaul）與 UE；E2SM-MAC 沒有 IMSI，無法直接區分。候選：依連線順序（MT 先連）或用流量比對（子 access 節點發佈每秒送出量，與 relay 端各 RNTI 吞吐量比對）。
4. **relay 的 reward**：目前把 MT 的 backhaul 流量與自己 UE 的流量一起計入，需重新評估。
5. **Critic 預訓練重做**：平台設定（out-of-band、-E、S=0.3、F1-U 上行修正）與拓樸都已改變。

**8.x 定案（2026-10-01 晚，Local DRL v2 單獨訓練前；之後 Stage 2～4 不得再改）**
- 子節點類型特徵：**採用**。每 UE 特徵 3→4 維（第 4 維 `is_iab_child`），state 53→69 維。inference_server.py 依 E2 回報順序標記：relay 節點前 `len(CHILDREN_OF)` 個子節點為 MT（MongoDB 紀錄證實 MT 先附著；relay 直連 UE 由 start_relay_ues.sh 在 13/13 E2 之後才啟動）。
- p̂：**改義**為 parent DU 的 RLC 佇列總和（上游壅塞）＝log1p(parent total_buf)/log1p(MAX_BUF_INFO×4)；無 parent／過期＝0。舊定義（parent bh_ratio 趨勢）在 backhaul 預算停用後恆為 0.5。
- relay reward：**不改**（PB 試驗中遮 relay 壞 UE 後 relay 送出總量增加，reward 與目標一致）。
- 同節點遮罩錯開：**暫不採用**。PB 試驗兩個 relay UE 用同一遮罩，讓出的 slot 由 MT 獨佔，拿到模型上限約 86%；錯開會讓 relay UE 輪流佔用 slot、MT 可獨佔的 slot 變少，可能損害主要增益來源。留作之後的 ablation。
- 離線測試（/tmp 測試腳本，映像內執行）：69 維位置、旗標、壅塞判定切片、推論、合成訓練、舊 53 維經驗過濾皆通過。

### A.9. 學習曲線：24 UE、HS 軌單獨訓練（2026-10-02，FL_MODE=none）

**條件**：24 UE 拓樸、state 69 維；Critic 先以 hs 家族 PF-shadow 資料（約 76 分鐘，每節點 4,352～4,720 筆）預訓練，時間保留集解釋比例 Node1～12＝91.5/82.8/57.9/93.2/91.9/91.6/90.1/85.0/85.3/12.1/87.2/64.7%（Node3/10/12 偏低是保留時段 reward 變異很小，絕對誤差與其他節點同量級）。訓練 02:13～09:01（約 6 h 48 min，step≈3,800），場景家族 `hs`＝(HS,HS,G,HS,HS,G)、TCP/UDP 交替、訓練 seed；`REWARD_MODE=throughput_only`。期間 FlexRIC 崩潰 2 次（05:19、08:38），watchdog 完整重啟、保留經驗與 checkpoint。凍結 checkpoint：`/home/lindor/drl_ckpt_local_v2_hs_frozen_20261002/`。

**指標**（`iab/learning_curve.py`，每 20 分鐘取最近 20 分鐘經驗）：「相對 PF」＝壅塞樣本的 (r − V_PF(s)) ÷ mean V_PF(s)，V_PF 是訓練開始前凍結的 PF 預訓練 Critic——**是估計值，不是量測**；Critic 對 PF 少見的狀態可能低估，數值可能偏高，只用來看趨勢。介入＝壅塞樣本中活躍 UE 選非全開檔位的比例。熵＝最後一輪有更新 Actor 的節點平均。原始資料：`experiment_results/local_drl_v2_hs_learning_curve.csv`。

| 訓練（分） | 時刻 | 相對 PF（估） | 介入 | 壅塞樣本 | step | 熵 |
|---|---|---|---|---|---|---|
| 20 | 02:33 | +0.5% | 18% | 1656 | 190 | 0.63 |
| 40 | 02:53 | +3.8% | 14% | 1549 | 399 | 0.54 |
| 60 | 03:13 | +8.2% | 15% | 1580 | 590 | 0.56 |
| 80 | 03:33 | +4.9% | 12% | 1651 | 790 | 0.49 |
| 100 | 03:54 | +17.0% | 14% | 1545 | 990 | 0.46 |
| 120 | 04:14 | +17.6% | 11% | 1064 | 1190 | 0.47 |
| 140 | 04:34 | +9.7% | 10% | 1481 | 1390 | 0.39 |
| 160 | 04:54 | +4.0% | 9% | 1522 | 1590 | 0.37 |
| 180 | 05:14 | +4.5% | 7% | 952 | 1790 | 0.34 |
| 200 | 05:34 | +5.1% | 6% | 149 | 1880 | 0.34 |
| 220 | 05:54 | −2.4% | 6% | 1210 | 2080 | 0.29 |
| 241 | 06:14 | +5.2% | 7% | 1106 | 2280 | 0.28 |
| 261 | 06:34 | +11.5% | 7% | 1130 | 2480 | 0.26 |
| 281 | 06:54 | +16.1% | 6% | 1278 | 2680 | 0.28 |
| 301 | 07:14 | −2.4% | 6% | 1439 | 2880 | 0.28 |
| 321 | 07:35 | +6.9% | 5% | 958 | 3080 | 0.30 |
| 341 | 07:55 | +4.0% | 7% | 1160 | 3280 | 0.31 |
| 361 | 08:15 | +13.3% | 6% | 1623 | 3480 | 0.25 |
| 381 | 08:35 | +5.1% | 5% | 1213 | 3680 | 0.25 |
| 401 | 08:55 | +13.6% | 7% | 388 | 3780 | 0.26 |

200 分與 401 分的壅塞樣本少，是取樣視窗含崩潰重啟期間。

各節點全程平均（最後 5 點平均）：N1 +5.6（+8.1）、N2 −6.3（−2.9）、N3 +2.2（+10.9）、N4 +7.0（+14.8）、N5 +13.7（+17.4）、N6 +42.7（+51.2）、N7 +13.1（+12.6）、N8 +5.7（−1.0）、N9 +10.6（+2.8）、N10 +35.6（+35.4）、N11 +4.9（+10.6）、N12 +36.1（+23.7）%。

**觀察**
- 20 點中 18 點在 PF 之上；約 2 小時後在 +5～+15% 間震盪、不再明顯上升。單點跳動主要來自每 20 分鐘輪到的場景不同。
- 介入比例 18%→5～7%，熵 0.63→0.25：策略變得挑時機才遮罩，尚未塌縮。
- 壅塞樣本佔批次僅約 2～16%（原校準預期 35～45%）：HS 每個壅塞相位只有熱點與混合 branch 真正壅塞，Actor 更新次數少、學得慢；部分節點常因批次壅塞樣本 <8 筆跳過 Actor 更新。
- **relay 節點未學會分辨遮罩對象**（訓練約 160 分鐘時，最近 60 分鐘壅塞樣本）：遮 MT 7～11%、遮 relay UE 10～14%，幾乎相同，像探索雜訊；PB 試驗的有效策略是只遮 relay 邊緣 UE、不遮 MT。relay 的增益要經 MT→下游 access 子樹才看得到，而 relay 自己的 reward 只算到 MT 那一段，訊號較弱。N2 全程平均為負。若之後確認是設計問題，可能方向：relay reward 納入子樹吞吐量、或禁止 relay 遮 MT——皆屬 Local 層修改，須在 Stage 2 開始前決定。
- 實際效果以凍結評估量測為準（`experiment_results/LocalDRLv2.md`）。
- **學習曲線指標有系統性偏差（2026-10-02 驗證）**：把同一個指標套到「控制完全等於 PF」的資料上——argmax 評估期間（所有 UE 全開）的經驗——得到 **+11.3%**；隨機採樣評估 HS TCP 期間 +11.9%（該次實測總送達與 PF 相同）；訓練後段 07:00～09:00 為 +7.3%。即凍結 PF Critic 在這段時間系統性低估 reward 約 7～12%（可能因預訓練資料只有 76 分鐘、與訓練/評估時段的場景 seed 與系統狀態不同），**訓練期間的「+5～15%」幾乎全部是指標偏差，不是策略真的贏 PF**。之後的學習曲線必須同時算一個純 PF 對照（例如定期插入 PF 控制時段，或用同時段的 PF 參考），才能扣掉偏差。

### A.10. 修訂設計（v2.1，2026-10-02 草案；依第 0 步動態規則 HS TCP 結果定案）

#### 10.1 v2 失敗原因（凍結評估 HS／G × TCP／UDP 全部與 PF 持平）
1. **學習訊號太弱**：reward 是節點 1 秒內的總吞吐量，遮一個 UE 造成的變化遠小於流量本身的波動；壅塞樣本只佔批次 2～16%，Actor 每輪常因 <8 筆跳過，約 3,800 步裡實際更新只有數百次。
2. **動作空間太散**：每個 UE 獨立從 5 檔抽一檔，好策略（壅塞時只遮「最差的那個 UE」）要從大量組合中自己摸出來；探索時隨機遮到好 UE、遮到 MT 都會被罰 → 策略退回「全開」（機率約 90%），argmax 評估完全等於 PF。
3. **起點是 PF**：PF 初始化加上 1、2，最容易學到的是「繼續當 PF」。
4. **評估指標偏差**：凍結 PF Critic 系統性低估 7～12%，學習曲線看起來贏、實測持平（§9）。
- 更正：先前推測「relay 的 reward 看不到子樹效果」不成立——reward 是各 UE 正規化吞吐量的平均，relay 的 MT 吞吐量就是送往下游子樹的量，遮 relay 邊緣 UE 讓出 slot 後 MT 吞吐量上升會直接反映在 relay 的 reward。reward 不改。

#### 10.2 修改
| # | 修改 | 內容 |
|---|---|---|
| a | **節點層級宏動作** | 每個節點每秒從 K 個宏動作選一個：0＝不介入（PF）；1＝遮「最差候選」一檔輕（0x9292）；2＝最差候選一檔重（0x1111）；3＝最差兩個候選都 0x1111。候選＝活躍、非當下 MCS 最高者、非 MT（`is_iab_child`=0）的 UE，依 MCS 由低到高排序；候選不足時對應動作退化成可行的最接近者。「遮誰」由排序決定，DRL 只學「要不要介入、介入多重」。 |
| b | **Actor 輸入** | DeepSets 節點編碼（同 Critic）＋最差候選的 [MCS、佇列、吞吐量]＋「好通道 UE 是否積壓」旗標（同動態規則的 starved 判定）＋ p̂、ĉ。輸出 K 個 logits。 |
| c | **以動態規則做行為複製初始化** | 規則（`RULE_KIND=dyn`）在 hs 訓練家族上跑約 60 分鐘收資料（rule 模式本來就把決策寫進 shadow 集合），把規則決策對應到宏動作（不遮→0、遮 1 個→2、遮 2 個→3），BC 訓練 Actor；Critic 同時用這批資料預訓練（V≈規則策略的價值）。DRL 從「已知比 PF 好的策略」出發，學的是規則做不到的：N 型／非邊緣情況不介入、介入強度。 |
| d | **決策點過濾** | Actor 只用「有介入機會」的樣本更新：有候選、且好通道 UE 積壓（starved）。取代原本「任一 UE 佇列 ≥100k」的壅塞判定，樣本更貼近真正的決策點。 |
| e | **真實學習曲線** | 不再用凍結 Critic 估計。每 2 小時凍結當下模型，量一份 HS TCP（約 25 分鐘，訓練暫停），與 PF 基準和規則比；學習曲線就是這些實測點。 |

#### 10.3 驗收
- 規則策略本身先在 HS TCP 贏 PF（第 0 步），否則代表 HS 改進空間不足，先改場景。
- v2.1 單獨訓練後的凍結評估（隨機採樣與 argmax 都量）：HS 須贏 PF，且目標不輸規則；G 不輸 PF。
- 雜訊：同策略兩次量測可差數 %（argmax=PF 那次差 6.7%），定案前 PF 與最終模型各重複量 2～3 次。

#### 10.4 實作（2026-10-02，`DRL_ACTION_SPACE=macro` 為預設；`per_ue` 退回 v2）
- **state 85 維**：每 UE 5 維＝v2 的 4 維＋`was_masked`（上一步回傳的 slot_mask≠0xFFFF）。Actor 沒有記憶，被遮的 UE 佇列與 MCS 會因遮罩改變（被遮後 MCS 由 3～7 回升到 8～14、好 UE 佇列下降），沒有這一維會「遮→條件消失→解除→再積壓→再遮」來回震盪；動態規則用遲滯處理同一件事。`inference_server.py` 在 active／rule／shadow 三種模式都維護 `_masked_rntis`，在本步 state 全部編碼完之後才更新。
- **候選與宏動作**（`drl_agent.macro_context()`／`macro_to_tiers()`，推論與訓練共用）：候選＝活躍、`is_iab_child=0`、非當下 MCS 最高者，依 MCS 升冪取前兩個；0＝全開、1＝最差候選 0x9292、2＝最差候選 0x1111、3＝最差兩個 0x1111；候選不足的動作 logit 設 −1e9。無候選時直接選 0、不存 behavior_logp。
- **Actor**（`ActorNetworkMacro`）：DeepSets 編碼（mean／max）＋節點 5 維＋兩個候選 [MCS、佇列、吞吐量、was_masked、有效位]＋starved＋候選數 → 4 logits。
- **Actor 更新樣本**：`_actor_sample_mask()`＝starved（MCS≥20 且佇列≥100 KB 的 UE 存在）且候選≥1，取代 v2 的「任一佇列≥100 KB」。
- **行為複製**（`bc_pretrain_actor()`、`pretrain_v21.py`）：規則模式的每筆 shadow 文件記 `rule_macro`（遮 0／1／≥2 個 → 0／2／3）與 `rule_masked_idx`；交叉熵＋標籤平滑 0.1（只分給有效動作）、決策點樣本權重 ×5、80 epochs；時間切分保留集（最後 20%）回報一致率；另回報「規則遮的 UE＝候選排序」的一致率。Critic 用同一批資料預訓練（V≈規則策略的 E[r|s]）。
- **經驗文件**：新增 `macro_action`；`_filter_valid_experiences` 在 macro 模式要求有此欄位（v2 的 69 維舊經驗自動排除）。
- 離線測試（映像內）：85 維與 was_masked 位置、MT 永不為候選、候選不足時的有效動作、推論只遮候選、合成資料 BC 決策點一致率 100%、PPO 訓練數值有限、count_contended、存檔／載入；伺服器端規則路徑的 rule_macro／rule_masked_idx／was_masked 與 macro_action 欄位皆正確。

> **⚠ 設計限制（2026-10-02，HS／HSH 改為 PB 結構後；Stage 2～4 FL 設計必須考慮）**
> 壅塞相位的改進空間全部集中在 4 個 relay 節點（relay 邊緣 UE 讓 slot 給 backhaul）；8 個 access 節點在壅塞時最好的動作永遠是「不介入」（舊版混合 access 結構平台實測無增益，已移除）。後果：
> 1. Local DRL 真正學到決策的只有 relay 節點，access 節點只學會「不要遮」。
> 2. Stage 2 FedAvg 以「決策點樣本數」（`count_contended()`＝starved 且有候選）加權，access 節點權重接近 0，聚合實際上幾乎是 4 個 relay 互相平均；access 節點只被動接收全域模型。
> 3. Stage 3（分 branch 聚合＋Hedge）與 Stage 4（角色個人化）的設計前提要重新檢查：branch 內平均時 access 節點幾乎沒有樣本、relay 主導；「relay／access 角色分軌」在 access 側沒有可學內容，個人化的收益只會出現在 relay 側。
> 4. 使用者 2026-10-02 決定先照此場景進行（唯一平台實測贏 PF 的結構）；若之後要讓 access 層也有可學決策，需先在平台上找到 access 層實測有增益的結構（P 原配置 4 節點同時壅塞僅 +4%）。


#### 10.5 預訓練與冒煙結果（2026-10-02）
- 規則資料：hs 訓練家族 75 分鐘、每節點約 4,600 筆；遮罩決策集中在 relay（Node1～4：386／228／170／292 筆），access 僅 Node5 6 筆。
- BC：relay「規則遮的 UE＝候選排序」91～98%；決策點時間保留集一致率 58～75%，錯誤幾乎都在規則 3 秒啟動延遲／5 秒遲滯的轉換附近（state 無法分辨）。Critic 時間保留集解釋比例 relay 80～95%。
- 冒煙（HS TCP、seed 20260930、不訓練、隨機採樣）：壅塞送達 PF 70／規則 77／**v2.1 BC 81**。
- **Actor 批次（2026-10-02 補）**：`_decision_actor_batch()` 每個訓練步從全部讀到的經驗中篩「決策點且有 behavior_logp」的樣本、隨機抽最多 TRAIN_BATCH_SIZE 筆給 Actor；Critic 仍用隨機 128 筆。原本混在同一個隨機批次裡，決策點常不到 8 筆而跳過 Actor。
- **決策點閘門與 MCS 差距（2026-10-02 補）**：`macro_context()` 新增 `decision`＝有可行遮罩動作 且（starved 或 已有活躍 UE was_masked）；非決策點推論固定選 0、不存 behavior_logp，Actor 也只在決策點更新。遮罩動作 1～3 只在候選 MCS ≤ 當下最好 UE 的 MCS − `DRL_MACRO_MIN_GAP`（10）時有效。理由：Actor 只在決策點有梯度，共用網路會讓非決策點輸出漂移（實測 access 節點大量誤遮通道相近的 UE）；平台實測通道相近 UE 互遮無增益。

#### 10.6 單獨訓練學習曲線與正式評估（2026-10-03）
- 學習曲線（HS TCP，seed 20260930，每 2 小時凍結實測；PF 70）：BC 起點 81 → 2 h 81 → 4 h 79 → 6 h 80（相對 PF +13～16%），RL 訓練維持 BC 水準、未再提升。
- 正式評估（各 2 次）：壅塞送達 DRL 80／80 對 PF 70／69（+15.1%）；滿足率 −2.8%、滿足率 JFI −5.6%、壅塞 RTT +17%；G 不傷害。詳見 `experiment_results/LocalDRLv2.md`。
- 待改進：RL 未超越 BC；滿足率／公平性／延遲的取捨（Stage 5 Lagrangian 或 reward 設計）；決策點樣本少、PPO 裁剪比例高的訓練效率問題。
