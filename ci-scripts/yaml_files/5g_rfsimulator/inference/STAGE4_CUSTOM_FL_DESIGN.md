# Stage 3/4 自訂聚合演算法設計書

> **狀態（2026-09-29 全面重新設計）：CAPA-Fed／ERA-Fed 這條路線已放棄**，改成從 2022 年後
> IAB/O-RAN 文獻重新設計 Stage 3／Stage 4 的 Global FL。**觸發原因**：CAPA-Fed 完整訓練＋量測
> （T：4.92 Mbps 打平 PF；TH：5.25/5.32 Mbps 仍輸 PF）＋Dirichlet 退火時間常數修正重訓後仍未
> 贏過 PF baseline，使用者判斷「與其在同一個未經文獻驗證的 Local DRL 基礎上持續換 Global FL
> 聚合方式，不如先把 Local DRL 用文獻立論紮實，Global FL 也一併重新設計」，要求 Stage 3／4
> 都要對應到 2022 年後真實存在的 IAB/O-RAN 文獻。完整決策過程、4 輪文獻研究＋2 輪引用驗證的
> 記錄見 `HISTORY.md` 續三十六；Local DRL 重新設計見 `LOCAL_DRL_V2_DESIGN.md`（Stage 3/4 的
> 前提：Local 層已改用新設計，Global 層假設接的是新版 Local 輸出）。
>
> **新方向總覽**：
> - **新 Stage 3（§10）= 兩層 Hierarchical FedAvg ＋ Hedge 自適應跨 branch 加權 ＋ Sattler CFL
>   分歧 fallback**——branch 內（relay+2 個 access 子節點）先做樣本數加權平均，跨 4 個 branch
>   聚合時改用 Read/14（Wang et al., ICC 2024，O-RAN 原生 Hierarchical FL）的 Hedge 線上自適應
>   加權取代單純樣本數加權，廣播回節點**仍是整份覆寫**（標準 FedAvg 語意，刻意保留，用來單獨
>   驗證「階層式＋異質性感知聚合」本身的貢獻，不跟 Stage 4 的個人化廣播混在一起比較）。
> - **新 Stage 4（§11）= HiRA-Fed（Hierarchical Role-Aware Federation）**——複用 Stage 3 完全
>   相同的聚合計算（branch 內平均＋Hedge 跨 branch），**只改廣播這一步**：不再整份覆寫，改成
>   雙錨點個人化插值（Actor：branch 平均＋全域平均的角色相關混合）＋角色條件式彈性拉扯
>   （Critic：relay 節點拉力比 access 節點強），修正 Stage 3 殘留的「整份覆寫抹掉本地分化」
>   問題（這個問題最早在舊版 CAPA-Fed／ERA-Fed 的離線診斷中發現，見下方附錄）。
>
> **使用者硬性邊界（2026-09-29）**：Global 端必須維持聯邦式學習範式，不接受 policy
> distillation／集中式多工訓練等跳出 FL 的替代方案，即使技術分析顯示可能表現更好（已存成
> `feedback_global_must_stay_fl` 記憶，見 `HISTORY.md` 續三十六）。這也是為什麼 §10/§11 的
> 聚合計算本身（算平均）一律用 FedAvg 數學，只有廣播這一步（把平均結果套用回節點）Stage 4 才
> 改用彈性拉扯個人化——彈性拉扯／個人化插值仍然是聯邦學習文獻內的既有方法（pFedMe／APFL／
> EASGD），不是離開 FL 範式。
>
> 舊版 AW-FedAvg（`IABClusterFedAvg`／`IABCustomFedAvg`）與舊版 Stage 3 soft/weighted cluster
> FL 已於 2026-09-29 路線圖重新定案時整段移除；CAPA-Fed／ERA-Fed（原 §10/§11）已於本次
> （2026-09-29 全面重新設計）移到本文件末尾的附錄，保留完整設計與診斷過程當論文方法論的失敗
> 案例記錄，不再是現行設計。§1~9（AW-FedAvg v1/v2 設計與失敗診斷）維持不動，一併保留。

---

## 10. Hierarchical FedAvg + Hedge 自適應加權 + CFL Fallback —— **新 Stage 3** 設計（Scenario TH 用）

> **2026-09-30 修訂（以論文第 3 章 `chapter3-system-model.tex` 為準，實作時照這版）**：①分歧偵測改為 leave-one-out——
> 每個貢獻成員的更新 vs 同 branch 其他貢獻成員的加權更新和取 cosine（原本的兩兩最小 cosine 只找得到「一對」、無法決定誰分歧），
> 只在 ≥3 個貢獻成員（w>0）時啟用，零向量 cosine 定為 1，全部都低於門檻時不排除；②**被排除的節點當輪不參與全域平均、
> 也不被覆寫，保留自己的模型**（單節點 cluster，每輪重新判定）——原設計「自成一群」但沿用所屬 branch 的 Hedge 權重，
> 展開後 Σ_b ζ_b Σ_{n∈N_b⁺} w_n Θ_n 跟沒排除一樣，機制無效；③全部 w=0 跳過本輪；branch 無貢獻成員不參與第二層、Hedge 權重不更新；
> ④退化成 Stage 2 只需 η_H=0 且 τ_CFL=−1。因此下方 §10.5「刻意維持整份覆寫」已不成立：分歧節點是 Stage 3 起就有的個人化。

### 10.1 為什麼選這個方向

CAPA-Fed（附錄 A）已完整訓練＋量測，T/TH 皆未贏過 PF。2026-09-29 全面重新設計時的文獻檢索
（`HISTORY.md` 續三十六）確認：IAB/O-RAN 的 FL 資源分配這個具體領域裡，幾乎所有論文的聚合機制
核心都是 FedAvg 或其加權變體（實際打開 PDF 核對過公式的：Read/19 O-RANFed Eq.(2) 樣本數加權
FedAvg、Unread/02 EcoFL Algorithm 1 純平均 FedAvg；O-RANFed 自己拿 FedAvg 比 FedProx，FedProx
在 RIC 資源受限場景下反而更差）——**留在 FedAvg 家族內，不是預設沒細想，是這個領域目前幾乎
沒有例外的做法**。既然要留在 FedAvg 家族，新設計改變的是「怎麼結構化地做加權平均」，不是
「要不要做加權平均」：

