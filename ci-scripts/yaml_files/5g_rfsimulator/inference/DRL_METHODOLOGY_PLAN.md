# DRL 方法論開發計劃書：Local（已完成）+ Global（規劃中）

> 本文件涵蓋 Local rApp（Actor-Critic DRL）與 Global rApp（Flower Federated Learning Server）
> 兩側的方法論演進，分兩部分：**第一部分是 Local 端已完成的三項方法**（回顧性質，記錄動機與
> 開發流程）；**第二部分是 Global 端正在規劃、尚未實作的方法**（Clustered FL + FedOpt）。
>
> 相關文件：`DRL_DESIGN.md`（Local 端現行架構的完整技術細節，本文件第一部分是濃縮摘要 +
> 開發流程，細節以 `DRL_DESIGN.md` 為準）、`PHASE5_GLOBAL_DEV_LOG.md`（2026-07-01~07-06
> Global 基礎架構開發紀錄，本文件第二部分是在那份基礎上的方法論擴充）。

---

# 第一部分：Local rApp（已完成）

## 1. 背景：舊版方法的侷限

Local DRL 原本是 Actor-Critic（MLP）+ 固定加權和獎勵函數（`R = W_tp·R_tp + W_fair·R_fair -
W_delay·R_delay`）。用 Scenario A/B/C 做驗收時發現：不存在一組固定權重能同時滿足三個場景——
提高公平性權重，Scenario B 吞吐量拉不起來；拿掉公平性權重，Scenario A 直接崩壞成「無腦倒向
通道好的 UE」。這個矛盾是後續三個方法改動的共同起點。

## 2. 已完成的三個方法

### 2.1 獎勵函數：Lagrangian 限制式

把「吞吐量與公平性的固定加權」改成「限制式最佳化」：

$$\max_\theta \mathbb{E}[R_{tp}] \quad \text{s.t.} \quad \mathbb{E}[JFI] \geq JFI_{min}$$

透過 Lagrangian 鬆弛落地成 $R = R_{tp} + \lambda \cdot (JFI_{raw} - JFI_{min})$。$\lambda$
不是手動設定，是 `DRLAgent.train_on_batch()` 每次 mini-batch 訓練後依「這批資料的 JFI
有沒有低於門檻」自動升降。$JFI_{min}=0.8291$ 是現場實測 PF 排程器在 Scenario R 條件下、
15 分鐘量出來的門檻，不是拍腦袋決定。

**動機**：不同場景需要的公平性強度天生不同，固定權重/固定退火時程表無法通用；限制式讓
$\lambda$ 針對每個場景、每個訓練時刻自動判斷「現在需不需要在意公平性」。

### 2.2 State 加入 Global 配額特徵

`encode_state()` 新增 `prb_quota_ratio` 維度（`STATE_DIM` 49→50），relay 節點恆為 1.0，
access 節點依 Global xApp 目前下發的配額換算比例。

**動機**：Global xApp 的回傳配額限制原本只在 Actor 輸出「之後」拿來裁切分配（Phase 5a
邏輯），Actor 本身不知道限制存在，每次都要等分配完被裁切才學到教訓。加入這個特徵後，
Actor 能在決策「之前」就感知配額緊繃程度。

### 2.3 Actor/Critic 從 MLP 改成 GRU

最大的一塊改動。舊版每次決策只看當下瞬間的 state，無法分辨 Scenario R 的 `P_IDLE`
機制造成的「剛進入閒置」「已經閒置很久」「快恢復流量」這幾種軌跡上不同、但瞬間看起來
相似的狀態（POMDP 問題，單步 state 不是充分統計量）。改用 GRU 讓 Actor/Critic 各自維護
獨立的跨步隱藏記憶。

連帶的訓練管線改造：
- 訓練資料從「打散抽樣獨立經驗」改成「抓時間上真的連續的序列」（`TRAIN_SEQ_LEN=32`
  步一段、`TRAIN_SEQ_COUNT=16` 段一批），連續性判斷靠 `next_state_vec == state_vec`
  的位元組相等（不需要新增 MongoDB 欄位）
- 閒置轉換也開始寫進 MongoDB（之前會跳過），讓訓練資料分佈跟推論時實際遇到的分佈一致，
  新增 `is_idle` 欄位供 $\lambda$ 更新時排除閒置樣本

**評估過的替代方案**：delta 特徵、frame stacking、GRU 三個選項中，選擇 GRU 是因為「不管
成本、哪個效果好做哪個」的決策——GRU 理論上最能學到「該記住什麼」，但工程量也最大。

## 3. 開發流程（實際執行順序）

