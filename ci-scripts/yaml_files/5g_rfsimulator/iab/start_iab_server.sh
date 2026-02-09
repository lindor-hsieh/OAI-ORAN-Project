#!/bin/bash
# PC 1: IAB Server
# Updates: 
#   1. Auto-fix UPF Forwarding (iptables)
#   2. Auto-fix Routing (via tunnel)

COMPOSE_FILE="docker-compose-iab-server.yaml"
IFACE_NAME="enxc84d44350030" # PC 1 的網卡名稱

# 自動判斷 docker compose
if command -v docker-compose &> /dev/null; then DOCKER_COMPOSE="docker-compose"; else DOCKER_COMPOSE="docker compose"; fi

# 設定路由
setup_nat_immediate() {
    NODE_NAME=$1
    echo "   -> [Fix] Configuring Route for $NODE_NAME..."
    
    # 1. 啟用轉發
    docker exec -u 0 $NODE_NAME sysctl -w net.ipv4.ip_forward=1 >/dev/null
    
    # 2. 設定路由 (加上 via 12.1.1.1 騙過 ARP)
    # 告訴 Node: 去 CU (.140) 和 外網 (.72.0) 都要丟進隧道,下一跳隨便指個 IP
    docker exec -u 0 $NODE_NAME ip route replace 192.168.71.140 via 12.1.1.1 dev oaitun_ue1 2>/dev/null || true
    docker exec -u 0 $NODE_NAME ip route replace 192.168.72.0/24 via 12.1.1.1 dev oaitun_ue1 2>/dev/null || true
    
    # 3. NAT 設定
    docker exec -u 0 $NODE_NAME iptables -t nat -A POSTROUTING -o oaitun_ue1 -j MASQUERADE 2>/dev/null || true
}

# 拿到 IP 後立刻設路由 
wait_for_ip() {
    CONTAINER=$1
    VAR_NAME=$2
    echo -n "Waiting IP for $CONTAINER... "
    local IP=""
    local COUNT=0
    
    while [ -z "$IP" ]; do
        sleep 2
        IP=$(docker exec $CONTAINER ip -f inet addr show oaitun_ue1 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}')
        COUNT=$((COUNT+1))
        if [ $COUNT -ge 40 ]; then echo " Timeout!"; exit 1; fi
        echo -n "."
    done
    echo "  IP: $IP"
    eval "$VAR_NAME='$IP'"
    
    # 在啟動 DU 之前，先確保 MT 知道怎麼走 5G 隧道
    setup_nat_immediate $CONTAINER
}

# 1. 清理與網路設定
echo "[1/6] Preparing Network..."
# 確保 Host IP 存在
if ! ip addr show $IFACE_NAME | grep -q "192.168.88.1"; then
    sudo ip addr add 192.168.88.1/24 dev $IFACE_NAME 2>/dev/null
    sudo ip link set $IFACE_NAME up
fi
sudo sysctl -w net.ipv4.ip_forward=1 > /dev/null
$DOCKER_COMPOSE -f $COMPOSE_FILE down
sudo ip route flush 12.1.1.0/24 2>/dev/null

# SCTP 修正
sudo ethtool -K rfsim5g-oai-public-net tx off rx off 2>/dev/null || true

# 2. 啟動 Core & Donor
echo "[2/6] Starting Core & Donor..."
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d mysql oai-amf oai-smf oai-upf oai-ext-dn rfsim5g-donor-cu rfsim5g-donor-du oai-flexric

echo "Waiting 30s for Core..."
sleep 30

# 設定 Donor CU 回程路由 (指向 UPF)
echo "   -> Configuring CU & UPF Routing..."
docker exec -u 0 rfsim5g-donor-cu ip route replace 12.1.1.0/24 via 192.168.71.134 dev eth0 2>/dev/null || true
docker exec -u 0 rfsim5g-oai-ext-dn ip route replace 12.1.1.0/24 via 192.168.72.134 dev eth0 2>/dev/null || true

# 強制開啟 UPF 轉發權限 (解決封包被丟棄問題)
echo "   -> [Fix] Applying UPF Forwarding Rules (The Magic Command)..."
docker exec -u 0 rfsim5g-oai-upf bash -c "iptables -P FORWARD ACCEPT && iptables -F FORWARD && iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE"

# 3. 啟動 Node 1
echo "[3/6] Starting Node 1..."
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-mt
wait_for_ip "rfsim5g-iab-mt" MT1_IP

# 更新設定並啟動 DU (此時路由已在 wait_for_ip 中設好)
sed -i "s/local_n_address *= *\".*\";/local_n_address = \"$MT1_IP\";/" ./conf/iab_du.conf
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-du

# 4. 啟動 Node 2
echo "[4/6] Starting Node 2..."
sleep 5
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-mt-2
wait_for_ip "rfsim5g-iab-mt-2" MT2_IP
sed -i "s/local_n_address *= *\".*\";/local_n_address = \"$MT2_IP\";/" ./conf/iab_du_2.conf
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-du-2

echo "Layer 1 IPs: Node1=$MT1_IP, Node2=$MT2_IP"

# # 5. 啟動 Node 3, 4, 5
# echo "[5/6] Starting Layer 2 Nodes..."
# $DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-mt-3 rfsim5g-iab-mt-4 rfsim5g-iab-mt-5

# wait_for_ip "rfsim5g-iab-mt-3" MT3_IP
# wait_for_ip "rfsim5g-iab-mt-4" MT4_IP
# wait_for_ip "rfsim5g-iab-mt-5" MT5_IP

# echo "Layer 2 IPs: Node3=$MT3_IP, Node4=$MT4_IP, Node5=$MT5_IP"

# sed -i "s/local_n_address *= *\".*\";/local_n_address = \"$MT3_IP\";/" ./conf/iab_du_3.conf
# sed -i "s/local_n_address *= *\".*\";/local_n_address = \"$MT4_IP\";/" ./conf/iab_du_4.conf
# sed -i "s/local_n_address *= *\".*\";/local_n_address = \"$MT5_IP\";/" ./conf/iab_du_5.conf

# $DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-du-3 rfsim5g-iab-du-4 rfsim5g-iab-du-5

# 6. 再次確保 UPF 規則存在
echo "[6/6] Finalizing..."
docker exec -u 0 rfsim5g-oai-upf iptables -t nat -A POSTROUTING -s 12.1.1.0/24 -o eth0 -j MASQUERADE 2>/dev/null || true
docker exec -d rfsim5g-oai-ext-dn iperf3 -s

echo "[5/5] Server setup complete!"