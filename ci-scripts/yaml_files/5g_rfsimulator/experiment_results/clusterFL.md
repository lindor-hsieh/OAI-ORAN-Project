# Stage 3 — soft cluster FL + 最基礎 DRL 量測結果

**量測日期**：2026-09-14
**狀態**：全部 12 個 Local xApp+Local rApp 上線（`REWARD_MODE=throughput_only`，`inference-nodeN` 與 `flower-supernode-nodeN` 兩條訓練路徑皆已確認正確套用），Global xApp（全域公平性廣播）+ Global rApp（`IABClusterFedAvg`，`FL_MODE=cluster`，依 `role_ratio_i` 加權聚合 relay/access 兩個原型模型並混合廣播，設計見 `CLAUDE.md` 第 3 節）全程運作
**比較基準**：`PF.md`（Stage 1）、`avgFL.md`（Stage 2，2026-09-14 重測版）

## 架構摘要（本階段換掉 Stage 2 的部分）

- **Local xApp + Local rApp**：與 Stage 2 完全相同，模型架構、`REWARD_MODE=throughput_only` 皆未變動。
- **Global xApp**：與 Stage 2 完全相同（全域公平性軟性廣播，不做硬性 PRB 裁切）。
- **Global rApp（本階段唯一變動）**：`server_app.py::IABClusterFedAvg`——依每個節點的連續角色比例 `role_ratio_i`（Node1~3=0 純 relay，Node4=0.2 混合，Node5~12=1 純 access）算出 `W_relay`/`W_access` 兩個加權平均原型，再依各節點自己的 `role_ratio_i` 混合廣播 `W_i = (1-role_ratio_i)·W_relay + role_ratio_i·W_access`，取代 Stage 2 的單一全域平均。完整設計動機、公式、offline 數學驗證見 `CLAUDE.md` 第 3 節與 Stage 3 開發階段的討論記錄。

## 上線前的清理與除錯過程

本次量測使用跟 Stage 2 重測版**完全相同**的乾淨環境（三個 root cause 修復後的設定，見 `HISTORY.md` 2026-09-14 條目與 `avgFL.md`「上線前的清理與除錯過程」），這裡不重複列出。切換到 `FL_MODE=cluster` 的過程本身沒有額外發現新的基礎設施問題；量測前對 Node12（PC3）做過一次針對性的 MT/DU 完整重建（`docker compose stop/rm/up` + 重新套用 NAT/路由/CU DNAT，比照既有 `configure_and_start_access_du()` 流程手動執行一次），因為它在量測前連通性檢查時單獨出現 tunnel 但資料面不通的情況，重建後確認正常，這是個別節點的既有已知不穩定模式（見 `CLAUDE.md` 第 7 節排查指南第 3、4 點），不是 Stage 3 專屬的新問題。

## 上線前驗證（全部通過才進行正式量測）

1. **REWARD_MODE 雙路徑確認**：與 Stage 2 相同方法驗證，`inference-node1`／`flower-supernode-node1` 皆為 `throughput_only`，`lambda` 全程 `0.0000`。
2. **IABClusterFedAvg 正確性（離線驗證）**：獨立的純 Python 數學驗證腳本（不依賴 Docker/Flower）驗證 `_weighted_average_flat()` 加權平均公式與混合廣播公式在已知輸入下的正確性，包含邊界情況（某一側全部節點 `num-examples=0`、Node4 混合節點的廣播值介於兩原型之間且不等於任一純原型）——全部通過，詳見 Stage 3 開發過程記錄。
3. **量測前現場連通性**：全部 17 個 UE（含 UE17）現場 ping 測試皆為 0% 封包遺失。
4. **`rfsim5g-donor-cu` RestartCount 基準點確認為 0**，量測全程未再增加。

## 量測方法（與 PF.md / avgFL.md 完全相同）

