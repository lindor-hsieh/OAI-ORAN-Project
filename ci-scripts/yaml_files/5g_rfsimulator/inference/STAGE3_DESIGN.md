# Stage 3 詳細設計文件（Local xApp + Local rApp + Global xApp + Global rApp）

> **文件定位**：本檔案整合 Stage 3（`REWARD_MODE=throughput_only`、`MODEL_ARCH=mlp`、`FL_MODE=cluster`）
> 四個元件的現行設計與完整數學公式，以程式碼現況（2026-09-28，`server_app.py` 的 ESS 收縮修正已上線、
> 2026-09-27/28 兩輪訓練與量測已完成）為準。格式比照 `STAGE2_DESIGN.md`。除錯過程與歷史沿革見
> `STAGE3_CLUSTER_FL_DESIGN.md`（Global rApp 聚合演算法的推導過程與離線驗證）、`DRL_DESIGN.md`
> （Local xApp/rApp 的 GRU／Lagrangian 分支）與 `HISTORY.md`；本檔案只保留現在式的架構事實與公式，
> 不重複踩坑敘事。拓樸、IP/ID 對照見 `CLAUDE.md` 第 1 節。
>
> **Stage 3 與 Stage 2 唯一的差異點**：Global rApp 的聚合演算法（`server_app.py::IABClusterFedAvg`，
> `FL_MODE=cluster`）。**Local xApp（`xapp_nodeN.c`）、Local rApp（`drl_agent.py`／`reward_calculator.py`／
> `training_pipeline.py`／`inference_server.py`）、Global xApp（`global_xapp.py`）三個元件的原始碼
> 逐行核對後確認與 Stage 2 完全相同**（`grep -rn "FL_MODE\|cluster" inference/drl_agent.py
> inference/inference_server.py inference/reward_calculator.py inference/training_pipeline.py
> inference/global_xapp.py` 全部零命中；`xapp_node1.c`／`xapp_node4.c` 亦無 Stage 3 專屬邏輯），
> 下面第 1~3 節據此逐一重列（非「同 Stage 2，故省略」，而是重新核對程式碼後確認内容一致才寫入）。
> **第 4 節（Global rApp）與第 5 節（分群優勢尚未發揮的診斷）才是本檔案的實質新內容。**

---

## 0. 系統總覽

Stage 3 是五階段路線圖（`CLAUDE.md` 第 3 節）的第三階：Global 層把 Stage 2 的標準 FedAvg 換成
**Soft/Weighted Clustered FedAvg**，Local 層維持「最基礎 DRL」不變（同 Stage 2，無 Lagrangian）。
單獨驗證「聚合策略」的貢獻，是五階段設計「每次只換一個變數」原則的具體實踐。

```
OAI gNB (C)                                    inference-nodeN (Python 容器，PC1)
  MAC indication (10ms callback)
    │ Rate Limiter：每 10 次才觸發一次 ZMQ
    ▼ 有效控制/觀測週期 = 1 秒
  xapp-nodeN (C / FlexRIC)  ──ZeroMQ REQ/REP(5ms timeout)──  inference_server.py
    │ MAC_CTRL_REQ                                              │ DRLAgent.infer()（drl_agent.py，同 Stage 2）
    ▼ PRB 上限寫回 OAI MAC 層                                    │ MongoDB nodeN_experiences
                                                                  │ 背景訓練執行緒（60 秒一輪，同 Stage 2）
                                                                  │
global-xapp（獨立容器，同 Stage 2 不變）──ZMQ PUB(fairness_bias)──┘
  每 2 秒讀 12 個節點的 MongoDB，算同角色相對落後程度

flower-superlink + flower-supernode-node{1..12} + flower-scheduler
  FL_MODE=cluster（Stage 3 專屬）、FL_ROUND_INTERVAL_S=60（第二輪起，見第 4.6 節）
  IABClusterFedAvg：依 role_ratio_i 算 relay/access 兩個原型（ESS 收縮修正）
  → 依各節點 role_ratio_i 混合廣播 → 寫回 model_nodeN.pt
  → inference-nodeN 的 _reload_worker 每 30 秒輪詢熱重載
```

**Stage 3 的環境變數組合**：`REWARD_MODE=throughput_only`、`MODEL_ARCH=mlp`、**`FL_MODE=cluster`**、
`DRL_CAP_MODE=relative`（同 Stage 2）、訓練時 `DRL_TRAIN_ENABLED=1`／評估量測時 `DRL_TRAIN_ENABLED=0`
+ `DRL_DETERMINISTIC=1`（同 Stage 2）。Global rApp 額外的 `CLUSTER_SHRINKAGE_C=1.0`（環境變數
`CLUSTER_SHRINKAGE_C`，`docker-compose-iab-server.yaml` 未覆寫，維持程式碼預設值）。

---

## 1. Local xApp（C，`xapp_nodeN.c`，每節點獨立，**與 Stage 2 逐行核對後確認相同**）

