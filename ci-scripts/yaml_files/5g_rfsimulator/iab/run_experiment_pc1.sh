#!/bin/bash
# =============================================================================
# run_experiment_pc1.sh — 論文 PRB 實驗主控腳本（在 PC1 執行）
#
# 職責：
#   1. 停止 DRL xApp 容器（inference-node*, xapp-node*）
#   2. 啟動 ext-dn iperf3 servers（ports 5201~5206）
#   3. 依序跑 5 種策略：
#        a. 啟動/停止 xapp_prb_alloc（PF 不啟動）
#        b. SSH 到 PC2 執行 run_experiment_pc2.sh（阻塞等待完成）
#        c. 擷取 PC1 relay DU CPU 使用率
#        d. SCP PC2 結果回 PC1
#
# 前提：
#   - IAB 系統已完全啟動（start_iab_server.sh + start_iab_client.sh 已跑完）
#   - xapp_prb_alloc binary 已編譯（ninja xapp_prb_alloc）
#   - PC2 免密 SSH：ssh-copy-id lindor@192.168.88.2
#
# 使用方式：
#   bash run_experiment_pc1.sh
# =============================================================================

set -euo pipefail

# ─── 路徑設定 ───────────────────────────────────────────────────────────────
COMPOSE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OAI_DIR="$(cd "${SCRIPT_DIR}/../../../.." && pwd)"

XAPP_BIN="${OAI_DIR}/openair2/E2AP/flexric/build/examples/xApp/c/ctrl/xapp_prb_alloc"
FLEXRIC_CONF="/usr/local/etc/flexric/flexric.conf"
FLEXRIC_PLUGINS="/usr/local/lib/flexric/"
SETUP_IPERF="${COMPOSE_DIR}/scenarios/setup_iperf_servers.sh"
PC2_SCRIPT="${SCRIPT_DIR}/run_experiment_pc2.sh"

# ─── PC2 設定 ───────────────────────────────────────────────────────────────
PC2_USER="lindor"
PC2_IP="192.168.88.2"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=5 -o BatchMode=yes"
PC2="ssh ${SSH_OPTS} ${PC2_USER}@${PC2_IP}"
PC2_SCRIPT_REMOTE="/tmp/run_experiment_pc2.sh"
PC2_RESULTS_DIR="/tmp/iab_exp"

# ─── 量測參數 ───────────────────────────────────────────────────────────────
WARMUP_SECS=15
CPU_INTERVAL=5       # CPU 輪詢間隔 (秒)

# PC1 上要監控的 relay DU 容器
RELAY_DU_CONTAINERS=("rfsim5g-iab-du" "rfsim5g-iab-du-2")

# 要跑的策略（PF = 不啟動 xApp）
POLICIES=(fixed50 fixed30 fixed10 adaptive pf)

RESULTS_DIR="${SCRIPT_DIR}/results"

# ─── 顏色輸出 ───────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()  { echo -e "${CYAN}[PC1 $(date '+%H:%M:%S')]${NC} $*"; }
ok()   { echo -e "${GREEN}  ✓${NC} $*"; }
warn() { echo -e "${YELLOW}  ⚠${NC} $*"; }
die()  { echo -e "${RED}  ✗ $*${NC}"; exit 1; }

# ─── 前提檢查 ────────────────────────────────────────────────────────────────
check_prereqs() {
    log "Checking prerequisites..."
    [ -f "$XAPP_BIN" ] || die "xApp binary not found: $XAPP_BIN\n       cd ${OAI_DIR}/openair2/E2AP/flexric/build && ninja xapp_prb_alloc"
    [ -f "$FLEXRIC_CONF" ] || die "flexric.conf not found: $FLEXRIC_CONF"
    [ -f "$PC2_SCRIPT" ] || die "PC2 script not found: $PC2_SCRIPT"

    $PC2 "echo ok" >/dev/null 2>&1 || \
        die "Cannot SSH to PC2 (${PC2_IP}).\n       Run: ssh-copy-id ${PC2_USER}@${PC2_IP}"

    local e2
    e2=$(docker logs flexric 2>&1 | grep -c "E2 SETUP-REQUEST" 2>/dev/null || echo 0)
    [ "$e2" -ge 5 ] || die "Only $e2 E2 connections (need ≥ 5). Is IAB running?"
    ok "Prerequisites OK  (E2 nodes: $e2)"
}

# ─── 停止 DRL xApp 容器 ──────────────────────────────────────────────────────
stop_drl_xapps() {
    log "Stopping DRL xApp and inference containers..."
    for n in 1 2 3 4 5; do
        docker stop "xapp-node${n}"       2>/dev/null || true
        docker stop "inference-node${n}"  2>/dev/null || true
    done
    # 也停止任何在跑的 xapp_prb_alloc process
    pkill -f "xapp_prb_alloc" 2>/dev/null || true
    sleep 2
    ok "DRL xApps stopped"
}

