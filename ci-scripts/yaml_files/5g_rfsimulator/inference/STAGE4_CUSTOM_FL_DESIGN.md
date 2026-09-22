# Stage 4 自訂聚合演算法設計書：Backhaul-Aware Fair Aggregation with Proximal Regularization（BAFA-Prox）

> 狀態：**設計草案，尚未實作**。取代舊版「RDA-Agg」草案（單純 reward-deficit 加權）——
> 這版整合三個機制：(1) q-FFL 式的 reward-deficit 公平性加權、(2) 本系統特有的
> backhaul 緊繃度加權、(3) FedProx 式的本地訓練近端正則化。前二者疊加在 Global
> 聚合權重上，第三者作用在 Local 端訓練 loss——**三者互相獨立、可分開消融
> （ablation）驗證**，不是綁死在一起的單一黑盒機制。相關文件：
> `DRL_METHODOLOGY_PLAN.md`（Local 端方法論回顧）、
> `experiment_results/{PF,avgFL,clusterFL}.md`（Stage 1~3 實測數據，含 2026-09-21
> 三方 Scenario T 乾淨比較）、`HISTORY.md`（2026-09-20~21 條目，iperf3 bug／收斂
> 診斷／逐 UE 相對改善分析的完整過程）。

---

## 1. 背景：這次設計要解決的三個已實測驗證的弱點

### 1.1 弱點一：`REWARD_MODE=throughput_only` 沒有公平性項，FedAvg／Cluster FedAvg 聚合權重也不補償

2026-09-21 的三方 Scenario T 乾淨比較（`PF.md`／`avgFL.md`／`clusterFL.md`）＋逐 UE
相對改善分析（`HISTORY.md` 對應條目）發現：avg FL／cluster FL 對 PF 的「總平均」
吞吐量/JFI 看起來略有改善，但**逐 UE 拆開看，兩者的中位數相對變化都是負的**
（avg FL −17.7%、cluster FL −25.9%）——代表對「典型 UE」而言兩種 FL 方法其實是
變差的，只有少數幾個 PF 原本冷落的 UE（例如同一條 backhaul 分支裡的手足 UE）獲得
巨幅改善，把總平均拉正。這是**零和式重分配**，不是真正的整體效率提升，且是
`throughput_only` reward 沒有任何公平性項、FedAvg／Cluster FedAvg 的聚合權重
（分別是 `num_examples`、`num_examples × role_ratio`）也完全不看「這個節點目前
相對表現如何」的必然結果。

### 1.2 弱點二：既有的角色分群（relay/access）沒有捕捉到真正的效能落差來源

三份量測表格比對後發現一個持續模式：**UE5~8（Node2→Node7,8，全部跑在 PC1，跟
Donor CU/DU/FlexRIC 同一台主機）在三次量測裡全部都是吞吐量最高的一群，UE1~4
（PC2）跟 UE9~16（PC3，且是雙重跨主機）全部是最低的一群**——這個分組跟
`ROLE_RATIO`（relay vs access）完全無關，反而高度對應**實體主機位置**（同主機
省去一段真實網路傳輸開銷，跨主機、尤其雙重跨主機的節點多一段實體延遲）。Stage 3
的 `role_ratio_i` 分群假設「relay/access 結構角色」是效能差異的主因，這次資料
顯示這個假設**方向錯了**——真正該加權的訊號不是「這個節點的下游是誰」，而是
「這個節點的 backhaul 實際有多緊繃」，剛好呼應 CLAUDE.md 原本就提示的方向：
「要不要把全網 JFI 或 backhaul 緊繃程度也當作聚合權重的輸入」。

### 1.3 弱點三：兩個 checkpoint 收斂診斷顯示訓練還在早期、不穩定階段

