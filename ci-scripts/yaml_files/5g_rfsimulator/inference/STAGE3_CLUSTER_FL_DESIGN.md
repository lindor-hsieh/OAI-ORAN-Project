# Stage 3 設計書：Soft/Weighted Clustered FedAvg（連續角色比例加權聚合）

> 狀態：**已實作、已完成量測**（2026-09-13 設計定案，2026-09-14 量測）。本文件補完
> CLAUDE.md 第 3 節 Stage 3 段落的完整設計脈絡與公式推導，程式碼位置：
> `inference/flower-app/iab_fl/server_app.py`（`IABClusterFedAvg`）、`client_app.py`
> （`train()` 的 `node_id` 欄位）。量測數據見 `experiment_results/clusterFL.md`；
> Stage 4 在此設計之上疊加 reward-deficit 加權，見 `STAGE4_CUSTOM_FL_DESIGN.md`。

---

## 1. 背景與動機

### 1.1 為什麼不能用標準的硬性分群 FL

Clustered FL 的傳統做法（文獻上常見的形式）是把 client 硬性分派進互斥的群組
$C_k$，各群組內部獨立做 FedAvg：

```
廣播：伺服器把 W_{C_k}^t 發給群組 C_k 內所有 client i（每個 client 屬於恰好一個群組）
本地訓練：client 用本地資料算出 W_i^{t+1}
上傳：client 回傳 W_i^{t+1}（或 ΔW_i）與樣本數 n_i
群組聚合：W_{C_k}^{t+1} = Σ_{i∈C_k} (n_i/N_k) · W_i^{t+1}
```

Stage 3 原本規劃沿用舊 5-node 拓樸的做法：依節點是 relay 還是 access，硬分成
兩群各自 FedAvg。這個分群理由本身沒問題——relay 的下游是「其他有 DU 的節點」，
access 的下游是「純 UE」，結構性質確實不同，值得分開建模。

**問題出在 12-node 拓樸裡的 Node4**：它的 DU 同時中繼給 Node11/Node12（各帶 2 個
UE，共 4 個），也直接服務 UE17——是唯一橫跨 relay/access 兩種下游型態的節點。若
強制二選一硬分群，Node4 的模型會被拉去擬合單一下游型態、犧牲另一種下游的吞吐量
表現，跟「單調遞增總吞吐量」的驗收目標直接衝突。

### 1.2 設計決策：從離散群組指派，推廣成連續角色比例

2026-09-13 討論定案：把「節點屬於哪個群組」這個離散指標函數 $\mathbb{1}(i \in C_k)$，
換成連續值 `role_ratio_i ∈ [0,1]`，並讓聚合與廣播都依這個連續值加權，而不是依
離散的群組成員資格。硬性二分群是 `role_ratio_i ∈ {0,1}` 時的特例——不是丟棄硬分群
的想法，而是把它嵌進一個更一般的公式裡。

這個改法還有兩個附帶好處：
1. 不需要對混合節點做人工判定（Node4 不用「湊整數」歸邊）。
2. 機制本身不寫死拓樸大小——未來若有更大規模的 IAB 樹、出現更多混合角色節點，
   不需要重新手動定義分群邊界，`role_ratio_i` 的計算方式可以直接套用。

---

## 2. `role_ratio_i` 的定義

```
role_ratio_i = 直連 UE 數量 / (直連 UE 數量 + 經下游 DU 間接服務的 UE 數量)
```

這是**結構性常數**，依現行 12-node 拓樸直接算出，不需要即時量測、不會隨量測過程
變動，寫死在 `server_app.py::ROLE_RATIO`：

```python
ROLE_RATIO: dict[int, float] = {
    1: 0.0, 2: 0.0, 3: 0.0,
    4: 0.2,  # UE17 直連(1) / (UE17(1) + 經 Node11,12 服務的 UE13~16(4)) = 1/5
    5: 1.0, 6: 1.0, 7: 1.0, 8: 1.0,
    9: 1.0, 10: 1.0, 11: 1.0, 12: 1.0,
}
```