### 1.1 控制/觀測週期

xApp 向 E2 訂閱 E2SM-MAC 回報的名目週期是 `"100_ms"`，但 C 端 Rate Limiter 每 10 次 MAC callback
才觸發一次 ZMQ 請求，**實際有效週期是 1 秒**（實測 MongoDB 文件時間戳間隔 1.00 秒）。每筆狀態的
`bsr` 欄位因此是 **1 秒累積**的 `delta_dl_aggr_tbs`，不是單一 10ms 或 100ms 窗口值。

$$\Delta TBS_{i,t} = \text{dl\_aggr\_tbs}_{i,t} - \text{dl\_aggr\_tbs}_{i,t-1}$$

### 1.2 狀態擷取與 JSON Schema

每個週期，C 端組出：

```json
{"node_id": N, "bh_ratio": 0.97,
 "ues": [{"rnti": R, "bsr": ΔTBS, "wb_cqi": MCS, "dl_buffer_info": Q}, ...]}
```

| 欄位 | OAI 結構體來源 | 語意 |
|---|---|---|
| `bsr` | `dl_aggr_tbs` 差分 | 該 UE 過去 1 秒實際傳送的 DL bytes（吞吐量代理） |
| `wb_cqi`（鍵名沿用舊版，實際存 MCS） | `dl_mcs1` | 通道品質代理（0–28）；OAI RF Simulator 的真 3GPP `wb_cqi` 恆為 0 |
| `dl_buffer_info` | `sched_ctrl->num_total_bytes` | 真實 RLC 佇列位元組數，不受排程與否影響 |
| `bh_ratio` | `mac_ind_msg_t.backhaul_prb_ratio` | Backhaul-aware 動態 PRB 預算的可用比例 ∈[0,1] |

### 1.3 ZeroMQ 請求與逾時 Fallback

xApp 以 ZMQ REQ 送出上述 JSON，等待 Python 端 REP，**逾時上限 5ms**：

- **成功**：收到 `{"allocations": [{"rnti": R, "prb_abs": N}, ...]}`，逐 UE 換算比例
  $\text{prb\_ratio}_i = \text{clip}(prb\_abs_i / 106,\ 0,\ 1)$，寫入 `s_cached_slices[i].prb_quota`。
- **逾時（fallback）**：只送一次「解除上限」控制（`prb_quota=1.0`，等同無截斷），讓 MAC 真正退回
  PF，而不是讓最後一次成功的 DRL 上限無限期生效。

### 1.4 動作寫回 OAI MAC 層（`gNB_scheduler_dlsch.c`）

$$\text{max\_rbSize}_i \leftarrow \begin{cases}
\text{max\_rbSize}_i & \text{target\_ratio}_i \geq 1.0 \\[4pt]
\min\big(\text{max\_rbSize}_i,\ \lfloor \text{bwpSize} \times \text{target\_ratio}_i \rfloor\big) & \text{target\_ratio}_i < 1.0
\end{cases}$$

這個攔截點對 PF 排程器與 DRL 兩種控制來源一視同仁，是排程器的**輸入約束**，不是對輸出結果的事後
裁切。同一份 C 程式碼上的 Backhaul-aware 動態 PRB 預算機制（`n_rb_sched = bw × backhaul_prb_ratio`）
獨立作用，兩者不衝突、不疊加節流。**這一節與 `STAGE2_DESIGN.md` §1 逐字相同**（Stage 3 未改動任何
C 端邏輯）。

---

## 2. Local rApp（Python，`inference_server.py` + `drl_agent.py`，每節點獨立容器，**與 Stage 2 逐行核對後確認相同**）

### 2.1 State Space（51 維，`STATE_DIM = MAX_UE_COUNT×3 + 3`，`MAX_UE_COUNT=16`）

$$
s = [\underbrace{\hat{b}_0,\hat{m}_0,\hat{q}_0}_{\text{UE 0}}, \dots, \underbrace{\hat{b}_{15},\hat{m}_{15},\hat{q}_{15}}_{\text{UE 15}}, \ \hat{n},\ \hat{f},\ \hat{r}]
$$

| 特徵 | 公式 | 值域 |
|---|---|---|
| $\hat{b}_i$（`norm_tbs`） | $\log(1+\Delta TBS_i)\,/\,\log(1+B_{max})$，$B_{max}=2{,}000{,}000$ bytes | [0,1] |
| $\hat{m}_i$（`norm_mcs`） | $MCS_i / 28$ | [0,1] |
| $\hat{q}_i$（`norm_buf`） | $\log(1+Q_i)\,/\,\log(1+Q_{max})$，$Q_{max}=2{,}000{,}000$ bytes | [0,1] |
| $\hat{n}$（`active_ratio`） | $n_{active} / 16$ | [0,1] |
| $\hat{f}$（`fairness_bias_norm`） | $(\text{clip}(f_{raw},0.5,2.0)-0.5)/1.5$，$f_{raw}$ 見 §3 | [0,1] |
| $\hat{r}$（`bh_ratio`） | $\text{clip}(bh\_ratio,0,1)$ | [0,1] |

