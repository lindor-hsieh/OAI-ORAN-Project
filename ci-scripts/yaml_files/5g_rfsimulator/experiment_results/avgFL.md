# Stage 2 — avg FL + 最基礎 DRL 量測結果

> **最新結果請看文末「2026-09-27 — 新拓樸＋修正後平台、兩狀態 Scenario T 的 Stage 2 結果」章節。** 本檔前面的數字是舊拓樸、舊平台（網卡瓶頸、通道未真正惡化）下的結果，已作廢，勿引用比較。

**量測日期**：2026-09-19（三度重測版，取代 2026-09-14 的舊版本）；文末另含 2026-09-21 Scenario T 量測
**狀態**：全部 12 個 Local xApp+Local rApp 上線，`MODEL_ARCH=mlp`（取代先前悄悄引入的 GRU 架構，見下方「本次重測原因」）、`REWARD_MODE=throughput_only`，Global xApp（全域公平性廣播）+ Global rApp（標準 FedAvg，`FL_MODE=avg`，12 節點一起聚合）全程運作
**比較基準**：`PF.md`（Stage 1，JFI=0.3303、平均吞吐量 6.45 Mbps、平均 RTT 224.23 ms，seed `20260914`）

> **⚠️ 量測條件警語（2026-09-25 加註）**：本文件所有量測都發生在 PC3 的 USB 網卡接在 USB 2.0 埠（自 2026-09-11 19:13 起，實測跨主機頻寬上限約 320 Mbps，且 PC3 有流量時 PC2 的連線也被拖慢，見 `HISTORY.md` 2026-09-25「跨主機延遲根因」條目與 `CLAUDE.md` §8）的環境下。所有「跨主機節點吞吐量偏低／RTT 偏高／UE 間差異」的數字與歸因，都混有這個硬體瓶頸的影響，**不代表 IAB 多跳架構或排程/FL 演算法本身的表現**；實體修復後需重測再引用。文中數據保留為歷史記錄，不修改。

> **歷史註記（前兩次量測均已作廢，數字不可引用）**：第一次因乾淨重啟漏跑 `run_local_pc1.sh --skip-server`、12 個 xApp 容器從未啟動而作廢（教訓：乾淨重啟後光看 UE 連通性與 RestartCount 不足以代表 DRL 控制迴圈在生效，必須額外驗證 xApp 容器與 E2SM-MAC CONTROL-REQUEST）；第二次補測開始時仍有 5/17 UE（UE9、UE10、UE15、UE16、UE17）連不通，匯總指標被拉低、不建議引用。Stage 2 的 6.5 小時訓練 checkpoint 未受影響（封存於 `experiment_results/checkpoints_archive/stage2_avgfl_20260919/`）。完整過程見 `HISTORY.md` 2026-09-19 條目。

> ## ✅ 2026-09-19 第三次補測（下方「量測結果」已換成這次的乾淨數字，建議以這次為準）
>
> 這次量測前**逐項驗證過才開始**，吸取前兩次的教訓：三主機依序乾淨重啟（過程中修過 PC1 host 對 relay tunnel 的過期路由、PC1 本機 Node7/8 的 DNAT 漂移）→ 17/17 UE 現場 ping 確認 0% 封包遺失（**含 UE17**，這是本文件第一次做到 17/17 全部連通才開始量測）→ `docker ps` 確認 12/12 xApp 容器存在、`xapp-node1` log 確認真實的 `CONTROL-REQUEST tx`/`CONTROL ACK rx` 訊號持續出現（不是只看容器數量）→ `/proc/1/environ` 逐一確認 `inference-nodeN`／`flower-supernode-nodeN` 的 `REWARD_MODE=throughput_only`、`MODEL_ARCH=mlp`，`flower-superlink` 的 `FL_MODE=avg`→ 確認載入的 checkpoint 訓練步數 2860（延續 Stage 2 原本的訓練歷史，不是重置）。這是三次補測裡唯一一次排除了全部已知 confound 的乾淨量測，下方數字建議以此為準，前兩次的數字保留供對照但不建議引用。

## 本次重測原因（2026-09-14 版本已作廢）

