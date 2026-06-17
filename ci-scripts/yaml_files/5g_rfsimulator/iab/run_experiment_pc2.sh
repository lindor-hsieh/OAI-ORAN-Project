#!/bin/bash
# =============================================================================
# run_experiment_pc2.sh — PC2 入口腳本（由 PC1 透過 SSH 呼叫）
#
# 職責：呼叫 run_traffic.py 跑固定流量序列
# 流量序列定義在 run_traffic.py，所有 policy 使用完全相同的序列。
# =============================================================================

set -euo pipefail

POLICY="${1:-unknown}"
RESULTS_DIR="${2:-/tmp/iab_exp/${POLICY}}"
TRAFFIC_SCRIPT="${3:-/tmp/run_traffic.py}"

python3 "$TRAFFIC_SCRIPT" "$POLICY" "$RESULTS_DIR"
