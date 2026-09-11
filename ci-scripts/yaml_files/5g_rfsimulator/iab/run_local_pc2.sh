#!/bin/bash
# run_local_pc2.sh — PC2 三主機版資料面啟動流程
#
# 本輪範疇不含 CQI 校正 / PF Baseline / 流量場景（見 run_local_pc1.sh 開頭註記）。
# 啟動 Node1,3,4(relay，Node4 含直連 UE17) + Node5,6(access) + UE1~4,17。
# 2026-09-12 兩輪搬遷（見 CLAUDE.md/HISTORY.md）：Node2/7,8 + UE5~8 搬到
# PC1；Node3,4(relay)+UE17 從 PC3 搬到這裡（Node9~12 這 4 個 access 節點
# 仍留在 PC3，跨主機連到這裡的 relay DU）。

COMPOSE_DIR=~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()  { echo -e "${CYAN}[PC2 $(date '+%H:%M:%S')]${NC} $*"; }
ok()   { echo -e "${GREEN}  ✓${NC} $*"; }

log "啟動 IAB Node1,3,4(relay) + Node5,6(access) + UE1~4,17..."
cd "$COMPOSE_DIR"
bash iab/start_iab_pc2.sh
ok "PC2 IAB 資料面啟動完成"
