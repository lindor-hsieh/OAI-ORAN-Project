# Stage 2 詳細設計文件（Local xApp + Local rApp + Global xApp + Global rApp）

> **文件定位（2026-09-29 更新）**：§1、§2（Local xApp／Local rApp 設計）**已被
> `LOCAL_DRL_V2_DESIGN.md` 取代**，以下保留原文只作歷史沿革／消融實驗參考，不再是現行路徑——
> 舊設計（GRU/MLP＋連續 Dirichlet 動作空間，`DRL_CAP_MODE=relative`）從未在任何 Stage 真正贏過
> PF baseline，2026-09-29 決定全面重新設計 Local 層（離線 BC 預訓練＋離散動作空間＋反事實
> reward＋relational state），見 `LOCAL_DRL_V2_DESIGN.md` 與 `HISTORY.md` 續三十六。
>
> **2026-10-04**：Local 端現為 `LOCAL_DRL_V2_DESIGN.md` v3.4；FL 容器設定一致性修正與 Stage 2 現行流程見 §4.4。
>
> §3（Global xApp）、§4（Global rApp，`FL_MODE=avg`）**維持現行不變**——Stage 2 的 Global 層
> 定案是標準 FedAvg（IAB/O-RAN FL 文獻裡最直接對應 GLOBECOM 2022 那篇），這次重新設計沒有理由
> 改動；Stage 2 現在的完整定義 = `LOCAL_DRL_V2_DESIGN.md`（Local）＋本文件 §3/§4（Global）。
>
> 除錯過程與歷史沿革見 `DRL_DESIGN.md`（Local xApp/rApp 的 GRU 分支、Lagrangian 限制式等更早
> 期的推導過程）與 `HISTORY.md`；本檔案只保留現在式的架構事實與公式，不重複踩坑敘事。拓樸、
> IP/ID 對照見 `CLAUDE.md` 第 1 節。

---

## 0. 系統總覽

Stage 2 是五階段路線圖（`CLAUDE.md` 第 3 節）的第二階：Global 層加上標準 FedAvg，Local 層是「最基礎
DRL」（無 Lagrangian、無限制式）。12 個 Node（Node1~12）各自獨立部署一組 Local xApp + Local rApp，
另有一個 Global xApp + 一組 Global rApp（Flower SuperLink/SuperNode）跨全部 12 個節點運作。

```
OAI gNB (C)                                    inference-nodeN (Python 容器，PC1)
  MAC indication (10ms callback)
    │ Rate Limiter：每 10 次才觸發一次 ZMQ
    ▼ 有效控制/觀測週期 = 1 秒
  xapp-nodeN (C / FlexRIC)  ──ZeroMQ REQ/REP(5ms timeout)──  inference_server.py
    │ MAC_CTRL_REQ                                              │ DRLAgent.infer()（drl_agent.py）
    ▼ PRB 上限寫回 OAI MAC 層                                    │ MongoDB nodeN_experiences
                                                                  │ 背景訓練執行緒（60 秒一輪）
                                                                  │
global-xapp（獨立容器）──ZMQ PUB(fairness_bias)──────────────────┘
  每 2 秒讀 12 個節點的 MongoDB，算同角色相對落後程度

flower-superlink + flower-supernode-node{1..12} + flower-scheduler（FL_MODE=avg，180 秒一輪）
  IABFedAvg：num-examples（壅塞樣本數）加權平均 12 個節點的 actor/critic 權重
  → 寫回 model_nodeN.pt → inference-nodeN 的 _reload_worker 每 30 秒輪詢熱重載
```

**Stage 2 的環境變數組合**：`REWARD_MODE=throughput_only`、`MODEL_ARCH=mlp`、`FL_MODE=avg`、
`DRL_CAP_MODE=relative`（2026-09-27 起預設，見 §2.2）、訓練時 `DRL_TRAIN_ENABLED=1`／評估量測時
`DRL_TRAIN_ENABLED=0` + `DRL_DETERMINISTIC=1`（見 §2.6）。

---