與 2026-09-14 版本的差異：（1）Local DRL 由悄悄引入的 GRU 改為 `MODEL_ARCH=mlp`（新增 `MODEL_ARCH` 開關，Stage 2~4 統一用 mlp，GRU 保留供 Stage 5 或未來研究），mlp 訓練不受 GRU 時間連續性序列門檻限制、只看 `MIN_TRAIN_EXPERIENCES=200`；（2）真正訓練足夠久（MongoDB 經驗橫跨 2026-09-18 07:29~13:56 UTC，約 6.5 小時，搭配 `training_scenario_driver.sh`／`training_watchdog.sh`）；（3）relay/access DU 的 Random Access process pool 耗盡自動修復上線；（4）`start_iab_server.sh` 直接執行時 `inference-nodeN` 未帶 `REWARD_MODE`/`MODEL_ARCH` 而悄悄套用 `lagrangian` 預設值的 bug 已修復，污染到的 32 筆經驗已用 `iab/clean_lambda_contamination.py --execute` 清除（佔當時 114,140 筆的 0.03%；`clusterFL.md` 封存時數字為 114,108 筆，兩者是不同時間點的計數）。

2026-09-13 首次版本（JFI=0.3976、6.58 Mbps、360.70 ms）作廢原因：`flower-supernode-nodeN` 訓練路徑漏接 `REWARD_MODE`、以及兩個基礎設施問題（CU NAT table 被無條件 flush、cpuset 過度擁擠），修復後重測。以上完整根因分析見 `HISTORY.md` 2026-09-14、2026-09-18、2026-09-19 條目。

## 架構摘要（本階段新增於 PF baseline 之上的元件）

- **Local xApp + Local rApp**：與 PF baseline 相同的 C xApp 控制迴圈，但 Local rApp 從「純被動蒐集資料」改為主動執行 DRL Actor 推論並下發 PRB 權重（`REWARD_MODE=throughput_only`：`R = R_throughput`，無 JFI 限制式、λ 恆為 0——**這次已同時確認 `inference-nodeN` 與 `flower-supernode-nodeN` 兩條訓練路徑都正確套用**，見下方「上線前驗證」）。
- **Global xApp**：獨立 Python process，每 2 秒讀一次 MongoDB 全部 12 個節點的最近經驗，算出每個節點「相對全域平均吞吐量」的落差，換算成 `fairness_bias ∈ [0.5, 2.0]`，透過 ZMQ PUB 廣播給全部 12 個 Local rApp，寫入 DRL state vector 的第 49 維。純軟性 state 特徵，不做任何硬性 PRB 裁切。
- **Global rApp**：Flower ServerApp/ClientApp 標準 FedAvg（`IABFedAvg`，`FL_MODE=avg`），`FL_NUM_NODES=12`，`FL_ROUND_INTERVAL_S=180`。

## 上線前驗證（全部通過才進行正式量測）

1. **REWARD_MODE/MODEL_ARCH 雙路徑確認**：`docker exec inference-node{1..12} env | grep -E "REWARD_MODE|MODEL_ARCH"` 全部為 `throughput_only`/`mlp`；`flower-superlink` 的 `FL_MODE=avg`、`MODEL_ARCH=mlp`。
2. **清空後乾淨重啟**：三主機**完全依序**（不同時）啟動——`start_iab_server.sh`（PC1 基礎設施）跑完 → PC2 完整跑完 → PC3 完整跑完，13/13 E2 連線、12/12 xApp 零 fallback。
3. **量測前現場連通性**：全部 17 個 UE（含 UE17）現場 ping 測試皆為 0% 封包遺失（UE16、UE17 在乾淨重啟後各自遇到一次已知的獨立問題——UE16 是 RLC 層 RLF 卡住，重啟該 UE 容器解決；UE17 是預設路由在 PDU session 重建後消失，重新斷言路由解決，兩者皆非本次新增的 bug，修法見 `HISTORY.md`）。
4. **三主機 CU/DU/FlexRIC 與全部 12 個 relay/access DU 的 RestartCount 基準點確認為 0**，量測全程未再增加（見下方系統穩定性）。

## 量測方法（與 PF.md 完全相同）

```bash
python3 scenarios/traffic_scenario.py --scenario R --seed 20260914 --host {pc1,pc2,pc3} --phase-duration 60 --num-phases 15
python3 iab/measure_stage.py --host {pc1,pc2,pc3} --duration 900 --interval 5 --out /tmp/stage2clean_trial_{host}.csv
```

DRL（`inference-nodeN`／Global xApp／`flower-*`）全程保持啟用。同一組 Scenario R、同一個 seed（`20260914`），與 PF.md 構成有效的 paired comparison。這次量測用的是 Stage 2 封存的 checkpoint（原始 6.5 小時訓練 + 第二次補測期間累積的少量額外訓練，載入時訓練步數 2860），量測開始前 17/17 UE 已確認連通。

