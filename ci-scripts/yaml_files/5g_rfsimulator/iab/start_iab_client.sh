#!/bin/bash
# ==========================================
# PC 2: IAB Client Launch & Test Script
# Role: Node 3, Node 4, Node 5, UEs
# ==========================================

# [設定] 請確認這與您的 docker-compose 檔名一致
COMPOSE_FILE="docker-compose-iab-client.yaml"

# [設定] PC 2 連接 PC 1 的實體網卡名稱 (請務必確認正確)
IFACE_NAME="enxc84d44350008" 

# [設定] PC 2 自己的 IP (必須與 Server 端網段一致)
CLIENT_IP="192.168.88.2" 
SERVER_IP="192.168.88.1" # PC 1 的 IP

# 測試目標 IP (Server 端 Docker 內網)
IP_EXT_DN="192.168.72.135" # 外部測試伺服器 (End-to-End)

# 自動判斷 docker compose 指令
if command -v docker-compose &> /dev/null; then DOCKER_COMPOSE="docker-compose"; else DOCKER_COMPOSE="docker compose"; fi

# 顏色定義
RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
NC='\033[0m'

# ==========================================
# 函式定義區
# ==========================================

# 設定 IAB Node 內部的 NAT 與路由 (讓流量強制走 5G 隧道回 PC 1)
setup_nat_immediate() {
    NODE_NAME=$1
    echo "   -> [Fix] Configuring Route for $NODE_NAME..."
    
    # 1. 啟用轉發
    docker exec -u 0 $NODE_NAME sysctl -w net.ipv4.ip_forward=1 >/dev/null
    
    # 2. 設定路由 (加上 via 12.1.1.1 騙過 ARP)
    # 告訴 Node: 去 Server (.88.1) 和 Core Network (.72.0) 都要丟進隧道 (oaitun_ue1)
    # 注意: 這裡的 via 12.1.1.1 是虛擬的 Gateway IP
    docker exec -u 0 $NODE_NAME ip route replace 192.168.72.0/24 via 12.1.1.1 dev oaitun_ue1 2>/dev/null || true
    
    # 3. NAT 設定
    docker exec -u 0 $NODE_NAME iptables -t nat -A POSTROUTING -o oaitun_ue1 -j MASQUERADE 2>/dev/null || true
}

# 等待 MT 拿到 IP 並立刻設定路由
wait_for_ip() {
    CONTAINER=$1
    VAR_NAME=$2
    echo -n "Waiting IP for $CONTAINER... "
    local IP=""
    local COUNT=0
    
    while [ -z "$IP" ]; do
        sleep 2
        # 抓取 oaitun_ue1 的 IP
        IP=$(docker exec $CONTAINER ip -f inet addr show oaitun_ue1 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}')
        COUNT=$((COUNT+1))
        if [ $COUNT -ge 60 ]; then echo -e "${RED} Timeout! (Check Server/Cable)${NC}"; exit 1; fi
        echo -n "."
    done
    echo -e "${GREEN} IP: $IP${NC}"
    eval "$VAR_NAME='$IP'"
    
    # 拿到 IP 後立刻設定路由
    setup_nat_immediate $CONTAINER
}

# ==========================================
# 1. 網路前置檢查
# ==========================================
echo -e "${CYAN}[1/5] Configuring Network...${NC}"

# 設定本機 IP
if ! ip addr show $IFACE_NAME | grep -q "$CLIENT_IP"; then
    echo "   -> Setting IP $CLIENT_IP..."
    sudo ip addr add $CLIENT_IP/24 dev $IFACE_NAME
    sudo ip link set $IFACE_NAME up
fi

# 測試連線到 Server
echo "   -> Pinging Server ($SERVER_IP)..."
if ! ping -c 1 -W 1 $SERVER_IP &> /dev/null; then
    echo -e "${RED} Cannot reach PC 1 ($SERVER_IP)! Check cable.${NC}"
    exit 1
fi
echo -e "${GREEN} Connection OK.${NC}"

# 開啟轉發
sudo sysctl -w net.ipv4.ip_forward=1 > /dev/null

# 清理舊容器
echo "   -> Cleaning up..."
$DOCKER_COMPOSE -f $COMPOSE_FILE down

# ==========================================
# 2. 啟動 Layer 2 Nodes (Node 3, 4, 5)
# ==========================================
echo -e "${CYAN}[2/5] Starting Layer 2 Nodes (Node 3, 4, 5)...${NC}"

# 啟動 MT 部分
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-mt-3 rfsim5g-iab-mt-4 rfsim5g-iab-mt-5

# 等待 MT 連上 PC 1 並拿到 IP
wait_for_ip "rfsim5g-iab-mt-3" MT3_IP
wait_for_ip "rfsim5g-iab-mt-4" MT4_IP
wait_for_ip "rfsim5g-iab-mt-5" MT5_IP