非活躍 UE 的 slot 填 0，並以布林 mask（True=活躍）在 Actor 的 softmax 前遮蔽對應 logit
（$\text{logit}_i \leftarrow -\infty$）。

### 2.2 Action Space：動作 → 每 UE PRB 上限的對應（`DRL_CAP_MODE=relative`）

Actor 輸出 $\pi_\theta(s) \in \Delta^{15}$（16 維、遮蔽後 softmax）。推論時從
$\text{Dirichlet}(\alpha)$ 採樣一組份額 $\mathbf{a}$（$\alpha_i = \pi_{\theta,i}(s)\times K_t$，
$K_t$ 見 §2.5），換算成每 UE 的 PRB 上限：

$$\text{cap}_i = \text{clip}\big(\min(1,\ n_{active}\times a_i)\times 106,\ 5,\ 106\big)$$

均分（$a_i=1/n_{active}$）時 $\min(1, n_{active}\times a_i)=1$，全部 UE 上限都是 106（= 不截斷 =
PF）；策略只在**偏離均分**時才把份額低於平均的 UE 限制在低於 106 的上限。PF 因此是策略空間內的
**恆等點**。下限 5 PRB 防止 MAC 層 SIGSEGV。（舊版 `split` 對應已於 Stage 2 診斷階段作廢，見
`avgFL.md`；Stage 3 沿用 `relative`，未再變動。）

### 2.3 神經網路架構（MLP，`MODEL_ARCH=mlp`）

**Actor**（無記憶，單步 state 快照）：

$$\pi_\theta(s) = \text{softmax}\big(W_3\,\text{ReLU}(W_2\,\text{ReLU}(W_1 s))\big)_{\text{masked}}$$

**Critic**：

$$V_\phi(s) = W_3'\,\text{ReLU}(W_2'\,\text{ReLU}(W_1' s))$$

| 層 | 維度 |
|---|---|
| Actor：$W_1,W_2$ | $51\to128\to128$，ReLU |
| Actor：$W_3$（輸出頭） | $128\to16$（logits，遮蔽後 softmax） |
| Critic：$W_1',W_2'$ | $51\to128\to64$，ReLU |
| Critic：$W_3'$（輸出頭） | $64\to1$ |
| Optimizer | Adam，$lr_{actor}=3\times10^{-4}$（`DRL_LR_ACTOR`）、$lr_{critic}=3\times10^{-4}$ |
| Gradient clipping | 1.0（Actor、Critic 皆同） |

### 2.4 獎勵函數（`REWARD_MODE=throughput_only`）

$$R = R_{tp} = \frac{1}{n}\sum_{i=1}^{n} \min\!\left(\frac{\Delta TBS_i}{B_{max}},\,1\right),\qquad B_{max}=2{,}000{,}000\ \text{bytes/視窗}$$

全部 UE 皆無流量（idle）時直接回傳 $R=0$。$R_{fairness}$、$R_{delay}$ 仍計算並寫入 MongoDB 供監控，
但最終 `reward` 只等於 $R_{tp}$（`W_THROUGHPUT=1, W_FAIRNESS=0, W_DELAY=0`）。

### 2.5 信用分配與 Actor/Critic 更新

**(a) Critic：$\gamma=0$（`DRL_GAMMA_MLP`，contextual bandit）**

$$\mathcal{L}_{critic} = \text{MSE}\big(V_\phi(s),\ r\big)$$

**(b) Advantage：固定尺度（獎勵滾動標準差）**

$$\hat{\sigma}_r \leftarrow (1-\beta)\,\hat{\sigma}_r + \beta\,\sigma_r^{batch},\qquad \beta=0.1$$
$$A(s,a) = \text{clip}\!\left(\frac{r - V(s)}{\max(\hat{\sigma}_r,\ 10^{-3})},\ -3,\ 3\right)$$

**(c) 壅塞樣本遮罩**：只用「壅塞」樣本更新 Actor：

$$\text{contended}(s,s') = \Big(\max_i q_i(s) \geq \tau\ \lor\ \max_i q_i(s') \geq \tau\Big)\ \land\ (n_{active}(s)\geq 2)$$

$\tau = 100{,}000$ bytes（`CONTENDED_BUF_BYTES`）；batch 內壅塞樣本 $n_{actor} < 8$
（`MIN_CONTENDED_SAMPLES`）時整批跳過 Actor 更新。**這個門檻是 Stage 3 分群設計成敗的關鍵前提**
——見第 5 節：relay 節點（Node1~3）的 MT 通道從不被場景惡化，`max_i q_i` 幾乎恆低於 $\tau$，導致
這三個節點結構性幾乎產生不出可訓練 Actor 的樣本。

**(d) 離策略校正（PPO 式比例裁剪）**：