| 節點 | 直連 UE | 間接服務 UE | role_ratio | 意義 |
|---|---|---|---|---|
| Node1,2,3 | 0 | >0（純 relay，下游全是別的 DU 節點） | **0.0** | 分子恆為 0，不管分母多少都是 0——「純種」relay 端點 |
| **Node4** | 1（UE17） | 4（經 Node11/12 服務的 UE13~16） | **0.2** | 唯一混合節點，1/(1+4) |
| Node5~12 | >0 | 0（純 access，下游沒有再接 DU） | **1.0** | 分母只剩分子自己，恆為 1——「純種」access 端點 |

---

## 3. 聚合公式與推導

### 3.1 公式

`IABClusterFedAvg.aggregate_train()`（`server_app.py:224-263`）不是把節點切成兩組
各自獨立 FedAvg，而是讓**每個節點依自己的 `role_ratio_i` 同時、按比例貢獻進兩個
「原型」模型**：

```
W_relay  = Σ_i (1-role_ratio_i)·n_i·ΔW_i  /  Σ_i (1-role_ratio_i)·n_i     (i ∈ 全部 12 節點)
W_access = Σ_i   role_ratio_i  ·n_i·ΔW_i  /  Σ_i   role_ratio_i  ·n_i     (i ∈ 全部 12 節點)
```

程式碼裡（`server_app.py:227-239`）用兩個桶子（`relay_items`／`access_items`）
收集每個節點的 `(flat_state_dict, weight)`，`weight` 分別是
`(1-role)*num_examples` 與 `role*num_examples`，再各自丟給共用的加權平均函式
`_weighted_average_flat()`（`server_app.py:134-143`）。

### 3.2 與標準硬分群公式的關係

標準硬分群公式：

```
W_{C_k}^{t+1} = Σ_{i∈C_k} (n_i/N_k) · W_i^{t+1}
```

求和範圍限定在「屬於 $C_k$ 的節點」，本質是離散指標函數 $\mathbb{1}(i\in C_k)$。
把上面 Stage 3 的公式攤開：

```
W_relay = Σ_i (1-role_ratio_i)·n_i·ΔW_i / Σ_i (1-role_ratio_i)·n_i
```

當 `role_ratio_i` 剛好都落在 `{0,1}`（純硬性指派、沒有混合節點）時，
`(1-role_ratio_i)` 就精確等於 relay 群的指標函數 $\mathbb{1}(i\in C_{relay})$，
分母 `Σ(1-role_ratio_i)·n_i` 就精確等於 $N_{relay}$，整條式子退化成跟標準公式
完全等價。**Stage 3 的聚合公式，是把標準公式裡的離散指標函數換成連續權重的
推廣版，不是另一套獨立設計**——差別只在於這個推廣允許 Node4 這種
`role_ratio=0.2` 的中間值同時、按比例貢獻進兩個聚合，而不是二選一。

### 3.3 冷啟動防呆

跟 Stage 2 的 `IABFedAvg` 共用同一套「全部節點 `num-examples=0` 時跳過本輪聚合」
的降級語意（見 `server_app.py:169-187` 的 `ZeroDivisionError` 防呆），但因為
cluster 模式要分兩組分別判斷，`_weighted_average_flat()` 改成「權重總和 ≤ 0 時
回傳 `None`」而非拋例外，讓 relay／access 兩側可以獨立判斷「這一側這輪是否有
新資料可聚合」——例如只有 relay 側節點這輪湊到訓練門檻，`w_access` 會是 `None`，
廣播時該側直接沿用上一輪結果，不影響 relay 側正常廣播。

---

## 4. 廣播公式

`_broadcast_cluster_weights()`（`server_app.py:266-290`）把兩個原型依每個節點
自己的 `role_ratio_i` 混合後，個別寫回各自的 checkpoint：

```
W_i(廣播回去) = (1 - role_ratio_i) · W_relay + role_ratio_i · W_access
```

| 節點 | 廣播內容 |
|---|---|
| Node1,2,3（role=0.0） | `1.0·W_relay + 0·W_access` = 純 `W_relay` |
| **Node4（role=0.2）** | `0.8·W_relay + 0.2·W_access` = **個人化混合權重** |
| Node5~12（role=1.0） | `0·W_relay + 1.0·W_access` = 純 `W_access` |

只有 Node4 真正吃到「連續」這個設計精神帶來的差異——其餘 11 個節點在這個公式下
數值上等同硬性二分群的結果。若 `w_relay`／`w_access` 任一為 `None`（該側這輪沒有
新資料，見 3.3），整段退化用另一個原型；兩者皆 `None` 時全部節點這輪都不更新，
等同 Stage 2 的 no-op 語意。

