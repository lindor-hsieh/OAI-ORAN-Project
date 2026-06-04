#!/bin/bash
# =============================================================================
# run_experiment_pc2.sh — 論文 PRB 實驗量測腳本（在 PC2 執行）
#
# 由 PC1 的 run_experiment_pc1.sh 透過 SSH 呼叫，不需手動執行。
#
# 職責：
#   1. 同時從 6 個 UE 容器發起 iperf3 UDP（各連不同 port）
#   2. 同時從 6 個 UE 容器發起 ping（-c 300 -i 0.2）
#   3. 監控 PC2 access DU 容器的 CPU 使用率
#   4. 結果存至 <RESULTS_DIR>/
#
# 用法（PC1 呼叫）：
#   bash run_experiment_pc2.sh <policy> <results_dir>
#   例: bash run_experiment_pc2.sh fixed50 /tmp/iab_exp/fixed50
#
# 容器（皆在 PC2）：
#   UE:        rfsim5g-end-ue-1 ~ rfsim5g-end-ue-6
#   Access DU: rfsim5g-iab-du-3, rfsim5g-iab-du-4, rfsim5g-iab-du-5
# =============================================================================

set -euo pipefail

# ─── 參數 ───────────────────────────────────────────────────────────────────
POLICY="${1:-unknown}"
RESULTS_DIR="${2:-/tmp/iab_exp/${POLICY}}"

# ─── 量測參數 ───────────────────────────────────────────────────────────────
TRAFFIC_SERVER_IP="192.168.72.135"   # rfsim5g-oai-ext-dn（在 PC1）
IPERF_BASE_PORT=5201                 # UE i → port 5201+(i-1)
IPERF_BITRATE="30M"                  # UDP offered load per UE
IPERF_DURATION=60                    # seconds
PING_COUNT=300
PING_INTERVAL=0.2
CPU_INTERVAL=5                       # CPU 輪詢間隔 (秒)

# UE 容器名稱
UE_CONTAINERS=(
    rfsim5g-end-ue-1
    rfsim5g-end-ue-2
    rfsim5g-end-ue-3
    rfsim5g-end-ue-4
    rfsim5g-end-ue-5
    rfsim5g-end-ue-6
)

# PC2 上的 access DU 容器
ACCESS_DU_CONTAINERS=(
    rfsim5g-iab-du-3
    rfsim5g-iab-du-4
    rfsim5g-iab-du-5
)

# ─── 顏色輸出 ───────────────────────────────────────────────────────────────
CYAN='\033[0;36m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()  { echo -e "${CYAN}[PC2 $(date '+%H:%M:%S')]${NC} $*"; }
ok()   { echo -e "${GREEN}  ✓${NC} $*"; }
warn() { echo -e "${YELLOW}  ⚠${NC} $*"; }

# ─── 準備 ───────────────────────────────────────────────────────────────────
mkdir -p "$RESULTS_DIR"
log "Policy=${POLICY}  Results=${RESULTS_DIR}"

# ─── 確認 UE 有 oaitun_ue1 並能 ping 到 ext-dn ──────────────────────────────
check_ue_connectivity() {
    log "Checking UE connectivity to ${TRAFFIC_SERVER_IP}..."
    local ok_count=0
    for ue in "${UE_CONTAINERS[@]}"; do
        local tun_ip
        tun_ip=$(docker exec "$ue" ip -4 addr show oaitun_ue1 2>/dev/null \
                 | grep -oP '(?<=inet\s)\d+(\.\d+){3}' || true)
        if [ -n "$tun_ip" ]; then
            # 確保路由存在
            docker exec -u 0 "$ue" \
                ip route add 192.168.72.128/26 dev oaitun_ue1 2>/dev/null || true
            ok_count=$((ok_count+1))
        else
            warn "$ue: oaitun_ue1 not found (UE not attached?)"
        fi
    done
    log "  $ok_count/${#UE_CONTAINERS[@]} UEs attached"
}

# ─── CPU 背景監控（access DU） ───────────────────────────────────────────────
start_cpu_monitor() {
    {
        while true; do
            echo "=== $(date '+%H:%M:%S') ==="
            docker stats --no-stream --format \
                "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}" \
                "${ACCESS_DU_CONTAINERS[@]}" 2>/dev/null || true
            sleep "${CPU_INTERVAL}"
        done
    } >> "${RESULTS_DIR}/cpu_access.log" 2>&1 &
    echo $!
}

# ─── iperf3 UDP（6 UE 並發） ─────────────────────────────────────────────────
run_iperf3() {
    log "Running iperf3 UDP (${IPERF_DURATION}s, ${IPERF_BITRATE}/UE, parallel)..."
    local pids=()
    for i in "${!UE_CONTAINERS[@]}"; do
        local ue="${UE_CONTAINERS[$i]}"
        local port=$((IPERF_BASE_PORT + i))
        local out="${RESULTS_DIR}/iperf_ue$((i+1)).json"
        docker exec "$ue" \
            iperf3 -c "$TRAFFIC_SERVER_IP" -p "$port" \
                   -u -b "$IPERF_BITRATE" -t "$IPERF_DURATION" \
                   -J --get-server-output \
            > "$out" 2>&1 &
        pids+=($!)
    done
    for pid in "${pids[@]}"; do
        wait "$pid" || warn "iperf3 pid=$pid returned non-zero"
    done
    ok "iperf3 done"
}

# ─── ping RTT（6 UE 並發） ──────────────────────────────────────────────────
run_ping() {
    log "Running ping (${PING_COUNT} probes × 6 UEs, parallel)..."
    local pids=()
    for i in "${!UE_CONTAINERS[@]}"; do
        local ue="${UE_CONTAINERS[$i]}"
        local out="${RESULTS_DIR}/ping_ue$((i+1)).txt"
        docker exec "$ue" \
            ping -I oaitun_ue1 \
                 -c "$PING_COUNT" -i "$PING_INTERVAL" -W 2 \
                 "$TRAFFIC_SERVER_IP" \
            > "$out" 2>&1 &
        pids+=($!)
    done
    for pid in "${pids[@]}"; do
        wait "$pid" || warn "ping pid=$pid returned non-zero"
    done
    ok "ping done"
}

# ─── Main ────────────────────────────────────────────────────────────────────
check_ue_connectivity

# 啟動 CPU 監控
CPU_PID=$(start_cpu_monitor)

# iperf3 和 ping 同時跑（iperf3 先，ping 可以跑完整 300 個 probe）
# 先並發送出所有 iperf3，等它們全部完成，接著並發 ping
run_iperf3
run_ping

# 停止 CPU 監控
kill "$CPU_PID" 2>/dev/null || true
wait "$CPU_PID" 2>/dev/null || true

log "Policy=${POLICY} done. Results in ${RESULTS_DIR}/"
ls -lh "${RESULTS_DIR}/" 2>/dev/null || true
