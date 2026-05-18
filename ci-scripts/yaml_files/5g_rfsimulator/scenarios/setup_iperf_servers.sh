#!/bin/bash
# setup_iperf_servers.sh — 在 PC 1 的 ext-dn 容器中啟動 iperf3 server
#
# 在 traffic_scenario.py 啟動前，於 PC 1 執行一次：
#   bash setup_iperf_servers.sh
#
# ext-dn 容器：rfsim5g-oai-ext-dn
# 監聽 port 5201~5206，對應 UE1~UE6 的下行流量

CONTAINER="rfsim5g-oai-ext-dn"
PORTS=(5201 5202 5203 5204 5205 5206)

echo "[setup] 確認 ${CONTAINER} 容器狀態..."
if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    echo "[ERROR] 容器 ${CONTAINER} 未運行，請先啟動 docker-compose-iab-server.yaml"
    exit 1
fi

echo "[setup] 安裝 iperf3（若尚未安裝）..."
docker exec "${CONTAINER}" sh -c "which iperf3 >/dev/null 2>&1 || apt-get install -yq iperf3" || true

echo "[setup] 停止舊的 iperf3 server 進程（含 while-loop）..."
# 必須同時殺 iperf3 和外層 while-loop sh，否則重啟時舊 loop 殘存，造成每個 port 兩個 server 並發。
docker exec "${CONTAINER}" sh -c "pkill -9 -f iperf3 2>/dev/null; pkill -9 -f 'while true' 2>/dev/null; true"
sleep 1

echo "[setup] 啟動 iperf3 server，port 5201~5206（含自動重啟 loop）..."
# 每個 server 包在 while loop + timeout 400s 裡：
#   - client session 最長 330s（IPERF_DURATION 300 + timeout grace 30）
#   - server timeout 400s > client timeout，確保 client 先死並送 RST
#   - server 收到 RST 後正常退出，while loop 立即重啟新 server
#   - 若 client 沒送 RST（極少數情況），server 400s 後強制退出
for PORT in "${PORTS[@]}"; do
    docker exec -d "${CONTAINER}" sh -c \
        "while true; do timeout 400 iperf3 -s -p ${PORT} -i 0 --forceflush 2>/dev/null; sleep 1; done"
    echo "  iperf3 server 啟動：port ${PORT}"
done

echo ""
echo "[setup] 驗證監聽狀態："
docker exec "${CONTAINER}" sh -c "ss -tlnp 2>/dev/null | grep iperf3 || netstat -tlnp 2>/dev/null | grep iperf3 || echo '(ss/netstat 不可用，跳過驗證)'"

echo ""
echo "[setup] 完成！ext-dn iperf3 server 已就緒"
echo "        請在 PC 2 執行：python3 traffic_scenario.py --scenario D"