論文方法論章節可將本設計定位為 Clustered FL 的 soft/weighted 推廣版，精神上類似
Multi-Center FL 的 soft assignment、或 APFL（Adaptive Personalized FL）的模型
插值，只是插值對象換成 relay/access 兩個結構性原型，插值係數是拓樸結構直接算出的
`role_ratio_i` 而非額外學出來的參數。

---

## 5. 實作細節

### 5.1 `IABClusterFedAvg` 繼承 `IABFedAvg`

`server_app.py:203-263`。只覆寫 `aggregate_train()`；`aggregate_evaluate()`
（跟分群無關的診斷用 eval_loss 平均）直接繼承沿用，不重寫。

### 5.2 兩個原型的資料通道：`self._last_w_relay` / `self._last_w_access`

Flower 的 `Result` 物件一輪只能裝一個 `ArrayRecord`，裝不下「兩個原型」。
`aggregate_train()` 算完後直接存在 strategy 實例的 `self._last_w_relay`／
`self._last_w_access` 上（`server_app.py:244-247`），回傳值裡的 `arrays` 只是
用來滿足 Flower 內部「Result.arrays 非空」的判斷（`server_app.py:256-258` 的
註解特別強調這點），**真正的廣播邏輯在 `main()` 裡 `strategy.start()` 跑完後
直接讀這兩個屬性**（`server_app.py:365-369`），不透過 `aggregate_train()` 的
回傳值傳遞。

### 5.3 `client_app.py` 的必要改動：`node_id` 欄位

Stage 3 是這個檔案唯一需要改動的地方（CLAUDE.md 原本假設 Stage 2→3 只換 Global
聚合方式、`client_app.py` 完全不變，但這裡有一個例外）：`train()` 的回覆 metrics
多加一個整數欄位 `node_id`（`client_app.py:130-133`）。

原因：Server 端要依 `role_ratio_i` 分組加權，必須知道每筆回覆來自哪個實體節點，
但 Flower 的 `Message` 內部 node id 是 SuperLink 指派的亂數、跟本專案的
`NODE_ID`（1~12）沒有已知對應關係，只能由 client 端自己在 metrics 帶出來。
`evaluate()` 的回覆不受影響（跟分群無關，維持原樣）。

### 5.4 `FL_MODE` 切換

`main()`（`server_app.py:340`）依環境變數 `FL_MODE` 決定 instantiate
`IABClusterFedAvg`（`cluster`）還是 `IABFedAvg`（`avg`，預設值），比照既有的
`REWARD_MODE` 模式：

```bash
FL_MODE=cluster REWARD_MODE=throughput_only bash iab/run_stage2_fl.sh
```

`min_train_nodes`/`min_evaluate_nodes`/`min_available_nodes` 三個 Flower 參數
維持對全部 12 節點的門檻（`server_app.py:345-347`），因為現在是全體節點都貢獻
進兩個原型，不是子集分群，不需要拆成兩組各自的門檻。

---

## 6. 正確性驗證

上線前用獨立的純 Python 離線數學驗證腳本（不依賴 Docker/Flower，直接測
`_weighted_average_flat()` 加權平均與 `_broadcast_cluster_weights()` 混合廣播
公式），涵蓋：

- 已知輸入下的正確性（手算對照）。
- 邊界情況：某一側全部節點 `num-examples=0`（驗證 `None` 降級路徑不誤觸發另一側）。
- Node4 混合節點的廣播值介於兩原型之間、且不等於任一純原型（驗證連續加權真的
  生效，不是誤退化成硬分群）。

全部通過。

---

## 7. 量測結果摘要與已知限制

完整數據見 `experiment_results/clusterFL.md`。核心結論：

| 指標 | Stage 1 PF | Stage 2 avg FL | **Stage 3 cluster FL** |
|---|---|---|---|
| JFI | 0.3303 | 0.4779 | **0.4163** |
| 平均吞吐量 | 6.45 Mbps | 8.40 Mbps | **6.30 Mbps** |
| 平均 RTT | 224.23 ms | 296.07 ms | **372.57 ms** |

