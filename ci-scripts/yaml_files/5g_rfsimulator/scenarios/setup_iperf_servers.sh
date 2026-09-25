#!/bin/bash
# setup_iperf_servers.sh — 在 PC 1 的 ext-dn 容器中啟動 iperf3 server
#
# 在 traffic_scenario.py 啟動前，於 PC 1 執行一次：
#   bash setup_iperf_servers.sh
#
# ext-dn 容器：rfsim5g-oai-ext-dn
# 監聽 port 5201~5217，對應 UE1~UE17 的下行流量（一個 iperf3 server 同時支援
# TCP 與 UDP client，不需要為協定混合場景另外開 UDP-only server）

CONTAINER="rfsim5g-oai-ext-dn"
PORTS=($(seq 5201 5217))

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

echo "[setup] 啟動 iperf3 server，port 5201~5217（含自動重啟 loop）..."
# 每個 server 包在 while loop 裡，用 -1（one-off）：服務完一個 client 連線就退出、1 秒後由 loop 重啟。
# 不可再包 `timeout N`：17 個 server 幾乎同時啟動，每 N+1 秒會「同步」被砍一次，不管有沒有
# session 在跑——所有 UE 的 iperf3 client 會同一秒集體 rc=1（2026-09-26 查出，原 N=400）。
# client 被停止（kill docker exec）時 TCP 連線關閉，server 端測試結束並退出，loop 立即重啟。
for PORT in "${PORTS[@]}"; do
    docker exec -d "${CONTAINER}" sh -c \
        "while true; do iperf3 -s -1 -p ${PORT} -i 0 --forceflush 2>/dev/null; sleep 1; done"
    echo "  iperf3 server 啟動：port ${PORT}"
done

echo ""
echo "[setup] 驗證監聽狀態："
docker exec "${CONTAINER}" sh -c "ss -tlnp 2>/dev/null | grep iperf3 || netstat -tlnp 2>/dev/null | grep iperf3 || echo '(ss/netstat 不可用，跳過驗證)'"

echo ""
echo "[setup] 完成！ext-dn iperf3 server 已就緒"
echo "        請在 PC 2 執行：python3 traffic_scenario.py --scenario R --seed <N> --host pc2"
echo "        並在 PC 3 執行：python3 traffic_scenario.py --scenario R --seed <N> --host pc3（同一個 --seed）"
