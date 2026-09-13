# Stage 2 — avg FL + 最基礎 DRL 量測結果

**量測日期**：2026-09-13
**狀態**：全部 12 個 Local xApp+Local rApp 上線（`REWARD_MODE=throughput_only`，無 Lagrangian 限制式），Global xApp（全域公平性廣播）+ Global rApp（標準 FedAvg，12 節點一起聚合）全程運作
**比較基準**：`PF.md`（Stage 1，JFI=0.3303、平均吞吐量 6.45 Mbps、平均 RTT 224.23 ms，seed `20260914`）

## 架構摘要（本階段新增於 PF baseline 之上的元件）

- **Local xApp + Local rApp**：與 PF baseline 相同的 C xApp 控制迴圈，但 Local rApp 從「純被動蒐集資料」改為主動執行 DRL Actor 推論並下發 PRB 權重（`REWARD_MODE=throughput_only`：`R = R_throughput`，無 JFI 限制式、λ 恆為 0）。
- **Global xApp（新設計，取代舊版 5-node 配額廣播機制）**：獨立 Python process，每 2 秒讀一次 MongoDB 全部 12 個節點的最近經驗，算出每個節點「相對全域平均吞吐量」的落差，換算成 `fairness_bias ∈ [0.5, 2.0]`（低於平均→bias>1，代表可以更積極），透過 ZMQ PUB 廣播給全部 12 個 Local rApp，寫入 DRL state vector 的第 49 維。**純軟性 state 特徵，不做任何硬性 PRB 裁切**，因此不與 Stage 1 已完成的 C 層 backhaul-aware PRB 預算機制衝突（兩者作用在不同軸：C 層依「自己 MT 的真實忙碌度」縮小資源池上限，Global xApp 依「全域相對落後程度」調整 DRL 決策傾向）。
- **Global rApp**：Flower ServerApp/ClientApp 標準 FedAvg，`FL_NUM_NODES=12`，`FL_ROUND_INTERVAL_S=180`（15 分鐘量測視窗內約可跑 5 輪）。

## 上線前的清理與除錯過程——如實記錄避免未來重蹈覆轍

這次上線過程中發現並修復了以下基礎設施問題，已驗證的部分永久寫入啟動腳本（非手動 ad-hoc 修補）；詳細除錯過程見 `HISTORY.md` 2026-09-13 條目：

1. **PC1 CPU 資源競爭導致 FlexRIC 崩潰**：Stage 2 首次讓全部 12 個 Local rApp 同時做真實 DRL 推論+背景訓練，PC1 同時還跑著 Donor CU/DU + Node2/7/8 三組即時 RT-priority（`chrt -f 80`）softmodem，兩者疊加造成 CPU load average 一度飆到 37（16 核心主機），MAC indication delivery 延遲觸發全部 xApp 的 30 秒 watchdog 連鎖重連，最終壓垮 FlexRIC 的 pending event queue（`[NEAR-RIC]: WARNING: Pending event timeout`，對應 CLAUDE.md 已知的「現象一」崩潰模式）。修復：在 `docker-compose-iab-server.yaml` 幫全部 12 個 `inference-nodeN` 容器加上 `cpuset: "12-15"`，把 DRL 推論/訓練負載硬性限制在 4 個核心內，跟 RT-priority 的 DU/MT 執行緒物理隔離。套用後 load average 從 37 降回 7~9，FlexRIC 崩潰後續未再復發（**已驗證，已永久寫入 `docker-compose-iab-server.yaml`**）。
2. **PC2/PC3 Docker `DOCKER-USER` chain 缺少 GTP-U/SCTP 放行規則**：Docker 預設的 bridge 隔離規則會擋掉從 macvlan 進入 `iab_internal_net` 的 GTP-U（udp:2152/2153）與 SCTP 流量。修復：在 `iab/start_iab_pc2.sh`/`start_iab_pc3.sh` 開頭新增永久放行規則，**用固定子網（`192.168.74.0/24`／`192.168.75.0/24`）比對，不用 bridge 介面名稱**（因為每次 `docker compose down` 重來都會產生新的隨機 bridge 名稱，寫死介面名稱下次重啟就會失效），且用 `iptables -C` 先檢查再插入，冪等可重複執行（**已驗證，已永久寫入啟動腳本**）。
3. **清空 checkpoint 時誤用 compose service-local key 名稱**：`docker-compose-iab-server.yaml` 的 `inference_models_nodeN` 是 service 內部 volume key，實際 Docker volume 用 `name:` 覆寫成 `iab-xapp-model-nodeN`——第一次清空時誤用內部 key 掛載，建立了一個全新的空白同名 volume，實際 checkpoint 從未被清到，導致切換 `REWARD_MODE` 後仍載入舊模式殘留的權重（`train_steps=940, lambda=10.0`）。修復：改用 `docker volume ls` 確認的實際名稱清空（**已驗證，`iab/run_stage2_fl.sh` 已用正確名稱並附上踩坑註解**）。
4. **累積系統狀態導致 UE13/UE14（Node11, PC3）完全斷線**：長時間偵錯過程中累積的手動介入（多次容器重啟、DNAT 重新套用、iptables 補丁）造成 GTP-U/PFCP 層某種 stale 狀態（radio link 本身健康——RSRP -44dB、0 BLER、MAC 層持續有真實 TX/RX——但封包無法正確送達目的地）。**這不是本次正式量測時遇到的問題**（正式量測前已用乾淨重啟解決），而是提醒往後累積過多手動修補後，即使個別修補都正確，整體系統狀態仍可能劣化到無法單靠繼續 debug 排除，必須做一次完整乾淨重啟（見下方「量測前驗證」）。
5. **待釐清**：除錯過程中曾討論「PC2/PC3 access 節點 F1AP 跨主機路由缺失、SCTP association 卡在 `COOKIE_WAIT`」的假說與對應的 `ip route add` 修復動作，但事後檢查 `docker-compose-iab-pc2.yaml`/`docker-compose-iab-pc3.yaml`，**這個修改實際上不存在於目前的檔案內容裡**。由於後續多次乾淨重啟都確認 access 節點 F1AP 與資料面完全正常，無法確認這個問題是否曾真實存在、或已被其他修復間接解決——如實記錄，不聲稱已修復。

