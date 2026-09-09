# Local xApp DRL 設計文件

## 1. 系統定位

每個 IAB Node（共 5 個）部署一個獨立的 **Local xApp**，負責該節點的下行 PRB 資源分配。
OAI MAC 排程週期是 **10 ms**，但 C xApp 有 Rate Limiter（每 10 個 MAC callback 才觸發
一次 ZMQ），所以 Python 推論伺服器實際感受到的**有效控制週期是 100 ms**，不是 10ms——
這點會直接影響 state 裡 `Δtbs` 的量測窗口（見 §2.1）與 MongoDB 經驗的寫入頻率（見 §8）。

```
OAI gNB (C 語言)
  └─ MAC indication (每 10ms，Rate Limiter 每 10 次才觸發一次 ZMQ = 每 100ms)
       └─ xApp (C / FlexRIC)  ──ZeroMQ REQ/REP──  Inference Server (Python)
            └─ MAC_CTRL_REQ                              └─ DRL Actor Network（drl_agent.py）
                 └─ PRB 分配寫回 OAI MAC 層                   └─ MongoDB (experience 儲存)
                                                              └─ Flower ClientApp（Phase 5，
                                                                 共用同一份 checkpoint，
                                                                 見 §6.1、§9）
```

---

## 2. State Space（狀態空間）

### 2.1 原始觀測量（C 端 MAC indication）

| 欄位 | OAI 結構體欄位 | 說明 | 備註 |
|---|---|---|---|
| **Δ DL TBS** | `dl_aggr_tbs` 差分 | 每 **100ms** 實際傳送的 DL bytes | C xApp Rate Limiter：每 10 個 MAC callback 才觸發一次 ZMQ，測量窗口為 100ms |
| **DL MCS** | `dl_mcs1` | 下行 Modulation and Coding Scheme index (0–28) | 通道品質代理指標 |
| **DL Buffer Occupancy** | `dl_buffer_info` | 真實 RLC 佇列位元組數（`sched_ctrl->num_total_bytes`） | 2026-07-09 新增，見下方說明 |

> **已修正（原文件曾誤導）**：舊版文件宣稱 OAI RF Simulator 的 `dl_buffer_info` 與 `wb_cqi`
> 「恆為 0」，因此只用 `dl_aggr_tbs` 差分與 `dl_mcs1` 兩個欄位。實際查證（讀 OAI C 端原始碼 +
> 現場 MongoDB 資料）發現：
> - `wb_cqi`（真 3GPP CQI，非 `dl_mcs1`）確實恆為 0，這是 RF Simulator 本身不計算真實
>   通道傳播的限制，不是程式碼問題。
> - `dl_buffer_info` 其實**沒有**恆為 0——這個欄位在 C 端已經完整打通 E2SM-MAC 的
>   encode/decode pipeline，只是先前 xApp 端的 JSON 序列化沒有把它抓出來送給 Python
>   （一行程式碼等級的缺漏）。補上後現場驗證：UE 真閒置時 99% 讀到 0，有資料排隊時讀到
>   有意義的非零值（見 §2.2）。

> **`Δtbs` 與 `dl_mcs1` 的共同盲點**：OAI 排程器在 UE 的 RLC buffer（`num_total_bytes`）為 0
> 時會直接跳過該 UE（`gNB_scheduler_dlsch.c`），完全不進入 MCS 選擇/PRB 分配邏輯。這代表
> **`dl_aggr_tbs` 與 `dl_mcs1` 在 UE 閒置時會同時凍結在舊值**，無法區分「無資料可傳」與
> 「有資料但通道差/PRB 不足」——這正是新增 `dl_buffer_info` 的原因，它不受「是否被排程」
> 影響，是獨立的需求訊號。

> **流量方向前提（已修正誇大敘述）**：`Δ DL TBS` 有意義的前提是**流量方向必須是 DL**
> （`iperf3 -R`，ext-dn → UE）——若誤用 UL 方向，`dl_aggr_tbs` 差分**永遠**是 0，reward 才會
> 真的退化成常數，這是唯一的硬限制。舊版文件同時宣稱「頻寬需遠大於通道容量、確保 DL
> buffer 始終滿載」，把這點寫得像同一個硬限制的延伸，但查證後發現這只是被誇大的推論
> （4/27 commit 純文字筆記、無實驗佐證，且與 5/12 commit 已加入的空閒狀態特判自相矛盾，
> 見 §5.2）。訓練資料現在**刻意**混入間歇性流量（見 `traffic_scenario.py` Scenario R 的
> `P_IDLE` 機制），讓 policy 也能學會處理真實世界常見的非飽和流量情境。

> **Scenario R 頻寬範圍（`REALISTIC_BW_MIN_MBPS`／`REALISTIC_BW_MAX_MBPS`，2026-07-09 現場測試訂出）**：
> 訓練流量的目標頻寬（iperf3 `-b` 參數）現在是 `[5, 50]` Mbps 的 lognormal 抽樣
> （median≈11、mean≈12.2，詳見 `traffic_scenario.py` 常數區塊註解），不是文件過去暗示的
> 固定高頻寬。這個範圍是實測校準出來的，不是憑感覺選的：
> - **單一 UE、最佳通道（path_loss=0dB）下，掃描目標頻寬 5~200 Mbps，實測吞吐量天花板
>   卡在 ~8-12 Mbps，不管目標設多高都一樣**——這是 testbed 本身單一 TCP 串流 + RTT 的
>   物理限制，不是 OAI 排程器的問題（跟 CLAUDE.md §8 記錄的 4/20 PF baseline 逐 UE 數字
>   高度吻合，6.81~11.35 Mbps）。
> - 同一次測試也證實「目標頻寬超過 50 Mbps 會讓 DU 崩潰」的舊說法**不成立**——測到
>   200 Mbps，DU 全程穩定運行，沒有任何 crash/assert。
> - 既然目標頻寬只要略高於這個 ~10 Mbps 天花板就足以讓 RLC buffer 持續有積壓（`Δtbs`
>   訊號才有意義、`dl_buffer_info` 才會讀到非零值），下限沒必要設到舊版的 25 Mbps 那麼高，
>   下修至 5 Mbps 讓分佈能涵蓋真實世界常見的低需求情境（視訊通話、輕度瀏覽），跟 `P_IDLE`
>   的真閒置機制互補，組成「真閒置 → 低需求 → 中等需求 → 偶爾高需求」的完整光譜。
> - 上限維持 50 Mbps 不變：拉高上限不會讓「實際吞吐量」更極端（反正天花板卡在
>   ~10Mbps），只會讓 buffer 堆積更誇張，而 `MAX_BUF_INFO=2,000,000` 這個正規化常數
>   是照目前範圍現場校準的（見下方），拉高上限會撞上這個常數、需要重新校準+重訓，
>   不值得為此付出成本。

### 2.2 State 向量編碼

State 為固定長度 **50 維**的 float32 向量（MAX\_UE\_COUNT = 16，2026-07-09 同日先從
33 維擴充到 49 維加入 `dl_buffer_info`，稍晚再加一維 `prb_quota_ratio` 到 50 維，
見下方說明）：

