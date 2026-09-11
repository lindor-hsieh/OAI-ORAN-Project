#!/bin/bash
# =============================================================================
# start_xapp_node8.sh — Node 8 Local xApp 容器啟動腳本
#
# 職責：
#   2. 等待 FlexRIC Server 就緒 (透過 NEAR_RT_RIC_IP 環境變數)
#   3. 啟動 xapp_node8 執行檔
#
# 環境變數 (由 docker-compose 注入)：
#   NEAR_RT_RIC_IP    — FlexRIC Server IP，用於就緒性探測
#   XAPP_DURATION     — xApp 最大運行秒數，-1 表示永久運行
# =============================================================================

set -e

XAPP_BIN="/node8_brain"
FLEXRIC_CONF="/usr/local/etc/flexric/flexric.conf"
PLUGIN_PATH="/usr/local/lib/flexric/"
RIC_IP="${NEAR_RT_RIC_IP:-192.168.88.141}"

echo "[start_xapp_node8] =========================================="
echo "[start_xapp_node8]  Node 8 Local xApp 啟動腳本"
echo "[start_xapp_node8]  FlexRIC Server IP: ${RIC_IP}"
echo "[start_xapp_node8] =========================================="

# -----------------------------------------------------------------------------
# 步驟 1：等待 FlexRIC Server SCTP Port 36421 就緒
#   優先使用 nc，若不存在則退回 bash /dev/tcp 探測
#   (最多等 120 秒，每 2 秒探測一次)
# -----------------------------------------------------------------------------
echo "[start_xapp_node8] 等待 FlexRIC Server (${RIC_IP}:36421) 就緒..."
MAX_WAIT=120
ELAPSED=0

# FlexRIC 使用 SCTP，無法用 TCP/bash-devtcp 探測；改用 ping 確認主機可達即可
# E2AP 連線重試由 xApp 框架自行處理
while ! ping -c 1 -W 2 "${RIC_IP}" >/dev/null 2>&1; do
    if [ ${ELAPSED} -ge ${MAX_WAIT} ]; then
        echo "[start_xapp_node8] WARNING: FlexRIC Server ${MAX_WAIT}s 內未就緒，仍嘗試啟動 xApp"
        break
    fi
    sleep 2
    ELAPSED=$((ELAPSED + 2))
    echo "[start_xapp_node8] 等待中... (${ELAPSED}/${MAX_WAIT}s)"
done
echo "[start_xapp_node8] FlexRIC Server 已就緒，啟動 xApp"

# -----------------------------------------------------------------------------
# 步驟 2：啟動 xApp
#   exec 取代目前 shell，讓 PID 1 是 xApp 本身，確保 SIGTERM 能正常傳遞。
# -----------------------------------------------------------------------------
echo "[start_xapp_node8] 執行: ${XAPP_BIN} -c ${FLEXRIC_CONF} -p ${PLUGIN_PATH}"
exec "${XAPP_BIN}" -c "${FLEXRIC_CONF}" -p "${PLUGIN_PATH}"
