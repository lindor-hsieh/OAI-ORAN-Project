#!/bin/bash
# =============================================================================
# run_experiment_pc1.sh — 單一 PRB 策略實驗腳本（在 PC1 執行）
#
# 用法：
#   bash run_experiment_pc1.sh <policy>
#   policy: fixed50 | fixed30 | fixed10 | adaptive | pf
#
# 範例（依序手動跑五次）：
#   bash run_experiment_pc1.sh fixed50
#   bash run_experiment_pc1.sh fixed30
#   bash run_experiment_pc1.sh fixed10
#   bash run_experiment_pc1.sh adaptive
#   bash run_experiment_pc1.sh pf
#
# 每次只跑一種策略，跑完就結束，不會有 xApp 殘留問題。
# =============================================================================

set -euo pipefail

# ─── 參數 ───────────────────────────────────────────────────────────────────
POLICY="${1:-}"
if [[ -z "$POLICY" ]]; then
    echo "Usage: bash $0 <policy>"
    echo "  policy: fixed50 | fixed30 | fixed10 | adaptive | pf"
    exit 1
fi
if [[ ! "$POLICY" =~ ^(fixed50|fixed30|fixed10|adaptive|pf)$ ]]; then
    echo "Error: unknown policy '$POLICY'"
    echo "  Valid: fixed50 fixed30 fixed10 adaptive pf"
    exit 1
fi

# ─── 路徑 ───────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
OAI_DIR="$(cd "${SCRIPT_DIR}/../../../.." && pwd)"

XAPP_BIN="${OAI_DIR}/openair2/E2AP/flexric/build/examples/xApp/c/ctrl/xapp_prb_alloc"
FLEXRIC_CONF="${SCRIPT_DIR}/../conf/flexric.conf"   # 192.168.88.141，非 127.0.0.1
FLEXRIC_PLUGINS="/usr/local/lib/flexric/"
SETUP_IPERF="${COMPOSE_DIR}/scenarios/setup_iperf_servers.sh"
TRAFFIC_SCRIPT="${SCRIPT_DIR}/run_traffic.py"
PC2_ENTRY="${SCRIPT_DIR}/run_experiment_pc2.sh"

# ─── PC2 設定 ───────────────────────────────────────────────────────────────
PC2_USER="lindor"
PC2_IP="192.168.88.2"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=5 -o BatchMode=yes"
PC2="ssh ${SSH_OPTS} ${PC2_USER}@${PC2_IP}"
PC2_ENTRY_REMOTE="/tmp/run_experiment_pc2.sh"
PC2_TRAFFIC_REMOTE="/tmp/run_traffic.py"
PC2_RESULTS="/tmp/iab_exp/${POLICY}"

# ─── 量測設定 ───────────────────────────────────────────────────────────────
WARMUP_SECS=15
CPU_INTERVAL=5
RELAY_DU_CONTAINERS=("rfsim5g-iab-du" "rfsim5g-iab-du-2")
RESULTS_DIR="${SCRIPT_DIR}/results/${POLICY}"

# ─── 顏色 ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()  { echo -e "${CYAN}[PC1 $(date '+%H:%M:%S')]${NC} $*"; }
ok()   { echo -e "${GREEN}  ✓${NC} $*"; }
warn() { echo -e "${YELLOW}  ⚠${NC} $*"; }
die()  { echo -e "${RED}  ✗ $*${NC}"; exit 1; }

# ─── 前提檢查 ────────────────────────────────────────────────────────────────
log "Policy: ${POLICY}"
log "Checking prerequisites..."

[ -f "$XAPP_BIN" ] || die "xApp binary not found: $XAPP_BIN"
[ -f "$FLEXRIC_CONF" ] || die "flexric.conf not found"
[ -f "$TRAFFIC_SCRIPT" ] || die "run_traffic.py not found"
$PC2 "echo ok" >/dev/null 2>&1 || die "Cannot SSH to PC2 (${PC2_IP})"

E2=$(docker logs flexric 2>&1 | grep -c "E2 SETUP-REQUEST" 2>/dev/null || echo 0)
[ "$E2" -ge 5 ] || die "Only ${E2} E2 connections (need ≥ 5)"
ok "Prerequisites OK (E2=${E2})"