**全程系統穩定性**：本次 15 分鐘量測全程，**三主機的 FlexRIC、Donor CU/DU、全部 12 個 relay/access DU/MT 容器 `RestartCount` 全部維持不變（零新增崩潰）**。

## 量測結果（全部 17 個 UE，乾淨版本——量測開始時 17/17 UE 皆已確認連通）

| UE | 取樣總數 | 吞吐量取樣數 | RTT取樣數 | 平均吞吐量 (Mbps) | 平均 RTT (ms) | 吞吐量覆蓋率 | RTT覆蓋率 |
|---|---|---|---|---|---|---|---|
| UE1  | 146 | 143 | 118 | 28.55 | 109.74 | 97.9% | 80.8% |
| UE2  | 146 | 116 | 71  | 2.27  | 178.54 | 79.5% | 48.6% |
| UE3  | 146 | 142 | 41  | 2.30  | 248.23 | 97.3% | 28.1% |
| UE4  | 146 | 79  | 39  | 1.46  | 259.95 | 54.1% | 26.7% |
| UE5  | 148 | 145 | 148 | 35.42 | 145.14 | 98.0% | 100.0% |
| UE6  | 148 | 105 | 23  | 2.70  | 124.96 | 70.9% | 15.5% |
| UE7  | 148 | 147 | 139 | 33.32 | 77.76  | 99.3% | 93.9% |
| UE8  | 148 | 92  | 25  | 2.56  | 149.86 | 62.2% | 16.9% |
| UE9  | 148 | 67  | 11  | 1.69  | 335.72 | 45.3% | 7.4%  |
| UE10 | 148 | 41  | 11  | 2.09  | 388.73 | 27.7% | 7.4%  |
| UE11 | 148 | 15  | 4   | 0.61  | 323.75 | 10.1% | 2.7%  |
| UE12 | 148 | 57  | 4   | 0.57  | 385.00 | 38.5% | 2.7%  |
| UE13 | 148 | 0   | 6   | 0.00  | 304.17 | 0.0%  | 4.1%  |
| UE14 | 148 | 10  | 6   | 0.03  | 289.17 | 6.8%  | 4.1%  |
| UE15 | 148 | 57  | 8   | 0.45  | 451.75 | 38.5% | 5.4%  |
| UE16 | 148 | 55  | 8   | 0.36  | 389.75 | 37.2% | 5.4%  |
| UE17 | 146 | 47  | 6   | 1.44  | 716.83 | 32.2% | 4.1%  |

## 摘要指標與 PF.md 比較

| 指標 | PF baseline (Stage 1) | avg FL (Stage 2，乾淨重測) | 變化 |
|---|---|---|---|
| Jain's Fairness Index | 0.3303 | **0.2453** | **惡化 −25.7%** ❌ |
| 平均吞吐量（17 UE 平均） | 6.45 Mbps | **6.81 Mbps** | **改善 +5.6%** ✅ |
| 平均 RTT（有效樣本平均，排除 UE17） | 224.23 ms | **260.14 ms** | **惡化 +16.0%** ❌ |
| 零吞吐量樣本的 UE 數 | 0/17 | 1/17（UE13） | 惡化 |
| 全程崩潰/重啟 | 0 | 0 | 持平 |

## 結論（誠實記錄，這次是乾淨量測，數字可信度較高）

