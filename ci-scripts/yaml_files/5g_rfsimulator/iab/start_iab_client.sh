#!/bin/bash

# ==========================================
# PC 2: IAB Client Side Launch Script (FINAL)
# Connects to PC 1 (Server) via Ethernet
# ==========================================

COMPOSE_FILE="docker-compose-iab-ue.yaml"
IFACE_NAME="eno1"        # [請確認] 您 PC 2 連接 PC 1 的實體網卡名稱
CLIENT_IP="192.168.88.2" # PC 2 自己的 IP
SERVER_IP="192.168.88.1" # PC 1 的 IP

# Docker 子網段 (根據 Server 端的設定，通常是這兩個)
DOCKER_NET_RAN="192.168.71.0/24"  # IAB Nodes (RAN)
DOCKER_NET_CORE="192.168.72.0/24" # Ext-DN (Core)

# 自動判斷 docker compose 指令
if command -v docker-compose &> /dev/null; then DOCKER_COMPOSE="docker-compose"; else DOCKER_COMPOSE="docker compose"; fi

# ==========================================
# 0. 網路設定 (關鍵路由)
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

# 2. [關鍵] 設定靜態路由：讓 PC 2 找得到 PC 1 裡的 Docker 容器
echo "   -> Configuring Static Routes to Server Docker Networks..."
sudo ip route replace $DOCKER_NET_RAN via $SERVER_IP dev $IFACE_NAME 2>/dev/null
sudo ip route replace $DOCKER_NET_CORE via $SERVER_IP dev $IFACE_NAME 2>/dev/null

# 3. 測試連線 (Ping Server 實體 IP)
echo "   -> Pinging Server Physical IP ($SERVER_IP)..."
if ! ping -c 1 -W 1 $SERVER_IP &> /dev/null; then
    echo "❌ Cannot reach PC 1 ($SERVER_IP)! Check cable or firewall."
    exit 1
fi

# 4. 測試連線 (Ping Node Docker IP)
# 假設 Node 3 (MT-3) 的 Docker IP 是 .152 (常見預設值)，這步失敗不代表不能跑，但最好確認一下
TEST_NODE_IP="192.168.71.152" 
echo "   -> Testing reachability to Docker Container ($TEST_NODE_IP)..."
if ping -c 1 -W 1 $TEST_NODE_IP &> /dev/null; then
    echo "✅ Route check PASS: Can reach Server's Containers."
else
    echo "⚠️ Warning: Cannot reach Server's Containers. Check routing/firewall on PC 1."
    echo "   (Tip: Did you enable ip_forward on PC 1?)"
fi

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
        
        # 30秒沒拿到就嘗試重啟 UE (有時候 RFSim 需要重連)
        if [ $COUNT -eq 15 ]; then
             echo -n "[Restarting] "
             docker restart $UE_NAME > /dev/null
        fi

        if [ $COUNT -ge 30 ]; then echo "❌ Timeout"; return 1; fi
        echo -n "."
    done
    echo " ✅ IP: $IP"
    
    # 設定 UE 內部的路由 (去往 Ext-DN 必須走 5G 隧道)
    # 這行很重要，確保 iperf 流量真的走 5G 而不是走 Docker 網卡
    docker exec -u 0 $UE_NAME ip route add 192.168.72.0/24 dev oaitun_ue1 2>/dev/null || true
}

# 檢查所有 UE (依據您的 yaml 服務名稱調整)
# 這裡假設您有 6 台 UE，連到 Layer 2 的三個節點
wait_for_ue "rfsim5g-end-ue-1"
wait_for_ue "rfsim5g-end-ue-2"
wait_for_ue "rfsim5g-end-ue-3"
wait_for_ue "rfsim5g-end-ue-4"
wait_for_ue "rfsim5g-end-ue-5" 
# wait_for_ue "rfsim5g-end-ue-6" # 如果有的話

# ==========================================
# 2. 效能測試
# ==========================================
echo "[2/3] Performance Test"
TARGET_IP="192.168.72.135" # Ext-DN 的 IP (請確認 PC 1 上 Ext-DN 的 IP)

run_test() {
    UE=$1
    DESC=$2
    echo "--------------------------------"
    echo "Testing $UE ($DESC)..."
    
    # 檢查 UE 容器是否存在
    if ! docker ps | grep -q $UE; then
        echo "   Skipping (Container not running)"
        return
    fi

    # Ping 測試
    PING=$(docker exec $UE ping -I oaitun_ue1 -c 3 -W 1 $TARGET_IP 2>&1 | grep "avg" | awk -F '/' '{print $5}')
    if [ -z "$PING" ]; then
        echo "   Ping: ❌ FAIL (Check routes)"
    else
        echo "   Ping: ${PING} ms"
        # Throughput 測試 (3秒快速測試)
        # -R 代表 Reverse (Downlink: Server -> UE)，這才是下載速度
        echo -n "   Speed (DL): "
        SPEED=$(docker exec $UE iperf3 -c $TARGET_IP -t 3 -R -f m 2>&1 | grep "receiver" | awk '{print $(NF-2), $(NF-1)}')
        echo "🚀 ${SPEED:-0 Mbps}"
    fi
}

run_test "rfsim5g-end-ue-1" "Left Leaf (Connected to Node 3)"
run_test "rfsim5g-end-ue-3" "Middle Node (Connected to Node 4)"
run_test "rfsim5g-end-ue-5" "Right Leaf (Connected to Node 5)"

echo "Done! Full IAB Network Test Complete."