`iab/check_convergence_mongo.py` 對兩個 checkpoint 的診斷結果：avg FL 12 個節點裡
只有 1 個「疑似收斂」，cluster FL **12 個節點全部「仍在變動」**，且多數節點的
reward 變化量佔平均值 100%~500% 以上，代表現有的聚合機制（無論 FedAvg 還是
Cluster FedAvg）在節點各自訓練軌跡差異很大、還沒收斂的情況下就把它們平均在一起
——這正是 FedProx 這篇文獻要解決的「異質網路下的聯邦最佳化」問題：本地更新在兩次
聚合之間可能已經往很不同的方向跑掉，直接平均容易產生「四不像」的結果。

---

## 2. 設計目標與邊界

**必須做到**：
1. 把「全網相對表現落差」（q-FFL 精神）**與**「這個節點的 backhaul 緊繃程度」
   （本系統特有訊號）都變成真正影響聚合權重的輸入，兩者是**獨立、可疊乘**的因子，
   不是綁在一起的單一公式，方便做消融實驗歸因。
2. 額外疊加 FedProx 式的本地訓練近端正則化，抑制兩次聚合之間的本地漂移。
3. 保留 Stage 3 relay/access 兩原型混合廣播的結構——Node4 混合角色節點的論證
   依然成立，`role_ratio_i` 繼續決定「這個節點該混合多少比例的兩個原型」，
   本設計不是推翻 Stage 3，是在其上疊加兩層新的加權/正則化。
4. `FL_MODE=avg|cluster|custom` 三種模式並存，環境變數切換，`run_stage2_fl.sh`
   不需要改。
5. 三個機制各自有一個可以設成 0／關閉的超參數，退化情況下精確等於 Stage 3
   （`IABClusterFedAvg`）——符合「每次只換一個變數才能歸因」的方法論要求，這裡
   放寬成「每個變數各自可獨立開關」，量測時可以逐一消融。

**誠實揭露：這次設計比舊版 RDA-Agg 稍微超出「只動 Global 聚合邏輯」的邊界**——
CLAUDE.md 對 Stage 4 的原始定調是「只再換 `server_app.py` 的聚合邏輯，
`client_app.py`／Global xApp／Local xApp+Local rApp 皆不變」。本設計裡的
backhaul 緊繃度加權跟 FedProx 近端項，兩者都**需要 Local 層最小幅度的改動**
（見 2.1），這是刻意的取捨，理由如下：

**2.1 為什麼不能維持零 Local 層改動**

- **backhaul 緊繃度**：這個數值（MT 的 backhaul 忙碌程度）目前**只存在於 C 語言的
  `gNB_scheduler_dlsch.c` 內部**，用來動態縮小 DU 可用 PRB 池（CLAUDE.md 第 3 節
  「Backhaul-aware 動態 PRB 預算」機制），從未被送進 Python 側的 state/MongoDB
  schema。Global rApp（`server_app.py`）完全沒有管道可以在不新增任何 Local 層
  訊號的情況下取得這個值——這是跟弱點一（reward 落差，MongoDB 裡已經有
  `reward` 欄位可查）本質不同的情況：reward-deficit 加權可以做到零 Local 層
  改動，backhaul 緊繃度加權**做不到**，除非接受用某個間接代理（例如
  `dl_buffer_info` 持續累積但 `dl_aggr_tbs` 沒有對應成長，間接暗示「有需求但
  排不到」）——但代理訊號會混雜「backhaul 緊繃」跟「單純 policy 分配不佳」
  兩種完全不同的成因，失去這個設計原本想要的精確性。**本設計選擇老實面對這個
  邊界問題，走最小幅度的 Local xApp 改動，而不是用不精確的代理訊號假裝維持
  零改動**。
- **FedProx 近端項**：本質上是本地訓練 loss 的一部分，物理上不可能在不碰
  `drl_agent.py` 的情況下實作——這點文獻上也是如此（FedProx 原論文的改動就是在
  client 端的本地目標函數，不是 server 端）。