## 上線前驗證（全部通過才進行正式量測）

1. **FedAvg 正確性**：抽取 3 個節點（node1/node2/node7）的 `model_nodeN.pt`，比對 `actor`/`critic` state_dict 張量，`torch.equal()` 全部為 `True`（完全一致），`train_steps=0`／`lambda=0.0` 三節點一致（冷啟動起點正確，非殘留舊 checkpoint）。（註：整份 `.pt` 檔案的 sha256 不同，但確認差異只來自 optimizer state 的 pickle 序列化非決定性排列，不影響權重本身，改用張量級比對才是正確的驗證方法。）
2. **REWARD_MODE 生效確認**：抽查 `node1_experiences` 最新文件，確認**不含** `lambda_applied` 鍵（`compute_reward_breakdown()` 路徑才會缺這個鍵），欄位 `used_drl`/`r_throughput`/`r_fairness`/`r_delay` 皆正確產生。
3. **Global xApp 正確性**：`global-xapp` log 確認每 2 秒印出 12 個節點的 `fairness_bias`／`global_jfi`；`inference-nodeN` log 確認 `[Global xApp] Fairness SUB connected` 與收到廣播後更新 `state_vec[49]` 的行為正常。
4. **清空後乾淨重啟**：三主機全部 `docker compose down` → 依序 `run_local_pc1.sh` → `run_local_pc2.sh`/`run_local_pc3.sh` 重新啟動，13/13 E2 連線、12/12 xApp、**全部 17 個 UE 現場 ping 測試 0% 封包遺失**（含先前完全斷線的 UE13/14、與歷史上最差的 UE17）後，才進行正式量測。

## 量測方法（與 PF.md 完全相同）

```bash
REWARD_MODE=throughput_only bash iab/run_local_pc1.sh
docker compose -f docker-compose-iab-server.yaml --profile stage2-fl up -d global-xapp flower-superlink flower-supernode-node{1..12} flower-scheduler

python3 scenarios/traffic_scenario.py --scenario R --seed 20260914 --host {pc1,pc2,pc3} --phase-duration 60 --num-phases 15
python3 iab/measure_stage.py --host {pc1,pc2,pc3} --duration 900 --interval 5 --out /tmp/stage2_{host}_final.csv
```

同一組 Scenario R、同一個 seed（`20260914`），與 PF.md 構成有效的 paired comparison。

**全程系統穩定性**：本次 15 分鐘量測全程，**三主機的 FlexRIC、Donor CU/DU、全部 Node DU/MT 容器 RestartCount 均維持不變（0 新增崩潰/重啟）**；PC2、PC3 兩台主機純資料面、不部署 xApp/inference 容器，其全部容器 RestartCount 亦完全無變化。PC1 的 12 個 C 語言 xApp 容器有零星自我重啟（watchdog 觸發，範圍 0~27 次不等，以 Node2/7/8 較高），這是文件化的自我修復機制（見 CLAUDE.md 第 7 節「FlexRIC 崩潰規律」），過程中**不影響**FlexRIC/DU 狀態，也未再次觸發 FlexRIC 崩潰。

## 量測結果（全部 17 個 UE）