```
state_vec = [
    norm_tbs_0,  norm_mcs_0,  norm_buf_0,   # UE slot 0
    norm_tbs_1,  norm_mcs_1,  norm_buf_1,   # UE slot 1
    ...
    norm_tbs_15, norm_mcs_15, norm_buf_15,  # UE slot 15（不足補 0）
    active_ratio,                            # 全域 context 特徵
    prb_quota_ratio                          # 全域 context 特徵（Global xApp 回傳配額）
]
```

| 特徵 | 正規化方式 | 範圍 |
|---|---|---|
| `norm_tbs_i` | `log(1 + Δtbs_i) / log(1 + MAX_BSR)`，`MAX_BSR=1,000,000` | [0, 1] |
| `norm_mcs_i` | `mcs_i / 28.0` | [0, 1] |
| `norm_buf_i` | `log(1 + buf_i) / log(1 + MAX_BUF_INFO)`，`MAX_BUF_INFO=2,000,000` | [0, 1] |
| `active_ratio` | `n_active / 16` | [0, 1] |
| `prb_quota_ratio` | `1.0`（relay node，恆不受限）／`prb_quota / 106`（access node） | [0, 1] |

- `Δtbs`／buffer occupancy 都使用 **log 正規化**，壓縮大數值差距；`MAX_BUF_INFO` 取
  2,000,000 是現場實測（2026-07-09，Scenario R，**當時頻寬範圍還是舊版 `[25,50]`
  Mbps**）node3/5 最大值約 2,147,000 訂出來的，跟 `reward_calculator.py` 既有的
  `MAX_BSR=2,000,000` 常數同量級。同一天稍晚頻寬下限改成 `[5,50]`（見 §2.1），
  整體需求壓力只會變低不會變高，這個常數邏輯上仍是安全的上界，未重新校準
- MCS 使用**線性正規化**，MCS=0 合法（反映最差通道品質）
- 非活躍 UE 的 slot 填 0，並透過 **mask** 在 softmax 中遮蔽
- **`prb_quota_ratio`（2026-07-09 新增，Global 配額特徵）**：在此之前，Global xApp
  的回傳配額只在 Actor 輸出「之後」拿來裁切分配（見 §3.1 的 Phase 5a 邏輯），Actor
  本身完全不知道配額限制存在。加入這個特徵後，`InferenceServer._current_quota_ratio()`
  在 `infer()`／`encode_state()` 每個呼叫點都算好比例傳入，讓 Actor 能在輸出分配「之前」
  就感知配額緊繃程度，提前學會避免過度分配（而不是每次都被動裁切、浪費探索）。
  relay node（Node1/2）不受配額限制，恆為 1.0；access node（Node3/4/5）依
  `_quota_sub_worker()` 收到的最新配額換算。`DRLAgent` 本身不需要知道 relay/access
  的區別，這個職責劃分維持在 `InferenceServer` 層。
- **破壞性變更**：State 維度改變會讓所有舊 checkpoint 的 Actor/Critic 第一層權重
  input_dim 對不上，2026-07-09 已清空 MongoDB 經驗與 checkpoint、從隨機初始化重新訓練
  （此次連同下方 §4 的 GRU 架構改動一起做，兩者都要求同一次「清空重來」）

---

## 3. Action Space（動作空間）

### 3.1 動作定義

Actor Network 輸出每個 UE 的 **PRB 分配比例**（經 Masked Softmax），再乘以系統 PRB 總數轉換為絕對 PRB 數量。

| 參數 | 數值 |
|---|---|
| 系統 PRB 總數 | 106（對應 100 MHz 頻寬） |
| 最小 PRB 保底 | **5** PRB / 活躍 UE（`MIN_PRB_PER_UE`／`MIN_PRB`，防止 MAC 層 SIGSEGV；DRL 與啟發式兩條路徑一致） |
| Action 輸出維度 | 16（MAX\_UE\_COUNT） |
| 非活躍 UE | Mask 遮蔽（logit = −∞，softmax 輸出 ≈ 0） |

> **Phase 5 額外限制（Global xApp 回傳配額）**：Node3/4/5（access node）在完成 PRB 分配後，
> 若收到 Global xApp 下發的配額 `quota < 106`，會依比例裁切超額分配，讓實際下發的
> `sum(prb_abs) ≤ quota`（見 `inference_server.py` 主迴圈的 Phase 5a 邏輯），模擬 in-band
> IAB 回傳瓶頸。這一步發生在 Actor 輸出之後、寫回 OAI 之前，reward 仍以裁切後的實際
> 分配計算。**2026-07-09 起**，`prb_quota_ratio` 已加入 state（見 §2.2），Actor 事前就能
> 感知配額緊繃程度，這裡描述的裁切邏輯本身沒有改變，只是不再是 Actor「唯一」得知配額
> 限制的管道——裁切仍然保留，作為 Actor 輸出誤判時的硬性安全網。

### 3.2 PRB 分配計算流程

```
Actor logits (16 維)
  → masked_fill(非活躍 slot, -∞)
  → Softmax → 比例向量 (加總 = 1.0)
  → × 106 → 浮點 PRB 數
  → 取整 + 餘數補最大分數 UE
  → 每個活躍 UE 至少 1 PRB
  → MAC_CTRL_REQ 寫回 OAI
```

---

## 4. 神經網路架構

### 4.0 為什麼改成 GRU（2026-07-09）

舊版 MLP 是**無記憶的單步快照**：每次 `infer()` 只看當下這一筆 `state_vec`，
完全不知道前一步、前十步發生過什麼。這在 Scenario R 這種帶隨機閒置
（`P_IDLE`）與隨機頻寬需求的訓練場景下，會讓 policy 無法區分「剛進入閒置」
「已經閒置很久」「即將恢復流量」這幾種軌跡上很不一樣、但單步 state 長得
很像的情境。改用 **GRU（Gated Recurrent Unit）** 讓 Actor/Critic 都具備跨步的
隱藏狀態記憶，屬於 POMDP（部分可觀測 MDP）常見的處理方式——state 本身不再是
充分統計量，用一段歷史的隱藏狀態近似補足。

**Actor／Critic 使用各自獨立的 GRU trunk，不共用權重**：這是刻意的設計選擇，
不是疏漏。共用 trunk 會讓 Critic 的 value loss（無界 MSE，量級常常比 policy
loss 大很多）的梯度污染 Actor 的 policy 學習訊號，這是 actor-critic 方法的
已知風險。兩個 GRU 的參數量差異可忽略（各約 7 萬參數），不共用的成本很低。

### 4.1 Actor Network（Policy Network）

```
Input: state (batch, seq_len, 50), mask (batch, seq_len, 16)
  → GRU(input_size=50, hidden_size=128, num_layers=1)   ← 跨步記憶
  → Linear(128, 128) → ReLU
  → Linear(128, 16)          ← logits
  → Masked Softmax(mask)     ← 非活躍 UE 遮蔽
Output: probs (batch, seq_len, 16), new_hidden (num_layers, batch, 128)
```

`forward(state, mask, hidden=None)` 除了輸出機率分佈之外，也回傳更新後的
GRU 隱藏狀態，供呼叫端決定要不要延續到下一次呼叫（見 §4.3）。

### 4.2 Critic Network（Value Network）