**兩者的改動幅度都刻意壓到最小**（見第 5 節），backhaul 緊繃度只新增一個
telnet 查詢 + 一個 JSON 欄位傳遞，不改變 Local xApp 既有的控制迴圈邏輯；
FedProx 只在既有的 `train_on_batch_mlp()` 尾端多加一項 loss，不改變其他訓練
邏輯。`REWARD_MODE` 的語意、Local xApp 的 PRB 分配動作本身完全不變。

**刻意不做**（沿用舊版 RDA-Agg 的判斷，理由同前版）：
- 不加 server-side momentum／FedAvgM。
- 不修改 `STATE_DIM`／不新增 state feature 給 Local DRL 的 Actor 輸入——backhaul
  緊繃度這次只用來影響「聚合權重」，不餵進 Actor 的決策輸入，範疇跟「Stage 2
  評估過要不要把它加進 state」是兩件事，這裡刻意保持分離。
- 不引入新的 Python 套件。

---

## 3. 機制一：q-FFL 式 Reward-Deficit 加權（延續舊版 RDA-Agg 設計）

> 借鑑 **q-FFL**（Li, Sanjabi, Beirami, Smith, "Fair Resource Allocation in
> Federated Learning", ICLR 2020）——原論文用「每個 client 的 loss 的 $q$ 次方」
> 當聚合權重，loss 越高（表現越差）的 client 影響力越大。本設計把 loss 換成
> 「這個節點落後全網平均 reward 的程度」，因為 DRL 場景沒有天然可比的 loss，
> reward 才是跨節點可比的量。

### 3.1 訊號來源

沿用 `server_app.py` 既有的 `compute_global_jfi()` 查詢模式，新增一個保留
逐節點平均值（而非壓成單一 JFI 純量）的函式：

```python
def compute_node_mean_rewards(
    db: pymongo.database.Database, lookback: int = JFI_LOOKBACK
) -> dict[int, float]:
    """跟 compute_global_jfi() 用同一組查詢，但保留逐節點平均值。"""
    means: dict[int, float] = {}
    for node_id in range(1, NUM_NODES + 1):
        col = db[f"node{node_id}_experiences"]
        try:
            docs = list(
                col.find({"reward": {"$exists": True}}, projection={"reward": 1, "_id": 0})
                .sort("timestamp", pymongo.DESCENDING)
                .limit(lookback)
            )
            if docs:
                means[node_id] = float(np.mean([d["reward"] for d in docs]))
        except Exception:
            pass
    return means
```

查 MongoDB 歷史平均（`JFI_LOOKBACK=100` 筆）而不用 `client_app.py::train()`
單輪回傳的 `mean_reward`，理由是單輪樣本量小、方差大，歷史平均是更穩定的「這個
節點最近真實水準」代理，且完全不需要碰 `client_app.py`。

### 3.2 公式

$$
\bar{R} = \frac{1}{N}\sum_{i=1}^{N} R_i \qquad
\text{deficit}_i = \mathrm{clip}\!\left(\frac{\bar{R}}{R_i + \varepsilon},\; 1.0,\; Q_{max}\right)
$$

只放大不縮小（下界鎖在 1.0）——落後節點被放大聲量，表現好的節點維持原權重，
不因為表現好而被懲罰。$Q_{max}$（預設 `3.0`）防止單一節點 reward 趨近 0 時
（例如 UE17 的三重負載自我節流極端案例）deficit 發散、主宰整個全域模型。

---

## 4. 機制二：Backhaul 緊繃度加權（本系統特有，非文獻既有方法）

### 4.1 訊號來源與最小幅度的 Local 層改動

`gNB_scheduler_dlsch.c` 的 backhaul-aware PRB 預算機制，每個排程週期已經在內部
算出「這個節點的 DU 這次可用的 PRB 數量」（$<106$，隨 MT 忙碌程度動態縮小）。
定義：

$$
\text{tightness}_{\text{raw}} = 1 - \frac{\text{available\_prb\_du}}{106}
$$

$\text{tightness}_{\text{raw}} \in [0,1]$，越接近 1 代表這個節點的 backhaul
越緊繃、DU 能用的 PRB 池被壓縮得越厲害。

