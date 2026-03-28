#!/bin/bash
# =============================================================================
# start_xapp_node2.sh — Node 2 Local xApp 容器啟動腳本
#
# 職責：
#   1. 安裝 ZMQ 與 cJSON runtime 函式庫 (若容器 image 內尚未包含)
#   2. 等待 FlexRIC Server 就緒 (透過 NEAR_RT_RIC_IP 環境變數)
#   3. 啟動 xapp_node2 執行檔
#
# 環境變數 (由 docker-compose 注入)：
#   NEAR_RT_RIC_IP    — FlexRIC Server IP，用於就緒性探測
#   XAPP_DURATION     — xApp 最大運行秒數，-1 表示永久運行
# =============================================================================

set -e

XAPP_BIN="/node2_brain"
FLEXRIC_CONF="/usr/local/etc/flexric/flexric.conf"
PLUGIN_PATH="/usr/local/lib/flexric/"
RIC_IP="${NEAR_RT_RIC_IP:-192.168.88.141}"

echo "[start_xapp_node2] =========================================="
echo "[start_xapp_node2]  Node 2 Local xApp 啟動腳本"
echo "[start_xapp_node2]  FlexRIC Server IP: ${RIC_IP}"
echo "[start_xapp_node2] =========================================="

# -----------------------------------------------------------------------------
# 步驟 1：安裝 ZMQ / cJSON runtime 函式庫
# -----------------------------------------------------------------------------
echo "[start_xapp_node2] 安裝 runtime 相依套件..."
apt-get update -qq 2>/dev/null || true
for PKG in libzmq5 libcjson1 libsodium23 libgssapi-krb5-2 netcat-openbsd \
           libpgm-5.3-0 libpgm-5.3-0t64; do
    apt-get install -y -q --no-install-recommends "$PKG" 2>/dev/null || true
done
echo "[start_xapp_node2] 函式庫安裝完成"

# -----------------------------------------------------------------------------
# 步驟 2：等待 FlexRIC Server 主機可達
# -----------------------------------------------------------------------------
echo "[start_xapp_node2] 等待 FlexRIC Server (${RIC_IP}) 就緒..."
MAX_WAIT=120
ELAPSED=0

while ! ping -c 1 -W 2 "${RIC_IP}" >/dev/null 2>&1; do
    if [ ${ELAPSED} -ge ${MAX_WAIT} ]; then
        echo "[start_xapp_node2] WARNING: FlexRIC Server ${MAX_WAIT}s 內未就緒，仍嘗試啟動 xApp"
        break
    fi
    sleep 2
    ELAPSED=$((ELAPSED + 2))
    echo "[start_xapp_node2] 等待中... (${ELAPSED}/${MAX_WAIT}s)"
done
echo "[start_xapp_node2] FlexRIC Server 已就緒，啟動 xApp"

# -----------------------------------------------------------------------------
# 步驟 3：啟動 xApp
# -----------------------------------------------------------------------------
echo "[start_xapp_node2] 執行: ${XAPP_BIN} -c ${FLEXRIC_CONF} -p ${PLUGIN_PATH}"
exec "${XAPP_BIN}" -c "${FLEXRIC_CONF}" -p "${PLUGIN_PATH}"
