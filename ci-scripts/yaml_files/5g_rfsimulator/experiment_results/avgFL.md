# Stage 2 — avg FL + 最基礎 DRL 量測結果

**量測日期**：2026-09-19（三度重測版，取代 2026-09-14 的舊版本）
**狀態**：全部 12 個 Local xApp+Local rApp 上線，`MODEL_ARCH=mlp`（取代先前悄悄引入的 GRU 架構，見下方「本次重測原因」）、`REWARD_MODE=throughput_only`，Global xApp（全域公平性廣播）+ Global rApp（標準 FedAvg，`FL_MODE=avg`，12 節點一起聚合）全程運作
**比較基準**：`PF.md`（Stage 1，JFI=0.3303、平均吞吐量 6.45 Mbps、平均 RTT 224.23 ms，seed `20260914`）

> ## ⚠️ 2026-09-19 事後更正：下方「量測結果」整段作廢，不能引用
>
> 三主機乾淨重啟時（見上方「本次重測原因」第 4 點）遺漏了 `iab/run_local_pc1.sh --skip-server` 這一步——只跑了 `start_iab_server.sh`（基礎設施）就直接進到 PC2/PC3，**全部 12 個 C 語言 Local xApp 容器（`xapp-node1`~`xapp-node12`）從頭到尾沒有被啟動**，直到量測完成、寫完本文件、封存 Stage 2 資料、啟動 Stage 3 之後（Stage 3 空跑近 40 分鐘、MongoDB 經驗數全部掛零）才被發現並修復（`docker ps -a` 確認 `xapp-node1`「no such object」，從未建立過）。
>
> 這代表下方整段「量測結果」實際上是在**沒有任何 E2SM-MAC CONTROL-REQUEST、Local rApp 從未被呼叫、Global xApp/FedAvg 聚合結果從未被下發**的狀態下量到的——DU 端的 PRB 分配全程是 OAI 內建排程器行為（加上跟 PF baseline 相同的 backhaul-aware PRB 預算機制），**不代表 Stage 2 訓練出來的 policy 的真實表現**，即使數字看起來優於 PF baseline，也不能歸因給 DRL/FL，必須視為無效量測。
>
> 真正訓練了 6.5 小時的 Stage 2 checkpoint 本身沒有受影響（訓練過程 xApp 是有跑的，只有這次「乾淨重啟後的驗證量測」這個環節漏掉），已完整封存在 `experiment_results/checkpoints_archive/stage2_avgfl_20260919/`，重新量測只需要重新載入這份 checkpoint、確認 xApp 真的在跑、重跑一次相同方法論即可，不需要重新訓練。**這件事本身也如實記錄進 `HISTORY.md`，作為「乾淨重啟後，光看『UE 連通性正常』『RestartCount 不變』不足以代表 DRL 控制迴圈真的在生效，必須額外驗證 xApp 容器與 E2SM-MAC CONTROL-REQUEST」這個教訓。**

> ## ⚠️ 2026-09-19 第二次補測（下方「量測結果」已換成這次的數字）：xApp 這次確實有啟動，但量測當下有 5/17 UE 連通性未修復，數字仍需謹慎解讀
>
> 用 `iab/archive_stage_data.sh`／`iab/restore_stage_data.sh` 把 Stage 3 當時的即時狀態（checkpoint + MongoDB 經驗）封存起來、換回 Stage 2 封存的 checkpoint 與經驗做這次補測，`inference-nodeN`／`flower-supernode-nodeN` 的 `REWARD_MODE`/`MODEL_ARCH`、`flower-superlink` 的 `FL_MODE=avg` 皆已個別確認正確（含這次量測前才發現並修好的 `flower-supernode-nodeN` REWARD_MODE bug，見 `clusterFL.md` 與 `HISTORY.md` 2026-09-19 條目），xApp 12/12、E2 連線皆已確認正常運作。
>
> **但量測前的連通性檢查花了大量時間仍未能讓 17 個 UE 全部恢復**：UE9、UE10、UE15、UE16、UE17 這 5 個節點在量測開始時仍處於連不通狀態（路由/DNAT 重新斷言、DU 重啟、conntrack 清空等既有修法都試過，未能在合理時間內解決；UE15/16/17 恰好是整晚以來反覆出問題的節點）。量測結果因此如實記錄，但 **JFI/平均吞吐量/平均 RTT 這三個匯總指標會被這 5 個「事先已知失效」的節點拉低，不是單純反映 policy 決策品質**。

