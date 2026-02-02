#!/bin/bash

# ==========================================
# PC 2: IAB Client Side - Advanced Testing Script
# Features:
#   1. Auto-install network tools (iperf3, traceroute)
#   2. Per-Hop Latency Analysis (Radio vs Core)
#   3. Full Throughput Test (DL & UL)
# ==========================================

COMPOSE_FILE="docker-compose-iab-ue.yaml"
IFACE_NAME="eno1"        # [請確認] 您 PC 2 連接 PC 1 的實體網卡名稱
CLIENT_IP="192.168.88.2" # PC 2 自己的 IP
SERVER_IP="192.168.88.1" # PC 1 的 IP

# 測試目標 IP (根據您的 Server 架構)
IP_UPF_Internal="192.168.72.134"  # 核心網 UPF 出口 (代表 5G 內網邊界)
IP_EXT_DN="192.168.72.135"        # 外部測試伺服器 (代表 End-to-End)

# 自動判斷 docker compose 指令
if command -v docker-compose &> /dev/null; then DOCKER_COMPOSE="docker-compose"; else DOCKER_COMPOSE="docker compose"; fi

# 顏色定義
RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
NC='\033[0m'

# ==========================================
# 0. 網路設定
# ==========================================
echo -e "${CYAN}[0/4] Configuring Client Network...${NC}"

# 1. 設定本機 IP
if ! ip addr show $IFACE_NAME | grep -q "$CLIENT_IP"; then
    echo "   -> Adding experiment IP $CLIENT_IP..."
    sudo ip addr add $CLIENT_IP/24 dev $IFACE_NAME
    sudo ip link set $IFACE_NAME up
else
    echo "   -> IP $CLIENT_IP already set."
fi

# 2. 測試連線
echo "   -> Pinging Server Physical IP ($SERVER_IP)..."
if ! ping -c 1 -W 1 $SERVER_IP &> /dev/null; then
    echo -e "${RED}❌ Cannot reach PC 1 ($SERVER_IP)! Check cable or firewall.${NC}"
    exit 1
fi
echo -e "${GREEN}✅ Physical Connection OK.${NC}"

# ==========================================
# 1. 啟動 UE
# ==========================================
echo -e "${CYAN}[1/4] Starting UEs (Official Image)...${NC}"
$DOCKER_COMPOSE -f $COMPOSE_FILE down
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d

echo "Waiting for UEs to attach..."

# ==========================================
# 2. 智慧等待與工具安裝
# ==========================================
wait_for_ue_and_install() {
    UE_NAME=$1
    echo -n "   -> Checking $UE_NAME... "
    local IP=""
    local COUNT=0
    
    while [ -z "$IP" ]; do
        sleep 2
        # 檢查是否有拿到 IP
        IP=$(docker exec $UE_NAME ip -f inet addr show oaitun_ue1 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}')
        COUNT=$((COUNT+1))
        
        # 30秒沒反應嘗試重啟
        if [ $COUNT -eq 15 ]; then
             echo -n "${YELLOW}[Restarting]${NC} "
             docker restart $UE_NAME > /dev/null
        fi

        if [ $COUNT -ge 45 ]; then echo -e "${RED}❌ Timeout${NC}"; return 1; fi
        echo -n "."
    done
    echo -e "${GREEN} Attached! (IP: $IP)${NC}"
    
    # [關鍵修正] 設定路由：往核心網走 5G 隧道
    docker exec -u 0 $UE_NAME ip route replace 192.168.72.0/24 dev oaitun_ue1 2>/dev/null || true

    # [關鍵新增] 自動安裝測試工具 (如果沒有的話)
    if ! docker exec $UE_NAME which iperf3 &> /dev/null; then
        echo -n "      Installing tools (iperf3, traceroute)... "
        # 為了避免卡住，使用 -qq 靜默模式
        docker exec -u 0 $UE_NAME bash -c "apt-get update -qq && apt-get install -y -qq iputils-ping iperf3 traceroute iproute2 >/dev/null"
        echo "Done."
    fi
}