三項皆**低於** Stage 2，尚未達成 CLAUDE.md 規定的單調遞增要求（PF < avgFL <
clusterFL < ...）。15 分鐘全程三主機零新增崩潰，量測本身有效，但**聚合演算法
幾乎沒有機會真正運作**：`training_pipeline.py` 需要 200 筆連續原始經驗才觸發
訓練，15 分鐘視窗內 FL 觸發訓練幾乎每輪都因經驗數量不足而跳過聚合（`server_app.py`
的 `ZeroDivisionError` 防呆分支印出「全部節點 num-examples=0，跳過本輪聚合」）。

**這代表 Stage 2/3 觀察到的數據差異，主要反映 12 個節點各自獨立訓練軌跡的隨機
變異，不能直接歸因為聚合演算法本身有問題**——聚合公式已經過第 6 節的離線數學
驗證。為何落後、後續怎麼調整（拉長量測視窗／降低訓練門檻等）待下一輪討論，
Stage 4 設計書（`STAGE4_CUSTOM_FL_DESIGN.md` 第 1.2 節）已將這個風險獨立列為
必須正視、不能假設新演算法自動解決的既有限制。

---

## 8. 後續調整方向：量測視窗該拉長到多久？（2026-09-16 討論）

第 7 節已確認 Stage 3 落後的主因是「FL 聚合幾乎沒機會真正運作」，而不是聚合公式
本身有問題。這節接著討論一個具體問題：**下一次要重新量測 Stage 2 vs. Stage 3，
量測視窗該拉長到多久，才能讓兩者的聚合邏輯真正被觸發、比較才有意義？**

### 8.1 理論值嚴重低估所需時間，不能直接拿來用

`drl_agent.py` 的三個門檻常數：

```
MIN_TRAIN_EXPERIENCES = 200   # 第一道門檻：原始經驗筆數
TRAIN_SEQ_LEN = 32            # 每個訓練序列 32 步（100ms cadence ≈ 3.2 秒）
TRAIN_SEQ_COUNT = 16          # 一次梯度更新要 16 個序列 = 16×32 = 512 筆原始經驗（第二道門檻的下界）
```

C xApp 的 Rate Limiter 是「每 10 個 10ms MAC callback 觸發一次 ZMQ」，理論上限
**10 筆經驗/秒**（連續不中斷）。照這個理論值：200 筆只要 20 秒，512 筆（第二道
門檻真正需要的量）也只要 ~51 秒——遠小於 `FL_ROUND_INTERVAL_S=180` 秒一輪的間隔，
理論上每一輪都該輕鬆跨過門檻才對。

### 8.2 實測結果跟理論值差了兩個數量級以上

2026-09-16 查 `flower-supernode-node1` 的訓練 log，同一天早上連續 57 分鐘
（09:28~10:25，每 3 分鐘一輪，共 20 輪）**沒有任何一輪跨過 200 筆這第一道門檻**：

```
09:28:11 經驗數量不足 (需 200 筆)，等待更多資料累積...
09:31:10 經驗數量不足 (需 200 筆)，等待更多資料累積...
...（連續 20 輪皆同樣結果）...
10:25:30 經驗數量不足 (需 200 筆)，等待更多資料累積...
```

57 分鐘 vs. 理論值 20 秒，差距超過 170 倍。**這代表用理論上限反推「量測視窗該拉
長到多久」是不可靠的方法，估出來的數字很可能還是嚴重低估**，跟 Stage 3 已知的
「15 分鐘視窗完全不夠」互相印證，但無法告訴我們「到底要多久才夠」。

### 8.3 理論與實測落差的可能根因（尚未逐一驗證，留待後續排查）

1. **嚴格連續性要求放大中斷的代價**：`training_pipeline.py::_is_contiguous()`
   要求 `doc_a.next_state_vec == doc_b.state_vec` 逐位元組相等才算同一段連續
   運行；ZMQ 5ms 逾時 fallback、xApp 重連、FlexRIC pending event queue 相關
   問題（見 CLAUDE.md「FlexRIC 崩潰規律」段落），任何一次中斷都會把已經累積
   的連續運行整段砍斷重算，不是單純少一筆而已。