**最小幅度改動**（三個檔案各一行等級的新增，不改變既有邏輯）：

1. `gNB_scheduler_dlsch.c`：這個比例值已經在內部算出來（用於裁切 PRB 池），
   額外寫進 MAC layer 既有要送給 E2 Agent 的統計結構（沿用第 7 節「共用底層
   檔案」清單裡本來就會被 E2SM-MAC 讀取的資料結構，不新開一條資料管線）。
2. `xapp_nodeN.c`（12 份都要加）：組裝送給 Local rApp 的 JSON state 時，多帶一個
   欄位 `"backhaul_tightness": <float>`，讀值、加欄位，不改變既有的 Rate Limiter
   或控制迴圈邏輯。
3. `inference_server.py`：收到的 JSON 多一個欄位，寫進 MongoDB 經驗文件時原樣
   存進去（`_build_rl_experience()` 只是多存一個 key，不影響既有欄位）。

Global rApp 端新增對應的查詢函式（跟 `compute_node_mean_rewards()` 同一種
模式）：

```python
def compute_node_backhaul_tightness(
    db: pymongo.database.Database, lookback: int = JFI_LOOKBACK
) -> dict[int, float]:
    means: dict[int, float] = {}
    for node_id in range(1, NUM_NODES + 1):
        col = db[f"node{node_id}_experiences"]
        try:
            docs = list(
                col.find({"backhaul_tightness": {"$exists": True}},
                          projection={"backhaul_tightness": 1, "_id": 0})
                .sort("timestamp", pymongo.DESCENDING)
                .limit(lookback)
            )
            if docs:
                means[node_id] = float(np.mean([d["backhaul_tightness"] for d in docs]))
        except Exception:
            pass
    return means
```

**降級路徑**：欄位不存在（新增之前累積的舊經驗、或這次沒有機會改到 C 層）時，
`compute_node_backhaul_tightness()` 對該節點回傳空值，聚合時退回權重 1.0（不
補這個因子），不中斷聚合流程。

### 4.2 公式

$$
B_i = \text{tightness}_i \qquad
\text{bh\_weight}_i = \mathrm{clip}\!\left(1 + \beta \cdot B_i,\; 1.0,\; T_{max}\right)
$$

$\beta$（預設 `1.0`）控制 backhaul 緊繃度貢獻的強度，$T_{max}$（預設 `2.0`）
是安全上限。$B_i=0$（完全不緊繃）時 `bh_weight=1.0`（不加成），$B_i=1$（完全
緊繃、PRB 池被壓到 0）時在 $\beta=1$ 下 `bh_weight=2.0`（權重加倍）。

---

## 5. 機制三：FedProx 式本地訓練近端正則化

> 借鑑 **FedProx**（Li, Sahu, Zaheer, Sanjabi, Talwalkar, Smith, "Federated
> Optimization in Heterogeneous Networks", MLSys 2020）——在每個 client 的本地
> 目標函數上加一個近端項，把本地權重拉回聚合前的全域權重附近，抑制異質網路下
> 「兩次聚合之間本地訓練跑偏太遠」的問題。

### 5.1 訊號來源與錨點快照

`inference_server.py` 既有的熱重載機制（偵測 checkpoint mtime 變化即
`agent.load()`）是天然的掛載點——**每次成功 `load()` 一份聚合後的新 checkpoint，
順手把當下的 actor/critic 權重深拷貝存一份當「這一輪的全域錨點」**：

```python
# inference_server.py，agent.load() 成功後
if agent.load():
    agent.set_global_anchor()   # drl_agent.py 新增方法，深拷貝目前 state_dict
```

```python
# drl_agent.py，DRLAgent 新增方法
def set_global_anchor(self) -> None:
    self._anchor_actor = copy.deepcopy(self.actor.state_dict())
    self._anchor_critic = copy.deepcopy(self.critic.state_dict())
```

