#!/bin/bash
# run_local_pc1.sh — PC1 第四階段全自動流程
#
# 用法：
#   bash run_local_pc1.sh               # 完整流程
#   bash run_local_pc1.sh --skip-server # 跳過 start_iab_server.sh（已在跑時用）
#
# 流程（xApps 保證在 Baseline 測試結束後才啟動）：
#   Step 1: start_iab_server.sh
#   Step 2: 等待 E2 連線數 = 6（PC2 已完成）
#   Step 3: setup_iperf_servers.sh
#   Step 4: 等待 PC2 CQI 校正完成
#   Step 5: 等待 PC2 PF Baseline 完成（確保量到乾淨 OAI PF 數據）
#   Step 6: 逐一啟動 5 個 xApp + inference 容器
#   Step 7: 通知 PC2 xApps 就緒
#   Step 8: 開啟 DRL 監控儀表板

COMPOSE_DIR=~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator
COMPOSE_FILE="$COMPOSE_DIR/docker-compose-iab-server.yaml"
PC2_USER="lindor"
PC2_IP="192.168.88.2"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=5 -o BatchMode=yes"
FLAG_XAPPS_READY="/tmp/local_xapps_ready"
FLAG_CALIBRATION_DONE="/tmp/local_calibration_done"

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

log()  { echo -e "${CYAN}[PC1 $(date '+%H:%M:%S')]${NC} $*"; }
ok()   { echo -e "${GREEN}  ✓${NC} $*"; }
warn() { echo -e "${YELLOW}  ⚠${NC} $*"; }
err()  { echo -e "${RED}  ✗${NC} $*"; }

wait_ssh_pc2() {
    local desc=$1; local check_cmd=$2
    log "$desc"
    while ! ssh $SSH_OPTS ${PC2_USER}@${PC2_IP} "$check_cmd" 2>/dev/null; do
        echo -ne "\r  等待中... $(date '+%H:%M:%S')"
        sleep 5
    done
    echo ""
}

FLAG_BASELINE_DONE="/tmp/local_baseline_done"

# ── 清除舊 flag ──────────────────────────────────────────────
rm -f "$FLAG_XAPPS_READY" "$FLAG_CALIBRATION_DONE" "$FLAG_BASELINE_DONE"
ssh $SSH_OPTS ${PC2_USER}@${PC2_IP} \
    "rm -f /tmp/local_calibration_done /tmp/local_baseline_done /tmp/local_xapps_ready" \
    2>/dev/null || true

# ── Step 1: 啟動基礎設施 ────────────────────────────────────
if [[ "$*" == *"--skip-server"* ]]; then
    warn "跳過 start_iab_server.sh（--skip-server）"
else
    log "Step 1: 啟動基礎設施..."
    cd "$COMPOSE_DIR"
    bash iab/start_iab_server.sh
fi

# ── Step 2: 等待 6 個 E2 連線 ───────────────────────────────
log "Step 2: 等待 6 個 E2 連線（PC2 連線中）..."
while true; do
    E2=$(docker logs flexric 2>&1 | grep -c "E2 SETUP-REQUEST" 2>/dev/null || echo 0)
    echo -ne "\r  E2 連線：${E2}/6  $(date '+%H:%M:%S')"
    [ "$E2" -ge 6 ] && break
    sleep 5
done
echo ""
ok "6 個 E2 連線已就緒"

# ── Step 3: 啟動 iperf3 Servers ─────────────────────────────
log "Step 3: 啟動多 port iperf3 Servers..."
bash "$COMPOSE_DIR/scenarios/setup_iperf_servers.sh" || warn "setup_iperf_servers.sh 有錯誤，繼續執行..."
ok "iperf3 servers 就緒（port 5201~5206）"

# ── Step 4: 等待 PC2 CQI 校正完成 ───────────────────────────
wait_ssh_pc2 "Step 4: 等待 PC2 CQI 校正完成..." \
    "test -f /tmp/local_calibration_done"
ok "PC2 CQI 校正完成"

# ── Step 5: 等待 PC2 PF Baseline 完成 ────────────────────────
# xApps 必須在 Baseline 結束後才啟動，確保量到乾淨的 OAI PF Scheduler 數據
wait_ssh_pc2 "Step 5: 等待 PC2 PF Baseline 測試完成（xApps 尚未啟動）..." \
    "test -f /tmp/local_baseline_done"
ok "PC2 PF Baseline 完成，開始啟動 xApps"

# ── Step 6: 逐一啟動 xApps ──────────────────────────────────
log "Step 6: 逐一啟動 xApps..."
for node in 1 2 3 4 5; do
    docker compose -f "$COMPOSE_FILE" up -d "node${node}-l-xapp"
    ok "node${node}-l-xapp 已啟動"
    [ "$node" -lt 5 ] && sleep 3
done

# 更新所有 inference 容器的 reward_calculator.py（確保 all-idle guard 生效）
log "  更新 inference 容器 reward_calculator.py..."
REWARD_SRC="$COMPOSE_DIR/inference/reward_calculator.py"
for n in 1 2 3 4 5; do
    docker cp "$REWARD_SRC" "inference-node${n}:/app/reward_calculator.py" 2>/dev/null \
        && ok "inference-node${n} reward_calculator.py 已更新" \
        || warn "inference-node${n} cp 失敗（容器可能尚未就緒）"
done

# 驗證 ZMQ
sleep 2
ZMQ=$(ls /tmp/zmq_node*_inference.ipc 2>/dev/null | wc -l)
ok "ZMQ sockets: ${ZMQ}/5"
FB=$(docker logs xapp-node1 2>&1 | grep -c "fallback" 2>/dev/null || echo 0)
[ "$FB" -gt 0 ] && warn "Node1 fallback 次數：$FB（確認 inference-node1 容器）" \
                || ok "Node1 ZMQ 正常（無 fallback）"

# ── Step 7: 通知 PC2 xApps 就緒 ─────────────────────────────
log "Step 7: 通知 PC2 xApps 就緒..."
touch "$FLAG_XAPPS_READY"
if ssh $SSH_OPTS ${PC2_USER}@${PC2_IP} "touch /tmp/local_xapps_ready" 2>/dev/null; then
    ok "已通知 PC2，流量場景即將啟動"
else
    warn "SSH 通知失敗，請在 PC2 手動執行：touch /tmp/local_xapps_ready"
fi

# ── Step 8: 開啟監控儀表板 ──────────────────────────────────
log "Step 8: 開啟 DRL 監控儀表板..."
echo ""
bash "$COMPOSE_DIR/iab/monitor_drl.sh"