echo "   -> Layer 2 IPs: Node3=$MT3_IP, Node4=$MT4_IP, Node5=$MT5_IP"

# [重要] 將拿到的 IP 寫入 DU 設定檔 (因為在 Host Mode 下，DU 需要綁定正確的 IP)
# 注意：這裡假設 conf 檔案在 ./conf/ 目錄下
sed -i "s/local_n_address *= *\".*\";/local_n_address = \"$MT3_IP\";/" ./conf/iab_du_3.conf
sed -i "s/local_n_address *= *\".*\";/local_n_address = \"$MT4_IP\";/" ./conf/iab_du_4.conf
sed -i "s/local_n_address *= *\".*\";/local_n_address = \"$MT5_IP\";/" ./conf/iab_du_5.conf

# 啟動 DU 部分
echo "   -> Starting DUs..."
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-du-3 rfsim5g-iab-du-4 rfsim5g-iab-du-5

echo "   -> Waiting 20s for DUs to stabilize..."
sleep 20

# ==========================================
# 3. 啟動 UE (End Devices)
# ==========================================
echo -e "${CYAN}[3/5] Starting UEs...${NC}"
# 啟動所有 UE
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-end-ue-1 rfsim5g-end-ue-2 rfsim5g-end-ue-3 rfsim5g-end-ue-4 rfsim5g-end-ue-5 rfsim5g-end-ue-6

# ==========================================
# 4. UE 連線檢查與工具安裝
# ==========================================
echo -e "${CYAN}[4/5] Checking UE Connectivity...${NC}"

wait_for_ue_and_install() {
    UE_NAME=$1
    echo -n "   -> Checking $UE_NAME... "
    local IP=""
    local COUNT=0
    
    while [ -z "$IP" ]; do
        sleep 2
        IP=$(docker exec $UE_NAME ip -f inet addr show oaitun_ue1 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}')
        COUNT=$((COUNT+1))
        
        if [ $COUNT -eq 15 ]; then
             echo -n "${YELLOW}[Restarting]${NC} "
             docker restart $UE_NAME > /dev/null
        fi

        if [ $COUNT -ge 45 ]; then echo -e "${RED} Timeout${NC}"; return 1; fi
        echo -n "."
    done
    echo -e "${GREEN} Attached! (IP: $IP)${NC}"
    
    # 設定 UE 路由: 往 Core Network 走 5G 隧道
    docker exec -u 0 $UE_NAME ip route replace 192.168.72.0/24 dev oaitun_ue1 2>/dev/null || true

    # 自動安裝測試工具 (如果沒有的話)
    if ! docker exec $UE_NAME which iperf3 &> /dev/null; then
        echo -n "      Installing tools... "
        docker exec -u 0 $UE_NAME bash -c "apt-get update -qq && apt-get install -y -qq iputils-ping iperf3 >/dev/null"
        echo "Done."
    fi
}

# 檢查部分 UE 即可 (每組選一台)
wait_for_ue_and_install "rfsim5g-end-ue-1"
wait_for_ue_and_install "rfsim5g-end-ue-3"
wait_for_ue_and_install "rfsim5g-end-ue-5"

# ==========================================
# 5. 執行效能測試
# ==========================================
echo -e "\n${CYAN}[5/5] Running Network Test (Target: $IP_EXT_DN)${NC}"

run_test() {
    UE=$1
    DESC=$2
    echo -e "\nTesting ${YELLOW}$UE${NC} ($DESC)"
    
    # Ping 測試
    PING=$(docker exec $UE ping -I oaitun_ue1 -c 3 -W 1 $IP_EXT_DN 2>/dev/null | grep "avg" | awk -F '/' '{print $5}')
    if [ -z "$PING" ]; then 
        echo -e "   Latency: ${RED}FAIL${NC}" 
    else 
        echo -e "   Latency: ${GREEN}${PING} ms${NC}" 
    fi

    # 簡單 iperf 測試 (下行)
    echo -n "   Downlink Speed: "
    DL=$(docker exec $UE iperf3 -c $IP_EXT_DN -I oaitun_ue1 -R -t 2 -f m --connect-timeout 2000 2>/dev/null | grep "receiver" | awk '{print $(NF-2)}')
    if [ -z "$DL" ]; then echo "FAIL"; else echo "${DL} Mbps"; fi
}

run_test "rfsim5g-end-ue-1" "Group A (Node 3)"
run_test "rfsim5g-end-ue-3" "Group B (Node 4)"
run_test "rfsim5g-end-ue-5" "Group C (Node 5)"

echo -e "\n${GREEN}Client Setup Complete!${NC}"