兩次聚合之間（沒有新 checkpoint 可 load 時）`_anchor_*` 維持不變，本地訓練持續
以這份錨點為近端正則化的基準，直到下一次聚合帶來新錨點。

### 5.2 公式

$$
\mathcal{L}_{\text{actor}}' = \mathcal{L}_{\text{actor}} + \frac{\mu}{2}\sum_{k}\left\Vert\theta_k^{\text{actor}} - \theta_{k,\text{anchor}}^{\text{actor}}\right\Vert^2
$$

$$
\mathcal{L}_{\text{critic}}' = \mathcal{L}_{\text{critic}} + \frac{\mu}{2}\sum_{k}\left\Vert\theta_k^{\text{critic}} - \theta_{k,\text{anchor}}^{\text{critic}}\right\Vert^2
$$

$\mu$（預設 `0.01`，需要實測校調——這是這次三個超參數裡最需要現場調的一個，
太大會讓本地訓練幾乎學不到新東西，太小等於沒加）。$\mu=0$ 精確退化回現有
`train_on_batch_mlp()` 行為。

### 5.3 實作位置（`drl_agent.py::train_on_batch_mlp()`）

```python
# 緊接在既有的 critic_loss = F.mse_loss(current_values, targets) 之後
if FEDPROX_MU > 0 and self._anchor_critic is not None:
    prox_critic = sum(
        (p - self._anchor_critic[name].to(p.device)).pow(2).sum()
        for name, p in self.critic.named_parameters()
    )
    critic_loss = critic_loss + (FEDPROX_MU / 2) * prox_critic

# 緊接在既有的 actor_loss = actor_loss - entropy_coeff * entropy_t.mean() 之後
if FEDPROX_MU > 0 and self._anchor_actor is not None:
    prox_actor = sum(
        (p - self._anchor_actor[name].to(p.device)).pow(2).sum()
        for name, p in self.actor.named_parameters()
    )
    actor_loss = actor_loss + (FEDPROX_MU / 2) * prox_actor
```

冷啟動（`_anchor_actor is None`，還沒收到過任何聚合結果）時整項跳過，等同
`μ=0`，不影響第一輪聚合前的本地訓練。

---

## 6. 三個機制疊加後的完整聚合公式

`IABClusterFedAvg`（Stage 3）目前的權重：

$$
w_i^{relay} = (1-\text{role}_i)\cdot n_i \qquad w_i^{access} = \text{role}_i \cdot n_i
$$

`IABCustomFedAvg`（Stage 4，本設計）：

$$
w_i^{relay} = (1-\text{role}_i)\cdot n_i \cdot \text{deficit}_i \cdot \text{bh\_weight}_i
\qquad
w_i^{access} = \text{role}_i \cdot n_i \cdot \text{deficit}_i \cdot \text{bh\_weight}_i
$$

廣播公式（每節點依自己的 `role_ratio_i` 混合兩原型）**完全不變**，沿用
`_broadcast_cluster_weights()`。FedProx 的近端項**不出現在這個公式裡**——它
作用在 Local 端訓練 loss，跟 Global 端的聚合權重是兩個獨立作用點，數學上不
互相耦合，這也是為什麼可以分開做消融實驗。

**退化正確性**：$\beta=0$（或全部節點 backhaul_tightness 缺值）且全部節點
$R_i$ 相同時，$\text{deficit}_i \equiv \text{bh\_weight}_i \equiv 1.0$，
`IABCustomFedAvg` 的聚合結果與 `IABClusterFedAvg` 完全相同。

### 6.1 三個超參數總表

| 超參數 | 環境變數 | 預設值 | 作用 | =0／=1 時退化行為 |
|---|---|---|---|---|
| $Q_{max}$ | `FL_DEFICIT_MAX` | `3.0` | reward-deficit 加權上限 | 不適用（$Q_{max}\to1$ 才會關閉此機制） |
| $\beta$ | `FL_BACKHAUL_BETA` | `1.0` | backhaul 緊繃度加權強度 | `0` → `bh_weight`恆為 `1.0`，此機制關閉 |
| $\mu$ | `FEDPROX_MU` | `0.01` | 本地訓練近端正則化強度 | `0` → 精確退化回現有 `train_on_batch_mlp()` |