## 1. 〔已被 `LOCAL_DRL_V2_DESIGN.md` 取代，僅供歷史參考〕Local xApp（C，`xapp_nodeN.c`，每節點獨立）

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
| `dl_buffer_info` | `sched_ctrl->num_total_bytes` | 真實 RLC 佇列位元組數，不受排程與否影響（`Δtbs`／MCS 在 UE 無資料時會凍結在舊值） |
| `bh_ratio` | `mac_ind_msg_t.backhaul_prb_ratio` | Backhaul-aware 動態 PRB 預算的可用比例 ∈[0,1]，2026-09-26 起隨狀態一併送出 |

### 1.3 ZeroMQ 請求與逾時 Fallback

xApp 以 ZMQ REQ 送出上述 JSON，等待 Python 端 REP，**逾時上限 5ms**：

- **成功**：收到 `{"allocations": [{"rnti": R, "prb_abs": N}, ...]}`，逐 UE 換算比例
  $\text{prb\_ratio}_i = \text{clip}(prb\_abs_i / 106,\ 0,\ 1)$，寫入 `s_cached_slices[i].prb_quota`。
- **逾時（fallback）**：只送一次「解除上限」控制（`prb_quota=1.0`，等同無截斷），讓 MAC 真正退回
  PF，而不是讓最後一次成功的 DRL 上限無限期生效。

### 1.4 動作寫回 OAI MAC 層（`gNB_scheduler_dlsch.c`）

MAC 排程器對每個 UE 用時域 mask（本專案恆為全開）與頻域比例兩段式套用控制：

$$\text{max\_rbSize}_i \leftarrow \begin{cases}
\text{max\_rbSize}_i & \text{target\_ratio}_i \geq 1.0 \\[4pt]
\min\big(\text{max\_rbSize}_i,\ \lfloor \text{bwpSize} \times \text{target\_ratio}_i \rfloor\big) & \text{target\_ratio}_i < 1.0
\end{cases}$$

這個攔截點對 PF 排程器與 DRL 兩種控制來源一視同仁，是排程器的**輸入約束**（縮小可用 RB 池），
不是對輸出結果的事後裁切。同一份 C 程式碼上，另有 Backhaul-aware 動態 PRB 預算機制
（`n_rb_sched = bw × backhaul_prb_ratio`，CLAUDE.md 第 3 節）獨立作用，兩者不衝突、不疊加節流。

---

## 2. 〔已被 `LOCAL_DRL_V2_DESIGN.md` 取代，僅供歷史參考〕Local rApp（Python，`inference_server.py` + `drl_agent.py`，每節點獨立容器）

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
| $\hat{r}$（`bh_ratio`） | $\text{clip}(bh\_ratio,0,1)$，見 §1.2 | [0,1] |

非活躍 UE 的 slot 填 0，並以布林 mask（True=活躍）在 Actor 的 softmax 前遮蔽對應 logit
（$\text{logit}_i \leftarrow -\infty$）。

### 2.2 Action Space：動作 → 每 UE PRB 上限的對應

Actor 輸出 $\pi_\theta(s) \in \Delta^{15}$（16 維、遮蔽後 softmax，加總為 1）。推論時從
$\text{Dirichlet}(\alpha)$ 採樣一組份額 $\mathbf{a}$（$\alpha_i = \pi_{\theta,i}(s)\times K_t$，
$K_t$ 見 §2.5），再依 `DRL_CAP_MODE` 換算成每 UE 的 PRB 上限：

**`split`（舊，2026-09-27 前預設，現已作廢）**：

$$\text{cap}_i = \lfloor a_i \times 106 \rceil$$

均分（$a_i = 1/n$）時每個 UE 都被截斷在約 $106/n$，PF（完全不截斷）不是這個動作空間裡能表達
的點——只要 ≥2 個活躍 UE，每個週期都被人為切分頻域，跟策略是否學到東西無關，系統性劣於 PF
（見 `avgFL.md`「動作對應設計問題的診斷與修正」章節的實測數字）。

**`relative`（2026-09-27 起預設）**：

$$\text{cap}_i = \text{clip}\big(\min(1,\ n_{active}\times a_i)\times 106,\ 5,\ 106\big)$$

