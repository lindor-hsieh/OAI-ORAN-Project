#!/bin/bash

# ==========================================
# PC 2: IAB Client Side Launch Script (UEs)
# ==========================================

COMPOSE_FILE="docker-compose-iab-ue.yaml"
IFACE_NAME="eno1"  # [請確認] 您 PC 2 的實體網卡名稱
CLIENT_IP="192.168.88.2"
SERVER_IP="192.168.88.1" # PC 1 IP

# 自動判斷 docker compose
if command -v docker-compose &> /dev/null; then DOCKER_COMPOSE="docker-compose"; else DOCKER_COMPOSE="docker compose"; fi

# ==========================================
# 0. 網路設定
# ==========================================
echo "[0/3] Configuring Client Network..."
if ! ip addr show $IFACE_NAME | grep -q "$CLIENT_IP"; then
    echo "Adding experiment IP $CLIENT_IP..."
    sudo ip addr add $CLIENT_IP/24 dev $IFACE_NAME
    sudo ip link set $IFACE_NAME up
fi

# 測試連線
echo "Pinging Server ($SERVER_IP)..."
if ! ping -c 1 -W 1 $SERVER_IP &> /dev/null; then
    echo "Cannot reach PC 1 ($SERVER_IP)! Check cable or firewall."
    exit 1
fi
echo "✅ Server reachable."

# ==========================================
# 1. 啟動 UE
# ==========================================
echo "[1/3] Clean up & Start UEs..."
$DOCKER_COMPOSE -f $COMPOSE_FILE down
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d

echo "Waiting for UEs to attach (may take 30s)..."

wait_for_ue() {
    UE_NAME=$1
    echo -n "Checking $UE_NAME... "
    local IP=""
    local COUNT=0
    while [ -z "$IP" ]; do
        sleep 2
        IP=$(docker exec $UE_NAME ip -f inet addr show oaitun_ue1 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}')
        COUNT=$((COUNT+1))
        if [ $COUNT -ge 60 ]; then echo "Timeout"; return 1; fi
    done
    echo "✅ IP: $IP"
    # 設定路由 (去往 Ext-DN)
    docker exec -u 0 $UE_NAME ip route add 192.168.72.0/24 dev oaitun_ue1 2>/dev/null || true
}

# 檢查所有 6 台 UE
wait_for_ue "rfsim5g-end-ue-1"
wait_for_ue "rfsim5g-end-ue-2"
wait_for_ue "rfsim5g-end-ue-3"
wait_for_ue "rfsim5g-end-ue-4"
wait_for_ue "rfsim5g-end-ue-5"
wait_for_ue "rfsim5g-end-ue-6"

# ==========================================
# 2. 效能測試
# ==========================================
echo "[2/3] Performance Test"
TARGET_IP="192.168.72.135" # Ext-DN

run_test() {
    UE=$1
    DESC=$2
    echo "--------------------------------"
    echo "Testing $UE ($DESC)..."
    
    # Ping
    PING=$(docker exec $UE ping -I oaitun_ue1 -c 3 -W 1 $TARGET_IP 2>&1 | grep "avg" | awk -F '/' '{print $5}')
    if [ -z "$PING" ]; then
        echo "   Ping: FAIL"
    else
        echo "   Ping: ${PING} ms"
        # Throughput
        SPEED=$(docker exec $UE iperf3 -c $TARGET_IP -t 3 -R -f m 2>&1 | grep "receiver" | awk '{print $(NF-2), $(NF-1)}')
        echo "   Speed: 🚀 ${SPEED}"
    fi
}

run_test "rfsim5g-end-ue-1" "Left Leaf (Node 3)"
run_test "rfsim5g-end-ue-3" "Middle Node (Node 4)"
run_test "rfsim5g-end-ue-5" "Right Leaf (Node 5)"

echo "Done!"