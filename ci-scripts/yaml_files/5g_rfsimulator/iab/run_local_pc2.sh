#!/bin/bash
# run_local_pc2.sh — PC2 第四階段全自動流程
#
# 用法：
#   bash run_local_pc2.sh                  # 完整流程（自動判斷是否需要校正）
#   bash run_local_pc2.sh --force-calibrate # 強制重新校正（CQI mapping 有變時用）
#   bash run_local_pc2.sh --skip-calibrate  # 強制跳過校正
#
# 流程：
#   Step 1: start_iab_client.sh --no-benchmark（CU magic 自動 SSH 到 PC1）
#   Step 2: CQI 校正（首次或 --force-calibrate，已校正過自動跳過）
#   Step 3: 通知 PC1 校正完成
#   Step 4: PF Baseline 效能測試
#   Step 5: 等待 PC1 xApps 就緒
#   Step 6: 啟動動態流量場景（Scenario D）

COMPOSE_DIR=~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator
SCENARIO_DIR="$COMPOSE_DIR/scenarios"
PC1_USER="lindor"
PC1_IP="192.168.88.1"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=5 -o BatchMode=yes"
CALIBRATED_FLAG="$SCENARIO_DIR/.calibrated"
FLAG_CALIBRATION_DONE="/tmp/local_calibration_done"
FLAG_XAPPS_READY="/tmp/local_xapps_ready"

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

log()  { echo -e "${CYAN}[PC2 $(date '+%H:%M:%S')]${NC} $*"; }
ok()   { echo -e "${GREEN}  ✓${NC} $*"; }
warn() { echo -e "${YELLOW}  ⚠${NC} $*"; }

notify_pc1_calibration_done() {
    touch "$FLAG_CALIBRATION_DONE"
    if ssh $SSH_OPTS ${PC1_USER}@${PC1_IP} "touch /tmp/local_calibration_done" 2>/dev/null; then
        ok "已通知 PC1 校正完成"
    else
        warn "SSH 通知失敗，請在 PC1 手動執行：touch /tmp/local_calibration_done"
    fi
}

# ── 清除舊 flag ──────────────────────────────────────────────
rm -f "$FLAG_CALIBRATION_DONE" "$FLAG_XAPPS_READY"
ssh $SSH_OPTS ${PC1_USER}@${PC1_IP} "rm -f /tmp/local_calibration_done /tmp/local_xapps_ready" 2>/dev/null || true

# ── Step 1: 啟動 IAB Client ──────────────────────────────────
log "Step 1: 啟動 IAB Node 3/4/5 與 UE（CU magic 自動 SSH 到 PC1）..."
cd "$COMPOSE_DIR"
bash iab/start_iab_client.sh --no-benchmark
ok "IAB Client 啟動完成"

# ── Step 2: CQI 校正 ─────────────────────────────────────────
if [[ "$*" == *"--skip-calibrate"* ]]; then
    warn "跳過 CQI 校正（--skip-calibrate）"
    notify_pc1_calibration_done

elif [[ "$*" == *"--force-calibrate"* ]] || [ ! -f "$CALIBRATED_FLAG" ]; then
    if [ ! -f "$CALIBRATED_FLAG" ]; then
        log "Step 2: 首次執行，進行 CQI 校正（Node 3/4/5）..."
    else
        log "Step 2: 強制重新校正（Node 3/4/5）..."
    fi

    for node in 3 4 5; do
        log "  校正 Node ${node}..."
        python3 "$SCENARIO_DIR/traffic_scenario.py" --calibrate --node "$node"
        ok "Node ${node} 校正完成"
    done

    touch "$CALIBRATED_FLAG"
    ok "CQI 校正完成，已記錄 $CALIBRATED_FLAG（下次自動跳過）"
    notify_pc1_calibration_done

else
    ok "CQI 已校正過（$CALIBRATED_FLAG 存在），跳過"
    ok "如需重新校正，請使用 --force-calibrate"
    notify_pc1_calibration_done
fi

# ── Step 3: PF Baseline 效能測試 ─────────────────────────────
log "Step 3: PF Baseline 效能測試（3 輪取平均，請記錄結果）..."
bash "$COMPOSE_DIR/iab/iab_perf_test.sh" 3

# ── Step 4: 等待 PC1 xApps 就緒 ─────────────────────────────
log "Step 4: 等待 PC1 xApps 就緒..."
while ! test -f "$FLAG_XAPPS_READY" 2>/dev/null; do
    echo -ne "\r  等待中... $(date '+%H:%M:%S')"
    sleep 5
done
echo ""
ok "PC1 xApps 就緒"

# ── Step 5: 啟動動態流量場景 ─────────────────────────────────
log "Step 5: 啟動動態流量場景（Scenario D，每相位 60 秒）..."
echo -e "${YELLOW}  Ctrl+C 可停止，DRL 比較測試請在另一個終端執行 iab_perf_test.sh 3${NC}"
echo ""
cd "$SCENARIO_DIR"
python3 traffic_scenario.py --scenario D --phase-duration 60