$$\rho = \exp\big(\log\pi_{new}(a|s) - \log\pi_{old}(a|s)\big),\qquad
\mathcal{L}_{actor} = -\mathbb{E}_{\text{contended}}\Big[\min\big(\rho\,A,\ \text{clip}(\rho,1-\epsilon,1+\epsilon)\,A\big)\Big] - \beta_t H(\pi_\theta)$$

$\epsilon = 0.2$（`DRL_PPO_CLIP_EPS`）。

**(e) Entropy 正則化**：$\beta_t = \max(0.001,\ 0.01\times 0.997^t)$，entropy < −5.0 時強制拉高
$\beta_t \leftarrow \max(\beta_t,\ 0.1\times|H_{current}|/5.0)$。

### 2.6 Dirichlet 集中度退火與確定性評估

$$K_t = 5 + 45\times(1-e^{-t/5000})$$

**`DRL_DETERMINISTIC=1`**（凍結模型評估/量測專用）：跳過採樣，直接取 $a_i = \pi_{\theta,i}(s)$。

### 2.7 訓練資料管線與週期

- 背景訓練（`_train_worker`）每 `TRAIN_INTERVAL_S=60` 秒一輪，讀最新 `TRAIN_FETCH_LIMIT=5000` 筆，
  8:2 切 train/test，`TRAIN_EPOCHS_PER_ROUND=10` 個 epoch。
- 推論模式切換：`train_steps ≥ MIN_DRL_TRAIN_STEPS=100`（含 FL 客戶端的訓練步數）才從 BSR 啟發式
  切到 DRL 推論。
- 每個 process 限 1 個 torch/BLAS 執行緒，訓練起始依 `node_id` 錯開 ~5 秒。

**第 2 節全文與 `STAGE2_DESIGN.md` §2 內容一致**（逐行核對 `drl_agent.py`／`reward_calculator.py`／
`training_pipeline.py`／`inference_server.py` 確認，Stage 3 未修改任何 Local rApp 程式碼）。

---

## 3. Global xApp（`global_xapp.py`，獨立 Python process，**與 Stage 2 逐行核對後確認相同**）

**職責**：提供純軟性 state 特徵 $\hat{f}$（§2.1），不做任何硬性 PRB 裁切。

每 `GLOBAL_XAPP_INTERVAL_S=2` 秒，讀 MongoDB 全部 12 個節點最近 `GLOBAL_XAPP_LOOKBACK=300` 筆非閒置
經驗（≈5 分鐘）的 $R_{tp}$ 平均值：

$$\bar{T}_i = \text{mean}\big(r\_throughput_{i,\,\text{最近 300 筆}}\big)$$

**同角色內比較**（relay=Node1~4、access=Node5~12 分開算）：

$$\bar{T}_{\text{role}(i)} = \text{mean}\big(\{\bar{T}_j : \text{role}(j) = \text{role}(i)\}\big)$$

$$f_{raw,i} = \text{clip}\!\left(\frac{\bar{T}_{\text{role}(i)}}{\bar{T}_i + \epsilon},\ 0.5,\ 2.0\right),\qquad \epsilon=10^{-6}$$

透過 ZMQ PUB（`tcp://127.0.0.1:5560`，topic `node{i}`）廣播給對應的 Local rApp。**這個「同角色內比較」
的 role 定義（`RELAY_NODE_IDS = frozenset({1,2,3,4})`）與 Global rApp 的 `ROLE_RATIO`（第 4.2 節）是
兩套獨立、刻意不同粒度的角色概念**：Global xApp 用二元角色（relay／access）比較吞吐量高低，Global
rApp 用連續 `role_ratio_i ∈ [0,1]` 做模型混合權重——兩者互不影響，只是恰好都用「relay/access」這組
詞彙，容易混淆。

監控用全網 JFI（不寫回 MongoDB、不納入聚合權重）：
$\text{JFI} = (\sum x_i)^2 / (n \sum x_i^2)$，$x_i = \bar{T}_i / \bar{T}_{\text{role}(i)}$。

---

## 4. Global rApp（Flower ServerApp/ClientApp，`FL_MODE=cluster`，**Stage 3 唯一改動的元件**）

### 4.1 動機：為什麼從標準 FedAvg 換成 Soft/Weighted Clustered FedAvg

Stage 2 的 `IABFedAvg` 把全部 12 個節點的更新用同一組全域權重加權平均。但 12 個節點在拓樸上分成
結構性不同的兩種角色：relay（Node1~4，DU 服務的是其他有 DU 的節點的 MT，承載匯聚流量）與 access
（Node5~12，DU 服務的是純 UE）。**Node4 是唯一橫跨兩種角色的邊界案例**：它的 DU 同時中繼 Node11/12
（間接服務 UE13~16）又直連 UE17。標準 FedAvg 用同一個平均模型服務這兩種結構不同的角色，可能無法
同時對兩者都最優；硬性把節點分兩群（relay 群/access 群）各自 FedAvg，又會強迫 Node4 的模型只能
擬合單一型態，忽略它的混合本質。Stage 3 改用連續角色比例的 Soft/Weighted 設計來同時解決這兩個問題。

