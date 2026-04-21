# Local xApp DRL 設計文件

## 1. 系統定位

每個 IAB Node（共 5 個）部署一個獨立的 **Local xApp**，負責該節點的下行 PRB 資源分配。控制週期為 **10 ms**（OAI MAC 排程週期），透過 ZeroMQ IPC 與 Python 推論伺服器溝通。

```
OAI gNB (C 語言)
  └─ MAC indication (每 10ms)
       └─ xApp (C / FlexRIC)  ──ZeroMQ REQ/REP──  Inference Server (Python)
            └─ MAC_CTRL_REQ                              └─ DRL Actor Network
                 └─ PRB 分配寫回 OAI MAC 層                   └─ MongoDB (experience 儲存)
```

---

## 2. State Space（狀態空間）

### 2.1 原始觀測量（C 端 MAC indication）

| 欄位 | OAI 結構體欄位 | 說明 | 備註 |
|---|---|---|---|
| **Δ DL TBS** | `dl_aggr_tbs` 差分 | 每 10ms callback 實際傳送的 DL bytes | 累計值差分，反映瞬時吞吐量 |
| **DL MCS** | `dl_mcs1` | 下行 Modulation and Coding Scheme index (0–28) | 通道品質代理指標 |

> **注意**：OAI RF Simulator 的 `dl_buffer_info`（DL buffer queue depth）與 `wb_cqi`（Wideband CQI）在模擬環境下恆為 0，因此改用上述兩個欄位作為 state 輸入。

### 2.2 State 向量編碼

State 為固定長度 **33 維**的 float32 向量（MAX\_UE\_COUNT = 16）：

```
state_vec = [
    norm_tbs_0,  norm_mcs_0,   # UE slot 0
    norm_tbs_1,  norm_mcs_1,   # UE slot 1
    ...
    norm_tbs_15, norm_mcs_15,  # UE slot 15（不足補 0）
    active_ratio               # 全域 context 特徵
]
```

| 特徵 | 正規化方式 | 範圍 |
|---|---|---|
| `norm_tbs_i` | `log(1 + Δtbs_i) / log(1 + 100000)` | [0, 1] |
| `norm_mcs_i` | `mcs_i / 28.0` | [0, 1] |
| `active_ratio` | `n_active / 16` | [0, 1] |

- `Δtbs` 使用 **log 正規化**，壓縮大數值差距（高吞吐 UE 不會完全主導 state）
- MCS 使用**線性正規化**，MCS=0 合法（反映最差通道品質）
- 非活躍 UE 的 slot 填 0，並透過 **mask** 在 softmax 中遮蔽

---

## 3. Action Space（動作空間）

### 3.1 動作定義

Actor Network 輸出每個 UE 的 **PRB 分配比例**（經 Masked Softmax），再乘以系統 PRB 總數轉換為絕對 PRB 數量。

| 參數 | 數值 |
|---|---|
| 系統 PRB 總數 | 106（對應 100 MHz 頻寬） |
| 最小 PRB 保底 | 2 PRB / 活躍 UE |
| Action 輸出維度 | 16（MAX\_UE\_COUNT） |
| 非活躍 UE | Mask 遮蔽（logit = −∞，softmax 輸出 ≈ 0） |

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

### 4.1 Actor Network（Policy Network）

```
Input: state_vec (33,)
  → Linear(33, 128) → ReLU
  → Linear(128, 128) → ReLU
  → Linear(128, 16)          ← logits
  → Masked Softmax(mask)     ← 非活躍 UE 遮蔽
Output: PRB 分配比例 (16,)
```

### 4.2 Critic Network（Value Network）

```
Input: state_vec (33,)
  → Linear(33, 128) → ReLU
  → Linear(128, 64) → ReLU
  → Linear(64, 1)
Output: 狀態價值估計 V(s)，scalar
```

| 超參數 | 數值 |
|---|---|
| Hidden dim | 128 |
| Optimizer | Adam |
| Actor learning rate | 1e-4 |
| Critic learning rate | 3e-4 |
| Gradient clipping | 1.0（Actor & Critic） |

---

## 5. 獎勵函數（Reward Function）