1. **量測本身是乾淨的**：開始前確認 17/17 UE 連通（含 UE17，本文件第一次做到）、12/12 xApp 真實運作、三個環境變數皆正確，前兩版本的已知 confound 都已排除。這份數字可以視為目前對 avg FL 較可信的量測結果。
2. **結果是混合的，不是單純變好或變差**：平均吞吐量小幅優於 PF baseline（+5.6%），但 JFI 明顯惡化（−25.7%）、RTT 也惡化（+16.0%）。這代表這次的 policy 傾向把資源集中給少數表現好的節點換取更高的總吞吐量，但犧牲了公平性——UE1、UE5、UE7 這三個節點吞吐量特別突出（28~35 Mbps），但 UE13 掉到零、UE11/UE14 也接近零，拉低了 JFI。
3. **JFI 明顯低於 PF baseline 值得關注**：0.3303 → 0.2453 是本文件目前看到最差的 JFI（比先前兩次作廢的補測都低），不是連通性問題造成的（這次全部 UE 都連通），比較像是 policy 本身在這次量測窗口內做出了偏向少數節點的分配決策。這跟 `REWARD_MODE=throughput_only`（無 JFI 限制式）的設計是一致的——沒有 Lagrangian 公平性懲罰項時，policy 沒有直接誘因去平衡各節點的吞吐量，只追求總量最大化，Stage 5 規劃要重新啟用的 `REWARD_MODE=lagrangian` 正是要處理這個問題。
4. **不算「優於 PF」，也不算「明顯變差」**：吞吐量微幅勝出，但公平性與延遲都輸，三項指標沒有同時朝同一個方向移動，不滿足路線圖「單調遞增」的驗收標準（PF < avg FL 需要三項指標同時變好）。
5. **與 cluster FL（Stage 3）的比較**：`clusterFL.md` 同樣有一份乾淨重測的結果（JFI=0.3113、吞吐量=5.23 Mbps、RTT=301.93ms），三項都劣於 PF。兩相對照，avg FL 在吞吐量上表現優於 cluster FL 與 PF，但 JFI 是三者最差；cluster FL 則是三項都輸給 PF，但輸的幅度都不像 avg FL 的 JFI 那麼極端。這暗示目前的 cluster 聚合機制（依角色比例加權）可能反而讓 policy 在公平性上比單純 FedAvg 更保守（單次量測，且受頁首警語影響，此推測未經驗證）。
6. **建議**：若要進一步驗證「avg FL 犧牲公平性換吞吐量」這個假設，可以考慮啟用 `REWARD_MODE=lagrangian`（Stage 5 規劃項目）重新訓練後再比較 JFI 是否回升；目前的乾淨數字已經是三次補測中最可信的一份，可以作為後續比較的基準。

## 下一步

已完成：PF baseline 的同夜重測見 `PF.md` 2026-09-19 章節。

## 2026-09-21 — Scenario T 量測（PF／avg FL／cluster FL 三方乾淨比較的第二階段）

**量測日期**：2026-09-21（凌晨）
**狀態**：12/12 xApp 真實運作中，`REWARD_MODE=throughput_only`／`MODEL_ARCH=mlp`／`FL_MODE=avg` 逐一用 `/proc/1/environ` 確認正確，`inference-node1` log 確認載入訓練步數 4573 的真實 checkpoint
**動機**：與 `PF.md` 2026-09-21 條目同一批三方比較的第二階段，改用 Scenario T（3×3 流量×路徑損耗交叉設計）取代先前的 Scenario R，理由見 `PF.md` 對應章節與 `HISTORY.md` 2026-09-20 條目。

**⚠️ 重要限制，必須誠實揭露：這份 checkpoint 的訓練資料品質有已知問題**。這次的 Stage 2 重訓（2026-09-20 整晚，累積約 15 萬筆經驗）進行期間，`rfsim5g-oai-ext-dn` 的 iperf3 server 一直只監聽預設埠（5201），而非 `traffic_scenario.py` 需要的 5201~5217 全部 17 個獨立埠（根因與修復過程見 `HISTORY.md` 2026-09-20~21 條目）。用 MongoDB 資料實測驗證：12 個節點裡只有 Node1、Node5（剛好承載 UE1 的流量路徑，UE1 對應的埠 5201 是唯一始終正常的）有真實的 MAC 層活動（avgBsr 約 22~23 萬），其餘 10 個節點（Node2,3,4,6,7,8,9,10,11,12）全數趨近於零（avgBsr 個位數到數十）。**代表這份 checkpoint 訓練期間，10/12 節點幾乎沒有接收到真實壅塞流量，DRL 學到的主要是「近乎閒置」情境下的決策，不是這次重訓原本想要補足的 Scenario T 真實壅塞曝光**。使用者明確決定：不因此作廢重訓、不重新訓練，**照舊使用這份 checkpoint 完成這次三方比較，並在此如實註記這個限制**——下方數據應被視為「這份特定訓練品質下的 avg FL 表現」，不是「avg FL 演算法在充分訓練後的代表性表現」。

**量測前逐項驗證**：17/17 UE 現場 ping 確認 0% 封包遺失（含 UE17）；`docker ps` 確認 12/12 xApp 容器皆為 running；Scenario T、`measure_stage.py`，15 分鐘（900s）、每 5 秒取樣一次；量測過程中三主機 `RestartCount` 全程無新增崩潰。

### 量測結果（全部 17 個 UE）

