# 專案背景 (Project Context)
本專案為 IEEE 實驗性論文 "Performance Evaluation of xApp-based PRB Allocation in an O-RAN IAB Testbed"。

測試平台核心架構：
* OpenAirInterface (OAI) + USRPs + Ubuntu PREEMPT_RT 主機
* BAP-less 設計：Linux IP forwarding + 靜態路由取代 3GPP BAP 層
* near-RT RIC (FlexRIC) + 自定義 xApp，即時調整 Backhaul/Access PRB 分配

# 實驗目標 (No DRL)
比較以下 **5 種** PRB 分配策略：

| 策略 | BH 比例 | AC 比例 | 說明 |
|------|---------|---------|------|
| fixed50 | 50% | 50% | 固定均分 |
| fixed30 | 30% | 70% | 偏重 Access |
| fixed10 | 10% | 90% | 最大 Access |
| adaptive | 動態 | 動態 | Algorithm 1 (BSR-based) |
| pf | — | — | OAI 預設 PF 排程器（不啟動 xApp） |

# 拓撲與節點分類
```
5G Core ─── Donor DU (port 4043)
               ├─ IAB Node 1 (nb_id=3585, relay, port 4044)
               │    └─ IAB Node 3 (nb_id=3587, access, port 4046) ─ UE 1,2
               ├─ IAB Node 2 (nb_id=3586, relay, port 4045)
               │    ├─ IAB Node 4 (nb_id=3588, access, port 4047) ─ UE 3,4
               │    └─ IAB Node 5 (nb_id=3589, access, port 4048) ─ UE 5,6
```

* nb_id 3585, 3586 → **relay** → BH_ratio 適用
* nb_id 3587, 3588, 3589 → **access** → AC_ratio 適用

# 實驗腳本架構

## 核心檔案
| 檔案 | 執行位置 | 用途 |
|------|---------|------|
| `openair2/E2AP/flexric/examples/xApp/c/ctrl/xapp_prb_alloc.c` | PC1 | 統一 xApp（單一 binary，PRB_POLICY env var 切換策略） |
| `ci-scripts/yaml_files/5g_rfsimulator/iab/run_experiment_pc1.sh` | **PC1** | 主控腳本：管理 xApp、協調 PC2、收集結果 |
| `ci-scripts/yaml_files/5g_rfsimulator/iab/run_experiment_pc2.sh` | **PC2**（PC1 透過 SSH 呼叫） | 量測腳本：從 6 個 UE 容器跑 iperf3/ping |
| `ci-scripts/yaml_files/5g_rfsimulator/iab/analyze_results.py` | PC1 | 數據分析 + 論文圖表 |

## 雙機分工
```
PC1 (192.168.88.1) — server
  ├── FlexRIC, 5G Core, ext-dn (iperf3 server, 192.168.72.135)
  ├── IAB Node 1 DU (rfsim5g-iab-du)   ← relay, CPU 監控
  ├── IAB Node 2 DU (rfsim5g-iab-du-2) ← relay, CPU 監控
  └── xapp_prb_alloc (host process)

PC2 (192.168.88.2) — client
  ├── IAB Node 3/4/5 DU (rfsim5g-iab-du-3~5) ← access, CPU 監控
  └── End UE 1~6 (rfsim5g-end-ue-1~6) ← iperf3 client + ping
```

## 編譯 xApp
```bash
cd ~/openairinterface5g/openair2/E2AP/flexric/build
cmake -G Ninja -DCMAKE_BUILD_TYPE=Release \
      -DKPM_VERSION=KPM_V3_00 -DE2AP_VERSION=E2AP_V2 ..
ninja xapp_prb_alloc
# binary: build/examples/xApp/c/ctrl/xapp_prb_alloc
```

## 執行實驗（確保 IAB 系統已啟動後）
```bash
# 在 PC1 執行（PC2 腳本會自動 SCP 過去）：
cd ~/openairinterface5g
bash ci-scripts/yaml_files/5g_rfsimulator/iab/run_experiment_pc1.sh

# run_experiment_pc2.sh 由 PC1 透過 SSH 自動呼叫，不需手動執行
# 結果: iab/results/<policy>/iperf_ue*.json, ping_ue*.txt, cpu_relay.log, cpu_access.log
```

## 產生論文圖表
```bash
cd ~/openairinterface5g
python3 ci-scripts/yaml_files/5g_rfsimulator/iab/analyze_results.py
# 輸出: iab/results/fig2_throughput.pdf, fig3_latency_cdf.pdf, paper_variables.txt
```

## xApp 啟動方式（手動測試）
```bash
# 只跑一種策略（以 fixed30 為例）：
PRB_POLICY=fixed30 \
  ./openair2/E2AP/flexric/build/examples/xApp/c/ctrl/xapp_prb_alloc \
  -c /usr/local/etc/flexric/flexric.conf \
  -p /usr/local/lib/flexric/

# PF baseline: 不啟動 xApp，直接量測
```

# 數據處理指南 (Data Processing & Analysis Guidelines)

## 1. 吞吐量 (Throughput)
* **來源**：iperf3 UDP -b 30M -t 60s，6 UE 同時，取 receiver Mbps 加總
* **論文變數**：$X_{50/50}$, $X_{30/70}$, $X_{10/90}$, $X_{dyn}$, $X_{PF}$

## 2. 端到端延遲 (Latency)
* **來源**：ping -c 300 -i 0.2，提取每筆 RTT
* **繪圖**：CDF (Fig. 3)
* **論文變數**：
  * 平均：$L_{50/50}$, $L_{30/70}$, $L_{10/90}$, $L_{dyn}$, $L_{PF}$
  * 95th-pct：$P_{50/50}$, $P_{30/70}$, $P_{10/90}$, $P_{dyn}$, $P_{PF}$

## 3. CPU 使用率
* **來源**：docker stats snapshot，IAB 節點容器
* **目標**：Adaptive xApp 額外 CPU overhead < **3%**（相較 fixed 策略）

# LaTeX 寫作原則 (STRICT)
* **不宣稱絕對優勢**：展示「吞吐量-延遲 trade-off」，不能說任何 xApp 策略全面優於 PF
* **客觀陳述 PF**：若 PF 吞吐量最高，直接陳述，說明 fixed/adaptive 以峰值換取佇列穩定性
* **反例格式**：「50/50 雖吞吐量低於 PF，但 P_{50/50} < P_{PF}，顯示不同的 trade-off」

# 學術繪圖規範
* Python `matplotlib`，字體 `Times New Roman`（IEEE 規範）
* 多條線：不同 linestyle + marker，確保黑白印刷可辨識
* 輸出 PDF + `tight_layout()`

# 系統參數 (Table I)
| 參數 | 值 |
|------|----|
| OS | Ubuntu 22.04 PREEMPT_RT |
| SDR | USRP B210/N310 |
| 頻率 | 3.5 GHz (Band n78) |
| 頻寬 | 40 MHz |
| SCS | 30 kHz |
| Software | OAI + Docker Compose |
| RIC | FlexRIC |
| Total PRBs | **106** |