1. Plan agent 研究現有六個相關檔案（`drl_agent.py`／`training_pipeline.py`／
   `inference_server.py`／`client_app.py`／`server_app.py`／`reward_calculator.py`），
   逐項驗證關鍵事實（不是憑空設計），特別標記一個非顯而易見的正確性缺口（閒置轉換
   訓練/推論分佈不一致）供使用者明確確認方向，而非直接假設答案
2. 逐檔案實作：`drl_agent.py`（GRU 架構＋隱藏狀態生命週期）→ `training_pipeline.py`
   （序列抓取＋連續性偵測）→ `inference_server.py`（配額特徵串接＋閒置轉換寫入）→
   `client_app.py`（`evaluate()` 改用共用序列抓取函式）
3. 獨立單元測試（含真實 MongoDB 整合測試）逐檔驗證，全部通過後才進入部署
4. **清空重來**：MLP→GRU 是破壞性變更（checkpoint 的 state_dict key/形狀完全不相容），
   五個節點的 MongoDB 經驗與舊 checkpoint 全部清空，從隨機初始化重新訓練
5. 部署過程中連帶處理了兩次基礎設施問題（FlexRIC E2 連線斷線、DU assertion crash），
   跟這次方法論改動本身無關，是這個測試台既有的已知不穩定模式
6. **上線後發現並修正一個新問題**：GRU 訓練一輪的 BPTT 運算量遠比舊版 MLP 重，一度讓
   `_model_lock` 被連續佔用 3~4 秒，導致該視窗內即時推論全部逾時 fallback 回 PF 排程；
   修法是把鎖從「整輪訓練共用一個」改成「每個 epoch 邊界各自取得/釋放」，把單次中斷長度
   從 3.5 秒降到 200~800ms（觸發次數本身沒有降低，這點記錄在案，作為之後如果 FlexRIC
   又變得不穩定時的排查線索）

## 4. 目前狀態（誠實記錄，非樂觀陳述）

系統目前是**停著的**——五個節點的 MongoDB 經驗與 checkpoint 已於使用者要求下再次清空，
PC1／PC2 整個 docker-compose stack 都已停止，尚未重新啟動訓練。

**目前沒有這套新方法（Lagrangian + 配額特徵 + GRU）的吞吐量/JFI 對照數據**——GRU 上線後
只從隨機初始化訓練了幾小時就被要求清空重來，不曾達到有意義的收斂程度，`scenario_
comparison_2026-05-23.md` 與 CLAUDE.md 都沒有補上任何一筆新架構的量測紀錄。下一步若要
拿到數據，需要：重新啟動系統 → 訓練到 `mean_reward`／`entropy`／`lambda` 等指標穩定
（參考這個專案過去的經驗，至少需要 1~2 天量級，且 GRU 是全新架構、沒有收斂歷史可參考）
→ 依既有的「DRL vs PF 標準量測流程」跑 Scenario A/B/C 對照。

---

# 第二部分：Global rApp（規劃中，尚未實作）

## 5. 背景與動機

### 5.1 現況：`IABFedAvg` 是純樣本數加權的標準 FedAvg

`server_app.py` 的 `IABFedAvg`（繼承 `flwr.serverapp.strategy.FedAvg`）依各節點回傳的
`num-examples` 加權平均全部 5 個節點的 Actor/Critic 權重，產出**一個**全域模型，廣播
寫回全部 5 個節點的 checkpoint。`aggregate_train()` 有額外算一個全網 JFI
（`compute_global_jfi()`），但只用於 log 監控，沒有回饋進聚合權重。底層聚合是 Flower
內建的 `aggregate_arrayrecords()`：對 `ArrayRecord` 裡每一個 key（每一層網路參數）獨立
加權平均，`actor.*`跟`critic.*`互不影響。`min_train_nodes=min_evaluate_nodes=
min_available_nodes=5` 強制全部 5 個節點每輪都要參與。

`num-examples` 的實際語意（GRU 改版後）：`client_app.py` 的 `metrics.get("n_train_seq", 0)`，
來自 `training_pipeline.py` 的 `run_training_round()` 回傳的訓練序列數量（32 步一段的
時間連續窗口），不是原始 (state,action,reward) 筆數。

### 5.2 問題：5 個節點被迫混在同一個 global policy 裡

Node1/2（relay）跟 Node3/4/5（access）做的是結構上不同的任務：relay 中繼下游 MT 流量，
不直接面對終端 UE 的頻寬需求；access 直接處理真實的 iperf3 流量波動。加上 Node2 只有
1 個 MT（其餘節點 2 個），問題複雜度天生較低。現行 `IABFedAvg` 把這 5 個結構不同的節點
權重全部混在一起平均，relay 跟 access 的學習訊號會互相稀釋、甚至互相干擾。