# ─── 停止 xapp_prb_alloc ────────────────────────────────────────────────────
stop_xapp() {
    pkill -f "xapp_prb_alloc" 2>/dev/null || true
    sleep 1
}

# ─── 把 PC2 腳本 scp 過去 ────────────────────────────────────────────────────
deploy_pc2_script() {
    log "Deploying run_experiment_pc2.sh to PC2..."
    scp ${SSH_OPTS} "${PC2_SCRIPT}" "${PC2_USER}@${PC2_IP}:${PC2_SCRIPT_REMOTE}"
    $PC2 "chmod +x ${PC2_SCRIPT_REMOTE}"
    ok "PC2 script deployed"
}

# ─── CPU 背景監控（PC1 relay DUs） ─────────────────────────────────────────
start_cpu_monitor() {
    local outfile="$1"
    {
        while true; do
            echo "=== $(date '+%H:%M:%S') ==="
            docker stats --no-stream --format \
                "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}" \
                "${RELAY_DU_CONTAINERS[@]}" 2>/dev/null || true
            sleep "${CPU_INTERVAL}"
        done
    } >> "$outfile" 2>&1 &
    echo $!
}

stop_cpu_monitor() {
    local pid="$1"
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
}

# ─── 跑單一策略 ─────────────────────────────────────────────────────────────
run_policy() {
    local policy="$1"
    local outdir="${RESULTS_DIR}/${policy}"
    mkdir -p "$outdir"

    log "========================================================"
    log "Policy: ${policy}"
    log "========================================================"

    # 1. 啟動 xApp（PF 跳過）
    local xapp_pid=0
    if [ "$policy" != "pf" ]; then
        log "Starting xapp_prb_alloc (PRB_POLICY=${policy})..."
        PRB_POLICY="$policy" "$XAPP_BIN" \
            -c "$FLEXRIC_CONF" -p "$FLEXRIC_PLUGINS" \
            > "${outdir}/xapp.log" 2>&1 &
        xapp_pid=$!
        log "xApp PID=$xapp_pid, warming up ${WARMUP_SECS}s..."
        sleep "$WARMUP_SECS"
        kill -0 "$xapp_pid" 2>/dev/null || \
            die "xApp crashed immediately — check ${outdir}/xapp.log"
        ok "xApp running"
    else
        log "PF baseline: xApp not started"
        sleep 5
    fi

    # 2. 開始監控 PC1 relay DU CPU
    local cpu_pid
    cpu_pid=$(start_cpu_monitor "${outdir}/cpu_relay.log")

    # 3. SSH 到 PC2 執行量測（阻塞，直到 PC2 完成 iperf3+ping）
    log "Calling PC2 to run traffic measurements (iperf3 60s + ping)..."
    $PC2 "bash ${PC2_SCRIPT_REMOTE} ${policy} ${PC2_RESULTS_DIR}/${policy}" || \
        warn "PC2 script returned non-zero for policy ${policy}"
    ok "PC2 measurements done"

    # 4. 停止 CPU 監控
    stop_cpu_monitor "$cpu_pid"

    # 5. 抓回 PC2 結果
    log "Fetching results from PC2..."
    scp ${SSH_OPTS} -r \
        "${PC2_USER}@${PC2_IP}:${PC2_RESULTS_DIR}/${policy}/" \
        "${outdir}/" 2>/dev/null || warn "SCP: some files may be missing"
    ok "Results saved to ${outdir}/"

    # 6. 停止 xApp
    if [ "$xapp_pid" -ne 0 ]; then
        kill "$xapp_pid" 2>/dev/null || true
        wait "$xapp_pid" 2>/dev/null || true
        ok "xApp stopped"
    fi

    echo ""
}

# ─── Main ────────────────────────────────────────────────────────────────────
main() {
    mkdir -p "$RESULTS_DIR"
    check_prereqs
    stop_drl_xapps
    deploy_pc2_script

    # 啟動 ext-dn iperf3 servers（ports 5201~5206）
    log "Setting up iperf3 servers on ext-dn..."
    bash "$SETUP_IPERF"
    ok "iperf3 servers ready"

    # 清理 PC2 舊暫存
    $PC2 "rm -rf ${PC2_RESULTS_DIR} && mkdir -p ${PC2_RESULTS_DIR}"

    local total=${#POLICIES[@]}
    local idx=0
    for policy in "${POLICIES[@]}"; do
        idx=$((idx+1))
        log "=== Policy ${idx}/${total}: ${policy} ==="
        run_policy "$policy"
        [ "$idx" -lt "$total" ] && { log "Cool-down 10s..."; sleep 10; }
    done

    log "========================================================"
    log "All done. Results: ${RESULTS_DIR}/"
    log "Next: python3 ${SCRIPT_DIR}/analyze_results.py"
    log "========================================================"
}

main "$@"