三個超參數各自獨立可關閉，量測時建議的消融組合（見第 8 節）。

---

## 7. 介面與最小程式改動清單

### 7.1 C 語言側（`xapp_nodeN.c` ×12、`gNB_scheduler_dlsch.c`）

- `gNB_scheduler_dlsch.c`：backhaul-aware PRB 預算機制既有的
  `available_prb_du` 計算之後，寫進既有要送給 E2 Agent 的 MAC 統計結構一個
  新欄位（沿用第 7 節「共用底層檔案」清單既有的資料流，不新開 IPC 通道）。
- `xapp_nodeN.c`：組裝 ZMQ JSON payload 時多帶 `"backhaul_tightness"` 一個
  欄位，讀值＋序列化，不改變 Rate Limiter／控制迴圈。

### 7.2 Python 側

- `inference_server.py`：
  - `_build_rl_experience()` 多存一個 `backhaul_tightness` 欄位（缺值時存
    `None`，不報錯）。
  - `agent.load()` 成功後呼叫新增的 `agent.set_global_anchor()`。
- `drl_agent.py`：
  - `DRLAgent.__init__`：新增 `self._anchor_actor = None`、
    `self._anchor_critic = None`。
  - 新增 `set_global_anchor()` 方法（5.1 節）。
  - `train_on_batch_mlp()`：critic_loss／actor_loss 各加一行近端項（5.3 節）。
  - 新增常數 `FEDPROX_MU = float(os.getenv("FEDPROX_MU", "0.01"))`。
- `server_app.py`：
  - 新增 `compute_node_mean_rewards()`（3.1 節，`compute_global_jfi()` 可改
    呼叫這個函式取得 `means.values()` 再算 JFI，減少重複查詢）。
  - 新增 `compute_node_backhaul_tightness()`（4.1 節）。
  - 新增常數：
    ```python
    FL_DEFICIT_MAX: float = float(os.getenv("FL_DEFICIT_MAX", "3.0"))
    FL_BACKHAUL_BETA: float = float(os.getenv("FL_BACKHAUL_BETA", "1.0"))
    FL_BACKHAUL_TMAX: float = float(os.getenv("FL_BACKHAUL_TMAX", "2.0"))
    FL_REWARD_LOOKBACK: int = int(os.getenv("FL_REWARD_LOOKBACK", str(JFI_LOOKBACK)))
    ```
  - `IABClusterFedAvg` 重構出一個不改變既有行為的 hook（沿用舊版 RDA-Agg 的
    設計）：
    ```python
    class IABClusterFedAvg(IABFedAvg):
        def _extra_weights(self, replies) -> dict[int, float]:
            """回傳 {node_id: 倍率}，未列出的節點倍率視為 1.0。Stage 3 預設不調整。"""
            return {}

        def aggregate_train(self, server_round, replies):
            replies = list(replies)
            extra = self._extra_weights(replies)
            ...
            mult = extra.get(node_id, 1.0)
            w = num_examples * mult
            relay_items.append((flat, (1.0 - role) * w))
            access_items.append((flat, role * w))
            ...
    ```
  - 新增 `IABCustomFedAvg`，只覆寫 `_extra_weights()`：
    ```python
    class IABCustomFedAvg(IABClusterFedAvg):
        """Stage 4：Stage 3 的角色加權之上，疊加 reward-deficit × backhaul 緊繃度。"""

        def _extra_weights(self, replies) -> dict[int, float]:
            if self._db is None:
                return {}
            means = compute_node_mean_rewards(self._db, lookback=FL_REWARD_LOOKBACK)
            tight = compute_node_backhaul_tightness(self._db, lookback=FL_REWARD_LOOKBACK)
            if not means:
                return {}
            eps = 1e-6
            global_mean = float(np.mean(list(means.values())))
            out: dict[int, float] = {}
            for nid, r in means.items():
                deficit = float(np.clip(global_mean / (r + eps), 1.0, FL_DEFICIT_MAX))
                bh = tight.get(nid)
                bh_weight = (
                    float(np.clip(1.0 + FL_BACKHAUL_BETA * bh, 1.0, FL_BACKHAUL_TMAX))
                    if bh is not None else 1.0
                )
                out[nid] = deficit * bh_weight
            return out
    ```
  - `main()` 的 dispatch 與廣播分支各補一行：
    ```python
    STRATEGY_BY_MODE = {"avg": IABFedAvg, "cluster": IABClusterFedAvg, "custom": IABCustomFedAvg}
    strategy_cls = STRATEGY_BY_MODE.get(FL_MODE, IABFedAvg)
    ...
    if FL_MODE in ("cluster", "custom"):
        _broadcast_cluster_weights(strategy._last_w_relay, strategy._last_w_access)
    ```

