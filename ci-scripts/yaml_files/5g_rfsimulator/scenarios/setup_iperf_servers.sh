#!/bin/bash
# setup_iperf_servers.sh — 在 PC 1 的 ext-dn 容器中啟動 iperf3 server
#
# 在 traffic_scenario.py 啟動前，於 PC 1 執行一次：
#   bash setup_iperf_servers.sh
#
# ext-dn 容器：rfsim5g-oai-ext-dn
# 監聽 port 5201~5206，對應 UE1~UE6 的 UDP 上行流量

set -e

CONTAINER="rfsim5g-oai-ext-dn"
PORTS=(5201 5202 5203 5204 5205 5206)

echo "[setup] 確認 ${CONTAINER} 容器狀態..."
if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    echo "[ERROR] 容器 ${CONTAINER} 未運行，請先啟動 docker-compose-iab-server.yaml"
    exit 1
fi

echo "[setup] 安裝 iperf3（若尚未安裝）..."
docker exec "${CONTAINER}" bash -c "which iperf3 >/dev/null 2>&1 || apt-get install -yq iperf3"

echo "[setup] 停止舊的 iperf3 server 進程..."
docker exec "${CONTAINER}" bash -c "pkill -f 'iperf3 -s' 2>/dev/null || true"
sleep 1

echo "[setup] 啟動 iperf3 UDP server，port 5201~5206..."
for PORT in "${PORTS[@]}"; do
    docker exec -d "${CONTAINER}" \
        iperf3 -s -p "${PORT}" -i 0 --forceflush
    echo "  iperf3 server 啟動：port ${PORT}"
done

echo ""
echo "[setup] 驗證監聽狀態："
docker exec "${CONTAINER}" bash -c "ss -ulnp | grep iperf3 || netstat -ulnp | grep iperf3"

echo ""
echo "[setup] 完成！ext-dn iperf3 server 已就緒"
echo "        請在 PC 2 執行：python3 traffic_scenario.py --scenario D"