| UE | 取樣總數 | 吞吐量取樣數 | RTT取樣數 | 平均吞吐量 (Mbps) | 平均 RTT (ms) | 吞吐量覆蓋率 | RTT覆蓋率 |
|---|---|---|---|---|---|---|---|
| UE1  | 146 | 122 | 62  | 17.28 | 341.38 | 83.6%  | 42.5% |
| UE2  | 146 | 142 | 62  | 1.37  | 267.46 | 97.3%  | 42.5% |
| UE3  | 146 | 146 | 144 | 4.14  | 173.79 | 100.0% | 98.6% |
| UE4  | 146 | 142 | 137 | 8.48  | 192.36 | 97.3%  | 93.8% |
| UE5  | 153 | 146 | 103 | 22.78 | 457.13 | 95.4%  | 67.3% |
| UE6  | 153 | 149 | 132 | 10.94 | 141.68 | 97.4%  | 86.3% |
| UE7  | 153 | 149 | 141 | 25.45 | 335.29 | 97.4%  | 92.2% |
| UE8  | 153 | 151 | 143 | 12.82 | 112.66 | 98.7%  | 93.5% |
| UE9  | 148 | 99  | 32  | 1.13  | 239.84 | 66.9%  | 21.6% |
| UE10 | 148 | 103 | 32  | 0.92  | 240.75 | 69.6%  | 21.6% |
| UE11 | 148 | 113 | 17  | 1.64  | 265.65 | 76.4%  | 11.5% |
| UE12 | 148 | 66  | 17  | 0.85  | 258.71 | 44.6%  | 11.5% |
| UE13 | 148 | 98  | 17  | 0.94  | 337.53 | 66.2%  | 11.5% |
| UE14 | 148 | 89  | 17  | 0.81  | 397.71 | 60.1%  | 11.5% |
| UE15 | 148 | 22  | 1   | 0.61  | 790.00 | 14.9%  | 0.7%  |
| UE16 | 148 | 37  | 1   | 0.87  | 812.00 | 25.0%  | 0.7%  |
| UE17 | 146 | 14  | 1   | 0.75  | 768.00 | 9.6%   | 0.7%  |

## 摘要指標與 PF.md 誠實比較

| 指標 | PF baseline (Stage 1) | avg FL (Stage 2) | 變化 |
|---|---|---|---|
| Jain's Fairness Index | 0.3303 | **0.3976** | **改善 +20.4%** ✅ |
| 平均吞吐量（17 UE 平均） | 6.45 Mbps | **6.58 Mbps** | 持平／略升 +2.0% ✅ |
| 平均 RTT（有效樣本平均） | 224.23 ms | **360.70 ms** | **惡化 +60.9%** ❌ |
| 零吞吐量樣本的 UE 數 | 0/17 | 0/17 | 持平 |
| 全程崩潰/重啟（FlexRIC/DU/CU） | 0 | 0 | 持平 |

## 結論（誠實記錄，不僅挑贏的講）

1. **JFI 明顯改善**：Global xApp 的全域公平性廣播機制達成了設計目標——0.3303 → 0.3976，是本階段最主要的正向訊號，證明「讓落後節點的 Local DRL 感知到自己在全域中被犧牲、進而更積極爭取資源」這個設計方向有效。
2. **平均吞吐量持平**：6.45 → 6.58 Mbps，改善幅度很小（+2%），在量測雜訊範圍邊緣，不能視為顯著贏過 PF baseline，只能說「至少沒有變差」。
3. **平均 RTT 明顯惡化，這是本階段目前未達成單調遞增要求的項目**：224.23 → 360.70 ms（+60.9%）。追查後判斷主要原因是 DRL Actor 推論延遲——log 中觀察到多次「推論延遲 27~48ms 接近 5ms 上限」的警告（`inference_server.py` 的 5ms ZMQ round-trip 預算，實際 GRU forward pass 在忙碌時遠超此預算），這個延遲會直接堆疊進 MAC 排程週期，進而推高端到端 RTT。`measure_stage.py` 的 ping 逾時上限（`-W 1`，1 秒）與 PF.md 完全相同，因此這個比較是公平的 apples-to-apples，不是量測方法造成的假象。
4. **深層/極端節點（UE15~17）RTT 覆蓋率仍然很低（<1%）**，這點與 PF baseline 的整體模式一致（深層跨主機節點本來就是系統最大瓶頸），但覆蓋率絕對值比 PF.md 記錄的同類節點更差，可能同樣受到第 3 點 DRL 推論延遲疊加多跳開銷的影響。
5. **系統穩定性完全達標**：15 分鐘量測全程，FlexRIC、Donor CU/DU、全部 12 個 Node DU/MT 容器 RestartCount 零變化——Stage 2 引入的 Global xApp/Flower FL 邏輯確認不會拖累底層 RAN 穩定性（呼應 Phase 4 驗證假設：Global 層走獨立 TCP/Mongo，不碰 FlexRIC E2 queue）。
6. **後續建議**：Stage 3~5 若要讓 RTT 也單調遞增，值得評估是否需要優化 DRL Actor 的 forward pass 效能（例如減少 GRU hidden size、批次化推論、或放寬 5ms 預算並接受稍高的 fallback 比例），否則這個延遲代價可能會持續疊加到後續階段。這不影響本文件作為 Stage 2 的誠實記錄，但應該在論文的限制章節中明確討論「DRL 決策品質 vs. 推論延遲」的 trade-off。
