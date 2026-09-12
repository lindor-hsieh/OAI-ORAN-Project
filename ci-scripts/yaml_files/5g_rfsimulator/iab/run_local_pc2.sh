#!/bin/bash
# run_local_pc2.sh — PC2 三主機版資料面啟動流程
#
# 本輪範疇不含 CQI 校正 / PF Baseline / 流量場景（見 run_local_pc1.sh 開頭註記）。
# 啟動 Node1,3,4(relay，Node4 含直連 UE17) + Node5,6(access) + UE1~4,17。
# 2026-09-12 兩輪搬遷（見 CLAUDE.md/HISTORY.md）：Node2/7,8 + UE5~8 搬到
# PC1；Node3,4(relay)+UE17 從 PC3 搬到這裡（Node9~12 這 4 個 access 節點
# 仍留在 PC3，跨主機連到這裡的 relay DU）。

# 寫死絕對路徑，不要用 ~ ——PC2 的 OS 帳號是 mcalab（$HOME=/home/mcalab），
# 但專案檔案跟 docker-compose 的絕對路徑 bind-mount 都在 /home/lindor 下
# （見 CLAUDE.md 第 6 節）。~ 展開是照執行當下的 $HOME，不是照這支腳本
# 實際存放的路徑，兩者對不上會讓下面的 cd 直接失敗（2026-09-12 踩過，見
# HISTORY.md），即使是 mcalab 自己互動式登入也一樣會踩到，不是只有非互動
# SSH 呼叫才會發生。
COMPOSE_DIR=/home/lindor/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()  { echo -e "${CYAN}[PC2 $(date '+%H:%M:%S')]${NC} $*"; }
ok()   { echo -e "${GREEN}  ✓${NC} $*"; }

log "啟動 IAB Node1,3,4(relay) + Node5,6(access) + UE1~4,17..."
cd "$COMPOSE_DIR" || { echo "[FATAL] cd 到 $COMPOSE_DIR 失敗" >&2; exit 1; }
bash iab/start_iab_pc2.sh || { echo "[FATAL] start_iab_pc2.sh 執行失敗" >&2; exit 1; }
ok "PC2 IAB 資料面啟動完成"
