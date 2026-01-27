#!/bin/bash

# ==========================================
# PC 1: IAB Server Side Launch Script (FAST ROUTE VERSION)
# Architecture: Double Diamond (1 Donor -> 2 Aggregators -> 3 Leafs)
# Path: ~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator/start_iab_server.sh
# ==========================================

COMPOSE_FILE="docker-compose-iab-server.yaml"
IFACE_NAME="enp6s0"  # [請確認] 您 PC 1 的實體網卡名稱
SERVER_IP="192.168.88.1"

# 自動判斷 docker compose 指令
if command -v docker-compose &> /dev/null; then
    DOCKER_COMPOSE="docker-compose"
else
    DOCKER_COMPOSE="docker compose"
fi

# ---------------------------------------------------------
# [一般等待函數] 用於 Layer 1
# ---------------------------------------------------------
wait_for_ip() {
    CONTAINER=$1
    VAR_NAME=$2
    echo -n "Waiting IP for $CONTAINER... "
    local IP=""
    local COUNT=0
    local MAX_RETRIES=40 
    
    while [ -z "$IP" ]; do
        sleep 2
        IP=$(docker exec $CONTAINER ip -f inet addr show oaitun_ue1 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}')
        COUNT=$((COUNT+1))
        if [ $COUNT -ge $MAX_RETRIES ]; then 
            echo "❌ Failed! (Timeout)"
            exit 1 
        fi
        echo -n "."
    done
    echo " ✅ IP: $IP"
    eval "$VAR_NAME='$IP'"
}

# ---------------------------------------------------------
# [進階等待函數] 用於 Layer 2 (連不上會自動 Restart)
# ---------------------------------------------------------
wait_for_ip_with_retry() {
    CONTAINER=$1
    VAR_NAME=$2
    echo -n "Waiting IP for $CONTAINER (Auto-Restart enabled)... "
    local IP=""
    local COUNT=0
    local RESTARTED=0 
    
    while [ -z "$IP" ]; do
        sleep 2
        IP=$(docker exec $CONTAINER ip -f inet addr show oaitun_ue1 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}')
        COUNT=$((COUNT+1))
        
        # 30秒沒 IP -> 自動重啟
        if [ $COUNT -ge 15 ] && [ $RESTARTED -eq 0 ]; then
            echo ""
            echo "⚠️  $CONTAINER stuck? Triggering AUTO-RESTART..."
            docker restart $CONTAINER
            RESTARTED=1
            COUNT=0 
            echo -n "   -> Restarted. Waiting again... "
        fi

        # 重啟後又失敗 -> 放棄
        if [ $COUNT -ge 25 ] && [ $RESTARTED -eq 1 ]; then 
            echo "❌ Failed even after restart!"
            exit 1 
        fi
        echo -n "."
    done
    echo " ✅ IP: $IP"
    eval "$VAR_NAME='$IP'"
}

# ==========================================
# 0. 環境準備 & 網路設定
# ==========================================
echo "[0/7] Configuring Host Network..."
if ! ip addr show $IFACE_NAME | grep -q "$SERVER_IP"; then
    echo "Adding experiment IP $SERVER_IP to $IFACE_NAME..."
    sudo ip addr add $SERVER_IP/24 dev $IFACE_NAME
    sudo ip link set $IFACE_NAME up
else
    echo "IP $SERVER_IP already exists on $IFACE_NAME."
fi
sudo sysctl -w net.ipv4.ip_forward=1 > /dev/null

# ==========================================
# 1. 清除舊環境 (強制重置資料庫)
# ==========================================
echo "[1/7] Clean up Docker environment..."
$DOCKER_COMPOSE -f $COMPOSE_FILE down
echo "   -> Removing old database container to force reload..."
docker rm -f rfsim5g-mysql 2>/dev/null
docker volume rm 5g_rfsimulator_dbdata 2>/dev/null || docker volume rm 5g_rfsimulator_mysql_data 2>/dev/null
sudo ip route flush 12.1.1.0/24 2>/dev/null

# ==========================================
# 2. 啟動核心網與 Donor (並立刻設定關鍵路由)
# ==========================================
echo "[2/7] Starting Core Network & Donor (Root)..."
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d mysql oai-amf oai-smf oai-upf oai-ext-dn rfsim5g-donor-cu rfsim5g-donor-du oai-flexric

echo "Waiting for Core/Donor Initialization (30s)..."
sleep 30

