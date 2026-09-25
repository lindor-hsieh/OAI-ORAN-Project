# Stage 3 — soft cluster FL + 最基礎 DRL 量測結果

**量測日期**：2026-09-19（三度重測版，取代 2026-09-14 的舊版本）；文末另含 2026-09-21 Scenario T 量測
**狀態**：全部 12 個 Local xApp+Local rApp 上線，`MODEL_ARCH=mlp`、`REWARD_MODE=throughput_only`，Global xApp（全域公平性廣播）+ Global rApp（`IABClusterFedAvg`，`FL_MODE=cluster`）全程運作
**比較基準**：`PF.md`（Stage 1，JFI=0.3303、平均吞吐量 6.45 Mbps、平均 RTT 224.23 ms，seed `20260914`）；Stage 2 對照見 `avgFL.md`（JFI 0.2453／6.81 Mbps／260.14 ms）

> **⚠️ 量測條件警語（2026-09-25 加註）**：本文件所有量測都發生在 PC3 的 USB 網卡接在 USB 2.0 埠（自 2026-09-11 19:13 起，實測跨主機頻寬上限約 320 Mbps，且 PC3 有流量時 PC2 的連線也被拖慢，見 `HISTORY.md` 2026-09-25「跨主機延遲根因」條目與 `CLAUDE.md` §8）的環境下。所有「跨主機節點吞吐量偏低／RTT 偏高／UE 間差異」的數字與歸因，都混有這個硬體瓶頸的影響，**不代表 IAB 多跳架構或排程/FL 演算法本身的表現**；實體修復後需重測再引用。文中數據保留為歷史記錄，不修改。

> ## 歷史註記：舊版（confound 未排除）量測的兩個已知 confound，均已排除
>
> （1）`flower-supernode-nodeN` 曾整段訓練期間跑在 `REWARD_MODE=lagrangian`（compose 預設值生效；MLP 訓練路徑實際未使用 λ，FL 訓練純讀 MongoDB 已算好的 `reward` 欄位，影響有限）；（2）約 8.5 小時訓練歷經 11 次 FlexRIC 崩潰復原，量測窗口內個別 UE 連通性殘留。兩者已於下方乾淨重測前排除，程式碼追查與完整過程見 `HISTORY.md` 2026-09-19 條目。舊版數字不建議引用。
>
> **2026-09-19 更新：已在 confound 排除後重新量測一次，下方「量測結果」已換成這次的乾淨數字**——量測開始前明確逐項驗證：17/17 UE 現場 ping 確認 0% 封包遺失（含 UE17）、`docker ps` 確認 12/12 xApp 容器存在、`xapp-node1` log 確認有真實的 `CONTROL-REQUEST tx`/`CONTROL ACK rx`/`AI 決策` 訊號（不是只看容器數量）、`FL_MODE=cluster`／`REWARD_MODE=throughput_only`／`MODEL_ARCH=mlp` 三個環境變數逐一確認。confound 1（`REWARD_MODE` bug）在這次量測前已修復並生效；confound 2（連通性殘留）這次透過量測前的完整驗證排除——量測全程 17 個 UE 都量到非零吞吐量（0/17 零樣本），比舊版本（1/17 零樣本）更乾淨。這次的數字可以視為目前對 cluster FL 較可信的量測結果，舊版本（2026-09-14）數字已不在本檔中，不應再引用。

## 架構摘要（本階段換掉 Stage 2 的部分）

- **Local xApp + Local rApp**：與 Stage 2 完全相同的 C xApp 控制迴圈 + MLP DRL，`REWARD_MODE=throughput_only`。
- **Global xApp**：與 Stage 2 完全相同（全域公平性軟性廣播，不做硬性 PRB 裁切）。
- **Global rApp（本階段唯一變動）**：`server_app.py::IABClusterFedAvg`——依每個節點的連續角色比例 `role_ratio_i`（Node1~3=0 純 relay，Node4=0.2 混合，Node5~12=1 純 access）算出 `W_relay`/`W_access` 兩個加權平均原型，再依各節點自己的 `role_ratio_i` 混合廣播，取代 Stage 2 的單一全域平均。設計與離線數學驗證見 `CLAUDE.md` 第 3 節、`inference/STAGE3_CLUSTER_FL_DESIGN.md`。