### 4.2 `role_ratio_i`：結構性連續角色比例

$$\text{role\_ratio}_i = \frac{\text{直連 UE 數量}}{\text{直連 UE 數量} + \text{透過下游 DU 節點間接服務的 UE 數量}}$$

依現行 12-node 拓樸（結構性常數，不需即時量測）：

```python
ROLE_RATIO: dict[int, float] = {
    1: 0.0, 2: 0.0, 3: 0.0,
    4: 0.2,  # UE17 直連(1) / (UE17(1) + 經 Node11,12 服務的 UE13~16(4)) = 1/5
    5: 1.0, 6: 1.0, 7: 1.0, 8: 1.0,
    9: 1.0, 10: 1.0, 11: 1.0, 12: 1.0,
}
```

Node1~3 純 relay（$\rho=0$）、Node5~12 純 access（$\rho=1$）、**Node4 混合（$\rho=0.2$）**——是全部
12 個節點中唯一 $0<\rho<1$ 的節點。硬性二分群是 $\rho_i \in \{0,1\}$ 時的特例。

### 4.3 Client 端（`client_app.py::train()`，與 Stage 2 相同，僅回傳內容多帶 `node_id`）

1. `agent.load()` 讀回本節點目前的 optimizer/train_steps 狀態。
2. 套用這一輪收到的全域權重（只覆蓋 actor/critic 權重）。
3. 立刻 `agent.save()` 一次。
4. `training_pipeline.run_training_round()` 在本地經驗上微調，成功則再 `agent.save()`。
5. 回傳更新後的權重 + metrics：

$$\text{num-examples}_i = \text{count\_contended}(\text{train\_exp}_i),\qquad \text{node\_id}=i$$

`node_id` 是 Stage 3 專屬新增欄位（Stage 2 的 `IABFedAvg` 不需要按節點分組，`IABClusterFedAvg` 需要
依 `node_id` 查 `ROLE_RATIO`；Flower 內部的 node id 是 SuperLink 隨機指派，跟本專案 `NODE_ID`(1~12)
無對應關係，只能由 client 端自己在 metrics 帶出來）。

### 4.4 Server 端聚合第一步：算出兩個「原始」原型

`IABClusterFedAvg.aggregate_train()`（繼承 `IABFedAvg`，只覆寫這個方法）先把這一輪全部 12 個節點的
回覆按角色比例拆成三組加權列表：

$$\text{relay\_items} = \big[(W_i,\ (1-\rho_i)\,n_i)\big]_{i=1}^{12},\qquad
\text{access\_items} = \big[(W_i,\ \rho_i\, n_i)\big]_{i=1}^{12},\qquad
\text{all\_items} = \big[(W_i,\ n_i)\big]_{i=1}^{12}$$

（$W_i$ 為攤平後的 actor+critic state_dict，$n_i$ = `num-examples`_i）。逐 key 加權平均：

$$W_{\text{relay}}^{raw} = \frac{\sum_i (1-\rho_i)\,n_i\,W_i}{\sum_i (1-\rho_i)\,n_i},\qquad
W_{\text{access}}^{raw} = \frac{\sum_i \rho_i\,n_i\,W_i}{\sum_i \rho_i\,n_i},\qquad
W_{\text{global}} = \frac{\sum_i n_i\,W_i}{\sum_i n_i}$$

（`_weighted_average_flat()`；任一分母 $\le 0$ 時回傳 `None`，代表這一側這輪沒有新資料。）
$W_{\text{global}}$ 與 Stage 2 `IABFedAvg` 這一輪會產生的結果完全等價——這是第 4.5 節 ESS 收縮的
退回目標。

### 4.5 Server 端聚合第二步：ESS（有效樣本數）收縮修正

**問題（2026-09-27 設計審查發現）**：relay 側的加權平均 $W_{\text{relay}}^{raw}$ 結構性幾乎完全由
Node4 一個節點決定——Node1~3 的 MT 通道從未被流量場景惡化，$\max_i q_i$ 幾乎恆低於壅塞門檻 $\tau$
（§2.5(c)），$n_i \approx 0$；relay_items 的四筆權重實質上只有 Node4 那筆非零。若不修正，
$W_{\text{relay}}^{raw}$ 會退化成「Node4 專屬模型」而非 relay 群體共識，廣播回 Node1~3 後可能系統性
劣於 Stage 2（Node1~3 在 avg FedAvg 下拿到的是全體 12 節點的加權平均，樣本池遠大於單一節點）。

**修正**：用有效樣本數（inverse Simpson index）偵測「這一側是不是被單一節點壟斷」，壟斷程度越高就
把該側的原型越大幅度收縮回 $W_{\text{global}}$（等同 Stage 2 的結果）：

$$\text{ESS}(\text{weights}) = \frac{\big(\sum_j w_j\big)^2}{\sum_j w_j^2}$$