```
Input: state (batch, seq_len, 50)
  → GRU(input_size=50, hidden_size=128, num_layers=1)   ← 獨立於 Actor 的 GRU
  → Linear(128, 64) → ReLU
  → Linear(64, 1)
Output: value (batch, seq_len)，new_hidden (num_layers, batch, 128)
```

**Critic 也要是遞迴的**：state 已經不是充分統計量（這正是加 GRU 的前提），
value function 若維持無記憶會變成比 Actor 的信念狀態更差的 baseline。但
Critic 的隱藏狀態**不需要跨 `infer()` 呼叫持久化**——`infer()` 從來不呼叫
Critic，Critic 的遞迴只在訓練時用，每個訓練序列都從 `hidden=None`（零初始化）
開始（見 §6.1）。

| 超參數 | 數值 |
|---|---|
| Hidden dim（GRU 與 MLP head 共用） | 128 |
| GRU 層數 | 1 |
| Optimizer | Adam |
| Actor learning rate | 1e-4 |
| Critic learning rate | 3e-4 |
| Gradient clipping | 1.0（Actor & Critic） |

### 4.3 推論時的隱藏狀態生命週期

`DRLAgent` 為每個節點維護單一個 `self._actor_hidden`，隨每次 `infer()` 呼叫
更新（`.detach()` 後保存，避免跨呼叫累積計算圖）：

| 事件 | 是否重置 `_actor_hidden` | 原因 |
|---|---|---|
| 正常 `infer()` 呼叫（含閒置片段） | 否，持續累積 | 閒置片段的緩降→持平→恢復軌跡正是要 GRU 捕捉的訊號，人為重置等於把這個訊號丟掉，違背加 GRU 的本意 |
| UE 真正斷線（`ues` 變空） | **是**，`reset_hidden()` | `inference_server.py` 主迴圈既有的「無活躍 UE 時清除暫存」分支（見 §9）順帶呼叫；下一組 UE 跟前一組毫無關聯，不該延續記憶 |
| `agent.load()`（一般啟動或 FL 熱重載） | **是**，`load()` 內部呼叫 | 隱藏狀態是「輸入歷史 + 特定權重」共同決定的產物，換權重不換隱藏狀態等於餵給新網路一個它從沒訓練過要處理的輸入 |
| 每 60 秒背景訓練（`train_on_batch()`） | 否，訓練用的隱藏狀態是獨立的 | 訓練時序列一律從零初始化（見 §6.1），不影響／不讀取推論用的 `_actor_hidden`；若每次訓練都重置推論隱藏狀態，記憶每 ~600 個推論週期就被清空一次，接近失去加記憶的意義 |

**checkpoint 不存隱藏狀態**：`save()`/`load()` 被三種完全不同的呼叫端共用
（in-process 熱重載、FL ClientApp、FL ServerApp 的初始權重載入），這些情境下
「隱藏狀態」沒有一致的意義；訓練時序列一律從零開始，跟推論時的持久隱藏狀態
本來就是不同概念，不應該混在同一個 checkpoint 欄位裡。

---

## 5. 獎勵函數（Reward Function）

### 5.1 現行公式：Lagrangian 限制式（2026-07-09 起）

$$\max_\theta \mathbb{E}[R_{tp}] \quad \text{s.t.} \quad \mathbb{E}[JFI] \geq JFI_{min}$$

throughput 是唯一要最大化的目標，fairness 是**底線**（JFI 不能低於 $JFI_{min}$），
不是追求 JFI 越高越好。透過 Lagrangian 鬆弛落地成無限制問題：

$$\mathcal{L}(\theta,\lambda) = R_{tp} + \lambda \cdot (JFI_{raw} - JFI_{min})$$

$\lambda$（Lagrangian 乘子）**不是手動設的常數**，`DRLAgent.train_on_batch()` 每次
mini-batch 訓練後用下面這條簡單規則自動更新：

$$\lambda \leftarrow \max(0,\; \min(\lambda_{max},\; \lambda + \eta_\lambda \cdot (JFI_{min} - JFI_{observed})))$$

- $JFI_{observed}$：這個 mini-batch 攤平後所有時間步（16 個序列 × 32 步 = 512 筆
  經驗，2026-07-09 GRU 改版前是 128 筆獨立經驗，見 §6.1.1）中，排除閒置樣本
  （`is_idle=True`，見 §8）後的 `jfi_raw` 平均值
- JFI 低於門檻時 → $\lambda$ 變大 → 下次算 reward 時 fairness 懲罰加重，逼 policy 拉回來
- JFI 達標時 → $\lambda$ 變小、趨近 0 → reward 幾乎只剩 throughput，policy 全力衝吞吐量
- $\eta_\lambda$（`LAMBDA_LR`）＝0.02，$\lambda_{max}$（`LAMBDA_MAX`）＝10.0（安全上限，避免
  JFI 持續低於門檻時 $\lambda$ 無界成長、最終讓 fairness 項完全壓過 throughput 項）

**為什麼改用限制式，而不是繼續調整固定權重／退火時程表**：Scenario A/B/C 三個場景需要
的 fairness 強度差異很大（A 需要一路維持高權重才不崩壞，B/C 幾乎不需要），沒有一組
固定權重或固定的退火曲線能同時適應所有場景——這正是 Lagrangian 想解決的問題：$\lambda$
針對每個場景、每個訓練時刻依當下觀測到的 JFI 自動判斷「現在需不需要在意 fairness」，
不需要研究者事先猜一個通用的排程。

**$JFI_{min}$ 的訂定方式（2026-07-09 現場量測，非拍腦袋決定）**：停 xApp（PF 模式）、
保留 Scenario R 持續產生真實隨機流量，每 10 秒採樣一次 6 個 UE 的 `oaitun_ue1` rx_bytes
估算吞吐量，只對「當下有實際流量」的 UE（≥0.5 Mbps）計算 JFI（排除刻意閒置的 UE
稀釋樣本），15 分鐘、82 個有效區間（跨越同時活躍 1~6 個 UE 的各種組合）取平均：

```
JFI_MIN = 0.8291
分布：min=0.6345  median=0.8597  max=0.9533
```

**已排除的替代做法**：直接在動作空間做投影/裁切（Safe RL 的 safety layer 做法）——
需要一個「PRB 分配→預期吞吐量」的預測模型才能在推論當下算出候選分配的 JFI，但系統
目前沒有這種環境動態模型，且推論路徑有 5ms ZMQ 超時的硬性時限，不適合在熱路徑上做
額外最佳化求解。Lagrangian 對推論路徑零額外開銷，只在訓練迴圈多一步純量更新。

**部署時發現的兩個資料管線 bug（2026-07-09，記錄供之後排查類似問題參考）**：
1. **殘留舊維度資料混入訓練**：`state_vec` 曾經從 33 維改成 49 維（見 §2.2 的
   `dl_buffer_info` 加入紀錄），當時清空 MongoDB 沒有清乾淨，殘留 9~80 筆（各節點
   不等）33 維的舊文件混在新資料裡。`train_on_batch()` 隨機抽樣 mini-batch 時只要抽到
   一筆殘留資料，`np.array()` 建構張量就會因為長度不一致直接拋
   `inhomogeneous shape` 例外、整個訓練執行緒崩潰。修法：(a) 手動刪除殘留的錯誤維度
   文件；(b) `train_on_batch()`／`evaluate_on_batch()` 都新增防禦性過濾，事後只要再
   混進格式不對的資料就自動濾掉、記警告 log，不會再讓整個訓練執行緒崩潰。