## 6. 方法選擇的討論過程

### 6.1 排除：完全異質模型聯邦學習（HFL）

文獻上 HFL 的核心動機是「客戶端因硬體限制或隱私考量各自獨立設計不同架構」（IoT 裝置
算力不同、醫院不想洩漏模型設計），需要知識蒸餾跨架構傳遞知識。這兩個動機在本專案都不
成立：5 個節點同一團隊、同一份程式碼、同樣的 Docker 環境，沒有硬體異質性也沒有隱私
顧慮，直接引用 HFL 文獻會答不出「這裡到底哪裡有硬體或隱私限制」這個問題。

### 6.2 排除：Byzantine-robust 聚合（Krum／MultiKrum／Bulyan／FedTrimmedAvg）

`vendor/flwr/serverapp/strategy/` 裡都有現成實作，但設計動機是防範惡意/損毀的客戶端
更新（對抗性威脅模型）。本專案 5 個節點都是自己控制的，沒有這種威脅，套用這個是在解決
一個不存在的問題。

### 6.3 採用：Clustered Federated Learning

符合「Global 適應不同 local model、聚合後有多個 global policy」這個目標、且文獻定位
明確的方向是 Clustered FL（如 Sattler et al. 2020、IFCA 這條研究線）：客戶端依真實的
結構/資料分佈差異分群，群內用標準權重聚合（架構完全相同，不需要跨架構知識蒸餾），群跟
群之間各自產出獨立 global policy。與 HFL 的關鍵差異：不需要處理架構異質的複雜度，5 個
節點的網路結構維持完全一致，只是「哪些節點的權重互相平均」跟「結果廣播給誰」從「全部
5 個」改成「依群組各自處理」。

### 6.4 聚合演算法：從 FedAvg 換成 FedOpt 家族

分群是一個獨立維度，「群內怎麼聚合」是另一個可以疊加的獨立維度：

| 方案 | 動機 | 成本 |
|---|---|---|
| **FedOpt（FedAdam／FedYogi／FedAdagrad）** | 把「聚合後模型 - 舊模型」當偽梯度，餵給伺服器端 Adam-style 優化器，而非直接覆蓋權重；對節點間資料分佈不均通常收斂更穩定 | 低，只改 server 端 |
| **FedProx** | 本地訓練 loss 加「不要跟全域模型差太遠」的懲罰項，直接針對 non-IID 資料造成的 client drift | 較高，要改本地訓練迴圈 |

兩者不互斥，且跟分群是兩個獨立維度，可以分開驗證。

## 7. 具體設計

### 7.1 分群方案

```python
RELAY_CLUSTER: set[int]  = {1, 2}       # relay：中繼下游流量
ACCESS_CLUSTER: set[int] = {3, 4, 5}    # access：直接面對終端 UE
```

分群依據是既有的、真實的角色差異（`inference_server.py` 的 `self._is_relay = node_id in
(1, 2)` 本來就存在，只是目前只用在 Phase 5a/5b 的 PUB/SUB 邏輯，沒有用在聯邦學習聚合上），
不是為了做研究人工造出來的分群。

每一群各自產出獨立 global policy：relay 群結果只廣播回 Node1/2 checkpoint，access 群
結果只廣播回 Node3/4/5 checkpoint。`min_train_nodes` 等門檻要跟著改成群組層級（relay
群 `min_train_nodes=2`、access 群 `min_train_nodes=3`），兩群獨立判斷是否進行這輪聚合，
不會因為一群還沒到齊卡住另一群。

**已知取捨**：relay 群只有 2 個節點、access 群只有 3 個，群內樣本數比現行 5 節點少，
聯邦統計效益會變弱——但這跟分群動機一致（避免不同任務互相干擾），需要在論文裡明確討論。

### 7.2 群內聚合演算法：分階段導入 FedOpt

**第一階段先不換演算法**，只做分群，聚合方式維持跟現在一樣的 `aggregate_arrayrecords()`
加權平均，只是分成兩次獨立呼叫。這樣能把「分群本身有沒有用」跟「換聚合演算法有沒有用」
分開驗證，不會因為一次改兩個變因搞不清楚效果來源。

**第二階段**，兩群各自的策略類別從繼承 `FedAvg` 換成繼承 `FedAdam`
（`vendor/flwr/serverapp/strategy/fedadam.py`）。Flower 內建預設超參數：

| 參數 | 預設值 | 意義 |
|---|---|---|
| `eta` | 0.1 | 伺服器端學習率 |
| `eta_l` | 0.1 | 客戶端學習率（演算法內部計算用） |
| `beta_1` | 0.9 | 一階動量 |
| `beta_2` | 0.99 | 二階動量 |
| `tau` | 1e-3 | 調整自適應程度的常數 |