（$w_j>0$ 的子集；全 0 時 $\text{ESS}=0$。$n$ 個節點權重均等時 $\text{ESS}=n$；被單一節點壟斷時
$\text{ESS}\to1$。）

$$\alpha(\text{ess}) = \frac{\max(0,\ \text{ess}-1)}{\max(0,\ \text{ess}-1) + C},\qquad C = \text{CLUSTER\_SHRINKAGE\_C} = 1.0\ (\text{預設})$$

$\text{ess}=1$（完全壟斷）$\Rightarrow \alpha=0$（完全退回全域平均）；$\text{ess}\to\infty$（貢獻者
越多元）$\Rightarrow \alpha\to1$（完全信任該側自己的加權平均）。最終原型：

$$W_{\text{relay}} = \alpha(\text{ess}_{\text{relay}})\cdot W_{\text{relay}}^{raw} + \big(1-\alpha(\text{ess}_{\text{relay}})\big)\cdot W_{\text{global}}$$
$$W_{\text{access}} = \alpha(\text{ess}_{\text{access}})\cdot W_{\text{access}}^{raw} + \big(1-\alpha(\text{ess}_{\text{access}})\big)\cdot W_{\text{global}}$$

（`_shrunk_prototype()`；$W_{\text{raw}}$ 為 `None` 時直接回傳 $W_{\text{global}}$；$W_{\text{global}}$
為 `None`——極端冷啟動、全體都沒有訊號——時直接回傳 $W_{\text{raw}}$ 不收縮，沒有可退回的對象。）

**真實訓練資料校準結果**（`stage3_run_20260927` 3 小時訓練資料，`calibrate_ess.py` 對
MongoDB 實跑）：

| | $\text{ess}$ | $\alpha$ |
|---|---|---|
| relay | ≈1.00–1.01 | ≈0（幾乎完全退回 $W_{\text{global}}$） |
| access | ≈7.86–8.01 | ≈0.87–0.93（大部分信任自己的加權平均） |

證實 ESS 修正確實依設計生效：Node4 壟斷 relay 原型的問題已用「大幅退回全域平均」解決，避免了
「Node1~3 系統性劣於 Stage 2」的風險。**但這個修正的副作用是第 5 節要討論的核心發現。**

### 4.6 廣播：依 $\rho_i$ 混合回各節點

$$W_i(\text{廣播}) = (1-\rho_i)\cdot W_{\text{relay}} + \rho_i \cdot W_{\text{access}}$$

Node1~3 拿到幾乎純 $W_{\text{relay}}$（≈$W_{\text{global}}$）、Node5~12 拿到幾乎純 $W_{\text{access}}$、
**Node4 拿到 $0.8\cdot W_{\text{relay}} + 0.2\cdot W_{\text{access}}$**——是全部 12 個節點中唯一一個
真正拿到「relay/access 混合」權重的節點（`_broadcast_cluster_weights()`；任一原型為 `None` 時整段
退化用另一個；兩者皆 `None` 時全部節點這輪都不更新）。

**FL 輪次週期**：`FL_ROUND_INTERVAL_S`——第一輪訓練（3h）用預設值 180 秒；第一輪凍結量測結果
（4.92 Mbps）未優於 Stage 2 基準（5.02 Mbps）後，診斷 ESS 校準顯示聚合機制本身無結構性問題（見上），
判斷落差較可能是訓練軌跡隨機變異，選擇風險最低的 Global 端調整：把這個值從寫死的 `"180"` 改成
`"${FL_ROUND_INTERVAL_S:-180}"`（可覆寫、預設不變），第二輪訓練（2h）設為 **60** 秒——同樣訓練時間
內聚合機會變 3 倍。**聚合演算法（`IABClusterFedAvg`）本身在兩輪之間沒有再改動。**

### 4.7 兩輪訓練與量測結果

| 指標 | 第一輪（3h，FL 180s） | 第二輪（2h，FL 60s） | Stage 2 基準 | PF |
|---|---|---|---|---|
| 平均吞吐量 | 4.92 Mbps | **5.08 Mbps** | 5.02 | 4.93 |
| 壅塞相位需求滿足率 | 0.752 | 0.764 | 0.768 | 0.767 |
| 壅塞相位滿足率 JFI | 0.921 | 0.934 | 0.933 | 0.936 |
| 整段 JFI | 0.9854 | **0.9933** | 0.9896 | 0.9871 |
| 整段平均 RTT | 60.7 ms | 58.6 ms | 58.4 | 59.3 |

第二輪吞吐量 5.08 Mbps 優於 Stage 2 的 5.02（+1.2%）與 PF 的 4.93，整段 JFI 0.9933 是四者最高；
其餘指標與 Stage 2 打平或些微落後（差距在雜訊範圍內）。**Stage 3 在吞吐量這個主要判讀指標上達成
單調遞增**（完整數據、TCP/UDP 量測方法、原始資料位置見 `experiment_results/clusterFL.md` 最末章節）。

