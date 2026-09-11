#!/bin/bash
# run_local_pc2.sh — PC2 三主機版資料面啟動流程
#
# 本輪範疇不含 CQI 校正 / PF Baseline / 流量場景（見 run_local_pc1.sh 開頭註記）。
# 只啟動 Node1,2(relay) + Node5,6,7,8(access) + UE1~8。

COMPOSE_DIR=~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()  { echo -e "${CYAN}[PC2 $(date '+%H:%M:%S')]${NC} $*"; }
ok()   { echo -e "${GREEN}  ✓${NC} $*"; }

log "啟動 IAB Node1,2(relay) + Node5,6,7,8(access) + UE1~8..."
cd "$COMPOSE_DIR"
bash iab/start_iab_pc2.sh
ok "PC2 IAB 資料面啟動完成"
