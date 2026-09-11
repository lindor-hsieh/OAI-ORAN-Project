#!/bin/bash
# run_local_pc1.sh — PC1 三主機版基礎設施啟動流程
#
# 本輪範疇（1 donor + 4 relay + 8 access + 17 UE，三主機）明確不含 CQI 校正、
# PF Baseline 對比、流量場景（這些綁在舊的 5-node NODE_CONFIG，尚未針對新
# 12-node 拓樸更新，見 CLAUDE.md）。這支腳本只做：啟動基礎設施 → 等待全部
# 13 個 E2 連線（1 donor + 12 node）→ 逐一啟動 12 個 xApp。
#
# 用法：
#   bash run_local_pc1.sh               # 完整流程
#   bash run_local_pc1.sh --skip-server # 跳過 start_iab_server.sh（已在跑時用）

COMPOSE_DIR=~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator
COMPOSE_FILE="$COMPOSE_DIR/docker-compose-iab-server.yaml"

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

log()  { echo -e "${CYAN}[PC1 $(date '+%H:%M:%S')]${NC} $*"; }
ok()   { echo -e "${GREEN}  ✓${NC} $*"; }
warn() { echo -e "${YELLOW}  ⚠${NC} $*"; }

# ── Step 1: 啟動基礎設施 ────────────────────────────────────
if [[ "$*" == *"--skip-server"* ]]; then
    warn "跳過 start_iab_server.sh（--skip-server）"
else
    log "Step 1: 啟動基礎設施（CN5G + FlexRIC + MongoDB + Donor CU/DU + 12 組 inference）..."
    cd "$COMPOSE_DIR"
    bash iab/start_iab_server.sh
fi

# ── Step 2: 等待 13 個 E2 連線（1 donor + 12 node） ─────────
log "Step 2: 等待 13 個 E2 連線（PC2 的 6 個 + PC3 的 6 個 + Donor 自己）..."
while true; do
    E2=$(docker logs flexric 2>&1 | grep -c "E2 SETUP-REQUEST" 2>/dev/null || echo 0)
    echo -ne "\r  E2 連線：${E2}/13  $(date '+%H:%M:%S')"
    [ "$E2" -ge 13 ] && break
    sleep 5
done
echo ""
ok "13 個 E2 連線已就緒"

# ── Step 3: 逐一啟動 12 個 xApp ──────────────────────────────
log "Step 3: 逐一啟動 12 個 xApp（每個間隔 3 秒，避免 FlexRIC pending queue 打爆）..."
for node in 1 2 3 4 5 6 7 8 9 10 11 12; do
    docker compose -f "$COMPOSE_FILE" up -d "node${node}-l-xapp"
    ok "node${node}-l-xapp 已啟動"
    [ "$node" -lt 12 ] && sleep 3
done

sleep 5
ZMQ=$(ls /tmp/zmq_node*_inference.ipc 2>/dev/null | wc -l)
ok "ZMQ sockets: ${ZMQ}/12"
FB=$(docker logs xapp-node1 2>&1 | grep -c "fallback" 2>/dev/null || echo 0)
[ "$FB" -gt 0 ] && warn "Node1 fallback 次數：$FB（確認 inference-node1 容器）" \
                || ok "Node1 ZMQ 正常（無 fallback）"

echo ""
echo -e "${GREEN}==================================================${NC}"
echo -e "${GREEN} PC1 基礎設施 + 12 個 xApp 全部啟動完成${NC}"
echo -e "${GREEN}==================================================${NC}"
echo -e "驗證指令："
echo -e "  docker logs flexric 2>&1 | grep -c 'E2 SETUP-REQUEST'   # 應為 13"
echo -e "  docker logs xapp-nodeN 2>&1 | tail -20                  # 確認無 fallback"
echo -e "  docker exec mongodb mongosh iab_xapp --eval 'db.getCollectionNames()'"