## 訓練過程摘要

Stage 2 資料於 2026-09-19 00:08 封存（`experiment_results/checkpoints_archive/stage2_avgfl_20260919/`，六個半小時、114,108 筆經驗的訓練成果），隨即以 `FL_MODE=cluster REWARD_MODE=throughput_only bash iab/run_stage2_fl.sh` 從全新（0 經驗、隨機初始化 checkpoint）狀態啟動 Stage 3。訓練期間搭配 `iab/training_scenario_driver.sh`（訓練場景輪替，刻意排除測試用的 seed=20260914）與 `iab/training_watchdog.sh`（崩潰自動偵測/完整復原）跨夜運作。

- **總訓練時長**：約 8.5 小時 wall-clock（含復原空窗），累積 **143,580+ 筆經驗**（超過 Stage 2 的 114,108 筆）。
- **穩定性事件**：期間共發生 11 次 FlexRIC 崩潰，多數由 `training_watchdog.sh` 全自動復原；第 11 次在冷卻窗內復發，watchdog 依設計停止等待人工介入，本次量測前的乾淨重啟即針對這次事件（細節見 `HISTORY.md` 2026-09-19）。
- **量測前才發現的 `REWARD_MODE` 傳遞 bug**：見上方 confound 說明。

## 上線前驗證（全部通過才進行正式量測）

1. **xApp 確實啟動**（本次特別加強驗證的項目，2026-09-19 稍早發生過忘記啟動 xApp 導致整份量測無效的事故，見 `avgFL.md` 最上方更正說明）：`docker ps | grep -c xapp-node` = 12、`docker logs flexric | grep -c "E2 SETUP-REQUEST"` ≥ 13、`xapp-node1` log 確認有持續的 `CONTROL-REQUEST tx`/`CONTROL ACK rx`/`AI 決策` 訊號、MongoDB 經驗數即時增加。
2. **REWARD_MODE 三條路徑確認**：`inference-nodeN`（原本就正確）、`flower-supernode-nodeN`（量測前才發現＋修復，見上方 confound 1）皆為 `throughput_only`。
3. **量測前現場連通性**：全部 17 個 UE（含 UE17）現場 ping 測試皆為 0% 封包遺失。
4. **`rfsim5g-donor-cu` RestartCount 基準點確認為 1**（CU 於 09-19 訓練崩潰事件後已 restart 1 次，量測期間未再增加；與 PF/avgFL 的基準 0 不同屬正常）。

## 量測方法（與 PF.md / avgFL.md 完全相同）

```bash
python3 scenarios/traffic_scenario.py --scenario R --seed 20260914 --host {pc1,pc2,pc3} --phase-duration 60 --num-phases 15
python3 iab/measure_stage.py --host {pc1,pc2,pc3} --duration 900 --interval 5 --out /tmp/stage3clean_trial_{host}.csv
```

DRL（`inference-nodeN`／Global xApp／`flower-*`）全程保持啟用。同一組 Scenario R、同一個 seed（`20260914`），與 PF.md 構成有效的 paired comparison。量測前**沒有**清空 MongoDB/checkpoint——量到的是訓練步數 6768（持續訓練超過 8.5 小時）的 policy，量測完後訓練繼續（見下方「下一步」）。

**量測前逐項驗證**（吸取上一版的教訓，這次全部確認過才開始）：17/17 UE 現場 ping 0% 封包遺失（含 UE17）；`docker ps | grep -c xapp-node` = 12；`xapp-node1` log 確認真實的 `CONTROL-REQUEST tx`/`CONTROL ACK rx`/`AI 決策` 訊號持續出現；`FL_MODE=cluster`、`REWARD_MODE=throughput_only`、`MODEL_ARCH=mlp` 三個環境變數在 `inference-nodeN`／`flower-supernode-nodeN`／`flower-superlink` 上逐一用 `/proc/1/environ` 確認（不只看 `docker exec env`，避免看到跟實際運行中 process 不一致的環境）。

**全程系統穩定性**：本次 15 分鐘量測全程，**三主機的 FlexRIC、Donor CU/DU、全部 12 個 relay/access DU/MT 容器 `RestartCount` 全部維持不變（零新增崩潰）**。

## 量測結果（全部 17 個 UE，乾淨版本——量測開始時 17/17 UE 皆已確認連通）