| UE | 取樣總數 | 吞吐量取樣數 | RTT取樣數 | 平均吞吐量 (Mbps) | 平均 RTT (ms) | 吞吐量覆蓋率 | RTT覆蓋率 |
|---|---|---|---|---|---|---|---|
| UE1  | 148 | 121 | 7   | 2.18  | 315.91 | 81.8% | 4.7%  |
| UE2  | 148 | 113 | 7   | 1.94  | 324.74 | 76.4% | 4.7%  |
| UE3  | 148 | 120 | 7   | 2.00  | 311.50 | 81.1% | 4.7%  |
| UE4  | 148 | 104 | 7   | 1.60  | 319.61 | 70.3% | 4.7%  |
| UE5  | 149 | 146 | 38  | 6.99  | 257.83 | 98.0% | 25.5% |
| UE6  | 149 | 148 | 136 | 23.73 | 249.85 | 99.3% | 91.3% |
| UE7  | 149 | 144 | 68  | 7.60  | 270.95 | 96.6% | 45.6% |
| UE8  | 149 | 146 | 144 | 19.39 | 200.18 | 98.0% | 96.6% |
| UE9  | 148 | 104 | 6   | 0.86  | 541.83 | 70.3% | 4.1%  |
| UE10 | 148 | 98  | 7   | 0.96  | 601.14 | 66.2% | 4.7%  |
| UE11 | 148 | 116 | 15  | 1.31  | 328.13 | 78.4% | 10.1% |
| UE12 | 148 | 107 | 15  | 1.21  | 349.80 | 72.3% | 10.1% |
| UE13 | 148 | 83  | 5   | 0.61  | 507.20 | 56.1% | 3.4%  |
| UE14 | 148 | 82  | 5   | 0.58  | 527.20 | 55.4% | 3.4%  |
| UE15 | 148 | 102 | 7   | 0.84  | 404.86 | 68.9% | 4.7%  |
| UE16 | 148 | 55  | 7   | 0.84  | 411.29 | 37.2% | 4.7%  |
| UE17 | 148 | 122 | 3   | 1.28  | 896.67 | 82.4% | 2.0%  |

### 摘要指標與 PF.md（同為 Scenario T）比較

| 指標 | PF baseline (Scenario T) | avg FL (Scenario T) | 變化 |
|---|---|---|---|
| Jain's Fairness Index | 0.2812 | **0.3007** | **改善 +6.9%** ✅ |
| 平均吞吐量（17 UE 平均） | 4.23 Mbps | **4.35 Mbps** | **改善 +2.8%** ✅ |
| 平均 RTT（有效樣本平均，排除 UE17） | 308.66 ms | **370.13 ms** | **惡化 +19.9%** ❌ |
| 零吞吐量樣本的 UE 數 | 0/17 | 0/17 | 持平 |

### 結論

1. **三項指標中兩項優於 PF、一項劣於 PF**（JFI +6.9%／吞吐量 +2.8% 小於 PF 自身兩次重測間的雜訊 ~10%，不宜視為顯著）：JFI 與吞吐量都比 PF baseline 好，但 RTT 明顯變差（+19.9%）。跟先前 Scenario R 下的乾淨重測（JFI 惡化、吞吐量小贏、RTT 惡化）方向不完全一樣——這次 JFI 反而是改善的，但同樣沒有滿足「三項同時變好」的單調遞增驗收標準。
2. **必須把上方揭露的訓練資料品質限制納入解讀**：這份結果反映的是「10/12 節點訓練期間近乎閒置」的 policy 表現，不是完整壅塞情境訓練下的代表性結果。RTT 變差可能反映 policy 對多數節點的真實壅塞決策經驗不足；JFI/吞吐量小幅改善則可能只是巧合或訓練不足下的隨機表現，都不宜過度解讀因果。
3. **與 PF.md 同批比較，不與先前 Scenario R 的舊數字比較**：場景不同、checkpoint 也不同（這次是全新重訓的 checkpoint，非先前補測用的封存版本），混用會製造新的 confound。
4. **零吞吐量 UE 數 0/17**，量測本身乾淨可信；限制在於底層訓練資料，不在量測方法。

---

## 2026-09-27 — 新拓樸＋修正後平台、兩狀態 Scenario T 的 Stage 2 結果（TCP／UDP 各一份）

> **本章節取代上方全部舊拓樸、舊平台的數字（作廢，勿與 Stage 3~5 比較）。** 比較對象是 `PF.md` 最末章節（2026-09-26 兩狀態 Scenario T 的 PF 基準）。數字皆為模擬時間（吞吐量 ÷S、RTT ×S，S=0.4）。原始資料、分析輸出與訓練紀錄在 `data_20260927_stage2_twostate/`。