> ## ✅ 2026-09-19 第三次補測（下方「量測結果」已換成這次的乾淨數字，建議以這次為準）
>
> 這次量測前**逐項驗證過才開始**，吸取前兩次的教訓：三主機依序乾淨重啟（過程中修過 PC1 host 對 relay tunnel 的過期路由、PC1 本機 Node7/8 的 DNAT 漂移）→ 17/17 UE 現場 ping 確認 0% 封包遺失（**含 UE17**，這是本文件第一次做到 17/17 全部連通才開始量測）→ `docker ps` 確認 12/12 xApp 容器存在、`xapp-node1` log 確認真實的 `CONTROL-REQUEST tx`/`CONTROL ACK rx` 訊號持續出現（不是只看容器數量）→ `/proc/1/environ` 逐一確認 `inference-nodeN`／`flower-supernode-nodeN` 的 `REWARD_MODE=throughput_only`、`MODEL_ARCH=mlp`，`flower-superlink` 的 `FL_MODE=avg`→ 確認載入的 checkpoint 訓練步數 2860（延續 Stage 2 原本的訓練歷史，不是重置）。這是三次補測裡唯一一次排除了全部已知 confound 的乾淨量測，下方數字建議以此為準，前兩次的數字保留供對照但不建議引用。

## 本次量測方法論說明（取代原本的重測原因，見上方三次更正）

本文件記錄的「量測結果」是 2026-09-19 的**第三次**補測（第一次因 xApp 未啟動而作廢；第二次因量測開始時 5/17 UE 已知連不通而不建議引用；見上方三則更正）。訓練架構與過程摘要如下，量測前置檢查見上方第三則更正。

## 本次重測原因（2026-09-14 版本已作廢）

2026-09-14 版本用的 Local DRL 架構其實是 GRU，但 CLAUDE.md 五階段路線圖定義的「最基礎 DRL」原意不含 GRU（見 `inference/DRL_METHODOLOGY_PLAN.md` 補記）；且當時的量測只跑了 15 分鐘，`training_pipeline.py` 的 200 筆經驗門檻加上 GRU 版本的時間連續性序列要求（`TRAIN_SEQ_LEN`/`TRAIN_SEQ_COUNT`），導致訓練/FL 聚合幾乎沒有機會真正觸發（見 `inference/STAGE3_CLUSTER_FL_DESIGN.md` §8 的即時校準紀錄：連續 57 分鐘一次都沒跨過門檻）。本次重測前完成以下變更：