# ─── 清理殘留 xApp（用 kill -9 確保清掉）────────────────────────────────────
log "Killing any leftover xapp_prb_alloc processes..."
pkill -9 -f "xapp_prb_alloc" 2>/dev/null || true
sleep 2
ok "Cleanup done"

# ─── 建立結果目錄 ────────────────────────────────────────────────────────────
mkdir -p "$RESULTS_DIR"
$PC2 "rm -rf ${PC2_RESULTS} && mkdir -p ${PC2_RESULTS}"

# ─── 部署 PC2 腳本 ───────────────────────────────────────────────────────────
log "Deploying scripts to PC2..."
scp ${SSH_OPTS} "${PC2_ENTRY}"      "${PC2_USER}@${PC2_IP}:${PC2_ENTRY_REMOTE}"
scp ${SSH_OPTS} "${TRAFFIC_SCRIPT}" "${PC2_USER}@${PC2_IP}:${PC2_TRAFFIC_REMOTE}"
$PC2 "chmod +x ${PC2_ENTRY_REMOTE}"
ok "Scripts deployed"

# ─── 啟動 iperf3 servers ────────────────────────────────────────────────────
log "Setting up iperf3 servers on ext-dn..."
bash "$SETUP_IPERF"
ok "iperf3 servers ready"

# ─── 啟動 xApp（pf 不啟動）──────────────────────────────────────────────────
XAPP_PID=0
if [ "$POLICY" != "pf" ]; then
    log "Starting xapp_prb_alloc (PRB_POLICY=${POLICY})..."
    PRB_POLICY="$POLICY" "$XAPP_BIN" \
        -c "$FLEXRIC_CONF" -p "$FLEXRIC_PLUGINS" \
        > "${RESULTS_DIR}/xapp.log" 2>&1 &
    XAPP_PID=$!
    log "xApp PID=${XAPP_PID}, warming up ${WARMUP_SECS}s..."
    sleep "$WARMUP_SECS"
    kill -0 "$XAPP_PID" 2>/dev/null || die "xApp crashed — check ${RESULTS_DIR}/xapp.log"
    ok "xApp running"
else
    log "PF baseline: no xApp"
    sleep 3
fi

# ─── 背景監控 relay DU CPU ──────────────────────────────────────────────────
{
    while true; do
        echo "=== $(date '+%H:%M:%S') ==="
        docker stats --no-stream --format \
            "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}" \
            "${RELAY_DU_CONTAINERS[@]}" 2>/dev/null || true
        sleep "${CPU_INTERVAL}"
    done
} >> "${RESULTS_DIR}/cpu_relay.log" 2>&1 &
CPU_PID=$!

# ─── 呼叫 PC2 量測（阻塞等待完成）──────────────────────────────────────────
log "Calling PC2 to run traffic measurements (~205s)..."
$PC2 "bash ${PC2_ENTRY_REMOTE} ${POLICY} ${PC2_RESULTS} ${PC2_TRAFFIC_REMOTE}" || \
    warn "PC2 returned non-zero"
ok "PC2 measurements done"

# ─── 停止 CPU 監控 ───────────────────────────────────────────────────────────
kill "$CPU_PID" 2>/dev/null || true
wait "$CPU_PID" 2>/dev/null || true

# ─── 抓回 PC2 結果 ───────────────────────────────────────────────────────────
log "Fetching results from PC2..."
rsync -az -e "ssh ${SSH_OPTS}" \
    "${PC2_USER}@${PC2_IP}:${PC2_RESULTS}/" \
    "${RESULTS_DIR}/" 2>/dev/null || warn "rsync: some files may be missing"
ok "Results saved to ${RESULTS_DIR}/"

# ─── 停止 xApp（kill -9 確保清掉）───────────────────────────────────────────
if [ "$XAPP_PID" -ne 0 ]; then
    kill -9 "$XAPP_PID" 2>/dev/null || true
    ok "xApp stopped"
fi

log "========================================"
log "Policy '${POLICY}' DONE."
log "Results: ${RESULTS_DIR}/"
log "========================================"