2. **MongoDB projection 漏掉新欄位**：`training_pipeline.py` 的 `run_training_round()`
   用 `find(..., projection={...})` 明確指定只回傳哪些欄位（白名單），新增 `jfi_raw`
   欄位到 experience document 後忘記把它加進這個白名單，導致查詢回來的經驗完全沒有
   `jfi_raw`，$\lambda$ 更新規則因此永遠找不到樣本、卡在 `LAMBDA_INIT`不動（訓練 log
   持續顯示 `batch_jfi=N/A`）。修法：projection 白名單加上 `"jfi_raw": 1`。**這類
   「明確欄位白名單」的查詢，新增 experience document 欄位時要記得同步檢查有沒有
   類似的白名單需要更新**，是這次踩到的通用教訓。

**Reward 不做 `[-1,1]` 硬裁切**（舊版加權和公式有裁切，這次刻意不裁切）：
`train_on_batch()` 已經對 advantage 做 z-score 標準化，梯度尺度不受 reward 絕對量級
影響；裁切反而會在 $\lambda$ 變大時扭曲懲罰訊號強度，等於讓限制式失效。

### 5.1.1 舊版加權和公式（保留供消融實驗參考，非現行路徑）

$$R = W_{tp} \cdot R_{tp} + W_{fair} \cdot R_{fair} - W_{delay} \cdot R_{delay}$$

| 分量 | 公式 | 說明 |
|---|---|---|
| $R_{tp}$ | $\frac{1}{N}\sum_i \min\!\left(\frac{\Delta TBS_i}{B_{max}},\,1\right)$ | 各 UE 實際 DL 吞吐量平均，正規化至 [0,1] |
| $R_{fair}$ | $\dfrac{(\sum_i \Delta TBS_i)^2}{N \cdot \sum_i \Delta TBS_i^2}$（JFI 再線性縮放至 [0,1]，見下方說明） | Jain's Fairness Index，= 1 表示完全公平 |
| $R_{delay}$ | $\frac{1}{N}\sum_i \text{clip}\!\left(1 - \dfrac{\Delta TBS_i / B_{max}}{PRB_i / PRB_{total}},\;0,\;1\right)$ | PRB 效率懲罰（越高越差） |

| 版本 | $W_{tp}$ | $W_{fair}$ | $W_{delay}$ | 說明 |
|---|---|---|---|---|
| 歷史值（複合 reward） | 0.5 | 0.4 | 0.1 | 提高公平性權重至 0.4，防止 2-UE policy monopoly collapse（見 CLAUDE.md §7） |
| 純 Throughput Ablation（2026-07-06 起） | 1.0 | 0.0 | 0.0 | 刻意拿掉公平性/延遲校正，直接對比 PF 的 sum throughput，驗證了固定權重無法同時適應 A/B/C 三場景的問題，促成改用 Lagrangian（見 CLAUDE.md 純 Throughput Reward Ablation 量測結果） |

`reward_calculator.py` 裡 `W_THROUGHPUT`/`W_FAIRNESS`/`W_DELAY` 常數與 `compute_reward()`
函式**都還在，只是不再是 `inference_server.py` 呼叫的路徑**（現在呼叫的是
`compute_lagrangian_reward()`），保留下來是為了之後如果要做「加權和 vs 限制式」的
消融實驗，不用重寫這段公式。

以現在的程式碼狀態為準，**$R_{fair}$／$R_{delay}$／$JFI_{raw}$ 仍會計算並寫入 MongoDB
供監控**（`r_fairness`／`r_delay`／`jfi_raw` 欄位）。

**$B_{max}$ 有兩個不同的常數，不是同一個值**，文件過去把兩者混為一談：

| 常數 | 所在檔案 | 數值 | 用途 |
|---|---|---|---|
| `MAX_BSR`（drl_agent.py） | `drl_agent.py` | 1,000,000 bytes/100ms | **State 編碼**正規化（`encode_state()` 的 `norm_tbs`），對應峰值 ≈ 80 Mbps |
| `MAX_BSR`（reward_calculator.py） | `reward_calculator.py` | 2,000,000 bytes/100ms | **Reward 計算**正規化（`r_throughput`），對應峰值 ≈ 160 Mbps，實測高負載下 delta_tbs 可達 1~2.5M bytes/100ms |

兩者刻意設為不同值：state 的正規化上限較保守（讓中低吞吐量區間有更好的梯度解析度），
reward 的正規化上限較寬鬆（避免高負載下 `r_throughput` 提早封頂在 1.0 而失去梯度）。

### 5.2 各分量說明

**Reward 計算時序**：reward 在步驟 t 觀測到 S_t 時，使用 **S_t**（curr_ues）而非 S_{t-1}（prev_ues）來計算。原因：S_t 的 `Δtbs` 是 gNB 用 A_{t-1} 排程後的實際產出，代表 A_{t-1} 的真實效果；S_{t-1} 的 `Δtbs` 反映的是 A_{t-2}，與 A_{t-1} 無關。

**R_throughput**：衡量 DL 實際吞吐量。`Δtbs_i` 為 C 端 `dl_aggr_tbs` 的差分值（每 100ms 實際傳輸 bytes，因 Rate Limiter），直接反映 MAC 層真實產出，而非估計值。

**空閒狀態特判**：若所有 UE 的 `Δtbs` 皆為 0（idle，無 DL 流量），`compute_reward_breakdown()`
直接回傳 `reward=0.0`（不進入下方的公平性/延遲計算），避免空閒狀態被 `R_delay` 懲罰污染
訓練資料（早期版本曾經讓 idle 狀態算出 `reward≈-0.2`，混進正常經驗裡影響收斂）。

**R_fairness／JFI_raw**：Jain's Fairness Index（JFI）作用在各 UE 的吞吐量上，自然值域是
$[1/N,\,1.0]$（$N$ = 活躍 UE 數）。`compute_reward_breakdown()` 回傳**兩個版本**：
- `jfi_raw`：原始值，**現行 Lagrangian 公式用這個**（跟 `JFI_MIN=0.8291` 直接比較）
- `r_fairness`：線性縮放至 $[0,1]$ 的版本，只有舊版加權和公式與監控圖表用：

$$R_{fair} = \frac{JFI_{raw} - 1/N}{1 - 1/N}$$

（縮放動機：若不縮放，$N$ 個 UE 裡只有 1 個有流量時 $JFI_{raw}=1/N$ 對舊版加權和公式是
model-dependent 的固定值、缺乏梯度意義；這個問題不影響 Lagrangian 公式，因為 Lagrangian
比較的是 $JFI_{raw}$ 與 $JFI_{min}$ 的差距，不需要先縮放到 $[0,1]$。）

論文優化目標仍是 JFI 盡量接近或超過 PF baseline（CLAUDE.md §8 的舊全域基準 0.924，
或 2026-07-09 現場量測、Scenario R 條件下的 0.8291，見 §5.1），但 Lagrangian 公式的
語意稍有不同：目標不是「JFI 越高越好」，而是「JFI 不低於門檻，其餘全力衝 throughput」。

