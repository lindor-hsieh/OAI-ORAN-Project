#!/bin/bash
# run_local_pc3.sh — PC3 三主機版資料面啟動流程
#
# 只啟動 Node3,4(relay) + Node9,10,11,12(access) + UE9~17（含直連 Node4 的 UE17）。

COMPOSE_DIR=~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()  { echo -e "${CYAN}[PC3 $(date '+%H:%M:%S')]${NC} $*"; }
ok()   { echo -e "${GREEN}  ✓${NC} $*"; }

log "啟動 IAB Node3,4(relay) + Node9,10,11,12(access) + UE9~17..."
cd "$COMPOSE_DIR"
bash iab/start_iab_pc3.sh
ok "PC3 IAB 資料面啟動完成"