| UE | 取樣總數 | 吞吐量取樣數 | RTT取樣數 | 平均吞吐量 (Mbps) | 平均 RTT (ms) | 吞吐量覆蓋率 | RTT覆蓋率 |
|---|---|---|---|---|---|---|---|
| UE1  | 149 | 54  | 8   | 0.66  | 267.48 | 36.2% | 5.4%  |
| UE2  | 149 | 38  | 8   | 1.38  | 262.75 | 25.5% | 5.4%  |
| UE3  | 149 | 58  | 8   | 1.53  | 251.14 | 38.9% | 5.4%  |
| UE4  | 149 | 50  | 7   | 2.10  | 270.89 | 33.6% | 4.7%  |
| UE5  | 150 | 116 | 47  | 13.63 | 295.79 | 77.3% | 31.3% |
| UE6  | 150 | 130 | 134 | 23.84 | 48.36  | 86.7% | 89.3% |
| UE7  | 150 | 136 | 73  | 21.59 | 243.97 | 90.7% | 48.7% |
| UE8  | 150 | 143 | 145 | 16.00 | 87.43  | 95.3% | 96.7% |
| UE9  | 148 | 68  | 9   | 0.46  | 346.89 | 45.9% | 6.1%  |
| UE10 | 148 | 92  | 9   | 0.66  | 357.33 | 62.2% | 6.1%  |
| UE11 | 148 | 77  | 9   | 1.09  | 370.11 | 52.0% | 6.1%  |
| UE12 | 148 | 82  | 9   | 1.49  | 336.22 | 55.4% | 6.1%  |
| UE13 | 148 | 96  | 11  | 0.74  | 321.27 | 64.9% | 7.4%  |
| UE14 | 148 | 76  | 11  | 1.48  | 328.55 | 51.4% | 7.4%  |
| UE15 | 148 | 45  | 9   | 0.63  | 515.56 | 30.4% | 6.1%  |
| UE16 | 148 | 14  | 9   | 0.72  | 527.11 | 9.5%  | 6.1%  |
| UE17 | 149 | 25  | 0   | 0.92  | **—（全數失敗）** | 16.8% | 0.0%  |

## 摘要指標與 PF.md 比較

| 指標 | PF baseline (Stage 1) | cluster FL (Stage 3，乾淨重測) | 變化 |
|---|---|---|---|
| Jain's Fairness Index | 0.3303 | **0.3113** | 惡化 −5.8% |
| 平均吞吐量（17 UE 平均） | 6.45 Mbps | **5.23 Mbps** | **惡化 −18.9%** ❌ |
| 平均 RTT（有效樣本平均，排除 UE17） | 224.23 ms | **301.93 ms** | **惡化 +34.7%** ❌ |
| 零吞吐量樣本的 UE 數 | 0/17 | **0/17** | 持平（本次全部 UE 都量到非零吞吐量） |
| 全程崩潰/重啟 | 0 | 0 | 持平 |

## 結論（誠實記錄，這次是乾淨量測，數字可信度較高）