2. **Scenario R 的間歇閒置機率**：burst→idle→burst 的流量型態可能讓部分節點
   在 idle phase 沒有實際 DL 排程活動可寫入完整經驗，實際有效寫入速率遠低於
   「MAC callback 固定 10ms 觸發一次」暗示的上限。
3. 以上兩點目前只是推測，尚未在真正跑滿流量場景的即時系統上逐一驗證根因。

### 8.4 建議：用實測校準取代理論估計

不再嘗試用理論值或猜測的乘數去估視窗長度，改成**在真正的三主機 RAN +
`traffic_scenario.py` 跑起來時，直接量測真實的經驗累積速率**，用實測斜率反推。
校準腳本：`iab/calibrate_fl_rate.py`。

```bash
# 只需在 PC1 執行（MongoDB 只在 PC1），搭配三主機 RAN + traffic_scenario.py 同時在跑，
# 最好是 MongoDB 剛清空的乾淨起點，避免舊資料汙染成長曲線
python3 iab/calibrate_fl_rate.py --duration 600 --interval 30 --out /tmp/fl_calibration.csv
```

腳本邏輯：每 `--interval` 秒輪詢一次全部 `NUM_NODES` 個節點的
`node{N}_experiences` collection，用跟 `training_pipeline.py::fetch_sequences()`
完全相同的 filter（`reward` + `next_state_vec` 皆存在）算 `count_documents()`，
跑完後用「(最新筆數 − 第一筆數) / 經過秒數」算出每個節點的實測速率，取
**全網最慢的節點**（而非平均）當瓶頸——因為 `server_app.py` 的
`min_train_nodes=NUM_NODES` 要求全部節點都回覆，單一節點沒湊到門檻會拖累它
自己那份貢獻的 `num-examples=0`，也正好是 Stage 3 `role_ratio` 加權要吃到
有意義訊號的前提。最後套用安全係數（腳本預設 3x）估出建議的量測視窗長度，
確保 180 秒一輪的 FL 聚合有機會在視窗內反覆命中，而不是壓線剛好跨過一次。

**下一輪重測 Stage 2/3 之前，先跑一次這支校準腳本，把實測出的視窗長度寫回這份
文件，取代目前「拉長到多久」的未知數**，再決定要不要同時考慮 8.3 節的根因
（例如放寬 `_is_contiguous()` 的連續性要求、或降低 `MIN_TRAIN_EXPERIENCES`／
`TRAIN_SEQ_COUNT` 門檻）——但門檻本身的調整要謹慎：門檻是為了確保訓練資料
「真的是時間上連續的序列」給 GRU 學習，不是任意調低都沒代價，校準腳本的目的
是先把「現況到底多慢」量準確，再決定该動視窗長度還是動門檻本身。

> **2026-09-18 更新**：上述「連續性門檻不能隨便放寬」的但書**僅在
> `MODEL_ARCH=gru` 時適用**。`drl_agent.py` 現在預設 `MODEL_ARCH=mlp`
> （Stage 2~4「最基礎 DRL」的架構釐清，見 `DRL_METHODOLOGY_PLAN.md` 補記），
> `mlp` 模式下訓練走 `training_pipeline.fetch_experiences()` 打散抽樣，
> 完全不受 `_is_contiguous()`／`TRAIN_SEQ_LEN`／`TRAIN_SEQ_COUNT` 限制，只看
> `MIN_TRAIN_EXPERIENCES=200` 原始經驗數——上面提到「理論 20 秒 vs 實測 57
> 分鐘」的落差，主要根因就是這個序列連續性門檻本身，`MODEL_ARCH=mlp` 應該
> 從根本上緩解這個問題，不需要再靠放寬門檻這個間接手段。這份校準腳本仍然
> 有用，但下一輪重測應該用 `MODEL_ARCH=mlp` 重新跑一次校準，不能沿用這裡
> GRU 時期量到的數字。

---

## 9. 與後續階段的關係

- **Stage 4**：在本設計的 relay/access 兩原型混合廣播結構之上疊加
  reward-deficit 加權（`IABCustomFedAvg`），不推翻重來——見
  `STAGE4_CUSTOM_FL_DESIGN.md` 第 2 節設計邊界第 2 點。
- **Stage 5**：只換 `REWARD_MODE=lagrangian`（Local 層），Global 聚合方式沿用
  Stage 4（因此間接沿用本文件的角色加權結構），不在本階段變動。