### 訓練設定
- **系統**：12 個 Local xApp + 12 個 Local rApp（MLP、`REWARD_MODE=throughput_only`）、Global xApp、Global rApp（`FL_MODE=avg`，每 180 秒一輪）；E2 `bh_ratio` 為狀態第 51 維，PPO 比例裁剪、只用壅塞樣本更新 Actor（門檻 `CONTENDED_BUF_BYTES=100000`），設定細節見 CLAUDE.md 第 2 節。
- **訓練時間**：2026-09-26 21:39:25 ~ 2026-09-27 03:39:25（6 小時，牆鐘）。**訓練流量只用 TCP**（`training_scenario_driver.sh --protocol tcp`，TR 與 R 輪替）；UDP 沒有訓練過，UDP 量測是拿 TCP 訓練的模型去跑。
- **訓練量**：各節點訓練步數 4300~4450（FINAL checkpoint），MongoDB 每節點約 19400~20100 筆經驗。
- **訓練期間中斷**：整套重啟 3 次（21:56 UE13 崩潰、22:23 UE14 崩潰、01:38 FlexRIC segfault，各中斷約 9 分鐘）；22:13 watchdog 冷卻窗內第二次崩潰而停手，人工補救；UE 就地修復 3 次（UE8／UE10／UE15，全成功）；UE12 UPF session 掉線一次（人工停止再啟動 UE 容器修復）。全程約 30 分鐘沒有訓練流量。MongoDB 經驗與模型 checkpoint 在重啟中沒有遺失。
- **已知的訓練資料限制**：relay 節點（node1／node2）的壅塞樣本比例只有約 2~6%（目標 35~45%；access 節點 25~40%、node4 約 50~60%），常低於 Actor 更新門檻（每批 8 筆），它們的 Actor 幾乎沒有自己的更新，主要由 FL 聚合帶入其他節點的權重（後者是推論，未驗證）。原因分析見 HISTORY.md 2026-09-27。

### 量測設定
- 每一次量測前都做：`clean_env.sh` → 三台依序重啟 → 預檢查（13/13 E2、xApp 12/12、全部 RestartCount=0、17 UE ping 0% 遺失、17 個 iperf3 server、S 一致）；**訓練與 FL 在量測期間都開著**（線上學習持續）。TCP 量測期間各節點又增加約 280 步（node1：4860→5140），UDP 約 280 步（5210→5490）。
- 兩狀態 Scenario T：11 個相位（110 秒）＝1230 秒取樣、每 5 秒一筆；壅塞相位 5/11。與 PF 基準完全同一場景與同一份分析腳本（`analyze_stage.py`）。
- **TCP 第一次嘗試（`tcp_a1_INVALID_ue2_crash`）無效**：UE2 於量測第 17 分鐘崩潰（CrashGuard 中止），資料保留但不採用；第二次（`tcp_a2`）有效。UDP 第一次嘗試即有效（`udp_a1`）。

### 結果與 PF 基準比較

| 指標（壅塞相位為鑑別指標） | TCP：Stage 2 | TCP：PF | UDP：Stage 2 | UDP：PF |
|---|---|---|---|---|
| **壅塞相位 需求滿足率** | **0.667** | 0.767 | **0.704** | 0.789 |
| **壅塞相位 滿足率 JFI** | **0.862** | 0.936 | **0.892** | 0.945 |
| 壅塞相位 RTT（ms） | 119.0 | 115.7 | 45.4 | 32.3 |
| 壅塞相位 總送達／總目標 | 76／129 | 94／129 | 72／124 | 89／126 |
| 壅塞相位 過載 UE 比例 | 54% | 41% | 59% | 55% |
| 正常相位 需求滿足率（對照） | 0.907 | 0.896 | 0.996 | 0.999 |
| 整段 JFI（時間平均，天然偏高） | 0.9746 | 0.9871 | 0.9692 | 0.9831 |
| 平均吞吐量（Mbps） | 4.40 | 4.93 | 4.35 | 4.80 |
| 整段平均 RTT（ms） | 64.2 | 59.3 | 37.1 | 25.9 |

- **場景確實壅塞**：Stage 2 壅塞相位 54%（TCP）／59%（UDP）的 UE 過載、正常相位 ≈0~1%，與 PF 同量級；量測有效。
- **UE17（Node4 直連 UE）壅塞相位滿足率**：TCP 0.23（PF 0.77）、UDP 0.40（PF 0.58），是所有 UE 中最低，且都比 PF 更低。
- 各 UE 逐相位明細見 `data_20260927_stage2_twostate/{tcp_a2,udp_a1}/analysis.txt`。