`FedAdam → FedOpt → FedAvg` 的繼承鏈內部一樣會先呼叫 `aggregate_arrayrecords()` 算出
跟現在相同的加權平均，再把「這次平均值 - 上一輪全域權重」當偽梯度跑一次 server-side
Adam 更新——第二階段的程式碼改動很小（換繼承類別、調超參數），底層資料流跟第一階段
完全相同。

**第三階段（視第二階段效果決定要不要做）**：FedProx，在 `training_pipeline.py`／
`drl_agent.py` 的 loss 計算加入近端項，懲罰本地更新後的權重跟本輪收到的全域權重差距
過大。成本較高、會動到 Local 端訓練邏輯，先觀察前兩階段效果再決定是否值得做。

## 8. 開發流程（分階段任務拆解）

**階段 A：分群基礎設施（不換聚合演算法）**
1. `server_app.py` 新增 `RELAY_CLUSTER`／`ACCESS_CLUSTER` 常數
2. `IABFedAvg.aggregate_train()` 改造：把收到的 `replies` 依 `node_id` 拆成兩組，分別
   算出兩個獨立聚合結果
3. 廣播寫回 checkpoint 邏輯改成依群組寫回對應節點
4. `min_train_nodes` 等門檻改成群組層級判斷
5. `compute_global_jfi()` 比照分群，改成算 relay 群/access 群兩個獨立數字

**階段 B：換成 FedOpt（FedAdam）**

6. `IABFedAvg` 繼承從 `FedAvg` 換成 `FedAdam`，兩群各自帶入超參數（初期用 Flower 預設值）
7. 確認兩群各自是獨立的 `FedAdam` 實例，不共用動量狀態

**階段 C（視情況）：FedProx**

8. 訓練 loss 加入近端項，需要把「本輪收到的全域權重」傳進本地訓練迴圈當懲罰項參考點

## 9. 需要修改的檔案

- `inference/flower-app/iab_fl/server_app.py`（分群邏輯、階段 B 換 `FedAdam`）
- `inference/flower-app/iab_fl/client_app.py`（**理論上不需要改**——分群跟換聚合演算法
  都是 server 端的事；階段 C 才需要改本地訓練）
- `inference/training_pipeline.py`（僅階段 C）
- `inference/drl_agent.py`（僅階段 C）
- `inference/DRL_METHODOLOGY_PLAN.md`（本文件，隨開發進度更新）

## 10. 驗證方式

1. 獨立單元測試：假 `RecordDict`／`ArrayRecord` 資料構造 5 個節點的假 replies，驗證
   分群邏輯正確拆組、兩個聚合結果互不混雜
2. 語法檢查：`python3 -c "import ast; ast.parse(...)"`
3. 實機小規模驗證：`docker compose run --rm flower-scheduler bash -c "cd /app/flower-app
   && flwr run . local-deployment"` 手動觸發單一 FL round，確認 relay/access 群
   checkpoint mtime 各自獨立更新
4. 對照實驗（後續）：每完成一階段都要先讓 Local 端訓練到有意義的收斂程度，再跑
   Scenario A/B/C 對照，比較「分群前」「只分群」「分群+FedOpt」「分群+FedOpt+FedProx」
   的吞吐量/JFI 差異，作為消融實驗數據

## 11. 已知風險與取捨

- 群內節點數少（2、3 個），統計效益比現行 5 節點聯邦弱，是分群動機必然帶來的 trade-off
- `FedAdam` 的伺服器端動量狀態需要額外維護，分群後有兩份獨立狀態，不能共用同一個實例
- 階段 C 成本較高，建議只在階段 A／B 效果不夠明顯時才進行
- 這份計劃不處理「JFI 直接介入聚合權重」（CLAUDE.md 原本標記的待開發項目）——分群本身
  就是一種「讓角色差異影響聚合方式」的做法，但不是直接把 JFI 數值代入權重公式；若之後
  仍想做，可以在階段 A 完成後把 `weighted_by_key` 從純 `num-examples` 換成「num-examples
  × JFI 相關自訂係數」，是可疊加在本計劃之上的獨立擴充，不衝突

## 12. 本次執行範圍

**這份文件本身只是規劃，Global 端尚未開始實作**。下一步是先做階段 A（分群基礎設施，
不換聚合演算法），完成並驗證後再評估是否進入階段 B。實際動工前建議依專案慣例先進入
Plan 模式對階段 A 的具體程式碼改動做一次確認，避免像 GRU 改版那樣邊做邊調整範圍。