- **Abad, Ozfatura, Gündüz, Ercetin**（arXiv:1909.02362，ICASSP 2020，已驗證存在）：
  client→base-station→core-network 兩層 FedAvg，base-station 層先池化再送到 core 層聚合——
  直接對應本系統 relay→branch→全域 這個已知拓樸結構的兩層聚合範本。
- **Wang et al.**（`~/thesis_papers/Read/14`，ICC 2024）：O-RAN 原生的 Hierarchical FL，
  near-RT RIC 當 edge 聚合器、non-RT RIC 當 cloud 聚合器，**明確處理 client 異質性**（non-IID），
  有推導 generalization-gap bound；cloud 層聚合用 **Hedge 演算法的線上集成加權**，不是單純
  樣本數加權——這是比通用 cellular HFL 論文更貼近「Scenario TH 持久異質性」這個具體問題的
  文獻，選它當主要引用。
- **Sattler, Müller, Samek**（IEEE TNNLS 2020，已驗證存在）：Clustered FL，依更新方向的
  cosine similarity 做 bi-partition，當 branch 內成員分歧過大、不該被平均在一起時的 fallback。

### 10.2 拓樸分組（Branch 定義，靜態已知）

沿用 `CLAUDE.md` 第 1 節的節點對照表，4 個 branch 分別是每個 relay 與其 2 個 access 子節點：

$$B_1=\{1,5,6\},\quad B_2=\{2,7,8\},\quad B_3=\{3,9,10\},\quad B_4=\{4,11,12\}$$

（UE17 已於 2026-09-30 移除，4 個 branch 現在結構完全對稱：每個 relay 各帶 2 個 access、每個 access 各帶 2 個 UE。）靜態字典 `PARENT_RELAY={5:1,6:1,7:2,8:2,9:3,10:3,11:4,12:4}`（
relay 對映自己），在 Flower `server_app.py` 端 hardcode，不需要跨行程通訊。

### 10.3 Level 1：Branch 內聚合（標準 FedAvg，樣本數加權）

$$\bar\theta_b^{actor} = \sum_{i\in B_b}\frac{n_i}{N_b}\theta_i^{actor}, \qquad \bar\theta_b^{critic} = \sum_{i\in B_b}\frac{n_i}{N_b}\theta_i^{critic}, \qquad N_b=\sum_{i\in B_b}n_i$$

**Sattler CFL Fallback**（分歧偵測，branch 內只有 3 個節點，機制保持簡單）：計算 branch 內
兩兩更新方向的最小 cosine similarity

$$\alpha_b = \min_{i,j\in B_b,\,i\neq j} \cos\big(\theta_i-\theta_i^{(t-1)},\ \theta_j-\theta_j^{(t-1)}\big)$$

若 $\alpha_b < \tau_{CFL}$（門檻，初始建議 $-0.1$，訓練時依實際分佈校準），代表這個 branch 內
至少有一個節點的更新方向跟其他成員明顯相反，此時**不對該節點做 branch 內平均**（該節點單獨
自成一個「branch」參與 Level 2，其餘成員照常平均）——branch 只有 3 個節點，不需要 Sattler
原論文的遞迴二分法，simple exclusion 已足夠。

### 10.4 Level 2：跨 Branch 聚合（Hedge 自適應加權，取代單純樣本數加權）

標準寫法（Abad et al. 的基線）是 $\theta_{global}=\sum_b (N_b/N)\bar\theta_b$；本設計依 Read/14
的做法改成 **Hedge 演算法的線上自適應加權**，用「這個 branch 聚合出來的模型這一輪表現好不好」
動態調整權重，而不只是「這個 branch 有多少資料」：

$$\ell_b^{(t)} = \text{MSE}\big(V_{\bar\theta_b^{critic}}(s),\ r\big)\Big|_{\text{branch } b \text{ 這一輪的壅塞樣本}} \qquad(\text{既有 Critic MSE，不需要新指標})$$

$$w_b^{(t)} = w_b^{(t-1)}\cdot\exp(-\eta\,\ell_b^{(t)}),\qquad \eta = \texttt{HEDGE\_LR}\ (\text{預設 } 1.0)$$