```bash
FL_MODE=cluster REWARD_MODE=throughput_only bash iab/run_stage2_fl.sh

python3 scenarios/traffic_scenario.py --scenario R --seed 20260914 --host {pc1,pc2,pc3} --phase-duration 60 --num-phases 15
python3 iab/measure_stage.py --host {pc1,pc2,pc3} --duration 900 --interval 5 --out /tmp/stage3final_{host}.csv
```

同一組 Scenario R、同一個 seed（`20260914`），與 PF.md／avgFL.md 構成有效的三方 paired comparison。

**全程系統穩定性**：本次 15 分鐘量測全程，**三主機的 FlexRIC、Donor CU/DU、全部 12 個 Node DU/MT 容器 `RestartCount` 全部維持不變（零新增崩潰）**，與 Stage 2 重測版相同。

## ⚠️ 重要方法論限制：FL 聚合本輪量測全程幾乎沒有真實資料可聚合

量測後檢查 `flower-supernode-nodeN` 的訓練日誌，發現**整個 15 分鐘量測視窗內，全部 12 個節點的 FL 觸發訓練路徑幾乎每一輪都回報「經驗數量不足（需 200 筆），等待更多資料累積」**——`training_pipeline.py` 要求至少 200 筆連續原始經驗才能切出訓練序列，這個門檻在一次從乾淨清空狀態開始、只有 15 分鐘的視窗內幾乎不可能跨過（`FL_ROUND_INTERVAL_S=180` 秒一輪，15 分鐘內約有 5 輪機會，但每輪能累積的新經驗有限）。

**這代表 Stage 2（avg）與 Stage 3（cluster）在本次量測中，`IABFedAvg`／`IABClusterFedAvg` 的聚合邏輯本身幾乎沒有機會拿到非零的 `num-examples` 去做真正有意義的加權平均**——兩個 Stage 觀察到的效能差異，主要反映的是**全部 12 個節點各自獨立、未經聯邦同步的 `inference-nodeN` 背景訓練軌跡**（`_train_worker`，每 60 秒一輪，不受 `FL_MODE` 影響）疊加當次流量場景隨機性的結果，而不是「average FedAvg vs. soft clustered FedAvg」這個聚合演算法本身的效果對比。這是**本次實驗設計的方法論限制**，不是 `IABClusterFedAvg` 程式碼有 bug（聚合公式已經過離線數學驗證，執行期也確認零例外）。

**後續建議**：若要讓 Stage 2 vs. Stage 3 的比較真正反映聚合演算法的差異，需要拉長量測視窗（讓 FL 觸發訓練有機會真正跨過 200 筆門檻累積數輪）、或降低訓練所需的最小連續經驗數／訓練序列長度門檻，讓單次 15 分鐘視窗內至少能觀察到數輪真正非零權重的聚合。這個限制與後續改進方向留待下一輪討論。

## 量測結果（全部 17 個 UE）

| UE | 取樣總數 | 吞吐量取樣數 | RTT取樣數 | 平均吞吐量 (Mbps) | 平均 RTT (ms) | 吞吐量覆蓋率 | RTT覆蓋率 |
|---|---|---|---|---|---|---|---|
| UE1  | 145 | 139 | 115 | 16.40 | 164.64 | 95.9%  | 79.3% |
| UE2  | 145 | 110 | 104 | 4.94  | 199.89 | 75.9%  | 71.7% |
| UE3  | 145 | 141 | 88  | 2.70  | 242.29 | 97.2%  | 60.7% |
| UE4  | 145 | 143 | 89  | 8.68  | 257.12 | 98.6%  | 61.4% |
| UE5  | 151 | 134 | 68  | 8.49  | 258.92 | 88.7%  | 45.0% |
| UE6  | 151 | 151 | 150 | 18.44 | 83.59  | 100.0% | 99.3% |
| UE7  | 151 | 144 | 121 | 23.98 | 559.69 | 95.4%  | 80.1% |
| UE8  | 151 | 145 | 134 | 15.81 | 167.75 | 96.0%  | 88.7% |
| UE9  | 148 | 138 | 44  | 1.43  | 271.90 | 93.2%  | 29.7% |
| UE10 | 148 | 147 | 43  | 1.11  | 259.27 | 99.3%  | 29.1% |
| UE11 | 148 | 37  | 0   | 0.75  | 0.00   | 25.0%  | 0.0%  |
| UE12 | 148 | 37  | 1   | 0.85  | 956.00 | 25.0%  | 0.7%  |
| UE13 | 148 | 13  | 0   | 0.14  | 0.00   | 8.8%   | 0.0%  |
| UE14 | 148 | 14  | 0   | 0.44  | 0.00   | 9.5%   | 0.0%  |
| UE15 | 148 | 102 | 3   | 0.56  | 521.33 | 68.9%  | 2.0%  |
| UE16 | 148 | 27  | 3   | 1.40  | 545.00 | 18.2%  | 2.0%  |
| UE17 | 145 | 58  | 7   | 0.98  | 728.57 | 40.0%  | 4.8%  |