### 5.1 公式

$$R = W_{tp} \cdot R_{tp} + W_{fair} \cdot R_{fair} - W_{delay} \cdot R_{delay}$$

| 分量 | 權重 | 公式 | 說明 |
|---|---|---|---|
| $R_{tp}$ | 0.5 | $\frac{1}{N}\sum_i \min\!\left(\frac{\Delta TBS_i}{B_{max}},\,1\right)$ | 各 UE 實際 DL 吞吐量平均，正規化至 [0,1] |
| $R_{fair}$ | 0.3 | $\dfrac{(\sum_i \Delta TBS_i)^2}{N \cdot \sum_i \Delta TBS_i^2}$ | Jain's Fairness Index，= 1 表示完全公平 |
| $R_{delay}$ | 0.2 | $\frac{1}{N}\sum_i \text{clip}\!\left(1 - \dfrac{\Delta TBS_i / B_{max}}{PRB_i / PRB_{total}},\;0,\;1\right)$ | PRB 效率懲罰（越高越差） |

其中 $B_{max} = 100{,}000$ bytes/10ms（對應峰值 ≈ 80 Mbps）。

### 5.2 各分量說明

**R_throughput**：衡量 DL 實際吞吐量。`Δtbs_i` 為 C 端 `dl_aggr_tbs` 的差分值（每 10ms 實際傳輸 bytes），直接反映 MAC 層真實產出，而非估計值。

**R_fairness**：Jain's Fairness Index（JFI）作用在各 UE 的吞吐量上。JFI = 1 代表所有 UE 吞吐量完全相等；JFI = 1/N 代表資源全集中於單一 UE。論文優化目標為 JFI > 0.924（超越 OAI PF Scheduler baseline）。

**R_delay（PRB 效率懲罰）**：由於 OAI RF Simulator 的 `dl_buffer_info` 恆為 0，無法直接量測排隊延遲。本研究以 **PRB 效率**作為延遲代理指標：

$$\text{efficiency}_i = \frac{\Delta TBS_i / B_{max}}{PRB_i / PRB_{total}}$$

- efficiency ≥ 1：UE 以少量 PRB 達到高吞吐 → 懲罰 = 0（不額外懲罰）
- efficiency << 1：UE 佔用大量 PRB 但產出低吞吐（差通道 / 過度分配） → 懲罰趨近 1，反映其他 UE 因等待 PRB 而積累的排隊延遲

### 5.3 Reward 數值範圍

| 情境 | 估計 R 值 |
|---|---|
| 無 DL 流量（冷啟動） | −0.20（−W_delay × 1） |
| 全滿載、完全不公平 | ≈ 0.50（僅 R_tp） |
| 全滿載、完全公平、高效率 | ≈ 0.80（R_tp + R_fair） |

---

## 6. 訓練演算法（Offline A2C）

### 6.1 訓練流程

本系統採用**離線 Advantage Actor-Critic（Offline A2C）**，與標準 online A2C 的差異在於訓練資料來自 MongoDB experience replay，而非即時 rollout。

```
每 10ms（推論迴圈）：
  state_t → Actor → action_t（PRB 分配）→ 寫回 OAI
  下一個 10ms：state_{t+1}
  計算 reward_t → 寫入 MongoDB

每 60 秒（背景訓練執行緒）：
  從 MongoDB 讀取所有 experiences
  隨機取樣 mini-batch（128 筆）
  執行 10 次梯度更新（gradient update steps）
  儲存模型權重至 /app/models/model_nodeX.pt
```

### 6.2 Critic 更新