1. **量測本身是乾淨的**：開始前確認 17/17 UE 連通、12/12 xApp 真實運作、三個環境變數皆正確，量測全程 0/17 UE 零吞吐量（比上一版本的 1/17 更乾淨），前一版本的兩個 confound（`REWARD_MODE` bug、連通性殘留）都已排除。這份數字可以視為目前對 cluster FL（訓練步數 6768、約 8.5+ 小時訓練後）較可信的量測結果。
2. **即使排除兩個 confound，Stage 3 這次還是沒有優於 PF baseline**：JFI 惡化 5.8%、平均吞吐量惡化 18.9%、平均 RTT 惡化 34.7%——比上一版本（confound 未排除時）的降幅小，但方向仍然一致，沒有反轉成優於 PF。這代表先前懷疑的兩個 confound 確實有拉低數字，但排除後 Stage 3 仍未達成「優於 PF」的路線圖預期，不是量測方法的問題。
3. **吞吐量/RTT 取樣覆蓋率偏低是這次量測的主要特徵**（多數 UE 吞吐量覆蓋率在 30~65% 之間、RTT 覆蓋率普遍個位數到低雙位數百分比），但這次全部 17 個 UE 都至少量到部分非零樣本，不是「完全連不通」——比較像是在 Scenario R 高負載場景下，backhaul-aware PRB 預算機制與/或目前的 cluster FL policy 讓多數節點只能間歇性搶到排程機會（**未驗證假說；亦不能排除 PC3 USB 2.0 網卡造成的間歇性覆蓋率不足，見頁首警語**），不是連通性故障，值得後續深入分析（例如逐 UE 檢視是否某些角色比例 `role_ratio_i` 的節點特別容易被排擠）。
4. **與 PF.md 的既有觀察一致的部分**：UE1~4（PC2 access 節點）覆蓋率偏低（25~39%）、深層跨主機節點（PC3 UE9~16）平均吞吐量普遍偏低（0.46~1.49 Mbps）與既有模式相符；UE5~8（PC1）表現相對最好（13.6~23.8 Mbps），跟 PF/舊版本的既有模式一致（PC1 同主機優勢，見 `PF.md` 2026-09-22 章節）。
5. **UE17 依然全數 ICMP 失敗**（RTT 樣本 0/149）但這次吞吐量覆蓋率 16.8%、平均 0.92 Mbps，跟 `PF.md` 記錄的既有模式一致，量測前現場已確認 0% 封包遺失，量測開始後才復現 Node4 三重負載自我節流效應。
6. **後續**：這份乾淨數字顯示目前的 cluster FL policy（在這個訓練階段）還沒有達到優於 PF 的效果，值得討論是否需要更長訓練時間、調整聚合頻率、或檢視 reward/state 設計，但這是下一輪的討論——本文件先如實記錄這次乾淨量測的結果。

## 下一步

已完成：Stage 2 封存與重測見 `avgFL.md`，Stage 3 資料已還原（見 `HISTORY.md` 2026-09-19）。

## 2026-09-21 — Scenario T 量測（PF／avg FL／cluster FL 三方乾淨比較的第三、也是最後階段）

**量測日期**：2026-09-21（凌晨）
**狀態**：12/12 xApp 真實運作中，`REWARD_MODE=throughput_only`／`MODEL_ARCH=mlp`／`FL_MODE=cluster` 逐一用 `/proc/1/environ` 確認正確，`inference-node1` log 確認載入訓練步數 6978（原始封存記錄為 6768，訓練在封存前又多跑了一段）的真實 checkpoint（`stage3_clusterfl_20260919_v2`，243,004 筆經驗）
**動機**：與 `PF.md`／`avgFL.md` 2026-09-21 條目同一批三方比較的最後階段，改用 Scenario T 取代先前的 Scenario R，理由見 `PF.md` 對應章節與 `HISTORY.md` 2026-09-20 條目。

**量測前逐項驗證**：17/17 UE 現場 ping 確認 0% 封包遺失（含 UE17）；`docker ps` 確認 12/12 xApp 容器皆為 running；`rfsim5g-oai-ext-dn` 確認 17 個獨立 iperf3 server port（5201~5217）皆在監聽；Scenario T、`measure_stage.py`，15 分鐘（900s）、每 5 秒取樣一次；量測過程中三主機 `RestartCount` 全程無新增崩潰。上線過程曾兩次遇到 FlexRIC xApp-association 卡死（需 FlexRIC→DU→xApp 完整重啟）與 `rfsim5g-iab-du-5` 靜默未啟動，詳見 `HISTORY.md` 2026-09-19~21 條目。

### 量測結果（全部 17 個 UE）