1. **新增 `MODEL_ARCH` 開關**（`mlp`｜`gru`，比照既有 `REWARD_MODE` 模式，見 `inference/drl_agent.py`），Stage 2~4 統一用 `MODEL_ARCH=mlp`；GRU 保留供 Stage 5 或未來研究使用，不刪除。`mlp` 模式訓練改用 `training_pipeline.fetch_experiences()`（打散抽樣、i.i.d.），不受 GRU 版本的時間連續性序列門檻限制，只看 `MIN_TRAIN_EXPERIENCES=200` 原始經驗數，訓練觸發頻率大幅提升。
2. **真正訓練足夠久**：本次 Local DRL 的 MongoDB 經驗資料橫跨 2026-09-18 07:29~13:56 UTC（約 6.5 小時 wall-clock 累積，期間搭配 `iab/training_scenario_driver.sh` 的訓練場景輪替與 `iab/training_watchdog.sh` 的崩潰自動偵測/復原，撐過多輪 FlexRIC/DU 崩潰），不是先前的 15 分鐘一次性量測。
3. **新增並修復 relay/access DU 的 Random Access process pool 耗盡自動修復**（`gNB_scheduler_RA.c:719` "no free RA process"，OAI 內部固定 4 格陣列耗盡）——這是先前 watchdog 自動復原「有時候成功有時候失敗」的一個主因，這次已經做進 `training_watchdog.sh`（每輪詢週期主動檢查全部 12 個 DU）與三個 `start_iab_*.sh` 的健康檢查迴圈（見 `HISTORY.md` 2026-09-19 條目）。
4. **量測前的乾淨三主機重啟**發現並修復一個新 bug：`start_iab_server.sh` 直接執行（不透過 watchdog）時，`inference-nodeN` 的 plain `docker compose up -d` 沒有帶上 `REWARD_MODE`/`MODEL_ARCH` 環境變數，會悄悄套用 compose 檔預設值（`REWARD_MODE` 預設 `lagrangian`！），且 stage2-fl 服務（Flower FL 層）完全不會被重新帶起來。現場發現後，先手動強制覆蓋環境變數＋清除污染到的 32 筆經驗（`iab/clean_lambda_contamination.py --execute`，佔全部 114140 筆經驗的 0.03%），並把這個防護直接補進 `start_iab_server.sh` 本身（不再只依賴 `training_watchdog.sh` 的覆蓋邏輯），之後任何直接執行都不會再踩到。

## 舊版重測原因（2026-09-13 → 2026-09-14，已是歷史背景，供對照）

2026-09-13 首次量測的 `avgFL.md`（JFI=0.3976、吞吐量 6.58 Mbps、RTT 360.70 ms）在後續 Stage 3 除錯過程中，發現 `flower-supernode-nodeN`（FL 觸發的本地訓練路徑）從未收到 `REWARD_MODE` 環境變數，內部 `drl_agent.py` 悄悄用預設值 `lagrangian` 執行，跟原本「Stage 2 應該是純 `throughput_only`」的方法論假設不符——這條路徑觸發頻率遠低於 `inference-nodeN` 的每 60 秒背景訓練，實務影響是「疊加在正確配置的主要訓練迴圈上的次要污染源」，但仍是必須修正的既有 bug（完整根因分析見 `HISTORY.md` 2026-09-14 條目 root cause 1）。同一輪除錯還發現並修復兩個會導致資料面隨機斷線的基礎設施問題（root cause 2：CU NAT table 被無條件 flush；root cause 3：cpuset 過度擁擠造成 CU 隨機崩潰），因此決定連同 Stage 2 一起用修復後的乾淨環境重新量測，確保這份基準線可以跟修復後的 Stage 3 做公平比較。

## 架構摘要（本階段新增於 PF baseline 之上的元件）

- **Local xApp + Local rApp**：與 PF baseline 相同的 C xApp 控制迴圈，但 Local rApp 從「純被動蒐集資料」改為主動執行 DRL Actor 推論並下發 PRB 權重（`REWARD_MODE=throughput_only`：`R = R_throughput`，無 JFI 限制式、λ 恆為 0——**這次已同時確認 `inference-nodeN` 與 `flower-supernode-nodeN` 兩條訓練路徑都正確套用**，見下方「上線前驗證」）。
- **Global xApp**：獨立 Python process，每 2 秒讀一次 MongoDB 全部 12 個節點的最近經驗，算出每個節點「相對全域平均吞吐量」的落差，換算成 `fairness_bias ∈ [0.5, 2.0]`，透過 ZMQ PUB 廣播給全部 12 個 Local rApp，寫入 DRL state vector 的第 49 維。純軟性 state 特徵，不做任何硬性 PRB 裁切。
- **Global rApp**：Flower ServerApp/ClientApp 標準 FedAvg（`IABFedAvg`，`FL_MODE=avg`），`FL_NUM_NODES=12`，`FL_ROUND_INTERVAL_S=180`。

## 上線前的清理與除錯過程

