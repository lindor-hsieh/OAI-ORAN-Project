#!/bin/bash
# watchdog.sh — FlexRIC E2 崩潰偵測，自動停止所有服務並通知手動重啟
#
# 偵測方式：xapp-node1 最近 10 行全為 "等待 Node" = E2 subscription 狀態丟失
# 偵測到後：停止所有 xApp + 通知 PC2 停止流量，等待手動重啟
#
# 用法：bash watchdog.sh（在 run_local_pc1.sh 完成後的第三個終端執行）

COMPOSE_DIR=~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator
COMPOSE_FILE="$COMPOSE_DIR/docker-compose-iab-server.yaml"
PC2_USER="lindor"
PC2_IP="192.168.88.2"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=10 -o BatchMode=yes"

POLL_INTERVAL=30   # 每 30 秒檢查一次
CONFIRM_WAIT=60    # 初次偵測到後等 60 秒再確認（避免誤觸發）
STARTUP_GRACE=120  # 啟動後等 120 秒再開始監控

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

log()  { echo -e "${CYAN}[WD $(date '+%H:%M:%S')]${NC} $*"; }
ok()   { echo -e "${GREEN}  ✓${NC} $*"; }
warn() { echo -e "${YELLOW}  ⚠${NC} $*"; }
err()  { echo -e "${RED}  ✗${NC} $*"; }
alert(){ echo -e "${RED}"; echo ""; echo ""; echo ""; echo ""; echo ""; echo ""; echo ""; echo -e "  ██████████████████████████████████████████████"; echo -e "  ██  E2 崩潰！請手動重啟系統！  ██"; echo -e "  ██████████████████████████████████████████████${NC}"; echo ""; }

# ── 崩潰偵測 ────────────────────────────────────────────────
is_e2_crashed() {
    local waiting
    waiting=$(docker logs --tail 10 xapp-node1 2>/dev/null | grep -c "等待 Node")
    [ "$waiting" -ge 8 ]
}

# ── 停止所有服務 ─────────────────────────────────────────────
do_stop() {
    log "=== E2 崩潰確認，停止所有服務 ==="

    # 停止 PC1 xApps
    log "停止 PC1 xApps..."
    for node in 1 2 3 4 5; do
        docker compose -f "$COMPOSE_FILE" stop "node${node}-l-xapp" 2>/dev/null
    done
    ok "PC1 xApps 已停止"

    # 通知 PC2 停止流量場景
    log "通知 PC2 停止流量場景..."
    ssh $SSH_OPTS ${PC2_USER}@${PC2_IP} \
        "pkill -f 'traffic_scenario.py' 2>/dev/null; pkill -f 'iperf3 -c' 2>/dev/null; echo 'PC2 流量已停止'" \
        2>/dev/null && ok "PC2 流量已停止" || warn "PC2 SSH 失敗，請手動停止 traffic_scenario.py"

    # 清除 zombie iperf 進程
    ssh $SSH_OPTS ${PC2_USER}@${PC2_IP} \
        "kill -9 \$(ps aux | awk '/defunct/{print \$2}' | tr '\n' ' ') 2>/dev/null || true" \
        2>/dev/null

    alert

    echo ""
    log "請依序執行以下重啟指令："
    echo ""
    echo -e "  ${YELLOW}PC1 終端：${NC}"
    echo -e "    cd ~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator"
    echo -e "    bash iab/run_local_pc1.sh"
    echo ""
    echo -e "  ${YELLOW}PC2 終端（PC1 進入 Step 2 後）：${NC}"
    echo -e "    cd ~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator"
    echo -e "    bash iab/run_local_pc2.sh --skip-calibrate"
    echo ""
    log "Watchdog 退出，請手動重啟後再次執行 watchdog.sh"
}

# ── 主迴圈 ──────────────────────────────────────────────────
log "Watchdog 啟動（偵測模式，崩潰時自動停止服務）"
log "  輪詢：每 ${POLL_INTERVAL}s，確認等待 ${CONFIRM_WAIT}s"
log "  啟動靜默期：${STARTUP_GRACE}s"
echo ""

log "靜默期 ${STARTUP_GRACE}s..."
sleep $STARTUP_GRACE
log "開始監控..."

while true; do
    if is_e2_crashed; then
        warn "偵測到 E2 異常，等待 ${CONFIRM_WAIT}s 確認..."
        sleep $CONFIRM_WAIT
        if is_e2_crashed; then
            do_stop
            exit 0
        else
            log "誤報，系統已恢復，繼續監控"
        fi
    fi
    sleep $POLL_INTERVAL
done