**R_delay（PRB 效率懲罰）**：本研究以 **PRB 效率**作為延遲代理指標：

$$\text{efficiency}_i = \frac{\Delta TBS_i / B_{max}}{PRB_i / PRB_{total}}$$

- efficiency ≥ 1：UE 以少量 PRB 達到高吞吐 → 懲罰 = 0（不額外懲罰）
- efficiency << 1：UE 佔用大量 PRB 但產出低吞吐（差通道 / 過度分配） → 懲罰趨近 1，反映其他 UE 因等待 PRB 而積累的排隊延遲

> **已修正**：本節原本寫「由於 `dl_buffer_info` 恆為 0，無法直接量測排隊延遲」，這是錯誤
> 認知（見 §2.1）——`dl_buffer_info` 現在已經證實可用，理論上可以直接拿真實佇列位元組數
> 當延遲代理，比 PRB 效率這個間接指標更準確。**但 `reward_calculator.py` 目前尚未做這個
> 修改**（`R_delay` 仍是上面的 PRB 效率公式，不是本次改動範圍），是否要把 `R_delay` 換成
> 直接用 `dl_buffer_info` 是後續可以評估的方向，待評估後再決定。

### 5.3 Reward 數值範圍

**現行 Lagrangian 公式（$R = R_{tp} + \lambda \cdot (JFI_{raw} - JFI_{min})$）下**，reward
的範圍會隨 $\lambda$ 動態改變，不是固定的：

| 情境 | 估計 R 值 |
|---|---|
| 無 DL 流量（idle） | 0.0（特判提前回傳，不套用 Lagrangian 項，見 §5.2） |
| $\lambda=0$（限制式已滿足一段時間） | $R \approx R_{tp} \in [0,1]$，退化成純 throughput，不受公平性拖累 |
| $\lambda>0$ 且 $JFI_{raw} < JFI_{min}$（違反限制） | $R < R_{tp}$，$\lambda$ 越大懲罰越重，$\lambda$ 沒有上限裁切（見 §5.1 的 $\lambda_{max}=10$ 安全上限）可能讓 $R$ 明顯小於 0 |
| $\lambda>0$ 且 $JFI_{raw} > JFI_{min}$（超額公平） | $R > R_{tp}$，額外獎勵超出門檻的公平性 |

**2026-07-09 部署後現場觀測**（訓練早期，policy 尚未收斂）：$\lambda$ 從 `LAMBDA_INIT=0`
開始，因為早期 policy 的 $JFI_{raw}$（觀測到 0.07~0.62 左右，遠低於 $JFI_{min}=0.8291$）
持續違反限制，$\lambda$ 在數十個 mini-batch 內已經從 0 漲到 0.05~0.28 左右（各節點不同），
符合設計預期——早期 throughput-only policy 傾向集中資源在少數 UE，機制正確偵測到並
開始加重公平性懲罰。

**已修正的舊敘述**：本節原本描述的是純 Throughput Ablation（$W_{tp}{=}1.0$ 加權和公式）
下的數值範圍，現在已不是現行公式，相關內容移至 §5.1.1。

---

## 6. 訓練演算法（Offline A2C）

### 6.1 訓練流程

本系統採用**離線 Advantage Actor-Critic（Offline A2C）**，與標準 online A2C 的差異在於訓練資料來自 MongoDB experience replay，而非即時 rollout。

**2026-07-09 起，改成序列化訓練**：GRU 需要序列內部真的有時間上的先後關係才有
記憶可學，不能再像舊版 MLP 那樣打散抽樣獨立經驗。訓練資料的抓取單位從「一筆
經驗」改成「一段時間連續的經驗序列」（見下方 §6.1.1）。

```
每 100ms（推論迴圈，C xApp Rate Limiter = 10 × MAC callback）：
  state_t → Actor(GRU, 延續 _actor_hidden) → action_t（PRB 分配）→ 寫回 OAI
  下一個 100ms：state_{t+1}
  計算 reward_t → 寫入 MongoDB 批次緩衝區（背景執行緒每 1 秒 flush 一次，含閒置轉換，見 §8）

每 60 秒（背景訓練執行緒 _train_worker）：
  若 MongoDB 筆數與上一輪相同（無新資料）→ 跳過，防止在 stale 資料上反覆訓練
  fetch_sequences()：從 MongoDB 讀取最近 ≤2000 筆 experiences（TRAIN_FETCH_LIMIT），
    依時間連續性切成「運行」，再切成 TRAIN_SEQ_LEN=32 步、彼此不重疊的定長序列
  以「整個序列」為單位 8:2 切分 train/test（避免時間步層級的洩漏，用於 overfitting 偵測）
  10 個 epoch，每個 epoch 從候選序列池抽樣 TRAIN_SEQ_COUNT=16 個序列各做一次梯度更新
  於 test 序列上評估（不更新梯度）；test_actor_loss − train_actor_loss > 0.3 觸發 ⚠ OVERFIT 警告
  agent.save()：原子寫入 model_nodeX.pt（temp file + os.replace()，見 §10）
```

#### 6.1.1 時間連續性偵測（不需要新的 MongoDB schema 欄位）

`training_pipeline.py` 的 `_is_contiguous(doc_a, doc_b)` 用「`doc_a["next_state_vec"]`
是否逐位元組等於 `doc_b["state_vec"]`」判斷兩筆經驗是不是真正連續：對於沒有中斷的
兩筆經驗，這兩個向量是同一次 `encode_state()` 呼叫算出來的，理應完全相同；若中間
發生跳過寫入（例如舊版的閒置轉換過濾，或 ZMQ 逾時造成的斷點），這個相等關係就會
斷掉。這比額外加一個序號欄位更精確，也不需要改動既有的 MongoDB schema。

`fetch_sequences()` 的完整流程：
1. 依 `timestamp` **升序**（訓練要看時間先後，不是舊版打散抽樣的降序取最新）抓最近
   `fetch_limit` 筆
2. 用連續性判斷式切成一段一段的「連續運行」（run）
3. 每段長度 ≥ `TRAIN_SEQ_LEN` 的 run，切成**不重疊**的定長視窗（`stride=TRAIN_SEQ_LEN`）
4. 所有視窗匯集成候選序列池，回傳給呼叫端

不重疊視窗（而非滑動視窗）是刻意的：訓練要以「整個序列」為單位做 train/test 切分
避免洩漏——重疊視窗會共享大部分時間步，會讓切分後的 overfitting 偵測失真。

`TRAIN_SEQ_LEN=32` 步（~3.2 秒）遠小於一個流量相位的典型長度（Scenario R 預設
`--phase-duration 60` 秒），確保序列不會跨越相位邊界內部；`TRAIN_SEQ_COUNT=16`
刻意抓得比舊版 `TRAIN_BATCH_SIZE=128` 大（16×32=512 筆原始經驗才夠一次梯度更新），
因為序列內部的樣本彼此時間相關（不像舊版打散抽樣是獨立的），需要更多原始經驗才能
得到同樣品質的梯度估計，這是 BPTT-based RL 的標準做法。

#### 6.1.2 序列 Critic 前向傳播的效能/正確性優化