本次重測沿用 2026-09-14 除錯過程中定位並修復的三個 root cause（完整過程見 `HISTORY.md` 對應條目，這裡只列結論）：

1. **`REWARD_MODE` 未傳到 `flower-supernode-nodeN`**——`docker-compose-iab-server.yaml` 12 個 `flower-supernode-nodeN` 服務補上 `REWARD_MODE: "${REWARD_MODE:-lagrangian}"`（**已驗證，已永久寫入設定檔**）。
2. **CU NAT OUTPUT table 被無條件 flush，導致其他主機 DNAT 規則被砍**——`iab/start_iab_pc2.sh`／`start_iab_pc3.sh`／`start_iab_server.sh` 全部改用 `iptables -t nat -I OUTPUT 1 ...`（插入不清空），並加上 `verify_and_heal_ues()` 自我修復迴圈（**已驗證，已永久寫入啟動腳本**）。
3. **cpuset 過度擁擠（27 個 process 塞 4 核心）造成 CU 隨機崩潰**——移除誤加在 Global xApp/Flower FL 15 個服務上的 `cpuset: "12-15"`，只保留原本 12 個 `inference-nodeN` 的設定（**已驗證，已永久寫入設定檔，並在該處加上警告註解**）。

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
5. **與 cluster FL（Stage 3）的比較**：`clusterFL.md` 同樣有一份乾淨重測的結果（JFI=0.3113、吞吐量=5.23 Mbps、RTT=301.93ms），三項都劣於 PF。兩相對照，avg FL 在吞吐量上表現優於 cluster FL 與 PF，但 JFI 是三者最差；cluster FL 則是三項都輸給 PF，但輸的幅度都不像 avg FL 的 JFI 那麼極端。這暗示目前的 cluster 聚合機制（依角色比例加權）可能反而讓 policy 在公平性上比單純 FedAvg 更保守，值得後續針對這個假設做更深入的分析。
6. **建議**：若要進一步驗證「avg FL 犧牲公平性換吞吐量」這個假設，可以考慮啟用 `REWARD_MODE=lagrangian`（Stage 5 規劃項目）重新訓練後再比較 JFI 是否回升；目前的乾淨數字已經是三次補測中最可信的一份，可以作為後續比較的基準。

## 下一步

Stage 2 的封存資料量測完後已換回 Stage 3 繼續訓練（`REWARD_MODE`/`FL_MODE` 皆已確認正確，含這次順便修好的 `flower-supernode-nodeN` REWARD_MODE bug），詳見 `HISTORY.md` 與 `clusterFL.md`。接下來會切到 PF baseline（停用全部 xApp）用同一個 seed 重新量測一次，作為今晚同一套已修復基礎設施下的當代基準，取代／補充 `PF.md` 原本較早期的量測。

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

1. **三項指標中兩項優於 PF、一項劣於 PF**：JFI 與吞吐量都比 PF baseline 好，但 RTT 明顯變差（+19.9%）。跟先前 Scenario R 下的乾淨重測（JFI 惡化、吞吐量小贏、RTT 惡化）方向不完全一樣——這次 JFI 反而是改善的，但同樣沒有滿足「三項同時變好」的單調遞增驗收標準。
2. **必須把上方揭露的訓練資料品質限制納入解讀**：這份結果反映的是「10/12 節點訓練期間近乎閒置」的 policy 表現，不是完整壅塞情境訓練下的代表性結果。RTT 變差可能反映 policy 對多數節點的真實壅塞決策經驗不足；JFI/吞吐量小幅改善則可能只是巧合或訓練不足下的隨機表現，都不宜過度解讀因果。
3. **與 PF.md 同批比較，不與先前 Scenario R 的舊數字比較**：場景不同、checkpoint 也不同（這次是全新重訓的 checkpoint，非先前補測用的封存版本），混用會製造新的 confound。
4. **零吞吐量 UE 數 0/17**，量測本身乾淨可信；限制在於底層訓練資料，不在量測方法。