$$\mathcal{L}_{critic} = \text{MSE}(V(s),\; r + \gamma \cdot V(s'))$$

- TD target：$r + \gamma \cdot V(s')$，$\gamma = 0.95$
- 使用 `stop_gradient` 避免 bootstrapping 不穩定

### 6.3 Actor 更新

$$\mathcal{L}_{actor} = -\mathbb{E}\left[A(s,a) \cdot \sum_i \log\pi(a_i|s) \cdot \text{ratio}_i\right] - \beta_t \cdot H(\pi)$$

其中：
- **Advantage**：$A(s,a) = r + \gamma V(s') - V(s)$，標準化（zero-mean, unit-std）
- **加權 log 機率**：以儲存的 `action_ratios` 作為連續動作的示範權重
- **Entropy 正則化**：$\beta_t = \max(0.001,\; 0.01 \times 0.997^t)$，隨訓練步數衰減，初期鼓勵探索，後期允許收斂

### 6.4 訓練超參數

| 參數 | 數值 |
|---|---|
| Discount factor γ | 0.95 |
| Mini-batch size | 128 |
| Gradient updates / round | 10 |
| Training interval | 60 秒 |
| 首次訓練所需最少 experiences | 200 筆 |
| Entropy 初始係數 | 0.01 |
| Entropy 衰減率 | 0.997 / step |
| Entropy 最小值 | 0.001 |

---

## 7. 推論模式切換

### 7.1 冷啟動：BSR 啟發式（Heuristic）

訓練資料不足（< 200 筆）或尚未完成首次訓練時，使用 **BSR 比例加權啟發式**：

$$PRB_i \propto \Delta TBS_i \times \left(1 + \frac{MCS_i}{28} \times 0.2\right)$$

- 以 `Δtbs`（delta-TBS）為主要分配權重，MCS 提供 20% 的通道品質加成
- 每個活躍 UE 保底 2 PRB

### 7.2 探索注入（Dirichlet Exploration）

啟發式階段以 **30% 機率**改用 Dirichlet 隨機分配，產生多樣化的 (state, action, reward) 訓練資料：

$$\text{ratios} \sim \text{Dirichlet}(\alpha),\quad \alpha = [0.7, 0.7, \ldots]$$

Dirichlet(α < 1) 傾向生成稀疏、不均勻的分配，增加 reward 方差，有助於 DRL 收斂時識別有效策略。

### 7.3 DRL 推論

完成首次訓練後切換至 Actor Network 推論，啟發式模式作為 ZeroMQ 超時的 Fallback（5ms timeout）。

| 模式 | 觸發條件 | 動作來源 |
|---|---|---|
| 啟發式（BSR 加權） | experiences < 200 或未完成訓練 | `_infer_heuristic()` |
| Dirichlet 探索 | 啟發式階段 + 30% 機率 | `np.random.dirichlet()` |
| DRL 推論 | 完成首次訓練後 | `Actor.forward()` |
| Fallback | ZeroMQ 超時（5ms） | C 端等比例分配 |

---

## 8. Experience 儲存格式（MongoDB）

每個 10ms 週期儲存一筆 document：

```json
{
  "state_vec":      [float × 33],
  "mask_vec":       [bool × 16],
  "action_ratios":  [float × 16],
  "reward":         float,
  "next_state_vec": [float × 33],
  "next_mask_vec":  [bool × 16],
  "r_throughput":   float,
  "r_fairness":     float,
  "r_delay":        float,
  "used_drl":       bool
}
```

Collection 命名規則：`node{1~5}_experiences`（各 Node 獨立儲存）。

---

## 9. 整體資料流

```
[OAI gNB MAC, 每 10ms]
  dl_aggr_tbs (累計), dl_mcs1 (0-28)
        │
        ▼ delta_tbs = curr - prev  (C 端計算)
[xApp C / ZeroMQ REQ]
  JSON: {"node_id": X, "ues": [{"rnti", "bsr": delta_tbs, "wb_cqi": mcs}, ...]}
        │
        ▼
[Inference Server Python / ZeroMQ REP]
  encode_state() → state_vec (33,)
  Actor.forward() 或 _infer_heuristic()
  → allocations: [{"rnti", "prb_abs"}, ...]
        │
        ▼
[xApp C / MAC_CTRL_REQ]
  PRB 分配陣列寫回 OAI MAC 層
        │
        ▼ (下一個 10ms 收到新 state)
[reward_calculator.py]
  compute_reward(prev_ues, prev_alloc) → reward
  存入 MongoDB
        │
        ▼ (每 60 秒)
[train_thread]
  讀取 MongoDB → mini-batch → 10 × gradient update
  model_nodeX.pt 更新
```