---

## 5. 核心發現：分群機制尚未真正發揮「relay/access 專業化」優勢

**這是本檔案最重要的診斷結論，供論文方法論限制章節與後續改進方向引用。**

### 5.1 問題陳述

Stage 3 設計的初衷（§4.1）是讓 relay 與 access 兩種結構角色的節點，各自收斂到更貼合自己角色特性的
模型——這是「Clustered FL 比 vanilla FedAvg 更適合異質（non-IID）client 群體」這個假設的具體實踐。
但 §4.5 的真實資料校準顯示：

$$\text{ESS}_{\text{relay}} \approx 1.00\text{–}1.01 \quad\Longrightarrow\quad \alpha(\text{ESS}_{\text{relay}}) \approx 0$$

代入 §4.6 的廣播公式，Node1~3（$\rho=0$）拿到的 $W_i \approx W_{\text{relay}} \approx W_{\text{global}}$
——**與 Stage 2 標準 FedAvg 這三個節點會拿到的權重幾乎完全相同**。

同時，$\text{ESS}_{\text{access}}\approx7.86$–$8.01$ 雖然明顯大於 1（access 側的 8 個節點確實較
多元地共同貢獻），但 $W_{\text{global}}$ 本身也高度接近 $W_{\text{access}}^{raw}$——因為
$\sum_i n_i$ 這個全域加權平均的分母幾乎全部由 access 節點的 $n_i$ 貢獻（relay 節點 $n_i\approx0$，
對 $W_{\text{global}}$ 的加權貢獻本來就可忽略）。因此 Node5~12（$\rho=1$）拿到的
$W_i \approx W_{\text{access}} \approx W_{\text{access}}^{raw} \approx W_{\text{global}}$——**與
Stage 2 這 8 個節點會拿到的權重也幾乎相同**。

**結論**：12 個節點中，**只有 Node4**（唯一 $0<\rho<1=0.2$ 的節點）拿到一個與 Stage 2 標準 FedAvg
真正不同、且是 relay/access 混合的權重（$0.8 W_{\text{relay}} + 0.2 W_{\text{access}} \approx
0.8 W_{\text{global}} + 0.2 W_{\text{access}}^{raw}$）。其餘 11 個節點在目前的訓練資料型態下，收到的
權重跟「假設 Stage 3 直接用 Stage 2 的 `IABFedAvg`」幾乎沒有差別。**Stage 3 的分群設計，在目前 12
節點、11 個純角色節點 + 1 個邊界節點的拓樸與流量場景下，實質上只對 1/12 的節點產生了設計預期的
差異化效果。**

### 5.2 這如何解釋 §4.7 的量測結果，以及為什麼不能歸因於「分群」本身

第二輪（5.08 Mbps）優於 Stage 2（5.02 Mbps）的 +1.2% 改善，考慮到上述分析，**更合理的歸因是**：

1. `FL_ROUND_INTERVAL_S` 180→60 秒的調整（同一訓練時長內聚合機會變 3 倍，見 §4.6）；
2. 兩輪訓練皆為暖啟動延續而非獨立重跑，訓練軌跡本身的隨機變異（`clusterFL.md` 已記載的既知
   confound，未做對照組無法與第 1 點互相區隔）；
3. Node4 這單一節點確實拿到差異化的混合權重，但 Node4 只是 12 個節點之一，其個別改善不足以解釋
   整體平均吞吐量 +1.2% 的量級（若要驗證這個假說，需要拆解逐節點吞吐量變化，本輪量測未做）。

**分群機制的核心假設（relay/access 應該分別收斂到更貼合自己角色的模型）在這組資料下沒有被證偽，
但也沒有被證實發揮作用**——因為 relay 側事實上沒有機會形成一個「跟 access 側有意義地不同」的原型：
ESS 收縮修正雖然正確地避免了「被 Node4 單一節點壟斷、可能系統性劣化」的風險，但代價是讓 relay
側的原型幾乎完全等同全域平均，使分群本身在這批資料上「name 上存在、實質上沒有差異化」。

### 5.3 根本原因

relay 節點（Node1~3）結構性幾乎不產生壅塞樣本（$n_i\approx0$），是因為現行流量場景設計
（`scenarios/traffic_scenario.py`）**只惡化 access 節點下游 UE 的下行通道，從未惡化 relay 節點自己
的 MT 通道**（`CLAUDE.md` 第 8 節「通道惡化機制」：場景的路徑損耗調整走的是每個終端 UE 自己的
`nrue.uicc.chanmod.conf`，relay 節點的 MT 是另一個 rfsimulator 通道實例，場景控制器目前沒有對它
下指令）。relay 節點唯一的壅塞來源是 Backhaul-aware 動態 PRB 預算機制間接造成的排隊（`CLAUDE.md`
第 3 節的自我節流回饋迴圈），這個壅塞的量級遠低於 $\tau=100{,}000$ bytes 的門檻。