# [關鍵優化] 核心網一起來就馬上設路由，不用等到最後
echo "   -> Setting up Critical Routes (Donor CU & Ext-DN)..."
# 1. Donor CU 回程路由 (讓 Node 1 F1 Setup 能秒回)
docker exec -u 0 rfsim5g-donor-cu ip route replace 12.1.1.0/24 via 192.168.71.134 dev eth0 2>/dev/null || true
# 2. Ext-DN 回程路由 (讓測速能通)
docker exec -u 0 rfsim5g-oai-ext-dn ip route replace 12.1.1.0/24 via 192.168.72.134 dev eth0 2>/dev/null || true

# ==========================================
# 3. 啟動第一層 (Layer 1) - 排隊啟動
# ==========================================
echo "[3/7] Starting Layer 1 Nodes (Sequentially)..."

# --- Node 1 ---
echo "   -> Starting Node 1 (Left)..."
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-mt
wait_for_ip "rfsim5g-iab-mt" MT1_IP
sed -i "s/local_n_address *= *\".*\";/local_n_address = \"$MT1_IP\";/" ./conf/iab_du.conf
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-du

# --- Node 2 ---
echo "   -> Resting 5s before starting Node 2..."
sleep 5
echo "   -> Starting Node 2 (Right)..."
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-mt-2
wait_for_ip "rfsim5g-iab-mt-2" MT2_IP
sed -i "s/local_n_address *= *\".*\";/local_n_address = \"$MT2_IP\";/" ./conf/iab_du_2.conf
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-du-2

echo "Layer 1 IPs: Node1=$MT1_IP, Node2=$MT2_IP"
echo "Waiting for Layer 1 DUs to stabilize (25s)..." # [微調] 增加一點點等待時間 
sleep 25

# ==========================================
# 4. 啟動第二層 (Layer 2)
# ==========================================
echo "[4/7] Starting Layer 2 Nodes (Node 3, 4, 5)..."
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-mt-3 rfsim5g-iab-mt-4 rfsim5g-iab-mt-5

# 使用新的 wait_for_ip_with_retry 函數
wait_for_ip_with_retry "rfsim5g-iab-mt-3" MT3_IP
wait_for_ip_with_retry "rfsim5g-iab-mt-4" MT4_IP
wait_for_ip_with_retry "rfsim5g-iab-mt-5" MT5_IP

echo "Layer 2 IPs: Node3=$MT3_IP, Node4=$MT4_IP, Node5=$MT5_IP"

# Update Configs & Start DUs
sed -i "s/local_n_address *= *\".*\";/local_n_address = \"$MT3_IP\";/" ./conf/iab_du_3.conf
sed -i "s/local_n_address *= *\".*\";/local_n_address = \"$MT4_IP\";/" ./conf/iab_du_4.conf
sed -i "s/local_n_address *= *\".*\";/local_n_address = \"$MT5_IP\";/" ./conf/iab_du_5.conf

$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-du-3 rfsim5g-iab-du-4 rfsim5g-iab-du-5

# ==========================================
# 5. 設定路由與 NAT (這裡只需要設 Node 內部的 NAT)
# ==========================================
echo "[5/7] Configuring NAT for ALL Nodes..."

setup_nat() {
    NODE_NAME=$1
    NODE_IP=$2
    # 1. 啟用轉發
    docker exec -u 0 $NODE_NAME sysctl -w net.ipv4.ip_forward=1 >/dev/null
    # 2. NAT (讓內部 UE 出去時偽裝成 Node IP)
    docker exec -u 0 $NODE_NAME iptables -t nat -A POSTROUTING -o oaitun_ue1 -j MASQUERADE
    # 3. 路由設定：往核心網走 Uplink
    docker exec -u 0 $NODE_NAME ip route replace 192.168.71.140 dev oaitun_ue1 2>/dev/null || true
    # 4. 路由設定：往網際網路走 Uplink
    docker exec -u 0 $NODE_NAME ip route replace 192.168.72.0/24 dev oaitun_ue1 2>/dev/null || true
}

setup_nat "rfsim5g-iab-mt" "$MT1_IP"
setup_nat "rfsim5g-iab-mt-2" "$MT2_IP"
setup_nat "rfsim5g-iab-mt-3" "$MT3_IP"
setup_nat "rfsim5g-iab-mt-4" "$MT4_IP"
setup_nat "rfsim5g-iab-mt-5" "$MT5_IP"

# UPF 出口 NAT (確保保留)
docker exec -u 0 rfsim5g-oai-upf iptables -t nat -A POSTROUTING -s 12.1.1.0/24 -o eth0 -j MASQUERADE 2>/dev/null || true

# 啟動 iperf3 server
docker exec -d rfsim5g-oai-ext-dn iperf3 -s

echo "[6/7] Server Side Ready! Routes were applied early for fast convergence."