| UE | 取樣總數 | 吞吐量取樣數 | RTT取樣數 | 平均吞吐量 (Mbps) | 平均 RTT (ms) | 吞吐量覆蓋率 | RTT覆蓋率 |
|---|---|---|---|---|---|---|---|
| UE1  | 148 | 126 | 8   | 1.82  | 489.07 | 85.1%  | 5.4%   |
| UE2  | 148 | 109 | 8   | 1.87  | 519.77 | 73.6%  | 5.4%   |
| UE3  | 148 | 92  | 8   | 1.54  | 496.99 | 62.2%  | 5.4%   |
| UE4  | 148 | 109 | 8   | 1.69  | 513.30 | 73.6%  | 5.4%   |
| UE5  | 149 | 142 | 25  | 3.38  | 283.58 | 95.3%  | 16.8%  |
| UE6  | 149 | 149 | 149 | 25.93 | 184.36 | 100.0% | 100.0% |
| UE7  | 149 | 145 | 51  | 5.94  | 211.35 | 97.3%  | 34.2%  |
| UE8  | 149 | 145 | 142 | 19.92 | 136.04 | 97.3%  | 95.3%  |
| UE9  | 148 | 60  | 6   | 0.58  | 341.00 | 40.5%  | 4.1%   |
| UE10 | 148 | 72  | 6   | 0.85  | 396.67 | 48.6%  | 4.1%   |
| UE11 | 148 | 121 | 16  | 1.47  | 331.25 | 81.8%  | 10.8%  |
| UE12 | 148 | 106 | 16  | 1.22  | 342.12 | 71.6%  | 10.8%  |
| UE13 | 148 | 123 | 13  | 1.13  | 301.31 | 83.1%  | 8.8%   |
| UE14 | 148 | 69  | 13  | 1.02  | 313.31 | 46.6%  | 8.8%   |
| UE15 | 148 | 39  | 11  | 0.42  | 393.82 | 26.4%  | 7.4%   |
| UE16 | 148 | 68  | 11  | 0.70  | 355.55 | 45.9%  | 7.4%   |
| UE17 | 148 | 94  | 3   | 0.73  | 805.67 | 63.5%  | 2.0%   |

### 摘要指標與 PF.md／avgFL.md（同為 Scenario T）三方比較

| 指標 | PF baseline | avg FL | cluster FL | cluster FL vs PF | cluster FL vs avg FL |
|---|---|---|---|---|---|
| Jain's Fairness Index | 0.2812 | 0.3007 | **0.2552** | 惡化 −9.2% ❌ | 惡化 −15.1% ❌ |
| 平均吞吐量（17 UE 平均） | 4.23 Mbps | 4.35 Mbps | **4.13 Mbps** | 惡化 −2.4% ❌ | 惡化 −5.1% ❌ |
| 平均 RTT（排除 UE17） | 308.66 ms | 370.13 ms | **350.59 ms** | 惡化 +13.6% ❌ | 改善 +5.3% ✅ |
| 零吞吐量樣本的 UE 數 | 0/17 | 0/17 | 0/17 | 持平 | 持平 |

### 結論

1. **三方比較的最終結果：不支持「PF < avg FL < cluster FL」單調遞增假設**。JFI／吞吐量排序是 avg FL > PF > cluster FL（cluster FL 最差，不是最好）；RTT 排序是 PF < cluster FL < avg FL。Cluster FL 在三項指標中的兩項（JFI、吞吐量）都是三者最差，只有 RTT 略優於 avg FL。
2. **這次已經排除了先前懷疑的主要 confound**（訓練/測試 scenario 曝光比例不對稱），改用 Scenario T 做這次比較，Stage 2 也已用相近輪替表重新訓練過。即使如此，cluster FL 仍未展現優於 PF 或 avg FL 的效果，代表先前假設的「訓練/測試場景不對稱」並非 cluster FL 表現不佳的主要或唯一原因，需要回頭檢視聚合演算法本身（`role_ratio_i` 加權機制）或 local DRL 在 cluster FL 聚合下的收斂特性（在網卡修復、avg FL 重訓後才有意義）。
3. **必須註記的方法論限制**：avg FL 這一側使用的 checkpoint 本身有已知的訓練資料品質問題（10/12 節點訓練期間近乎閒置，見 `avgFL.md` 對應章節），因此 avg FL 的數字也不代表演算法的完整潛力；cluster FL 這一側的 checkpoint（`stage3_clusterfl_20260919_v2`）則是在 2026-09-19 用 Scenario R 主導的舊版輪替表訓練的，同樣不是在 Scenario T 為主的環境下訓練出來的。**三個 checkpoint 的訓練條件並不完全對等**，這次比較最乾淨的是「量測方法論」（三者都用同一個 Scenario T、同一套基礎設施、同一晚），但「訓練資料代表性」這一層的差異依然存在，是下一輪如果要下更強的結論，必須先解決的前提。
4. **零吞吐量 UE 數 0/17**，量測本身乾淨可信；覆蓋率偏低的模式（UE9/10/15/16 較差）跟先前版本一致，非本次量測特有。