### 結論（事實部分）
1. **在鑑別指標（壅塞相位滿足率、滿足率 JFI）與整段 JFI、吞吐量上，Stage 2 在 TCP 與 UDP 都未優於 PF**（壅塞相位滿足率低 0.10／0.085，滿足率 JFI 低 0.074／0.053，吞吐量低約 11%／9%）；RTT 方面 TCP 持平略高、UDP 明顯較高（+43%）。**Stage 2 尚未達成「贏過前一階」的單調遞增要求。**
2. 正常相位滿足率與 PF 相近（TCP 0.907 對 0.896、UDP 0.996 對 0.999），差距主要出現在壅塞相位。
3. **原因未驗證**。可能的方向（皆未檢驗）：DRL 政策本身、量測期間線上學習繼續改變模型、約 1.5% 的推論因超過 5ms 逾時而退回 PF、relay 節點 Actor 學得很少、UDP 沒有訓練。**不可把差異直接歸因為聚合演算法**（Stage 2 尚無 Stage 3 的同條件對照）。

### 方法論限制（供論文引用）
- 量測期間線上訓練與 FL 都開著，量測前後模型步數不同（TCP 與 UDP 各增加約 280 步）；TCP 與 UDP 兩次量測的起點模型也不完全相同（UDP 起點比 TCP 起點多約 350 步）。
- 只用 TCP 訓練；PF 沒有 xApp 與推論延遲；relay 節點壅塞樣本結構性偏少（MT 通道不受場景惡化，壅塞出現在 access／UE17）。
- 單次量測（每種協定一份有效資料），沒有重複次數，PF 自身重測雜訊約 10%（舊拓樸數據），本表差距是否顯著未檢驗。

### 檔案位置
- 原始資料、分析與流程紀錄：`experiment_results/data_20260927_stage2_twostate/`（含 `training_watchdog.log`、`review.txt`）。
- checkpoint：`checkpoints_archive/stage2_avgfl_tcp_20260926/`（每 20 分鐘一份；`FINAL_0339` 訓練結束、`PREMEASURE_0340` 量測前、`POSTMEASURE_0529` 量測後）。
- MongoDB 經驗：資料庫內複本 `node{N}_experiences_stage2_avgfl_tcp_20260926`（12 個，原 collection 未動）；檔案備份在 `/home/lindor/stage2_run_20260926/archive/mongo_iab_xapp_stage2_avgfl_tcp_20260926.archive.gz`（29 MB，不在版控內）。

---

## 2026-09-27（續）— 動作對應設計問題的診斷與修正（初步結果，正式定案待補跑 6 小時訓練）

**背景**：上方「2026-09-27 — 新拓樸＋修正後平台」章節的 6 小時訓練結果在 TCP、UDP 都劣於 PF。使用者要求查明原因並改善，找到並修正兩個獨立問題，診斷過程見 `HISTORY.md`。**下方數字都是診斷/修正過程的初步驗證（未跑滿 6 小時），不是正式 Stage 2 驗收結果**——正式結果待補跑一次完整 6 小時訓練（新設計）後才定案，屆時取代本章節與上方章節。

### 問題一：動作→PRB 上限的對應方式讓 PF 不是策略可達的動作

**原設計（`DRL_CAP_MODE=split`）**：Dirichlet 採樣值（總和為 1）直接乘以 106 當每 UE 的 PRB 上限。兩個 UE 時，均分（各 0.5）代表「各自被截斷在 53 PRB」，PF（不截斷）不是這個動作空間裡能表達的點——只要有 ≥2 個活躍 UE，每個 slot 都被人為切分頻域，即使策略沒學到任何東西，結果也會系統性劣於 PF。

**修正（`DRL_CAP_MODE=relative`，2026-09-27 起預設）**：上限改為 `min(1, 活躍UE數 × 份額)`。均分時所有 UE 上限都是 1.0（= 不截斷 = PF），策略只在偏離均分時才限制份額低於平均的 UE。PF 因此是策略空間內的恆等點：學不到東西時退化成 PF，而不是系統性劣於 PF。只改 Python 端（`inference/drl_agent.py::infer()`），C 端 xApp 逐 UE 獨立換算比例、夾到 [0,1]，不需要改 C 程式。