均分（$a_i=1/n_{active}$）時 $\min(1, n_{active}\times a_i)=1$，全部 UE 上限都是 106（= 不截斷 =
PF）；策略只在**偏離均分**時才把份額低於平均的 UE 限制在低於 106 的上限。PF 因此是策略空間內
的**恆等點**：學不到東西時退化成 PF，而不是系統性劣於 PF。下限 5 PRB 防止 MAC 層 SIGSEGV。
C 端 xApp 逐 UE 獨立換算比例（§1.3，`clip(cap_i/106, 0, 1)`），不需要改 C 程式。

### 2.3 神經網路架構（MLP，`MODEL_ARCH=mlp`，Stage 2 現行）

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

（GRU 分支 `MODEL_ARCH=gru` 保留供未來改良版/研究使用，Stage 2 不採用，完整推導見 `DRL_DESIGN.md` §4。）

### 2.4 獎勵函數（Stage 2：`REWARD_MODE=throughput_only`）

Stage 2 呼叫 `compute_reward_breakdown()`（純吞吐量，不套 Lagrangian 限制式）：

$$R = R_{tp} = \frac{1}{n}\sum_{i=1}^{n} \min\!\left(\frac{\Delta TBS_i}{B_{max}},\,1\right),\qquad B_{max}=2{,}000{,}000\ \text{bytes/視窗}$$

全部 UE 皆無流量（idle）時直接回傳 $R=0$，不進入下方公平性/延遲分量計算，避免污染訓練資料。
$R_{fairness}$（JFI 線性縮放）、$R_{delay}$（PRB 效率懲罰）仍照常計算並寫入 MongoDB **供監控**，但
`throughput_only` 模式下的最終 `reward` 只等於 $R_{tp}$（對應 `W_THROUGHPUT=1, W_FAIRNESS=0,
W_DELAY=0`）。Lagrangian 限制式公式（Stage 5 用）與 $JFI_{min}$ 訂定方式見 `DRL_DESIGN.md` §5.1。

### 2.5 信用分配與 Actor/Critic 更新（2026-09-26 重設計）

**動機**：不壅塞時 $\Delta TBS$ 主要由外生流量需求決定、跟 PRB 怎麼分無關；若每批做 advantage
z-score 標準化，會把純雜訊放大成隨機遊走，Actor 學不到東西。改採以下設計：

**(a) Critic：$\gamma=0$（`DRL_GAMMA_MLP`，contextual bandit）**

