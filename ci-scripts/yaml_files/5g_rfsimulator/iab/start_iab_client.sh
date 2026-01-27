#!/bin/bash

# ==========================================
# PC 2: IAB Client Side Launch Script (PORT FORWARDING VERSION)
# Connects to PC 1 (Server) via Physical IP + Ports
# ==========================================

COMPOSE_FILE="docker-compose-iab-ue.yaml"
IFACE_NAME="eno1"        # [請確認] 您 PC 2 連接 PC 1 的實體網卡名稱
CLIENT_IP="192.168.88.2" # PC 2 自己的 IP
SERVER_IP="192.168.88.1" # PC 1 的 IP

# 自動判斷 docker compose 指令
if command -v docker-compose &> /dev/null; then DOCKER_COMPOSE="docker-compose"; else DOCKER_COMPOSE="docker compose"; fi

# ==========================================
# 0. 網路設定
# ==========================================
echo "[0/3] Configuring Client Network..."

# 1. 設定本機 IP
if ! ip addr show $IFACE_NAME | grep -q "$CLIENT_IP"; then
    echo "   -> Adding experiment IP $CLIENT_IP..."
    sudo ip addr add $CLIENT_IP/24 dev $IFACE_NAME
    sudo ip link set $IFACE_NAME up
else
    echo "   -> IP $CLIENT_IP already set."
fi

# 2. 測試連線 (只 Ping 實體 IP，不 Ping Docker IP)
echo "   -> Pinging Server Physical IP ($SERVER_IP)..."
if ! ping -c 1 -W 1 $SERVER_IP &> /dev/null; then
    echo "❌ Cannot reach PC 1 ($SERVER_IP)! Check cable or firewall."
    exit 1
fi
echo "✅ Physical Connection OK. Using Port Forwarding to reach Nodes."

# ==========================================
# 1. 啟動 UE
# ==========================================
echo "[1/3] Clean up & Start UEs..."
$DOCKER_COMPOSE -f $COMPOSE_FILE down
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d

echo "Waiting for UEs to attach (Timeout: 60s)..."

wait_for_ue() {
    UE_NAME=$1
    echo -n "Checking $UE_NAME... "
    local IP=""
    local COUNT=0
    
    while [ -z "$IP" ]; do
        sleep 2
        IP=$(docker exec $UE_NAME ip -f inet addr show oaitun_ue1 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}')
        COUNT=$((COUNT+1))
        
        if [ $COUNT -eq 15 ]; then
             echo -n "[Restarting] "
             docker restart $UE_NAME > /dev/null
        fi

        if [ $COUNT -ge 30 ]; then echo "❌ Timeout"; return 1; fi
        echo -n "."
    done
    echo " ✅ IP: $IP"
    
    # [關鍵] 設定 UE 內部的路由
    # 這裡的邏輯是：告訴 UE，所有去 192.168.72.x (Ext-DN) 的流量，都走 OAI 通道
    docker exec -u 0 $UE_NAME ip route replace 192.168.72.0/24 dev oaitun_ue1 2>/dev/null || true
}

# 檢查所有 UE
wait_for_ue "rfsim5g-end-ue-1"
wait_for_ue "rfsim5g-end-ue-2"
wait_for_ue "rfsim5g-end-ue-3"
wait_for_ue "rfsim5g-end-ue-4"
wait_for_ue "rfsim5g-end-ue-5" 
wait_for_ue "rfsim5g-end-ue-6"

# ==========================================
# 2. 效能測試
# ==========================================
# Ext-DN 的 IP (依據您之前的截圖)
TARGET_IP="192.168.72.135" 
echo "[2/3] Performance Test (Target: $TARGET_IP)"

run_test() {
    UE=$1
    DESC=$2
    echo "--------------------------------"
    echo "Testing $UE ($DESC)..."
    
    if ! docker ps | grep -q $UE; then
        echo "   Skipping (Container not running)"
        return
    fi

    PING=$(docker exec $UE ping -I oaitun_ue1 -c 3 -W 1 $TARGET_IP 2>&1 | grep "avg" | awk -F '/' '{print $5}')
    if [ -z "$PING" ]; then
        echo "   Ping: ❌ FAIL (Check routes)"
    else
        echo "   Ping: ${PING} ms"
        echo -n "   Speed (DL): "
        SPEED=$(docker exec $UE iperf3 -c $TARGET_IP -t 3 -R -f m 2>&1 | grep "receiver" | awk '{print $(NF-2), $(NF-1)}')
        echo "🚀 ${SPEED:-0 Mbps}"
    fi
}

run_test "rfsim5g-end-ue-1" "Group A (via Node 3)"
run_test "rfsim5g-end-ue-3" "Group B (via Node 4)"
run_test "rfsim5g-end-ue-5" "Group C (via Node 5)"

echo "Done! Full IAB Network Test Complete."