`train_on_batch()`／`evaluate_on_batch()` 善用連續性保證：把每個長度 `L=32` 的視窗
延伸成長度 `L+1` 的狀態序列（`L` 個 `state_vec` + 該視窗最後一筆的 `next_state_vec`，
不需要重新編碼，直接沿用既有資料），Critic GRU **只跑一次**：
`value_seq[:, :L]` 當 `current_values`、`value_seq[:, 1:].detach()` 當
`next_values`。這既是效能優化，也是理論正確的寫法——$V(s'_t)$ 應該是「包含
$s'_t$ 的完整歷史」的函數，剛好就是 `value_seq` 往後移一位。Actor 只需要看 `L`
個實際造訪過的 state（維持既有「不評估 $\pi(a|s')$」的 1-step actor-critic 設計，
不擴大範圍）。

**這次刻意維持 1-step TD，不做 GAE/N-step**：這是有序列基礎設施之後很自然可以
做的下一步，但這次刻意不做，避免範圍蔓延；這次建的序列基礎設施是之後要做 GAE
的前提，不會白做。

**Phase 5 之後，訓練迴圈的實際程式碼被抽到共用模組 `training_pipeline.py` 的
`run_training_round()`**，`inference_server.py` 的 `_train_worker`（本節，每 60 秒）與
Phase 5 的 Flower `ClientApp`（`flower-app/iab_fl/client_app.py` 的 `@app.train()`，
每小時的聯邦學習輪次）**共用同一份訓練邏輯**，只是呼叫的時機、資料存檔的時機不同。
`_train_worker` 呼叫時會傳入 `self._model_lock`，包住實際碰觸 `actor`/`critic` 權重的
forward/backward 段落（不包 MongoDB I/O），因為近即時 ZMQ 推論迴圈也會用同一個
`DRLAgent` 實例的 `infer()`，兩者必須互斥存取，避免權重被「撕裂」讀取。

### 6.2 Critic 更新

$$\mathcal{L}_{critic} = \text{MSE}(V(s),\; r + \gamma \cdot V(s'))$$

- TD target：$r + \gamma \cdot V(s')$，$\gamma = 0.95$
- 使用 `stop_gradient` 避免 bootstrapping 不穩定
- **實作順序**：$V(s)$、$V(s')$、Advantage 均在 `critic_opt.step()` **之前**用同一版本的 Critic 計算，確保 TD target 與 baseline 一致，避免新舊 Critic 混用導致梯度偏移

### 6.3 Actor 更新（Dirichlet Policy Gradient）

$$\mathcal{L}_{actor} = -\mathbb{E}\left[A(s,a) \cdot \log p_{\text{Dir}}(a \mid \alpha(s))\right] - \beta_t \cdot H\!\left(\text{Dir}(\alpha(s))\right)$$

其中：
- **策略分佈**：$\text{Dir}(\alpha)$，集中度參數 $\alpha_i = \pi_{\theta}(s)_i \times K_t$，
  $K_t$ 隨訓練步數 $t$ 退火（見下方 §6.5，`DRLAgent._current_concentration()`）
- **Advantage**：$A(s,a) = r + \gamma V(s') - V(s)$，標準化（zero-mean, unit-std）
- **log 機率**：$\log p_{\text{Dir}}(a \mid \alpha)$ 為 Dirichlet 分佈在採樣動作 $a$ 處的對數機率，由 `torch.distributions.Dirichlet.log_prob()` 計算，對每個樣本的活躍 UE 子集獨立計算
- **Entropy 正則化**：$\beta_t = \max(0.001,\; 0.01 \times 0.997^t)$

> ✅ **已修復（原「entropy 衰減死碼」問題）**：舊版程式碼把下限誤寫成跟初始值相同的
> `max(0.01, 0.01 × 0.997^t)`，導致衰減公式對任何 $t>0$ 恆等於 0.01，形同死碼。已改為
> `ENTROPY_COEFF_MIN=0.001`（遠小於初始值 `ENTROPY_COEFF_INIT=0.01`），衰減公式現在會
> 在約 $t \approx 766$ 步（$\ln(0.1)/\ln(0.997)$）觸底到 0.001 並維持該值。**由於現有
> checkpoint 的 `train_steps` 已經遠超過 766（實測約 1200–1600），修復上線後，正在訓練中
> 的模型 entropy 係數會立即從常數 0.01 跳到下限 0.001**，這是預期中的行為（表示這些
> 模型「應該」早就進入低探索階段了，只是舊程式碼的 bug 讓它們一直維持高探索狀態）。

**Entropy 緊急保護**：無論上面的 `entropy_coeff` 算出多少，若當輪 batch 的平均 entropy
低於 −5.0（策略已經幾乎完全確定性、可能正在崩潰），會強制拉高係數：
`entropy_coeff = max(entropy_coeff, 0.1 × |current_entropy| / 5.0)`，逼模型重新探索，
防止 policy collapse 進一步惡化。這個機制修復前後都存在、行為不變。

**為何改用 Dirichlet Policy Gradient**：

舊設計使用 $\sum_i \log\pi(a_i|s) \cdot \text{ratio}_i$，在 DRL 推論階段 `action_ratios` ≈ actor 輸出的 softmax probs，導致：

$$\sum_i \log\pi(a_i|s) \cdot \pi(a_i|s) = -H(\pi)$$

Actor loss 退化為熵的最大化/最小化，而非 policy gradient，Actor 無法學習。

新設計以 **Dirichlet 分佈**作為策略：推論時從 $\text{Dir}(\pi_\theta(s) \times K)$ **採樣** action，儲存採樣值（非 softmax 均值），訓練時用 Dirichlet log_prob 計算真正的 policy gradient 梯度。

### 6.4 訓練超參數

| 參數 | 數值 |
|---|---|
| Discount factor γ | 0.95 |
| 序列長度（`TRAIN_SEQ_LEN`） | 32 步（~3.2 秒） |
| 每次梯度更新的序列數（`TRAIN_SEQ_COUNT`） | 16（= 512 筆原始經驗） |
| Gradient updates / round | 10 |
| Training interval | 60 秒 |
| 首次訓練所需最少 experiences（原始筆數門檻） | 200 筆（`MIN_TRAIN_EXPERIENCES`） |
| 首次訓練所需最少候選序列數 | 16（`MIN_TRAIN_SEQUENCES`，= `TRAIN_SEQ_COUNT`） |
| MongoDB 單次讀取上限 | 2000 筆（`TRAIN_FETCH_LIMIT`） |
| Train/Test 切分比例 | 8:2（以序列為單位） |
| Overfitting 警告閾值 | test_actor_loss − train_actor_loss > 0.3 |
| Entropy 係數 | $\max(0.001,\ 0.01 \times 0.997^t)$（`ENTROPY_COEFF_MIN/INIT/DECAY_RATE`，已修復死碼問題） |
| Entropy 緊急保護閾值 | entropy < −5.0 時強制拉高係數 |
| Dirichlet 集中度 K | 退火：$K_{\min}{=}5 \to K_{\max}{=}50$，時間常數 $\tau{=}5000$ 步（見 §6.5） |

> **2026-07-09 起，`TRAIN_SEQ_LEN`/`TRAIN_SEQ_COUNT` 取代舊版 `TRAIN_BATCH_SIZE=128`**：
> 舊版是從打散抽樣的獨立經驗池直接取 128 筆做一次梯度更新；現在改成先切出時間連續
> 的序列候選池，再從池中抽 16 個序列（見 §6.1.1）。兩者的「每次梯度更新看到幾筆
> 原始經驗」大致同量級（128 vs 512，序列版更大是因為序列內部樣本時間相關、需要
> 更多原始經驗才能達到同等品質的梯度估計，見 §6.1.1 說明）。

### 6.5 Dirichlet 集中度退火（已修復「K 不隨訓練調整」問題）

**舊版問題**：`K=5.0` 是寫死常數，從隨機初始化到訓練後期都不變，代表即使 policy 已經
收斂到很好的 `actor` 輸出機率，`infer()` 實際下發的 PRB 分配仍然是從
`Dirichlet(probs × 5)` **隨機採樣**出來的，不是 `probs` 本身——推論階段永遠帶著不小的
雜訊，不會隨訓練進度減少。在 CQI 差異化情境（如 Scenario A）這種「分配精準度影響大」
的場景，這個雜訊很可能是實測吞吐量輸給 PF（PF 是確定性排程、沒有這個雜訊）的原因
之一，且跟訓練是否收斂無關。

**修復方式**：新增 `DRLAgent._current_concentration()`，讓 $K$ 隨 `self._train_steps`
指數退火：

$$K_t = K_{\min} + (K_{\max} - K_{\min}) \times \left(1 - e^{-t/\tau}\right)$$

| 參數 | 數值 | 說明 |
|---|---|---|
| $K_{\min}$（`DIRICHLET_K_MIN`） | 5.0 | 訓練初期集中度，與舊版固定值相同，不改變早期探索行為 |
| $K_{\max}$（`DIRICHLET_K_MAX`） | 50.0 | 訓練後期集中度上限，大幅降低採樣雜訊 |
| $\tau$（`DIRICHLET_K_ANNEAL_TAU`） | 5000 步 | 退火時間常數；$t=\tau$ 時已完成 63% 的退火進度 |

`infer()`、`train_on_batch()`、`evaluate_on_batch()` 三處都改用
`self._current_concentration()`（依當下 `train_steps` 即時計算），取代原本的模組常數。

**取捨**：`train_on_batch()` 對 replay buffer 裡的舊經驗重新計算 log_prob 時，用的是
**目前**的退火後 $K$，不是該筆經驗被採樣「當下」的 $K$（同一批 batch 可能橫跨數千步、
K 已經改變）。這是 Offline A2C 既有的 off-policy 近似之一——經驗本身的 staleness
（`actor` 權重同樣也已經改變）已經是同等級的近似，沒有另外做嚴格的重要性採樣校正，
多這一項不改變近似的性質，屬於刻意接受的簡化，不是遺漏。

**部署注意**：現有 checkpoint 的 `train_steps` 已經遠超過退火時間常數的量級（實測約
1200–1600），修復上線後，$K$ 會立即從常數 5.0 跳到約 15–17（依各節點實際 `train_steps`
而定），而非從 0 重新退火——這是預期行為，代表「已經訓練這麼多步的模型，本來就該有
更低的採樣雜訊了」。

---

## 7. 推論模式切換

### 7.1 冷啟動：BSR 啟發式（Heuristic）

訓練資料不足（< 200 筆）或尚未完成首次訓練時，使用 **BSR 比例加權啟發式**：

$$PRB_i \propto \Delta TBS_i \times \left(1 + \frac{MCS_i}{28} \times 0.2\right)$$

- 以 `Δtbs`（delta-TBS）為主要分配權重，MCS 提供 20% 的通道品質加成
- 每個活躍 UE 保底 **5** PRB（`MIN_PRB_PER_UE`，見 §3.1）

### 7.2 探索注入（Dirichlet Exploration）

啟發式階段以 **30% 機率**改用 Dirichlet 隨機分配，產生多樣化的 (state, action, reward) 訓練資料：

$$\text{ratios} \sim \text{Dirichlet}(\alpha),\quad \alpha = [0.7, 0.7, \ldots]$$

Dirichlet(α < 1) 傾向生成稀疏、不均勻的分配，增加 reward 方差，有助於 DRL 收斂時識別有效策略。

### 7.3 DRL 推論（Dirichlet 隨機策略）

完成首次訓練後切換至 Actor Network 推論，啟發式模式作為 ZeroMQ 超時的 Fallback（5ms timeout）。

DRL 推論使用**隨機策略**：以 actor softmax 輸出乘以集中度 K 作為 Dirichlet 分佈的參數，從中採樣 PRB 比例向量。此設計確保：

1. 推論動作與 actor 輸出不同（有隨機性），訓練時 log_prob 梯度非零
2. 採樣值仍受 actor 引導（期望值 = softmax 輸出），策略方向正確
3. 訓練步數增加後，entropy 係數與 Dirichlet 集中度 $K$ 都會退火（見 §6.3／§6.5），
   推論階段的採樣雜訊隨訓練收斂逐漸變小，同時仍保留隨機策略架構，系統不會完全停止
   探索

| 模式 | 觸發條件 | 動作來源 |
|---|---|---|
| 啟發式（BSR 加權） | experiences < 200 或未完成訓練 | `_infer_heuristic()` |
| Dirichlet 探索 | 啟發式階段 + 30% 機率 | `np.random.dirichlet(α=0.7)` |
| DRL 推論 | 完成首次訓練後 | `Dirichlet(actor_probs × K).sample()` |
| Fallback | ZeroMQ 超時（5ms） | C 端等比例分配 |

---

## 8. Experience 儲存格式（MongoDB）

**每次收到 C xApp 的 ZMQ 請求儲存一筆 document**——因 C xApp Rate Limiter，實際週期是
**100ms**，不是原始 MAC callback 的 10ms（見 §1、CLAUDE.md §7「狀態觀測窗口」）：

```json
{
  "node_id":        int,
  "timestamp":      datetime (UTC),
  "state":          [原始 ues 資料，未正規化，供人工除錯/分析],
  "action":         [原始 PRB 分配結果，未正規化],
  "state_vec":      [float × 50],
  "mask_vec":       [bool × 16],
  "action_ratios":  [float × 16],
  "reward":         float,
  "next_state_vec": [float × 50],
  "next_mask_vec":  [bool × 16],
  "r_throughput":   float,
  "jfi_raw":        float,
  "r_fairness":     float,
  "r_delay":        float,
  "lambda_applied": float,
  "is_idle":        bool,
  "used_drl":       bool
}
```

`state`／`action` 兩個欄位是文件先前版本沒列出的原始（非正規化）資料，訓練不會用到，
純粹用來人工查驗某筆經驗實際觀測到的 UE 狀態與下發的 PRB 分配是否合理。寫入採**批次
緩衝 + 背景執行緒每 1 秒 flush**，不在近即時 ZMQ 迴圈裡同步寫入，避免拖慢 5ms 內的推論
時限。

**閒置轉換也會寫入（2026-07-09 起，破壞性行為變更）**：舊版只在
`sum(bsr for u in ues) > 0` 時才寫入 MongoDB，所有 `P_IDLE` 造成的閒置轉換完全不會
進訓練資料。但推論時 `_actor_hidden`（見 §4.3）照樣會連續經歷這些閒置片段——只要
UE 還在線，C xApp 就會持續送 ZMQ 請求。這代表加了 GRU 之後，若訓練資料完全沒有
閒置片段，訓練看到的序列分佈會跟推論時實際遇到的序列分佈不一致，GRU 學不到「剛進入
閒置」「已經閒置很久」「即將恢復」這類真實會遇到的軌跡。現在只在 UE **完全消失**（
xApp 重連後的空白 state，`len(ues) == 0`）時才跳過寫入——這種情況下 prev/curr 不是
同一組 UE，reward 沒有意義；閒置但 UE 仍在線（`bsr` 為 0）則正常寫入，`reward` 自然
算出 0.0（見 §5.2 空閒狀態特判），`is_idle` 欄位標記 `r_throughput < 1e-9`。

`is_idle` 欄位的用途：`training_pipeline.py` 用它（實際上是用等價的 `r_throughput`
門檻）把閒置樣本排除在 $\lambda$ 平均之外——如果讓 `jfi_raw=0.0` 的閒置樣本混進批次，
會把 `batch_jfi_mean` 錯誤拉低，讓 $\lambda$（見 §5.1）誤判成不公平而上升，但閒置只是
沒有流量可言公平，不是真的公平性問題。閒置樣本仍然正常拿去訓練 Actor/Critic（該學會
怎麼處理閒置狀態），只是不計入公平性平均。副作用：每個節點的文件量大約增加
（對應 Scenario R 的 `P_IDLE` 比例）。

Collection 命名規則：`node{1~5}_experiences`（各 Node 獨立儲存）。

---

## 9. 整體資料流

```
[OAI gNB MAC, 每 10ms callback]
  dl_aggr_tbs (累計), dl_mcs1 (0-28)
        │  Rate Limiter: 每 10 次 callback 才觸發一次（= 每 100ms）
        ▼ delta_tbs = curr - prev  (C 端計算，測量窗口 100ms)
[xApp C / ZeroMQ REQ]
  JSON: {"node_id": X, "ues": [{"rnti", "bsr": delta_tbs, "wb_cqi": mcs}, ...]}
        │
        ▼
[Inference Server Python / ZeroMQ REP]
  quota_ratio = _current_quota_ratio()
  encode_state(ues, quota_ratio) → state_vec (50,)
  （取得 _model_lock）
  DRL: Actor.forward(state, mask, _actor_hidden) → probs, new_hidden
       → _actor_hidden = new_hidden.detach()
       → Dirichlet(probs × K).sample() → action_ratios
  啟發式: _infer_heuristic() → action_ratios
  → allocations: [{"rnti", "prb_abs"}, ...]
  （釋放 _model_lock）
        │
        ▼ Phase 5a（僅 access node 3/4/5）：依 Global xApp quota 裁切
  effective_prb = min(106, quota_from_global) → 依比例裁切 allocations
        │
        ▼
[xApp C / MAC_CTRL_REQ]
  PRB 分配陣列寫回 OAI MAC 層
        │
        ▼ (下一個 100ms 收到新 state)
[reward_calculator.py]
  compute_reward_breakdown(curr_ues, prev_alloc) → reward   ← 用 S_t 計算 A_{t-1} 的效果
  寫入記憶體緩衝區 _write_buffer
        │
        ▼ (每 1 秒，_flush_worker)
  insert_many(batch) → MongoDB node{N}_experiences
        │
        ▼ (每 60 秒，_train_worker，經 training_pipeline.run_training_round())
  fetch_sequences()：讀取 MongoDB（≤2000 筆）→ 依連續性切運行 → 切成不重疊定長序列
  → 8:2 train/test 切分（以序列為單位）→ 10 epoch，每 epoch 抽 16 序列梯度更新
  agent.save() 原子寫入 model_nodeX.pt（temp file + os.replace()）
        │
        ▼ (每 30 秒，_reload_worker，僅 Phase 5 FL 相關)
  比對 model_nodeX.pt 的 mtime，若被 Flower ClientApp 覆寫過 → 熱重載進記憶體
```

**Phase 5b（relay node 1/2 專屬）**：`_alloc_pub_sock` 會把該節點對其下游 MT 的即時
PRB 分配結果 PUB 出去（`tcp://127.0.0.1:556{1,2}`），由 `global_xapp_bridge.py` 訂閱、
彙整成 access node 的配額，再 PUB 到 `tcp://127.0.0.1:5560` 給 Node3/4/5 訂閱——這條
資料流跟上面近即時 ZMQ REQ/REP 迴圈平行運作、互不阻塞。詳見 `PHASE5_GLOBAL_DEV_LOG.md`。

---

## 10. Checkpoint 持久化與 Phase 5 FL 同步

### 10.1 為什麼需要原子寫入

`DRLAgent.save()` 不是直接 `torch.save(..., model_nodeX.pt)`，而是先寫到帶 PID 的暫存檔
`model_nodeX.pt.tmp.{pid}`，再用 `os.replace()`（POSIX 上是原子操作）換成正式檔名：

```python
tmp_path = path.with_suffix(f".pt.tmp.{os.getpid()}")
torch.save({...}, tmp_path)
os.replace(tmp_path, path)
```

背景是 Phase 5 之後，同一個 checkpoint 檔案有**兩個行程**可能同時寫入：`inference_server.py`
的 `_train_worker`（每 60 秒）與 Flower `ClientApp`（每小時 FL round，見下方 §10.2，
以 `--isolation subprocess` 跑在獨立行程）。若不是原子寫入，`_reload_worker` 可能讀到
寫一半的檔案（`torch.load()` 拋例外或載入損毀權重）。`os.replace()` 保證讀者永遠讀到
「完整的舊檔」或「完整的新檔」，不會讀到中間狀態。

### 10.2 Phase 5 FL 熱重載機制

Flower `ClientApp` 因為 `--isolation subprocess`（SuperNode 預設模式），**跟 `inference_server.py`
的近即時推論行程是完全獨立的作業系統行程，無法共享記憶體中的 `DRLAgent` 實例**。
兩者之間唯一的溝通管道是磁碟上的 `model_nodeX.pt` 檔案：

```
[flower-supernode-nodeN 容器]
  ClientApp @app.train() 被 SuperLink 呼叫
    agent.load()（讀目前磁碟上的權重，保留 train_steps）
    套用 Global rApp 下發的聚合權重
    agent.save()（立即存檔，確保就算後面 fine-tune 失敗，聚合結果不會遺失）
    training_pipeline.run_training_round()（本地 fine-tune）
    agent.save()（fine-tune 後再存一次）
        │
        ▼ （磁碟檔案 mtime 改變）
[inference-nodeN 容器 / _reload_worker，每 30 秒輪詢]
  比對 model_nodeX.pt 的 mtime 與上次讀取時記錄的 mtime
  若不同 → 取得 _model_lock → agent.load() 熱重載進記憶體 → 釋放 _model_lock
```

這代表**近即時推論行程用的模型權重，最多會延遲 30 秒（`RELOAD_POLL_INTERVAL_S`）才會
反映 FL 聚合後的最新結果**，不是 FL round 結束的當下就立即生效。這個延遲是刻意的
（輪詢間隔而非檔案系統事件通知），因為 FL round 本身是小時級週期，30 秒的反應延遲
相對可忽略。