**驗證**：拿舊 6 小時訓練出的 checkpoint、不重新訓練，只切換 `DRL_CAP_MODE=relative`、凍結模型（`DRL_TRAIN_ENABLED=0`）跑一次 TCP 量測（`e3_relative_randomsample_tcp.txt`）：壅塞相位總送達 92（對照 PF 94、舊 split 版 76），吞吐量 4.82 Mbps（對照 PF 4.93、舊版 4.40）——單獨這個修正就已經把大部分缺口拉近。

### 問題二：Dirichlet 採樣雜訊在新對應下是非對稱的損耗

**分析**：對 2 小時暖啟動再訓練（見下）期間的 reward 按 15 分鐘分段檢視，12 節點平均值在 0.14~0.18 間來回震盪、無上升趨勢，代表訓練很快就到了目前設定下的平衡點，單純延長訓練時間效益有限（相關數字見 `HISTORY.md`）。推論階段的 Dirichlet 採樣雜訊在 `relative` 上限下是非對稱損耗：採樣偏高的 UE 上限被夾在 1.0（沒有額外好處，白白浪費潛在份額），採樣偏低的 UE 上限直接降低、扣掉吞吐量——雜訊本身、與策略是否收斂無關，會系統性拉低平均送達量。

**修正**：新增 `DRL_DETERMINISTIC` 開關（`inference/drl_agent.py`）。開啟時 `infer()` 直接用 Actor 的機率當份額（Dirichlet 期望值），不做採樣，訓練邏輯完全不動（訓練時仍用隨機採樣做探索、計算 `behavior_logp` 梯度）；只在凍結模型做評估/量測時開啟，是標準的「訓練隨機、評估用均值」做法。

### 診斷用的暖啟動再訓練（2 小時，非正式 6 小時訓練）

用舊 6 小時訓練出的 checkpoint 當起點（保留權重，只清空 MongoDB 經驗——`run_stage2_fl.sh` 新增 `STAGE2_KEEP_MODELS=1`），`DRL_CAP_MODE=relative`、`REWARD_MODE=throughput_only`、FL 開著，訓練場景改 TCP/UDP 混合，跑 2 小時（12:05~14:05）。訓練中 3 顆 UE 各崩潰 1~2 次（UE4、UE13×2、UE15），皆由 watchdog 就地修復成功，無整套重啟。

### 結果對照（TCP，數字為模擬時間；`DRL_CAP_MODE=relative` 全程適用）

| 指標 | PF 基準 | 舊 6h（`split`） | 只換對應（未訓練，`e3`） | +2h 暖訓練（隨機採樣） | +確定性輸出（`e4`） |
|---|---|---|---|---|---|
| 壅塞相位總送達 | 94 | 76 | 92 | 94 | **97** |
| 壅塞相位需求滿足率 | 0.767 | 0.667 | 0.715 | 0.759 | **0.768** |
| 壅塞相位滿足率 JFI | 0.936 | 0.862 | 0.889 | 0.921 | **0.933** |
| 整段 JFI | 0.9871 | 0.9746 | 0.9877 | **0.9905** | 0.9896 |
| 平均吞吐量 | 4.93 Mbps | 4.40 | 4.82 | 4.87 | **5.02** |
| 整段平均 RTT | 59.3 ms | 64.2 | 63.9 | 57.2 | 58.4 |

**結論**：兩個修正疊加後（`relative` 上限 + 確定性輸出評估），吞吐量 5.02 Mbps **首次超過 PF 的 4.93**（+1.8%），壅塞相位滿足率 0.768 追平 PF 的 0.767，滿足率 JFI 0.933 接近 PF 的 0.936。只換動作對應（不訓練）就已挽回大部分缺口，確定性輸出又進一步超車，代表原本「劣於 PF」主要是動作對應設計問題，其次是採樣雜訊，不是策略本質上學不到有用的東西。

**尚未驗證/待辦**：
- 只跑了 2 小時暖啟動、TCP 一份；正式 Stage 2 結果需補跑一次完整 6 小時訓練（`DRL_CAP_MODE=relative`，從隨機初始化或本次暖啟動繼續皆可）＋量測時預設 `DRL_DETERMINISTIC=1`，TCP／UDP 各一份，才能取代本章節與上方 6h 章節、正式定案。
- UDP 尚未用 `relative`／確定性輸出量過。
- relay 節點（node1/node2）壅塞樣本比例仍偏低（結構性，MT 通道不受場景惡化），2 小時內 Actor 幾乎沒有更新機會，正式 6 小時訓練預期仍有此限制，是否要調整 `CONTENDED_BUF_BYTES` 待另外決定。
- 原始資料：`experiment_results/data_20260927_stage2_capmode_fix/`。