**這不是 ESS 收縮修正的錯**（收縮修正是對「relay 樣本稀少」這個既有事實的正確因應，見 §4.5）——
根本原因在更上游：relay 節點缺乏能讓「relay 專屬模型」與「access 專屬模型」產生有意義差異的訓練
訊號。

### 5.4 若要讓分群優勢真正發揮，需要什麼改動（供後續參考，本輪未實作）

- **最直接**：讓流量場景額外惡化 relay 節點自己的 MT 通道（需要場景控制器新增對 relay MT 通道的
  telnet 控制邏輯，且要評估這是否符合「relay 之間本不該搶同一份頻譜」的既有簡化假設，見 CLAUDE.md
  第 2 節「與標準規格的差異」表）——這是能從根本上解決 §5.3 問題的方案，但涉及新的場景設計與
  重新校準，風險與工作量較高，此次不在使用者授權的「只能調整 Global 端或訓練場景，Local 端不動」
  範圍內優先選擇（當時選擇了風險最低的 `FL_ROUND_INTERVAL_S` 調整，見 §4.6）。
- **較保守**：降低 relay 側判定「壅塞」的門檻 $\tau$（僅對 relay 節點），讓現有的、量級較小的
  backhaul 自我節流排隊也能被計入壅塞樣本——但這會讓 relay/access 兩側用不同標準判定「壅塞」，
  破壞 §2.5(c) 門檻的物理意義一致性，需要額外校準與論證。
- **診斷驗證**：若要在不改動場景或門檻的前提下確認分群機制「有沒有用」，可以做一次 Stage 3 vs
  一個「假想的 Stage 2.5」（即 Stage 3 訓練資料、但廣播時故意換回 Stage 2 的 `IABFedAvg` 邏輯）的
  逐節點吞吐量 A/B 對照，直接驗證 Node4 的差異化權重是否真的比它在標準 FedAvg 下的權重表現更好。

---

## 6. 參數總表

| 參數 | 數值 | 來源 | 與 Stage 2 是否相同 |
|---|---|---|---|
| 控制/觀測週期 | 1 秒（實測） | xApp Rate Limiter | 相同 |
| 系統 PRB 總數 | 106 | `total_prb` | 相同 |
| ZMQ REQ 逾時 | 5 ms | `xapp_nodeN.c` | 相同 |
| State 維度 | 51 | `STATE_DIM` | 相同 |
| $B_{max}$ | 2,000,000 bytes/視窗 | `MAX_BSR` | 相同 |
| `DRL_CAP_MODE` | `relative` | 2026-09-27 | 相同 |
| `DRL_DETERMINISTIC` | 0（訓練）／1（凍結評估） | 2026-09-27 | 相同 |
| 壅塞門檻 $\tau$ | 100,000 bytes | `CONTENDED_BUF_BYTES` | 相同 |
| Global xApp 週期／視窗 | 2 秒／最近 300 筆 | `GLOBAL_XAPP_INTERVAL_S`／`_LOOKBACK` | 相同 |
| **`FL_MODE`** | **`cluster`** | Stage 3 專屬 | **不同（Stage 2=`avg`）** |
| **`ROLE_RATIO`** | Node1-3=0, Node4=0.2, Node5-12=1 | Stage 3 專屬 | Stage 3 新增 |
| **`CLUSTER_SHRINKAGE_C`** | 1.0（預設，未覆寫） | 2026-09-27 ESS 修正 | Stage 3 新增 |
| **`FL_ROUND_INTERVAL_S`** | 180（第一輪）→ **60**（第二輪起） | Global 端調整 | Stage 3 改動（Stage 2 為 180 固定） |
| $\text{ESS}_{\text{relay}}$（實測） | ≈1.00–1.01 | 真實訓練資料校準 | Stage 3 專屬診斷 |
| $\text{ESS}_{\text{access}}$（實測） | ≈7.86–8.01 | 真實訓練資料校準 | Stage 3 專屬診斷 |

---

## 7. 與其他設計文件的關係

- `STAGE2_DESIGN.md`：Stage 2（`FL_MODE=avg`）的完整設計，本檔案第 1~3 節與其對應章節逐字相同，
  第 4~5 節為 Stage 3 的實質新內容。
- `STAGE3_CLUSTER_FL_DESIGN.md`：Global rApp 聚合演算法的完整歷史沿革、離線數學驗證與第 10 節 ESS
  修正的推導過程（本檔案第 4.4~4.5 節是其現況摘要）。
- `DRL_DESIGN.md`：Local xApp/rApp 的完整歷史沿革與 GRU／Lagrangian 分支的詳細推導（非現行路徑）。
- `STAGE4_CUSTOM_FL_DESIGN.md`：Stage 4 自訂聚合演算法草案。
- `../experiment_results/clusterFL.md`：Stage 3 量測結果原始數據與方法論限制章節。
- `HISTORY.md`：逐日除錯記錄，含 2026-09-27/28 訓練與量測過程的完整敘事。
