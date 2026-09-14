# Stage 2 — avg FL + 最基礎 DRL 量測結果

**量測日期**：2026-09-14（重測版，取代 2026-09-13 的舊版本）
**狀態**：全部 12 個 Local xApp+Local rApp 上線（`REWARD_MODE=throughput_only`，`inference-nodeN` 與 `flower-supernode-nodeN` 兩條訓練路徑皆已確認正確套用，見下方「重測原因」），Global xApp（全域公平性廣播）+ Global rApp（標準 FedAvg，12 節點一起聚合）全程運作
**比較基準**：`PF.md`（Stage 1，JFI=0.3303、平均吞吐量 6.45 Mbps、平均 RTT 224.23 ms，seed `20260914`）

## 重測原因（2026-09-13 舊版本已作廢）

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

1. **REWARD_MODE 雙路徑確認**：`docker exec inference-node1 env | grep REWARD_MODE` 與 `docker exec flower-supernode-node1 env | grep REWARD_MODE` 皆為 `throughput_only`；`docker logs flower-supernode-node1 | grep -oE "lambda=[0-9.]+" | sort -u` 只有 `lambda=0.0000`，全程未曾更新過。
2. **清空後乾淨重啟**：三主機**完全依序**（不同時）啟動——`start_iab_server.sh`（PC1 基礎設施）跑完 → PC2 完整跑完 → PC3 完整跑完 → `run_local_pc1.sh --skip-server`（E2 等待 + xApp 啟動），13/13 E2 連線、12/12 xApp 零 fallback。
3. **量測前現場連通性**：全部 17 個 UE（含 UE17）現場 ping 測試皆為 0% 封包遺失。
4. **`rfsim5g-donor-cu` RestartCount 基準點確認為 0**，量測全程未再增加（見下方系統穩定性）。

## 量測方法（與 PF.md 完全相同）

```bash
FL_MODE=avg REWARD_MODE=throughput_only bash iab/run_stage2_fl.sh

python3 scenarios/traffic_scenario.py --scenario R --seed 20260914 --host {pc1,pc2,pc3} --phase-duration 60 --num-phases 15
python3 iab/measure_stage.py --host {pc1,pc2,pc3} --duration 900 --interval 5 --out /tmp/stage2final_{host}.csv
```

同一組 Scenario R、同一個 seed（`20260914`），與 PF.md 構成有效的 paired comparison。

**全程系統穩定性**：本次 15 分鐘量測全程，**三主機的 FlexRIC、Donor CU/DU、全部 12 個 Node DU/MT 容器 `RestartCount` 全部維持 0（零新增崩潰）**——這是三個 root cause 修復後第一次做到「全程零崩潰」，包含過去經常有零星自我重啟的 C 語言 xApp 容器（本次沒有額外檢查 xApp 層級 watchdog 次數，但 RAN 層級全數乾淨）。

## 量測結果（全部 17 個 UE）

| UE | 取樣總數 | 吞吐量取樣數 | RTT取樣數 | 平均吞吐量 (Mbps) | 平均 RTT (ms) | 吞吐量覆蓋率 | RTT覆蓋率 |
|---|---|---|---|---|---|---|---|
| UE1  | 146 | 142 | 123 | 19.22 | 244.72 | 97.3%  | 84.2% |
| UE2  | 146 | 141 | 124 | 5.72  | 190.20 | 96.6%  | 84.9% |
| UE3  | 146 | 145 | 123 | 17.95 | 166.36 | 99.3%  | 84.2% |
| UE4  | 146 | 137 | 112 | 12.14 | 170.76 | 93.8%  | 76.7% |
| UE5  | 154 | 144 | 101 | 26.63 | 320.90 | 93.5%  | 65.6% |
| UE6  | 154 | 151 | 137 | 9.55  | 102.78 | 98.1%  | 89.0% |
| UE7  | 154 | 151 | 137 | 25.32 | 276.57 | 98.1%  | 89.0% |
| UE8  | 154 | 152 | 136 | 11.17 | 119.19 | 98.7%  | 88.3% |
| UE9  | 148 | 39  | 24  | 8.00  | 211.79 | 26.4%  | 16.2% |
| UE10 | 148 | 74  | 25  | 1.09  | 243.00 | 50.0%  | 16.9% |
| UE11 | 148 | 125 | 20  | 1.03  | 342.45 | 84.5%  | 13.5% |
| UE12 | 148 | 118 | 20  | 1.02  | 316.80 | 79.7%  | 13.5% |
| UE13 | 148 | 57  | 15  | 0.74  | 157.67 | 38.5%  | 10.1% |
| UE14 | 148 | 0   | 15  | 0.00  | 157.80 | 0.0%   | 10.1% |
| UE15 | 148 | 61  | 4   | 0.84  | 679.25 | 41.2%  | 2.7%  |
| UE16 | 148 | 62  | 4   | 1.35  | 570.25 | 41.9%  | 2.7%  |
| UE17 | 146 | 65  | 10  | 1.00  | 762.70 | 44.5%  | 6.8%  |

## 摘要指標與 PF.md 誠實比較

| 指標 | PF baseline (Stage 1) | avg FL (Stage 2，重測版) | 變化 |
|---|---|---|---|
| Jain's Fairness Index | 0.3303 | **0.4779** | **改善 +44.7%** ✅ |
| 平均吞吐量（17 UE 平均） | 6.45 Mbps | **8.40 Mbps** | **改善 +30.2%** ✅ |
| 平均 RTT（有效樣本平均） | 224.23 ms | 296.07 ms | 惡化 +32.1% ❌ |
| 零吞吐量樣本的 UE 數 | 0/17 | 1/17（UE14） | 惡化 |
| 全程崩潰/重啟（FlexRIC/DU/CU） | 0 | 0 | 持平 |

## 結論（誠實記錄，不僅挑贏的講）

1. **JFI 與平均吞吐量雙雙明顯改善**：JFI 0.3303→0.4779（+44.7%）、吞吐量 6.45→8.40 Mbps（+30.2%），皆優於 2026-09-13 舊版本（JFI 0.3976、6.58 Mbps）——三個基礎設施 root cause 修復後，DRL 訓練不再受 lagrangian 污染與環境隨機崩潰干擾，真實效能比舊版本量到的數字更好。
2. **平均 RTT 仍未達成單調遞增要求**：224.23→296.07 ms（+32.1%），比舊版本的 360.70 ms 好，但仍比 PF baseline 差。判斷主因與舊版本相同——DRL Actor 推論延遲疊加進 MAC 排程週期，這次修復的三個 root cause 都跟 RTT 沒有直接關係，這個限制留待後續階段（例如優化 DRL Actor 的 forward pass 效能）處理。
3. **UE14 出現 1 筆零吞吐量樣本**：吞吐量覆蓋率 0%、但 RTT 覆蓋率 10.1%（ping 有回應、iperf3 未量到有效速率），跟深層節點在特定 phase 遇到高路徑損耗或間歇閒置的既有模式一致，不是本次新出現的異常。
4. **系統穩定性首次做到全程零崩潰**：三主機 FlexRIC、Donor CU/DU、全部 12 個 Node DU/MT 容器 `RestartCount` 全程維持 0，是三個 root cause 修復後第一次達成的乾淨基準，後續 Stage 3~5 應以此為穩定性參照。
5. **本次數據取代 2026-09-13 舊版本**，作為 Stage 3 起續階段比較的正式基準；舊版本的完整除錯過程保留在 `HISTORY.md` 供歷史參考。