$$\mathcal{L}_{critic} = \text{MSE}\big(V_\phi(s),\ r + \gamma\,V_\phi(s')\big),\qquad \gamma=0 \Rightarrow \text{target} = r$$

**(b) Advantage：固定尺度（獎勵滾動標準差），不做每批 z-score**

$$\hat{\sigma}_r \leftarrow (1-\beta)\,\hat{\sigma}_r + \beta\,\sigma_r^{batch},\qquad \beta=0.1\ (\text{REWARD\_STD\_EMA})$$
$$A(s,a) = \text{clip}\!\left(\frac{(r+\gamma V(s')) - V(s)}{\max(\hat{\sigma}_r,\ 10^{-3})},\ -3,\ 3\right)$$

**(c) 壅塞樣本遮罩**：只用「壅塞」樣本更新 Actor（非壅塞樣本只訓練 Critic）：

$$\text{contended}(s,s') = \Big(\max_i q_i(s) \geq \tau\ \lor\ \max_i q_i(s') \geq \tau\Big)\ \land\ (n_{active}(s)\geq 2)$$

$\tau = 100{,}000$ bytes（`CONTENDED_BUF_BYTES`，2026-09-26 依真實 1 秒視窗資料校準：正常相位誤判
13.7%、壅塞相位命中 65%）；單一活躍 UE 時 Dirichlet 退化成一維、log_prob 恆為 0，不能算成可更新
Actor 的樣本。batch 內壅塞樣本 $n_{actor} < 8$（`MIN_CONTENDED_SAMPLES`）時整批跳過 Actor 更新。

**(d) 離策略校正（PPO 式比例裁剪）**：推論時把當時的 $\log\pi_{old}(a|s)$ 存進經驗
（`behavior_logp`）；訓練時：

$$\rho = \exp\big(\log\pi_{new}(a|s) - \log\pi_{old}(a|s)\big),\qquad
\mathcal{L}_{actor} = -\mathbb{E}_{\text{contended}}\Big[\min\big(\rho\,A,\ \text{clip}(\rho,1-\epsilon,1+\epsilon)\,A\big)\Big] - \beta_t H(\pi_\theta)$$

$\epsilon = 0.2$（`DRL_PPO_CLIP_EPS`）。$\log\pi(a|s)$ 為 Dirichlet 分佈 log 機率：
$a \sim \text{Dir}(\alpha),\ \alpha_i = \pi_{\theta,i}(s)\times K_t$。沒有 `behavior_logp` 的經驗
（BSR 啟發式階段寫入的舊資料）只訓練 Critic、不更新 Actor。

**(e) Entropy 正則化**：$\beta_t = \max(0.001,\ 0.01\times 0.997^t)$（`ENTROPY_COEFF_*`），
$t$=`train_steps`；當前批 entropy < −5.0（策略近乎確定性、可能崩潰）時強制拉高：
$\beta_t \leftarrow \max(\beta_t,\ 0.1\times|H_{current}|/5.0)$。

### 2.6 Dirichlet 集中度退火與確定性評估

推論時的隨機策略集中度隨訓練步數指數退火：

$$K_t = K_{min} + (K_{max}-K_{min})\times(1-e^{-t/\tau}),\qquad K_{min}=5,\ K_{max}=50,\ \tau=5000\ \text{步}$$

$t\to\infty$ 時 $K_t\to 50$，採樣雜訊隨訓練收斂變小但不會消失。**`DRL_DETERMINISTIC=1`**
（2026-09-27 新增，只用於凍結模型評估/量測，訓練時不開）：跳過 Dirichlet 採樣，直接取
$a_i = \pi_{\theta,i}(s)$（Dirichlet 期望值）當份額。動機：`relative` 上限的
$\min(1, n\cdot a_i)$ 對採樣雜訊是非對稱損耗——採樣偏高被夾在 1.0（浪費，沒有額外好處），採樣
偏低直接降低該 UE 的上限（扣掉吞吐量），雜訊本身、與策略是否收斂無關，會系統性拉低平均送達
量。訓練邏輯（隨機採樣、`behavior_logp`、PPO 裁剪）完全不受影響。

### 2.7 訓練資料管線與週期

- **經驗寫入**：每次收到 xApp 請求即產生一筆經驗（含 `state_vec`、`action`（原始 PRB 分配）、
  `reward`、`next_state_vec`、`behavior_logp`、`is_idle`……），背景執行緒每 1 秒批次 flush 進
  MongoDB `node{N}_experiences`。
- **背景訓練（`_train_worker`）**：每 `TRAIN_INTERVAL_S=60` 秒一輪，`fetch_experiences()` 讀最新
  `TRAIN_FETCH_LIMIT=5000` 筆（≈83 分鐘，i.i.d. 打散抽樣、不要求時間連續性），8:2 切
  train/test（以單筆經驗為單位），`TRAIN_EPOCHS_PER_ROUND=10` 個 epoch，`TRAIN_BATCH_SIZE=128`
  一次梯度更新，最後在 test set 評估（偵測 overfitting）。首次訓練門檻
  `MIN_TRAIN_EXPERIENCES=200` 筆。
- **推論模式切換**：`train_steps ≥ MIN_DRL_TRAIN_STEPS=100`（含 FL 客戶端的訓練步數）才從 BSR
  啟發式（$PRB_i \propto \Delta TBS_i \times (1+0.2\,MCS_i/28)$，每 UE 保底 5 PRB）切到 DRL 推論；
  未達門檻或 ZMQ 逾時時退回啟發式/fallback。
- **執行緒/CPU**：每個 process 限 1 個 torch/BLAS 執行緒（12 個 inference + 12 個 FL ClientApp
  共用 cpuset 12-15），訓練起始依 `node_id` 錯開 ~5 秒，避免拖長推論延遲超過 xApp 的 5ms 逾時。

---

## 3. Global xApp（`global_xapp.py`，獨立 Python process，Stage 2~5 全程固定存在）

**職責**：提供純軟性 state 特徵 $\hat{f}$（§2.1），不做任何硬性 PRB 裁切；與 C 層 Backhaul-aware
機制作用在不同軸，不會衝突（後者依「該節點自己 MT 的忙碌度」硬性縮小資源池，是排程器的輸入約
束；Global xApp 依「全域相對落後程度」提供 Actor 看不到的全域視角）。

每 `GLOBAL_XAPP_INTERVAL_S=2` 秒，讀 MongoDB 全部 12 個節點最近 `GLOBAL_XAPP_LOOKBACK=300` 筆
非閒置經驗（≈5 分鐘）的 $R_{tp}$ 平均值：

$$\bar{T}_i = \text{mean}\big(r\_throughput_{i,\,\text{最近 300 筆}}\big)$$

**同角色內比較**（2026-09-26 起，relay=Node1~4、access=Node5~12 分開算，避免結構性落差被誤判成
不公平）：

$$\bar{T}_{\text{role}(i)} = \text{mean}\big(\{\bar{T}_j : \text{role}(j) = \text{role}(i)\}\big)$$

$$f_{raw,i} = \text{clip}\!\left(\frac{\bar{T}_{\text{role}(i)}}{\bar{T}_i + \epsilon},\ 0.5,\ 2.0\right),\qquad \epsilon=10^{-6}$$

節點吞吐量低於同角色平均 → $f_{raw,i} > 1$（被犧牲，可以更積極）；高於平均 → $f_{raw,i} < 1$。
透過 ZMQ PUB（`tcp://127.0.0.1:5560`，topic `node{i}`）廣播給對應的 Local rApp，
`_fairness_sub_worker()` 接收後供 `encode_state()` 取用（§2.1 的 $\hat{f}$）。

**監控用全網 JFI**（不寫回 MongoDB、不納入聚合權重，只印 log）：對角色正規化後的吞吐量
$x_i = \bar{T}_i / \bar{T}_{\text{role}(i)}$ 算 Jain's Fairness Index
$\text{JFI} = (\sum x_i)^2 / (n \sum x_i^2)$，消除 relay/access 的結構性差距後純看節點間離散度。

---

## 4. Global rApp（Flower ServerApp/ClientApp，`FL_MODE=avg`，Stage 2 現行）

### 4.1 架構

`flower-superlink` + 12 個 `flower-supernode-node{1..12}`（`--isolation subprocess`，各自獨立
OS process，與 `inference-nodeN` 只透過磁碟 `model_nodeN.pt` 同步）+ `flower-scheduler` 每
`FL_ROUND_INTERVAL_S=180` 秒觸發一輪 `flwr run`。

### 4.2 Client 端（`client_app.py::train()`，每輪每節點）

1. `agent.load()` 讀回本節點目前的 optimizer/train_steps 狀態。
2. 套用這一輪收到的全域權重（只覆蓋 actor/critic 權重，optimizer 狀態保留）。
3. 立刻 `agent.save()` 一次（即使後面本地微調失敗也能保住這輪聚合結果）。
4. `training_pipeline.run_training_round()` 在本地經驗上微調（同 §2.7 的訓練流程），成功則再
   `agent.save()` 一次。
5. 回傳更新後的權重 + `num-examples`：

$$\text{num-examples}_i = \text{count\_contended}(\text{train\_exp}_i)$$

即該節點這一輪**可更新 Actor 的壅塞訓練樣本數**（§2.5(c) 的定義，只計有 `behavior_logp` 者），
不是全部訓練樣本數——沒壅塞的節點 Actor 沒被更新，`num-examples=0`，FedAvg 權重也是 0（但仍會
收到聚合後的權重）。任一環節失敗（MongoDB／訓練）都降級為「回傳未修改權重、num-examples=0」，
不拋例外阻塞這一輪 FL。

### 4.3 Server 端聚合（`IABFedAvg`，標準 FedAvg）

$$W_{\text{global}} = \frac{\sum_{i=1}^{12} n_i \cdot W_i}{\sum_{i=1}^{12} n_i},\qquad n_i = \text{num-examples}_i$$

（$W$ 為攤平後的 actor+critic state\_dict，逐 key 加權平均）。全部節點 $n_i=0$（例如 MongoDB
剛清空、尚無足夠壅塞資料）時，`aggregate_arrayrecords()` 的 $\sum n_i = 0$ 會觸發除以零，
`IABFedAvg.aggregate_train()` 攔截該例外，跳過本輪聚合（等同 no-op，沿用上一輪權重），不讓
`flower-scheduler` 崩潰。

聚合後的權重廣播回 12 個節點（`_apply_weights_to_node()`：先 `agent.load()` 讀回目前
`train_steps`/optimizer 狀態，只覆寫 actor/critic 權重，再 `agent.save()` 原子寫入），對應
`inference-nodeN` 的 `_reload_worker` 每 30 秒輪詢 `model_nodeN.pt` 的 mtime，偵測到變化即熱重
載進記憶體——FL 聚合結果最多延遲 30 秒才反映到近即時推論，相對 180 秒的 FL 輪次週期可忽略。

### 4.4 搭配 Local DRL v3.3 的現行設定（2026-10-04，24 UE 拓樸）

- **Local 端**：與 Stage 1.5（`FL_MODE=none`）逐行相同——`LOCAL_DRL_V2_DESIGN.md` v3.4（子樹 α-fair reward α=0.2、兩段式動作、
  6 檔遮罩、動作持續 5 秒、PPO γ=0.5＋3 步回報、Critic 暖身 600 步）；`inference-nodeN` 的本地訓練（每 60 秒 15 次更新）照常進行，
  FL 是疊在上面的第二條更新路徑。Global xApp（`fairness_bias`）在 Stage 1.5 與 Stage 2 都開，兩者 state 相同。
- **FL 容器的設定必須與 inference 一致**（2026-10-04 修正）：ClientApp 與 ServerApp 都會建 `DRLAgent`、ClientApp 經
  `fetch_experiences()` 重算 α-fair reward 並併合動作持續的經驗，所以 `flower-supernode-nodeN`／`flower-superlink` 也要拿到
  `DRL_ACTION_SPACE`、`DRL_MASK_TIERS`、`DRL_ALPHA`、`DRL_GAMMA_MLP`、`RFSIM_SPEED`、`DRL_ACTION_HOLD`、`DRL_NSTEP` 等（compose 已補）。
  修正前 supernode 會用程式預設的 21 檔選單建出不同形狀的 `tier_head`（聚合直接失敗）、用 α=0.5 算 reward。
- **聚合權重** $n_i$＝這一輪可更新 Actor 的「有競爭」**決策**經驗數（動作持續併合後計數）。
- **為什麼 relay 與 access 共用一個全域模型合理**：兩層「該遮」的條件是同一個規律——同節點有 MCS 較高的子節點在積壓（relay：
  子節點 MT；access：好通道 UE）時，遮 MCS 低的子節點；Actor 的每子節點特徵含 `is_iab_child`，可區分 MT。FedAvg 把 4 個 relay
  與 8 個 access 遇到的稀有決策狀態合在一起學（每個 access 節點約 3% 的時間是 M 類，見 `LOCAL_DRL_V2_DESIGN.md` §9）。
- **Stage 2 的兩個目標與對應量測**：
  1. **以 FL 補資料量**：同樣 3 小時、同樣從零開始，比較 Stage 2 與 Stage 1.5 的策略分化（relay 該遮 vs 不該遮、access M vs N 的
     介入機率，`iab/policy_prob.py`）與 90／180 分鐘實測（HS TCP、seed 20260930、只跑壅塞相位）。
  2. **跨節點的資源分配**：relay 的 reward 是子樹（自己直連 UE＋下游 access UE）效用，state 含 parent 佇列 $\hat p$、children
     需求 $\hat c$ 與 Global xApp 的 `fairness_bias`；relay 遮自己的邊緣 UE 讓 slot 給 backhaul（子節點 MT）就是跨節點的資源重新分配。
     量測看熱點 branch 下游 access UE 的吞吐量相對 PF／Stage 1.5 的變化。
- **流程**：`/home/lindor/s2_avgfl_20261004/run.sh`（同 Stage 1.5 腳本，只差 `FL_MODE=avg`、帶起 `stage2-fl` profile；實測期間停
  `flower-scheduler`、模型凍結）。不用 `iab/run_stage2_fl.sh`（它會刪除 checkpoint 與經驗；本流程改名封存）。

（`FL_MODE=capa`／`elastic` 分別是新 Stage 3（CAPA-Fed）／新 Stage 4（ERA-Fed）專用，公式與
設計見 `STAGE4_CUSTOM_FL_DESIGN.md` §10／§11；`server_app.py` 依環境變數擇一 instantiate，
不影響 Stage 2 的路徑。）

---

## 5. 參數總表

| 參數 | 數值 | 來源 |
|---|---|---|
| 控制/觀測週期 | 1 秒（實測） | xApp Rate Limiter |
| 系統 PRB 總數 | 106 | `total_prb` |
| 最小 PRB 保底 | 5 | `MIN_PRB` |
| ZMQ REQ 逾時 | 5 ms | `xapp_nodeN.c` |
| State 維度 | 51 | `STATE_DIM` |
| $B_{max}$（reward／state 共用） | 2,000,000 bytes/視窗 | `MAX_BSR` |
| $Q_{max}$ | 2,000,000 bytes | `MAX_BUF_INFO` |
| `DRL_CAP_MODE` | `relative`（預設） | 2026-09-27 |
| `DRL_DETERMINISTIC` | 0（訓練）／1（凍結評估） | 2026-09-27 |
| $\gamma$（Critic TD target） | 0（`DRL_GAMMA_MLP`） | 2026-09-26 |
| Advantage clip | ±3 | `ADV_CLIP` |
| 壅塞門檻 $\tau$ | 100,000 bytes | `CONTENDED_BUF_BYTES` |
| 每批最少壅塞樣本 | 8 | `MIN_CONTENDED_SAMPLES` |
| PPO clip $\epsilon$ | 0.2 | `DRL_PPO_CLIP_EPS` |
| Dirichlet $K$ | 5 → 50，$\tau=5000$ 步 | `DIRICHLET_K_{MIN,MAX}`／`_TAU` |
| Actor/Critic lr | 3e-4 / 3e-4 | `DRL_LR_ACTOR`／`LR_CRITIC` |
| DRL 切換門檻 | train_steps ≥ 100 | `DRL_MIN_TRAIN_STEPS` |
| 背景訓練週期 | 60 秒 | `TRAIN_INTERVAL_S` |
| 訓練回放緩衝區 | 最新 5000 筆 | `TRAIN_FETCH_LIMIT` |
| Global xApp 週期／視窗 | 2 秒／最近 300 筆 | `GLOBAL_XAPP_INTERVAL_S`／`_LOOKBACK` |
| $f_{raw}$ 值域 | [0.5, 2.0] | `FAIRNESS_BIAS_{MIN,MAX}` |
| FL 輪次週期 | 180 秒 | `FL_ROUND_INTERVAL_S` |
| FL 熱重載輪詢 | 30 秒 | `RELOAD_POLL_INTERVAL_S` |

---

## 6. 與其他設計文件的關係

- **`LOCAL_DRL_V2_DESIGN.md`**：**現行 Local 層設計**（取代本文件 §1/§2），Stage 2~5 全程共用。
- `DRL_DESIGN.md`：更早期的 GRU／Lagrangian 分支歷史推導（比 §1/§2 更舊，供之後改良版/消融
  實驗參考，非現行路徑）。
- `STAGE4_CUSTOM_FL_DESIGN.md`：新 Stage 3（Hierarchical FedAvg+Hedge）／新 Stage 4（HiRA-Fed）
  的完整設計（§10/§11），CAPA-Fed／ERA-Fed 已移至該文件附錄，僅供歷史參考。
- `avgFL.md`：**已刪除**（2026-09-29 全面重新設計時清理，舊 Local DRL 設計的量測結果已不適用，
  過程見 `HISTORY.md` 續三十六）。
- `HISTORY.md`：逐日除錯記錄；續三十六是本次全面重新設計的決策過程與文獻依據。