# 依序檢查所有 UE
for i in {1..6}; do
    wait_for_ue_and_install "rfsim5g-end-ue-$i"
done

# ==========================================
# 3. 效能與每一跳測試
# ==========================================
echo -e "\n${CYAN}[3/4] Running Advanced Network Analysis${NC}"
echo "Target: Ext-DN ($IP_EXT_DN) via UPF ($IP_UPF_Internal)"

run_advanced_test() {
    UE=$1
    DESC=$2
    echo -e "\n=============================================="
    echo -e "Testing ${YELLOW}$UE${NC} ($DESC)"
    echo "=============================================="
    
    if ! docker ps | grep -q $UE; then
        echo "   Skipping (Container not running)"
        return
    fi

    # 1. 每一跳延遲測試 (Hop-by-Hop Latency)
    echo -e "${CYAN}--- [Latency Analysis] ---${NC}"
    
    # Ping UPF (核心網邊界) - 這是 "無線電 + IAB 回程" 的延遲
    PING_UPF=$(docker exec $UE ping -I oaitun_ue1 -c 3 -W 1 $IP_UPF_Internal 2>&1 | grep "avg" | awk -F '/' '{print $5}')
    if [ -z "$PING_UPF" ]; then PING_UPF="FAIL"; else PING_UPF="${PING_UPF} ms"; fi
    
    # Ping Ext-DN (端對端) - 這是 "總延遲"
    PING_E2E=$(docker exec $UE ping -I oaitun_ue1 -c 3 -W 1 $IP_EXT_DN 2>&1 | grep "avg" | awk -F '/' '{print $5}')
    if [ -z "$PING_E2E" ]; then PING_E2E="FAIL"; else PING_E2E="${PING_E2E} ms"; fi
    
    echo "   1. Radio+Core Latency (to UPF):  $PING_UPF"
    echo "   2. End-to-End Latency (to DN):   $PING_E2E"

    # Traceroute (嘗試顯示路徑)
    echo -n "   3. Path Trace: "
    TRACE=$(docker exec $UE traceroute -n -w 1 -m 5 $IP_EXT_DN 2>/dev/null | tail -n+2 | awk '{print $2}' | tr '\n' ' -> ')
    echo "${TRACE:-No Trace Info}"

    # 2. 吞吐量測試 (Throughput)
    echo -e "${CYAN}--- [Throughput Analysis] ---${NC}"
    
    # 下行 (Downlink)
    echo -n "   ⬇️  Downlink (Server->UE): "
    DL_SPEED=$(docker exec $UE iperf3 -c $IP_EXT_DN -I oaitun_ue1 -R -t 3 -f m --connect-timeout 2000 2>/dev/null | grep "receiver" | awk '{print $(NF-2)}')
    if [ -z "$DL_SPEED" ]; then echo "FAIL"; else echo "${DL_SPEED} Mbps"; fi

    # 上行 (Uplink)
    echo -n "   ⬆️  Uplink   (UE->Server): "
    UL_SPEED=$(docker exec $UE iperf3 -c $IP_EXT_DN -I oaitun_ue1 -t 3 -f m --connect-timeout 2000 2>/dev/null | grep "receiver" | awk '{print $(NF-2)}')
    if [ -z "$UL_SPEED" ]; then echo "FAIL"; else echo "${UL_SPEED} Mbps"; fi
}

# 執行測試 (可以選擇只測每組的一台代表，節省時間)
run_advanced_test "rfsim5g-end-ue-1" "Group A: via Node 3 (Left Leaf)"
run_advanced_test "rfsim5g-end-ue-3" "Group B: via Node 4 (Middle)"
run_advanced_test "rfsim5g-end-ue-5" "Group C: via Node 5 (Right Leaf)"

echo -e "\n${GREEN}Done! Full IAB Network Test Complete.${NC}"