$$\hat w_b^{(t)} = \frac{(N_b/N)\cdot w_b^{(t)}}{\sum_{b'}(N_{b'}/N)\cdot w_{b'}^{(t)}} \qquad(\text{樣本數當先驗，Hedge 權重做動態修正})$$

$$\theta_{global}^{(t)} = \sum_b \hat w_b^{(t)}\,\bar\theta_b^{(t)}$$

$w_b$ 需要跨 `flwr run` 行程持久化（沿用既有磁碟序列化模式，存到 `hfedavg_hedge_state.pt`，
只存 4 個 branch 的純量權重，比 AW-FedAvg 當年的動量緩衝更輕量）。

### 10.5 廣播（本階段刻意維持整份覆寫）

$$\theta_i \leftarrow \theta_{global}^{(t)}\qquad \forall i$$

**刻意不做個人化混合**——這是跟 Stage 4（HiRA-Fed）的關鍵差異，目的是單獨驗證「階層式拓樸分組
＋異質性感知加權」這個機制本身的貢獻，不要跟「廣播不整份覆寫」這個效果混在一起比較，符合
`CLAUDE.md` 第 3 節「每次只換一個變數」的方法論原則。

### 10.6 概念出處

| 機制 | 借鑑對象 | 這裡的改動 |
|---|---|---|
| 兩層階層式聚合（branch→全域） | Abad, Ozfatura, Gündüz, Ercetin（ICASSP 2020） | 套用到 relay+access branch 這個已知拓樸，取代泛用的 device→BS→core 分層 |
| 異質性感知的線上自適應跨層加權 | Wang et al.（Read/14, ICC 2024，O-RAN 原生 Hierarchical FL） | 用既有 Critic MSE 當 Hedge 的損失訊號，不需要新指標 |
| Branch 內分歧 fallback | Sattler, Müller, Samek（Clustered FL, TNNLS 2020） | 簡化成 3 節點 branch 內的單次排除，不做遞迴二分 |

### 10.7 退化條件（驗證用）

- `HEDGE_LR=0` → $w_b$ 恆為初始值 → 精確退化成 Abad et al. 基線（純樣本數加權兩層 FedAvg，
  Read/14 增強關閉）。
- 只有 1 個 branch（例如把全部 12 節點視為同一個 branch）→ 精確退化成 Stage 2 的扁平 `IABFedAvg`。
- $\tau_{CFL} = -1$（cosine similarity 恆大於門檻）→ CFL fallback 永不觸發，退化成純 Hierarchical
  FedAvg（不含分歧保護）。

### 10.8 相容性與新增項

- **`client_app.py` 新增欄位**：`node_id`（已有，CAPA-Fed 就在傳）；聚合端新增 branch 分組邏輯
  （`PARENT_RELAY` 靜態字典）與 Hedge 權重狀態持久化，不需要 Local 端任何改動。
- **新增環境變數**：`HEDGE_LR`（預設 1.0）、`CFL_COS_THRESHOLD`（預設 -0.1）。
- 假設 Local 層已是 `LOCAL_DRL_V2_DESIGN.md` 的新設計（離散動作、53 維 state）——聚合的是
  新架構的 Actor/Critic 權重，`_flatten_state_dicts()`／`_weighted_average_flat()` 等既有工具
  函式不需要改（只要新舊 Actor/Critic 的 `state_dict()` key 結構一致就能直接沿用，維度變了
  但沒有新增/刪除子模組）。

### 10.9 風險與監控

- Hedge 權重可能過度懲罰「資料量本來就少」的 branch（例如某個 relay 分支的 access 節點剛好
  流量輕），而不是真的「模型不好」——訓練時應監控 $\hat w_b$ 是否長期偏離 $N_b/N$ 太多，
  必要時對 Hedge 修正幅度加上 clip。
- CFL fallback 觸發頻率應該很低（branch 只有 3 個節點，多數輪次不該分歧到需要排除）；若
  訓練 log 顯示頻繁觸發，代表 $\tau_{CFL}$ 設得太嚴或某個節點的訓練本身有問題，需要分開排查。

---

## 11. HiRA-Fed（Hierarchical Role-Aware Federation）—— **新 Stage 4** 設計，在 §10 的結構上修正「整份覆寫」問題（Scenario T/TH 皆用）

> **2026-09-30：本節待重新設計**。Stage 3 已改成分歧節點保留自身模型（見 §10 開頭修訂），「Stage 3 整份覆寫、Stage 4 才個人化」
> 的分工不再成立，HiRA-Fed 的定位需要重新想；下方內容僅供參考，不是現行設計。

### 11.1 設計動機：跟 §10 的關係

§10 的聚合計算（branch 內平均＋Hedge 跨 branch）本身已經是文獻證實有效的階層式加權方式，但
廣播這一步仍是整份覆寫——附錄 A（CAPA-Fed）的離線診斷已經證實**這個問題本身**（不管聚合公式
多精巧，只要廣播是覆寫，兩次廣播之間的本地分化就會被抹掉）是這個系統 FL 效果有限的根本原因
之一。HiRA-Fed **完全複用 §10 的聚合計算**（branch 內平均、Hedge 加權、CFL fallback 全部不變），
**只替換廣播這一步**，這樣可以把「Stage 3→4 的改進」精確歸因到「修正整份覆寫」這一個變數上，
符合單調遞增鏈的方法論要求。

### 11.2 文獻依據

你的論文庫裡沒有論文做過「兩層階層式聚合＋雙錨點角色相關個人化廣播」這個確切組合——HiRA-Fed
是真正的自創綜合，不是抄來的，但每個組成元件都有明確文獻依據：

- **pFedMe**（Dinh, Tran, Nguyen, NeurIPS 2020，已驗證存在）：Moreau envelope 正則化，每個
  節點的個人化模型永遠朝全域共識被拉、但不整份覆寫——已有延伸到聯邦 actor-critic 強化學習的
  應用先例（同時個人化 actor 與 critic），跟 HiRA-Fed 的場景高度吻合。
- **Elastic Averaging SGD**（Zhang, Choromanska, LeCun, NeurIPS 2015，已驗證存在）：彈性拉扯
  的具體數學形式（比例拉扯 $\theta\leftarrow(1-\rho)\theta_{own}+\rho\bar\theta$，取代整份
  覆寫），沿用附錄 B（ERA-Fed）已經推導過的形式。
- **APFL**（Deng, Kamani, Mahdavi, 2020）：本地-全域凸組合插值的個人化框架，這裡的「雙錨點」
  （branch＋全域，不是單一全域）是對 APFL 單錨點插值的延伸。
- **Unread/07（LLM-hRIC，arXiv:2504.18062，已在 IAB 場景驗證過）**：non-RT 端 guider 廣播
  全域策略、near-RT 端 RL 融合局部即時狀態的兩層模式，架構上跟 HiRA-Fed「branch 錨點＋全域
  錨點」的雙層資訊來源呼應，是獨立於 FL 聚合數學本身的第二個文獻立足點。

### 11.3 廣播：雙錨點個人化插值（Actor）＋ 角色條件式彈性拉扯（Critic）

沿用 §10.3/§10.4 算出的 $\bar\theta_b^{(t)}$（branch 平均）與 $\theta_{global}^{(t)}$（Hedge
加權全域平均），對每個節點 $i$（$b=branch(i)$）：

**Actor —— 雙錨點插值**：

$$A_i \leftarrow (1-\beta_i)\,A_{i,\text{own}} + \beta_i\big[\gamma_{\text{role}(i)}\,\bar A_b^{(t)} + (1-\gamma_{\text{role}(i)})\,A_{global}^{(t)}\big]$$

$$\beta_i = \text{clip}\Big(1-\frac{n_i}{\max_j n_j+\epsilon},\ \beta_{\min},\ \beta_{\max}\Big)\qquad(\text{沿用附錄 A CAPA-Fed 已驗證的信心加權公式})$$

$\gamma_{\text{role}}$ 為固定角色常數：$\gamma_{\text{access}}=0.6$（access 節點更信任自己
branch 內的平均——同一個 relay 底下的 access 節點共享同一段 backhaul 壅塞動態，`bh_ratio` 本來
就相關）、$\gamma_{\text{relay}}=0.2$（relay 節點更信任全域平均——自己的 children 服務不同的
下游負載，branch 內平均對 relay 自己代表性較弱）。

**Critic —— 角色條件式彈性拉扯**：

$$C_i \leftarrow C_i - \rho_{\text{role}(i)}\big(C_i - C_{global}^{(t)}\big)$$

$\rho_{\text{relay}} > \rho_{\text{access}}$（預設 0.5／0.2）——relay 節點的 Critic 被拉向全域
共識的力道比 access 節點強（呼應「Critic 該較快同步知識、Actor 該保留更多本地特化」的設計
動機，沿用附錄 B ERA-Fed 已推導過的角色分軌構想），但兩者都**永遠保留一部分本地身份**，不會
有任何子網路被完全覆寫。

### 11.4 退化條件（驗證用）

- $\beta_i=1,\ \gamma_{\text{role}}\equiv0\ \forall\text{role},\ \rho_{\text{role}}\equiv1\ \forall\text{role}$ → $A_i\leftarrow A_{global}^{(t)}$、$C_i\leftarrow C_{global}^{(t)}$，精確退化成 §10 的整份覆寫廣播（即新 Stage 3 本身）。
- 在此基礎上再讓 §10 退化成扁平 FedAvg（單一 branch）→ 精確退化成 Stage 2。
- $\gamma_{\text{relay}}=\gamma_{\text{access}}$（角色不分軌）→ 驗證「角色相關的固定混合比例」
  這個機制單獨的貢獻。

### 11.5 相容性與新增項

- 完全複用 §10 的聚合計算與新增欄位，額外新增環境變數：`HIRA_BETA_MIN/MAX`（沿用 CAPA-Fed
  的 0.1/0.9 預設）、`HIRA_GAMMA_ACCESS`（0.6）、`HIRA_GAMMA_RELAY`（0.2）、
  `HIRA_RHO_ACCESS`（0.2）、`HIRA_RHO_RELAY`（0.5）。
- 不需要額外跨行程持久化狀態——「記憶」直接是每個節點自己的 checkpoint 檔案本身（$A_{i,\text{own}}$
  每輪讀回、插值後存回，天然跨輪持久化），沿用附錄 B ERA-Fed 已驗證過的這個實作簡化。

### 11.6 風險與監控

- 雙錨點插值的 $\gamma_{\text{role}}$ 是固定常數（不像 $\beta_i$ 是逐輪動態量測），如果某個
  branch 內部其實跟其他 branch 一樣同質（Scenario T 對稱場景下應該如此），固定的 0.6/0.2 混合
  比例不會造成傷害（branch 平均本來就趨近全域平均），但若某個 branch 剛好因為抽樣噪音而暫時
  偏離，$\gamma_{\text{access}}=0.6$ 可能讓 branch 內 access 節點暫時學到有偏差的東西——訓練時
  應監控 branch 平均與全域平均的差異是否隨時間穩定（不應該是持續發散）。
- JFI 只監控不優化（使用者明確吞吐量優先），但角色分軌可能讓 relay 節點（拉力較強、個人化
  保留較少）表現落後 access 節點更多，需要留意逐節點吞吐量分布，不能只看整體平均。

---

## 附錄 A：CAPA-Fed（Critic-Aggregated, Personalized-Actor Federation）—— **已放棄，原 Stage 3 設計**

> 已完整訓練＋量測：T 4.92 Mbps（打平 PF 4.93）、TH 5.25/5.32 Mbps（仍輸 PF 5.47）。2026-09-29
> 決定放棄，改用本文件 §10 的新設計（見文件頂端狀態說明）。以下內容保留作論文方法論的失敗案例
> 記錄與離線診斷過程（12 節點權重逐位元相同、整份覆寫問題）——**這個診斷是 §11 HiRA-Fed「廣播
> 改用彈性拉扯個人化、不整份覆寫」這個設計決定的直接依據，不是無關的舊資料**。

### 10.1 為什麼放棄 AW-FedAvg 這整個方向（不只是參數問題）

v1/v2 的診斷已經確立兩個關於這個平台的事實：
1. **這次流量場景下 12 個節點的異質性偏低**——v1 checkpoint 的節點間權重差異檢查顯示全部
   12 節點在 FedAvg 下本來就收斂到幾乎相同的權重（差異 norm 0.06~0.09，相對總 norm ~13
   極小）。在這種「本來就高度一致」的前提下，用單輪 advantage 去區分「誰的策略比較好」
   這件事本身訊噪比就低，很可能是在加雜訊而非訊號——這是 v2 吞吐量只小幅超過 PF、仍不夠
   突出的根本限制，不是單純把 EMA/溫度/學習率再調一次可以解的。
2. **單一全域純量動量係數（不分參數）會造成過衝**，即使加上 `AW_SERVER_LR` damping 後 180
   輪合成測試仍看得到暖機震盪，只是最終會收斂——這說明「用同一個係數縮放全部參數」這個
   機制本身粗糙，不是最適合這個平台的伺服器端穩定化手段。

因此不再嘗試調整或替換 AW-FedAvg 的權重公式（那只是換一種方式算同一件事：全部節點共用
**同一份**全域廣播模型），改成**改變聚合的結構**：不同網路子模組（Actor／Critic）用不同的
聚合策略，且**不再對全部節點廣播同一份 Actor**。

### 10.2 尚未解決的真正病灶：relay 節點資料稀疏問題

舊版 Stage 3 設計文件已診斷過但沒解決（文件已刪除，過程見 `HISTORY.md`）：Node1~3（relay）壅塞樣本趨近 0，導致這些節點的本地
Critic／Actor 訓練訊號先天就稀薄；Stage 3 想用 `role_ratio_i`（**結構角色**：relay vs access）
決定混合比例，但 ESS≈1 讓 relay 側原型幾乎完全退回 Stage 2 的全域平均，等於沒有真正做到「relay
節點該多依賴全域知識」這件事——因為 `role_ratio_i` 是靜態的拓樸屬性，不是「這個節點這輪實際
訓練資料夠不夠」的直接量測。CAPA-Fed 直接用**每輪實際的訓練樣本數 `n_i`**（已經在傳的
`num-examples`）取代靜態角色比例，是對同一個問題更貼近資料本身的解法。

### 10.3 核心設計：Actor／Critic 分軌處理

**Critic（價值函數）—— 全域池化，同一份廣播給全部 12 節點**：
價值估計「這個狀態底下能拿到多少 reward」這件事，在結構相似的壅塞動態下應該可以跨節點共用
知識——尤其能幫助 relay 節點（自己壅塞樣本太少，Critic 訓練本來就不足）借用其他節點更充分的
訓練訊號，得到更準的價值估計。做法沿用標準 FedAvg（樣本數加權平均），並重用 v2 已驗證穩定的
伺服器端動量+學習率機制防止過衝（因為 Critic 是全部節點共用同一份，過衝風險與 AW-FedAvg v1
完全相同，直接套用已驗證的解法）：

$$\bar\theta^{critic} = \frac{\sum_i n_i \cdot \theta^{critic}_i}{\sum_i n_i}\quad\text{（}\texttt{\_weighted\_average\_flat()}\text{，重用既有函式）}$$

$$v^{critic}_t = \mu_c \cdot v^{critic}_{t-1} + (1-\mu_c)\cdot(\bar\theta^{critic}_t - \theta^{critic}_{t-1})\qquad
\theta^{critic}_t = \theta^{critic}_{t-1} + \eta_c \cdot v^{critic}_t$$

`μ_c`（`CAPA_CRITIC_MOMENTUM`）、`η_c`（`CAPA_CRITIC_LR`）預設值直接沿用 v2 已通過 180 輪
延長合成測試驗證過的 0.7／0.3，不重新調參（同一個穩定化問題、同一個已驗證的解法）。

**Actor（PRB 分配策略）—— 不廣播同一份，改成逐節點信心加權個人化**：
不做「全部節點共用一份全域 Actor」這件事——本地資料充足、策略已經對自己的流量型態調適過的
節點，不應該被跟資料稀疏節點簡單平均、稀釋掉自己已經學到的東西；本地資料稀疏的節點（尤其
relay），則應該更依賴其他節點的集體經驗。用每節點自己這輪的訓練樣本數 `n_i` 當信心訊號，
決定「這個節點的 Actor 要多信任自己 vs 多信任全體平均」：

$$\bar\theta^{actor} = \frac{\sum_i n_i \cdot \theta^{actor}_i}{\sum_i n_i}$$

$$\beta_i = \text{clip}\left(1 - \frac{n_i}{\max_j n_j + \epsilon},\ \beta_{\min},\ \beta_{\max}\right)$$

$$\theta^{actor}_i \leftarrow (1-\beta_i)\cdot\theta^{actor}_{i,\text{own}} + \beta_i\cdot\bar\theta^{actor}$$

`β_min`／`β_max`（預設 0.1／0.9，`CAPA_BETA_MIN`／`CAPA_BETA_MAX`）確保永遠保留一點個人化
（不會完全變成全域平均）也永遠保留一點集體知識注入（不會完全變成純本地、零聯邦效果）。
本地資料量最多的節點（通常是 access 節點）`β_i` 最低、最信任自己；relay 節點（`n_i` 趨近 0）
`β_i` 趨近 `β_max`，大量依賴集體平均——直接對症 §10.2 的 relay 資料稀疏問題，且是逐輪動態
量測（不是靜態角色標籤），一個節點若某輪剛好資料變多，`β_i` 會自動降低。

### 10.4 概念出處（不是憑空發明，是既有方法的重組）

| 機制 | 借鑑對象 | 這裡的改動 |
|---|---|---|
| Critic 全域池化 | Multi-task／meta-RL 的「共享價值函數」思路（結構相似任務間共用 V(s) 估計） | 套用到聯邦場景，用既有 `_weighted_average_flat()` 實作 |
| Actor 本地-全域插值 | APFL（Adaptive Personalized Federated Learning, Deng, Kamani, Mahdavi, 2020）——為每個 client 維護「本地模型＋全域模型」的凸組合 | APFL 原始設計用梯度法學習插值係數（需要 Local 端配合，這裡不允許）；改用**資料量信心**（`n_i` 相對值）直接算出 `β_i`，不需要 Local 端任何改動 |
| 伺服器端動量+學習率（Critic 專用） | FedAvgM（借鑑自 AW-FedAvg v2 已驗證的修正） | 直接重用 v2 的參數與實作，只把作用範圍從「全部參數」縮小到「只有 critic」 |
| relay 資料稀疏問題的解法 | Stage 3 的 `role_ratio_i` 靜態角色分群（已知只對 Node4 有效） | 用逐輪實際訓練樣本數 `n_i` 取代靜態角色標籤，是對同一問題更貼近資料本身的診斷式解法 |

### 10.5 與 Local 端、既有工具的相容性

- **Local xApp／Local rApp 完全不變**——`client_app.py::train()` 已經在傳 `num-examples`（Stage 2
  起就有的既有欄位），CAPA-Fed 不需要任何新欄位（`mean_adv` 這個 v1/v2 才加的欄位不再使用，
  但留著不動也無害，不強制回退）。
- **完全重用既有函式**：`_split_flat_state_dict()`／`_flatten_state_dicts()`／
  `_weighted_average_flat()`／`_apply_weights_to_node()` 全部直接沿用，不需要新工具。
  「逐節點個人化廣播」這個介面 Stage 3 已經驗證過可行（`role_ratio_i` 混合廣播），CAPA-Fed
  只是換一個決定混合比例的訊號來源。
- **新增環境變數**：`CAPA_CRITIC_MOMENTUM`（預設 0.7）、`CAPA_CRITIC_LR`（預設 0.3）、
  `CAPA_BETA_MIN`（預設 0.1）、`CAPA_BETA_MAX`（預設 0.9）。
- **狀態持久化**：Critic 動量緩衝需要跨 `flwr run` 行程持久化（`num-server-rounds=1` 的既有
  限制），沿用 v2 `aw_fedavg_state.pt` 的磁碟序列化模式，改存到 `capa_fed_state.pt`（只需要存
  critic 的動量緩衝，不需要 advantage EMA）。

### 10.6 退化條件（驗證用）

- `CAPA_BETA_MIN=CAPA_BETA_MAX=1`（Actor 永遠等於全域平均）＋`CAPA_CRITIC_MOMENTUM=0`／
  `CAPA_CRITIC_LR=1`（Critic 無動量直接套用平均）→ 精確退化成 Stage 2 的 `IABFedAvg`（Actor
  和 Critic 都變成普通 FedAvg，且個人化係數對全部節點相同）。
- `CAPA_BETA_MIN=CAPA_BETA_MAX=0` → Actor 完全不聯邦、純本地訓練（拿掉 Actor 端聯邦效果的
  對照組，用來驗證「Actor 個人化」這個機制本身有沒有貢獻）。

### 10.7 風險與監控

- **Critic 池化若跨節點 reward 尺度不一致**（不同節點目標流量不同，reward 絕對值量級可能有
  差異）可能造成 Critic 估計偏誤——`reward_calculator.py` 目前的正規化（`MAX_BSR` 等常數）
  是全節點共用同一組常數，理論上 reward 尺度已經一致，訓練時仍應該監控各節點 `critic_loss`
  是否穩定下降，若某節點的 critic_loss 明顯發散，代表跨節點池化在該節點上出了問題。
- **`β_i` 用 `max_j n_j` 當分母**，若某一輪剛好有節點樣本數極端偏高（例如某節點單輪壅塞爆量），
  會讓其餘節點的 `β_i` 集體被推高（過度依賴全域平均）——可考慮改用當輪 `n_i` 的中位數而非
  最大值當正規化基準，實作時先用 `max_j n_j` 跑一輪，訓練 log 觀察 `β_i` 分布是否合理
  （不應長期集中在 `β_min`／`β_max` 兩端），必要時再切換正規化基準。
- JFI 仍只監控不優化（使用者明確吞吐量優先）；但 Actor 個人化理論上可能讓表現差的節點更難
  被「拉齊」，需要留意壅塞相位滿足率 JFI 是否明顯惡化。

### 10.8 現況（2026-09-29）

已實作（`server_app.py::IABCapaFedAvg`，`FL_MODE=capa`；`client_app.py` 無新改動）、已通過
合成測試（多輪聚合、冷啟動邊界、退化條件：`CAPA_BETA_MIN=CAPA_BETA_MAX=1`＋
`CAPA_CRITIC_MOMENTUM=0`／`CAPA_CRITIC_LR=1` 精確退化成 Stage 2，差異 norm=0.000016，浮點
精度範圍內）。**已完成從頭訓練（2h37min）＋TCP 量測**：4.92 Mbps，與 PF 打平、未超越
Stage2，RTT 為現有方法中最佳；完整結果見 `experiment_results/stage3_capaFed.md`。舊版
`IABClusterFedAvg` 已於 2026-09-29 整段移除，不再保留。**待辦：Scenario TH 暖啟動續訓**
（見 CLAUDE.md 第 3／8 節 T/TH 雙軌框架）。

---

## 附錄 B：ERA-Fed（Elastic Role-Aware Federation）—— **已放棄，原 Stage 4 設計**

> 已實作＋合成測試通過，未真實訓練（2026-09-29 決定全面重新設計時中止，改用本文件 §11 的
> HiRA-Fed）。pFedMe/EASGD 的彈性拉扯數學形式與「Critic 拉力應比 Actor 強」的角色分軌構想被
> §11 HiRA-Fed 直接沿用，不是整個丟棄——差別在於 HiRA-Fed 額外疊加了 §10 的階層式拓樸分組，
> ERA-Fed 當時的聚合仍是扁平（單層）FedAvg。以下內容保留作論文方法論記錄。

### 11.1 離線診斷：確認問題與排除範圍

用 v2 FINAL checkpoint（12 節點）+ MongoDB 真實經驗做的離線驗證（`/tmp` 腳本，非正式測試檔，
過程見對話紀錄）確認：

1. **12 節點 Actor 權重逐位元相同**（node1/5/9 兩兩比對，`net.0/2/4.weight/bias` 全部
   `max|diff|=0.00000000`），`train_steps` 不同（3510/3410/2900）證實不是同一份檔案的複製
   錯誤，是三個節點各自獨立收到廣播後被覆寫成同一份值。
2. 在此前提下，Ensemble Distillation（輸出空間平均 12 個節點的輸出，跟直接做權重平均的模型
   輸出比較）與 PBT-copy（leader 節點的策略套用到 laggard 節點自己的 state 上）兩個方向的
   離線測試差異全部是 0.0000——因為兩者都是「觀察 12 份現有輸入、決定怎麼組合／選擇」，若
   12 份輸入本身沒有差異，這類方法在結構上就沒有發揮空間，**不是這兩個方法本身不好，是
   目前的廣播機制讓它們沒有素材可用**。

**根因**：`_apply_weights_to_node()`（Stage 2/3/4 v1/v2 共用的廣播寫入函式）每輪都用
`agent.actor.load_state_dict(actor_sd)` 整份覆寫，`actor_sd`/`critic_sd` 是這一輪算出的
（唯一或個人化的）聚合結果——不管聚合公式多精巧，寫入的動作本身是「取代」而不是「混合進
既有的本地累積狀態」。`FL_ROUND_INTERVAL_S=60` 與 Local rApp 背景訓練週期（60 秒一輪、
`TRAIN_FETCH_LIMIT` 內做約 10 次梯度更新、`LR_ACTOR=3e-4`）幾乎同步，兩次廣播之間能累積的
本地分化非常有限，下一輪廣播一到就整個歸零重來。

### 11.2 設計：Elastic Averaging（部分拉扯）取代整份覆寫

**主要對標 pFedMe**（T. Dinh, N. Tran, T. D. Nguyen, *"Personalized Federated Learning with
Moreau Envelopes"*, *NeurIPS 2020*）：用正則化項讓每個節點的個人化模型永遠朝全域共識模型
被拉、但不會被整份覆寫成全域模型——pFedMe 原論文是監督式學習場景，但已有延伸到聯邦 actor-critic
強化學習的應用（同時對 actor 與 critic 做個人化，回報中每個 agent 都保留自己的環境特化，同時
透過全域模型維持跨節點的知識共享），跟 ERA-Fed 的場景（12 個節點各自面對不同的本地流量/通道
分佈，但共享同一套 DRL 架構）高度吻合。彈性拉扯的具體數學形式（每輪朝中心移動一個比例 ρ，
而非整份覆寫）沿用 **Elastic Averaging SGD**（EASGD，Zhang, Choromanska, LeCun, *NeurIPS
2015*）——這是最早提出「分散式 worker 不用整份同步、改用比例拉扯」這個機制形式的論文，雖然
原本設計給一般分散式 SGD（非聯邦學習、非個人化語境），但拉扯公式本身直接可用：分散式 worker
不是每輪都被强制設成與中心一致，而是每輪**朝中心的方向被拉一小步**（彈性係數 ρ），worker
自己的參數在拉扯之間可以持續累積、保留自己的訓練歷史，不會被瞬間抹平。套用到這裡：

$$\theta_i \leftarrow (1-\rho_i)\cdot\theta_{i,\text{own}} + \rho_i\cdot\bar\theta_t \qquad(\text{取代原本的 }\theta_i \leftarrow \bar\theta_t\text{ 整份覆寫})$$

其中 `θ_{i,own}` 是節點 i **目前已經在 checkpoint 裡的權重**（`_apply_weights_to_node()`
一開始就會 `agent.load()` 讀到）——注意這個 `θ_{i,own}` 本身就是「上一輪彈性拉扯後、又經過
一輪本地訓練」的結果，所以本地分化可以跨很多輪持續累積、以 `1/ρ` 量級的「記憶半衰期」逐漸
被拉向群體共識，而不是每輪歸零重來。`ρ` 越小，個人化保留得越久；`ρ=1` 精確退化成現在的整份
覆寫（見 §11.6 退化條件）。

### 11.3 結合 Actor/Critic 分軌（延續 §10 CAPA-Fed 的構想，但修正 Critic 路徑的殘留問題）

**與 §10 CAPA-Fed 的關鍵差異**：CAPA-Fed 的 Critic 路徑用「標準 FedAvg 加權平均＋動量+學習率
damping」——這只是把「覆寫成群體共識」這件事在**時間上**拖慢（`η_c<1` 讓每輪只走一小步），
但只要訓練輪數夠多，動量最終還是會讓 Critic **完全收斂**到群體平均，跟 Actor 用的「永遠保留
`(1-ρ_i)` 比例本地身份」在**性質上**不同——CAPA-Fed 只解決了 Actor 的整份覆寫問題，Critic
路徑仍然殘留同一個根本問題，只是被動量掩蓋、沒那麼明顯。

ERA-Fed 把 Critic 也改成跟 Actor 同一種**彈性拉扯**（不是動量），差別只在於拉力係數用一個
`>1` 的倍率放大，讓 Critic 確實比 Actor 更快朝群體共識移動（呼應「Critic 該較快同步知識、
Actor 該保留更多本地特化」的設計動機），但**兩者都永遠保留一部分本地身份，不會有任何一個
子網路最終被完全覆寫**：

$$\rho^{critic}_i = \text{clip}(\rho_i \cdot \kappa,\ 0,\ 1)\qquad \kappa > 1\ (\text{預設 }\texttt{ERA\_CRITIC\_KAPPA}=2.5)$$

即 Critic 用「基礎彈性係數 `ρ_i` 乘上一個 `>1` 的放大倍率」，Actor 直接用 `ρ_i`——Critic 被
拉向中心的力道永遠比 Actor 強（或相等，當 κ 貼近 1 或 `ρ_i·κ` 被 clip 到 1 時，Critic 退化
成當輪完全覆寫，但下一輪 `θ_{i,own}` 又會重新累積本地訓練，不是「永久」覆寫）。

### 11.4 結合信心加權個人化（重用 §10 CAPA-Fed 的構想）

基礎彈性係數 `ρ_i` 本身依節點這輪實際訓練樣本數 `n_i` 動態決定（沿用既有的 `num-examples`
欄位，不需要 Local 端任何改動）：

$$\rho_i = \text{clip}\left(\rho_{\min} + (\rho_{\max}-\rho_{\min})\cdot\left(1-\frac{n_i}{\max_j n_j+\epsilon}\right),\ \rho_{\min},\ \rho_{\max}\right)$$

本地資料充足的節點（通常是 access）`ρ_i` 偏低、被拉向中心的力道小、個人化保留得多；本地資料
稀疏的節點（尤其 relay，見上方 §10.2 已診斷但未解決的問題）`ρ_i` 偏高、更依賴
群體共識，同時因為是「拉扯」而非「覆寫」，即使 `ρ_i` 較高也不會讓 relay 節點的僅存本地訊號
被瞬間歸零。

### 11.5 完整聚合公式

$$\bar\theta^{actor}_t = \frac{\sum_i n_i\theta^{actor}_i}{\sum_i n_i}\qquad
\bar\theta^{critic}_t = \frac{\sum_i n_i\theta^{critic}_i}{\sum_i n_i}\qquad(\texttt{\_weighted\_average\_flat()}\text{，重用既有函式}）$$

對每個節點 i 分別計算 $\rho_i$（§11.4）與 $\rho_i^{critic}=\text{clip}(\rho_i\kappa,0,1)$（§11.3），
寫回節點 i 自己的 checkpoint（`_apply_weights_to_node(i, \cdot, \cdot)`，重用既有函式，介面
不變）：

$$\theta^{actor}_i \leftarrow (1-\rho_i)\cdot\theta^{actor}_{i,\text{own}} + \rho_i\cdot\bar\theta^{actor}_t$$
$$\theta^{critic}_i \leftarrow (1-\rho_i^{critic})\cdot\theta^{critic}_{i,\text{own}} + \rho_i^{critic}\cdot\bar\theta^{critic}_t$$

**不需要額外的跨行程持久化狀態**——這是相對 AW-FedAvg 的一個實作簡化：AW-FedAvg 需要額外
的 `aw_fedavg_state.pt` 存動量緩衝／advantage EMA，因為它的動量作用在「聚合後的單一全域模型」
上；ERA-Fed 的「記憶」直接就是每個節點自己的 checkpoint 檔案本身（`θ_{i,own}` 每輪讀回、
拉扯後存回，天然跨輪持久化），不需要另外的狀態檔。

### 11.6 退化條件（驗證用）

- `ρ_min=ρ_max=1`，`κ=1` → 每個節點的 `ρ_i≡1`，Actor/Critic 都變成 `θ_i←θ̄_t`（整份覆寫）
  → 精確退化成 Stage 2 的 `IABFedAvg`。
- `ρ_min=ρ_max=0` → 完全不聯邦、純本地訓練（驗證「聯邦本身有沒有貢獻」的對照組）。
- `κ=1`（Critic 與 Actor 用相同彈性係數，無分軌）→ 驗證「Actor/Critic 分軌」這個機制單獨的
  貢獻。

### 11.7 概念出處

| 機制 | 借鑑對象 | 這裡的改動／新意 |
|---|---|---|
| 同時個人化 Actor 與 Critic、永遠保留本地身份 | **pFedMe**（T. Dinh, N. Tran, T. D. Nguyen, *"Personalized Federated Learning with Moreau Envelopes"*, NeurIPS 2020）——已有延伸到聯邦 actor-critic 強化學習的應用先例 | 原論文用 Moreau envelope 正則化達成個人化－全域的權衡；這裡改用更輕量的顯式線性插值（彈性拉扯）達成類似效果，避免額外的 proximal 最佳化步驟，適合 Local rApp 的即時性限制 |
| 彈性拉扯的具體數學形式（比例拉扯取代整份覆寫） | Elastic Averaging SGD（Zhang, Choromanska, LeCun, NeurIPS 2015） | 原論文是同步分散式 SGD 訓練單一任務、非聯邦學習語境；這裡借用其「比例拉扯」的公式形式，套用到聯邦 DRL 場景，且拉扯係數依節點/子網路動態調整（原論文用單一全域常數） |
| Critic 快、Actor 慢的分軌拉扯 | 承接 §10 CAPA-Fed 的 Actor/Critic 分軌構想 | 從「個人化混合」的框架搬進「彈性拉扯」的框架，語意更貼近解決 §11.1 診斷出的根本問題 |
| 信心加權彈性係數 `ρ_i` | 承接 §10 CAPA-Fed 的信心加權構想；概念上類似 APFL（Deng et al., 2020）的本地-全域插值 | 插值係數改用資料量信心（`n_i` 相對值）動態決定，不需要 Local 端配合 |

**2026-09-29 文獻脈絡更新**：原設計只引用 EASGD（2015，通用分散式 SGD，非聯邦學習專屬），
使用者提出質疑後覆查文獻，確認 pFedMe（2020）更貼切現在的問題定義（同時個人化 actor 與
critic、且已有聯邦 RL 應用案例），改以 pFedMe 為主要對標；EASGD 保留作為彈性拉扯數學形式
的原始出處，不是誤引用。過程見 `HISTORY.md`。

### 11.8 現況（2026-09-29）

已實作（`server_app.py::IABElasticFedAvg`，`FL_MODE=elastic`；`client_app.py` 無新改動，
`num-examples` 是既有欄位）、已通過合成測試（多輪聚合、冷啟動邊界、§11.6 退化條件：
`ERA_RHO_MIN=ERA_RHO_MAX=1`＋`ERA_CRITIC_KAPPA=1` 精確退化成 Stage 2，差異 norm=0.000016，
浮點精度範圍內）。Stage 3（CAPA-Fed）T 場景結果 4.92 Mbps（與 PF 打平）、TH 場景結果
5.25 Mbps（略遜 PF），完整數據見 `experiment_results/stage3_capaFed.md`；另外 2026-09-29
診斷發現 Dirichlet 探索退火時間常數（`DRL_DIRICHLET_K_ANNEAL_TAU`）原本跟平台可負擔的訓練
時長嚴重不匹配（每次訓練全程停留在高探索雜訊階段），已修正（5000→800）並重訓驗證中，見
`HISTORY.md`——**ERA-Fed 訓練前應沿用這個修正過的排程，不要用舊的 tau=5000**。**待辦：真實
從頭訓練＋T/TH 雙軌量測**（見 CLAUDE.md 第 3／8 節 T/TH 雙軌框架）。

> **⚠ 設計限制（2026-10-02，HS／HSH 改為 PB 結構後；Stage 2～4 FL 設計必須考慮）**
> 壅塞相位的改進空間全部集中在 4 個 relay 節點（relay 邊緣 UE 讓 slot 給 backhaul）；8 個 access 節點在壅塞時最好的動作永遠是「不介入」（舊版混合 access 結構平台實測無增益，已移除）。後果：
> 1. Local DRL 真正學到決策的只有 relay 節點，access 節點只學會「不要遮」。
> 2. Stage 2 FedAvg 以「決策點樣本數」（`count_contended()`＝starved 且有候選）加權，access 節點權重接近 0，聚合實際上幾乎是 4 個 relay 互相平均；access 節點只被動接收全域模型。
> 3. Stage 3（分 branch 聚合＋Hedge）與 Stage 4（角色個人化）的設計前提要重新檢查：branch 內平均時 access 節點幾乎沒有樣本、relay 主導；「relay／access 角色分軌」在 access 側沒有可學內容，個人化的收益只會出現在 relay 側。
> 4. 使用者 2026-10-02 決定先照此場景進行（唯一平台實測贏 PF 的結構）；若之後要讓 access 層也有可學決策，需先在平台上找到 access 層實測有增益的結構（P 原配置 4 節點同時壅塞僅 +4%）。

> **FL 各 Stage 目的的建議方向（2026-10-02，待 Local DRL v2.1 單獨訓練結果驗證後定案）**
> - FL 的核心價值改為「4 個 relay 共享稀少的熱點經驗」：HS 每個 relay 只有約 25% 的壅塞相位是熱點；HSH 熱點分布約 [26,54,9,12]%，branch 3 的 relay 幾乎遇不到熱點、單獨學不起來。
> - Stage 2（FedAvg，依決策點樣本數加權）：解決單一 relay 經驗不足 → 預期比單獨訓練收斂快、最終不輸。
> - Stage 3（branch 平均＋Hedge＋分歧節點保留）：HSH 中各 relay 熱點頻率差異大、更新品質不同，Hedge 調整權重 → 預期 HSH 贏 Stage 2、HS 持平。
> - Stage 4（角色分開聚合＋個人化）：避免 FedAvg 把以 relay 為主的全域模型套到 access 節點（在 G 等一般場景可能誤介入）；access 只學「不介入」→ 預期兩軌都贏、G 不傷害。
> - 待驗證假設：(1) relay 單獨訓練時學習速度確實受經驗量限制（v2.1 單獨訓練學習曲線、HSH 各 relay 比較）；(2) FedAvg 廣播 relay 主導模型給 access 在 G 上是否造成損失（Stage 2 的 G 量測）。