### 7.3 不需要改動的部分

- `docker-compose-iab-server.yaml`：`FL_MODE` 已是既有環境變數，新增 `custom`
  字串值不需要改 compose 檔；新增的 `FL_DEFICIT_MAX`／`FL_BACKHAUL_BETA`／
  `FL_BACKHAUL_TMAX`／`FEDPROX_MU` 比照既有模式加進 `flower-superlink`／
  `inference-nodeN` 服務的 `environment` 區塊（各自需要用到的那幾個）。
- `run_stage2_fl.sh`：本來就透傳任意 `FL_MODE` 字串，啟動指令：
  ```bash
  FL_MODE=custom REWARD_MODE=throughput_only \
  FL_BACKHAUL_BETA=1.0 FEDPROX_MU=0.01 \
  bash iab/run_stage2_fl.sh
  ```

---

## 8. 消融實驗設計（三個機制可獨立開關，這是本設計刻意保留的實驗維度）

| 組合 | $Q_{max}$ | $\beta$ | $\mu$ | 等同於 |
|---|---|---|---|---|
| baseline | — | 0 | 0 | `IABClusterFedAvg`（Stage 3 原樣） |
| A：只加 reward-deficit | 3.0 | 0 | 0 | 舊版 RDA-Agg |
| B：只加 backhaul 緊繃度 | 1.0（等同關閉deficit，因固定=1） | 1.0 | 0 | — |
| C：只加 FedProx | 1.0 | 0 | 0.01 | — |
| D：deficit + backhaul（不含 FedProx） | 3.0 | 1.0 | 0 | — |
| **E：完整版（本設計推薦）** | 3.0 | 1.0 | 0.01 | — |

建議至少跑 baseline、A、D、E 四組（各 15 分鐘 Scenario T，同 seed 條件），
可以把「reward-deficit 加權」「backhaul 緊繃度加權」「兩者疊加」「加上 FedProx」
四層貢獻分開看，比單純「Stage 4 比 Stage 3 好」更有論文深度，也符合
「一次只換一個變數才能歸因」的方法論要求（這裡是用消融實驗系統性地一次換一個，
而不是一次全上）。

---

## 9. 離線驗證計畫（比照 Stage 3 的獨立數學驗證腳本）

實作後、上線量測前，先用不依賴 Docker/Flower/MongoDB 的純 Python 腳本驗證：

1. **退化情況**：$\beta=0$、全部節點 reward 相同 → `IABCustomFedAvg` 聚合結果與
   `IABClusterFedAvg`（相同輸入）逐 key 數值相等。
2. **單一落後節點**：reward 遠低於其餘節點 → `deficit` 被限制在 $Q_{max}$
   以內，原型貢獻份量確實高於同條件、reward 正常的節點。
3. **單一異常趨零節點**（模擬 UE17 完全斷線）：reward → 0 → deficit 鎖在
   $Q_{max}$，不發散成 inf/NaN。
4. **backhaul_tightness 缺值**：部分／全部節點沒有這個欄位 → `bh_weight`
   對缺值節點回退 `1.0`，不中斷聚合。