## UE17 特別說明（延續既有追蹤項目）

UE17（直連 Node4 relay、Node4 同時中繼 Node11+Node12）本次量測吞吐量覆蓋率 40.0%、RTT 覆蓋率 4.8%，平均吞吐量 0.98 Mbps——延續歷史上一貫的「三重負載疊加深層節點」模式（見 `PF.md`「UE17 特別說明」），量測前現場 ping 已確認 0% 封包遺失，取樣覆蓋率偏低是量測期間流量場景本身的路徑損耗/間歇閒置機率所致，不是連線異常。

## 摘要指標與 PF.md / avgFL.md 三方誠實比較

| 指標 | PF baseline (Stage 1) | avg FL (Stage 2) | cluster FL (Stage 3) | Stage 3 vs. avg FL |
|---|---|---|---|---|
| Jain's Fairness Index | 0.3303 | 0.4779 | 0.4163 | 惡化 ❌ |
| 平均吞吐量（17 UE 平均） | 6.45 Mbps | 8.40 Mbps | 6.30 Mbps | 惡化 ❌ |
| 平均 RTT（有效樣本平均） | 224.23 ms | 296.07 ms | 372.57 ms | 惡化 ❌ |
| 零吞吐量樣本的 UE 數 | 0/17 | 1/17 | 0/17 | 改善 |
| 全程崩潰/重啟（FlexRIC/DU/CU） | 0 | 0 | 0 | 持平 |

## 結論（誠實記錄，不僅挑贏的講）

1. **Stage 3 目前在三個核心指標（JFI、吞吐量、RTT）上都不如 Stage 2**，未達成路線圖「單調遞增」的驗收標準。JFI 0.4163 仍優於 PF baseline 的 0.3303，但吞吐量、RTT 皆比 PF baseline 更差。
2. **這次的比較是在兩邊都零基礎設施崩潰、零 NAT/路由異常的乾淨環境下量到的**——三個 root cause（`REWARD_MODE` 缺口、CU NAT flush race、cpuset 過度擁擠）修復後，Stage 2/3 皆全程零新增崩潰，17/17 UE 量測前連通性正常，數據本身可信。
3. **但如上方「重要方法論限制」所述，本次量測全程 FL 聚合幾乎沒有真實資料可用**——`IABFedAvg`／`IABClusterFedAvg` 在這 15 分鐘視窗內幾乎沒有機會做出有意義的非零權重聚合，因此 Stage 2 與 Stage 3 觀察到的差異，主要反映的是 12 個節點各自獨立訓練軌跡的隨機變異，而非「average vs. soft clustered」聚合演算法本身的效果對比。`IABClusterFedAvg` 的程式碼正確性已經過離線數學驗證，這次的落後**不能**直接歸因為聚合公式有 bug。
4. **Stage 3 為什麼比 avg FL 差、以及後續要怎麼調整**，留待下一輪討論決定方向（例如拉長量測視窗、調整訓練門檻、或重新檢視 soft clustering 在稀疏訓練資料下的有效樣本量問題），這份文件先如實記錄本次乾淨環境下量到的數字與已知的方法論限制。