5. **FedProx 近端項數值檢查**：構造一組已知的 `anchor` 權重與當前權重，手算
   近端項數值，跟 `drl_agent.py` 算出來的比對，確認沒有維度或符號寫反。
6. **Node4 混合角色 + 雙重加權疊加**：驗證混合原型的廣播值同時反映
   `role_ratio_4`、`deficit_4`、`bh_weight_4`，且仍然介於（加權後的）relay
   原型與 access 原型之間。

---

## 10. 現場量測計畫

1. 三主機基礎設施就緒、13/13 E2、12/12 xApp、17/17 UE 現場 ping 0% 封包遺失、
   `bash scenarios/setup_iperf_servers.sh` 確認 17 個埠監聽中（見 CLAUDE.md
   第 6 節的強制步驟，這次的教訓）。
2. 清空 12 個節點的 MongoDB 經驗與模型 checkpoint（用 `docker volume ls`
   確認實際 volume 名稱）。
3. 依第 8 節消融表，逐組啟動、訓練到 `check_convergence_mongo.py` 顯示大部分
   節點收斂（參考這次的教訓：Stage 2/3 在完全沒收斂的狀態下量測，數字只能
   當方向性證據，不能當最終結論）、`REWARD_MODE=throughput_only`
   `FL_MODE=custom` + 對應超參數啟動。
4. `iab/measure_stage.py` + `scenarios/traffic_scenario.py --scenario T`
   （沿用這次改用 Scenario T 的理由，跟 avg FL／cluster FL 的最新版本同場景
   可比）跑滿 15 分鐘。
5. 除了總平均 JFI／吞吐量／RTT，**比照這次新增的逐 UE 相對改善分析**
   （`HISTORY.md` 對應條目的方法），算每組消融實驗的「逐 UE 相對 PF 改善的
   中位數」——這是本設計驗收的核心指標：如果三個機制真的有效，中位數應該從
   負轉正（多數 UE 都有感改善），不是只有總平均被少數贏家拉高。
6. 結果寫入 `experiment_results/customFL.md`，比照現有格式，明確標註每組
   消融的差異，並誠實記錄是否達成單調遞增要求。

---

## 11. 與 Stage 5 的銜接

Stage 4→5 只換 `REWARD_MODE`（`throughput_only` → `lagrangian`），Global
聚合邏輯（`IABCustomFedAvg`）與 FedProx 近端項維持不變、直接沿用——符合
CLAUDE.md「這階段只換 Local reward 機制、Global 聚合不變」的既有規則。

---

## 12. 待確認事項（實作前建議跟使用者對齊）

1. **backhaul_tightness 需要碰 C 語言共用檔案**（`gNB_scheduler_dlsch.c`），
   依 CLAUDE.md 第 7 節規則，修改後要先在 PC1 編譯驗證、再 rsync 到 PC2/PC3
   個別重新編譯全部受影響的 build target（含 `rfsimulator`，這次踩過漏編的
   教訓），且要走完整的三主機驗證流程——這比純 Python 端的 Stage 2/3 改動
   多了一段編譯/部署成本，需要抓時間。
2. $\mu$（FedProx 強度）目前只有經驗預設值 `0.01`，沒有理論推導——建議上線
   前至少用第 8 節的組合 C 單獨測一次，確認 $\mu=0.01$ 不會讓本地訓練完全學
   不動（可以看 `actor_loss`／`critic_loss` 有沒有還在正常下降）。
3. 是否要在論文方法論章節把三個超參數（$Q_{max}$、$\beta$、$\mu$）都列為
   正式可調參數並附消融結果，還是只呈現推薦組合 E 的結果、其餘當附錄——
   建議前者，三個超參數各自對應一個明確的設計動機（q-FFL 的 fairness 強度、
   本系統特有的 backhaul 感知、FedProx 的異質網路穩定性），消融結果本身就是
   論文的一個章節，不只是工程細節。
