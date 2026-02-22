#!/bin/bash
# PC 2: IAB Client Script (Node 3, Node 4, Node 5, 6x End-UEs)

COMPOSE_FILE="docker-compose-iab-client.yaml"
IFACE_NAME="enxc84d44350008" 

# IP 設定
CLIENT_IP="192.168.88.2"
SERVER_IP="192.168.88.1"     
CN_SUBNET="192.168.71.0/24" 
UE_SUBNET="12.1.1.0/24" 
UPF_DN_IP="192.168.72.135"   

# DU 內部 IP
DU3_IP="192.168.74.20"
DU4_IP="192.168.74.21"
DU5_IP="192.168.74.22"

if docker compose version &> /dev/null; then DOCKER_COMPOSE="docker compose"; else DOCKER_COMPOSE="docker-compose"; fi

RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
NC='\033[0m'

DONE_NODE3=0; DONE_NODE4=0; DONE_NODE5=0
# [修正] 初始化即包含清空指令，確保魔法指令執行時先歸零
CU_MAGIC_COMMANDS="docker exec -u 0 rfsim5g-donor-cu iptables -t nat -F OUTPUT"

echo -e "${CYAN}[0/5] Loading Kernel Modules...${NC}"
sudo modprobe sctp
sudo modprobe nf_conntrack_sctp 2>/dev/null || sudo modprobe nf_conntrack_proto_sctp 2>/dev/null

# 函式：設定網路與啟動 DU 
configure_and_start_du() {
    local MT_NAME=$1
    local DU_NAME=$2
    local DU_DOCKER_IP=$3

    echo -e "\n${GREEN}[Action] Setting up network for $DU_NAME ($DU_DOCKER_IP)${NC}"
    
    local MT_TUNNEL_IP=$(docker exec $MT_NAME ip -f inet addr show oaitun_ue1 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}')
    CU_MAGIC_COMMANDS+="\ndocker exec -u 0 rfsim5g-donor-cu iptables -t nat -A OUTPUT -d $DU_DOCKER_IP -p udp --dport 2152 -j DNAT --to-destination $MT_TUNNEL_IP"

    docker exec -u 0 $MT_NAME sysctl -w net.ipv4.ip_forward=1 >/dev/null
    docker exec -u 0 $MT_NAME iptables -t nat -F PREROUTING
    docker exec -u 0 $MT_NAME iptables -t nat -F POSTROUTING
    docker exec -u 0 $MT_NAME iptables -t nat -A POSTROUTING -s $DU_DOCKER_IP -o oaitun_ue1 -j MASQUERADE
    docker exec -u 0 $MT_NAME iptables -t nat -A PREROUTING -i oaitun_ue1 -p sctp -j DNAT --to-destination $DU_DOCKER_IP
    docker exec -u 0 $MT_NAME iptables -t nat -A PREROUTING -i oaitun_ue1 -p udp --dport 2152 -j DNAT --to-destination $DU_DOCKER_IP
    
    docker exec -u 0 $MT_NAME ip route del $CN_SUBNET 2>/dev/null || true
    docker exec -u 0 $MT_NAME ip route add $CN_SUBNET via 12.1.1.1 dev oaitun_ue1
    docker exec -u 0 $MT_NAME ethtool -K oaitun_ue1 tx off 2>/dev/null || true
    docker exec -u 0 $MT_NAME ip link set oaitun_ue1 mtu 1300 2>/dev/null
    echo "   -> [Network] MTU optimized for IAB Tunnel (1300)"

    # IAB 模式路徑設定
    docker exec -u 0 $MT_NAME ip route del default 2>/dev/null || true
    docker exec -u 0 $MT_NAME ip route add default via 12.1.1.1 dev oaitun_ue1
    echo "   -> [Network] MT forced to use 5G Tunnel for all traffic."

    echo "   -> [Docker] Starting DU: $DU_NAME"
    $DOCKER_COMPOSE -f $COMPOSE_FILE up -d --force-recreate $DU_NAME
    
    local MT_INTERNAL_IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' $MT_NAME | grep 192.168.74)
    sleep 2
    # 使用 replace 避免重複啟動報錯
    docker exec -u 0 $DU_NAME ip route replace $CN_SUBNET via $MT_INTERNAL_IP 2>/dev/null
    # 讓 DU 知道如何把 End-UE 流量傳回 MT
    docker exec -u 0 $DU_NAME ip route replace $UE_SUBNET via $MT_INTERNAL_IP 2>/dev/null

    # 確保指向 PC 1 (192.168.88.x) 的 E2/F1 控制面流量走實體網線，不要進 5G 隧道
    # 確保 Node 3, 4, 5 能穩定連上 FlexRIC 
    # docker exec -u 0 $DU_NAME ip route add 192.168.88.0/24 via 192.168.74.1 2>/dev/null
    echo -e "${YELLOW} Waiting 10s for CU F1AP stability...${NC}"
    sleep 10
}

wait_for_ue() {
    local UE_NAME=$1
    echo -n "   -> Checking $UE_NAME... "
    local IP=""
    local COUNT=0
    while [ -z "$IP" ]; do
        sleep 2
        IP=$(docker exec $UE_NAME ip -f inet addr show oaitun_ue1 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}')
        echo -n "."
        COUNT=$((COUNT+1))
        if [ $COUNT -ge 30 ]; then break; fi
    done
    if [ ! -z "$IP" ]; then
        echo -e "${GREEN} Attached! ($IP)${NC}"
        docker exec -u 0 $UE_NAME ip link set oaitun_ue1 mtu 1200 2>/dev/null
        docker exec -u 0 $UE_NAME ip route replace default via 12.1.1.1 dev oaitun_ue1 2>/dev/null
    else
        echo -e "${RED} Not Found${NC}"
    fi
}

# 統計設定與初始化
ITERATIONS=3      # 每台 UE 測試 3 次採樣
NET_UE_COUNT=0     # 成功測試的 UE 總數

# 全網總和累加器 (用於計算最後的全網平均)
G_LAT_SUM=0
G_TCP_DL_SUM=0; G_TCP_UL_SUM=0
G_UDP_DL_SUM=0; G_UDP_UL_SUM=0

# 函式：效能測試 (包含 UL/DL 與 平均值計算)
run_benchmarks() {
    local UE=$1
    echo -e "\n${CYAN} Analysis ${UE} (ITERATIONS: $ITERATIONS)${NC}"
    
    # 單一 UE 的局部累加器
    local u_lat=0; local u_lat_c=0
    local u_tcp_dl=0; local u_tcp_dl_c=0; local u_tcp_ul=0; local u_tcp_ul_c=0
    local u_udp_dl=0; local u_udp_dl_c=0; local u_udp_ul=0; local u_udp_ul_c=0

    for ((i=1; i<=ITERATIONS; i++)); do
        echo -e "   [$i/$ITERATIONS]"
        sleep 2

        # 1. latency (Ping)
        local lat=$(docker exec $UE ping -c 2 -W 1 $UPF_DN_IP 2>/dev/null | grep "avg" | awk -F'/' '{print $5}')
        if [ ! -z "$lat" ]; then 
            u_lat=$(echo "$u_lat + $lat" | bc)
            u_lat_c=$((u_lat_c+1))
        fi

        # 2. TCP Downlink -R 
        local dl_raw=$(docker exec $UE iperf3 -c $UPF_DN_IP -t 2 -R 2>/dev/null)
        local dl_val=$(echo "$dl_raw" | grep "receiver" | awk '{print $7}')
        if [ ! -z "$dl_val" ] && [ "$dl_val" != "0.00" ]; then 
            u_tcp_dl=$(echo "$u_tcp_dl + $dl_val" | bc)
            u_tcp_dl_c=$((u_tcp_dl_c+1))
        fi

        # 3. TCP Uplink
        local ul_raw=$(docker exec $UE iperf3 -c $UPF_DN_IP -t 2 2>/dev/null)
        local ul_val=$(echo "$ul_raw" | grep "receiver" | awk '{print $7}')
        if [ ! -z "$ul_val" ] && [ "$ul_val" != "0.00" ]; then 
            u_tcp_ul=$(echo "$u_tcp_ul + $ul_val" | bc)
            u_tcp_ul_c=$((u_tcp_ul_c+1))
        fi

        # 4. UDP Downlink
        local udl_raw=$(docker exec $UE iperf3 -c $UPF_DN_IP -u -b 20M -t 2 -R 2>/dev/null)
        local udl_val=$(echo "$udl_raw" | grep "receiver" | awk '{print $7}')
        if [ ! -z "$udl_val" ] && [ "$udl_val" != "0.00" ]; then 
            u_udp_dl=$(echo "$u_udp_dl + $udl_val" | bc)
            u_udp_dl_c=$((u_udp_dl_c+1))
        fi

        # 5. UDP Uplink
        local uul_raw=$(docker exec $UE iperf3 -c $UPF_DN_IP -u -b 20M -t 2 2>/dev/null)
        local uul_val=$(echo "$uul_raw" | grep "receiver" | awk '{print $7}')
        if [ ! -z "$uul_val" ] && [ "$uul_val" != "0.00" ]; then 
            u_udp_ul=$(echo "$u_udp_ul + $uul_val" | bc)
            u_udp_ul_c=$((u_udp_ul_c+1))
        fi
    done

    # 計算並顯示該 UE 的平均值報告
    echo -e "--------------------------------------"
    echo -e "${UE}"
    
    if [ $u_lat_c -gt 0 ]; then
        local a_lat=$(echo "scale=2; $u_lat / $u_lat_c" | bc)
        echo -e "Avg.Latency: ${GREEN}${a_lat} ms${NC}"
        G_LAT_SUM=$(echo "$G_LAT_SUM + $a_lat" | bc)
    fi
    
    if [ $u_tcp_dl_c -gt 0 ]; then
        local a_tdl=$(echo "scale=2; $u_tcp_dl / $u_tcp_dl_c" | bc)
        echo -e "Avg.Throughput(TCP-DL): ${GREEN}${a_tdl} Mbps${NC}"
        G_TCP_DL_SUM=$(echo "$G_TCP_DL_SUM + $a_tdl" | bc)
    fi

    if [ $u_tcp_ul_c -gt 0 ]; then
        local a_tul=$(echo "scale=2; $u_tcp_ul / $u_tcp_ul_c" | bc)
        echo -e "Avg.Throughput(TCP-UL): ${GREEN}${a_tul} Mbps${NC}"
        G_TCP_UL_SUM=$(echo "$G_TCP_UL_SUM + $a_tul" | bc)
    fi

    if [ $u_udp_dl_c -gt 0 ]; then
        local a_udl=$(echo "scale=2; $u_udp_dl / $u_udp_dl_c" | bc)
        echo -e "Avg.Throughput(UDP-DL): ${GREEN}${a_udl} Mbps${NC}"
        G_UDP_DL_SUM=$(echo "$G_UDP_DL_SUM + $a_udl" | bc)
    fi

    if [ $u_udp_ul_c -gt 0 ]; then
        local a_uul=$(echo "scale=2; $u_udp_ul / $u_udp_ul_c" | bc)
        echo -e "Avg.Throughput(UDP-UL): ${GREEN}${a_uul} Mbps${NC}"
        G_UDP_UL_SUM=$(echo "$G_UDP_UL_SUM + $a_uul" | bc)
    fi
    echo -e "--------------------------------------"
    
    NET_UE_COUNT=$((NET_UE_COUNT + 1))
}

# ==========================================
# 主流程 (維持原邏輯)
# ==========================================
echo -e "${CYAN}[1/5] Host Network Prep...${NC}"
sudo sysctl -w net.ipv4.ip_forward=1 > /dev/null

$DOCKER_COMPOSE -f $COMPOSE_FILE down

echo -e "${CYAN}[2/5] Launching MTs...${NC}"
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-mt-3 rfsim5g-iab-mt-4 rfsim5g-iab-mt-5

echo -e "${CYAN}[3/5] Deploying DUs Sequentially...${NC}"
COUNT=0
while [ $COUNT -lt 60 ]; do
    if [ $DONE_NODE3 -eq 1 ] && [ $DONE_NODE4 -eq 1 ] && [ $DONE_NODE5 -eq 1 ]; then break; fi

    if [ $DONE_NODE3 -eq 0 ] && docker exec rfsim5g-iab-mt-3 ip addr show oaitun_ue1 &>/dev/null; then
        configure_and_start_du "rfsim5g-iab-mt-3" "rfsim5g-iab-du-3" "$DU3_IP"
        DONE_NODE3=1
    fi
    if [ $DONE_NODE4 -eq 0 ] && docker exec rfsim5g-iab-mt-4 ip addr show oaitun_ue1 &>/dev/null; then
        configure_and_start_du "rfsim5g-iab-mt-4" "rfsim5g-iab-du-4" "$DU4_IP"
        DONE_NODE4=1
    fi
    if [ $DONE_NODE5 -eq 0 ] && docker exec rfsim5g-iab-mt-5 ip addr show oaitun_ue1 &>/dev/null; then
        configure_and_start_du "rfsim5g-iab-mt-5" "rfsim5g-iab-du-5" "$DU5_IP"
        DONE_NODE5=1
    fi
    sleep 5; COUNT=$((COUNT+1))
done

echo -e "${CYAN}Finalizing Control Plane, waiting 5s...${NC}"
sleep 5

echo -e "\n${CYAN}[4/5] Launching All 6 End-UEs...${NC}"
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-end-ue-1 rfsim5g-end-ue-2 rfsim5g-end-ue-3 rfsim5g-end-ue-4 rfsim5g-end-ue-5 rfsim5g-end-ue-6
for i in {1..6}; do wait_for_ue "rfsim5g-end-ue-$i"; done

echo -e "\n${YELLOW}====================================================${NC}"
echo -e "${YELLOW}[ACTION REQUIRED] FINAL ROUTING FIX ON PC 1 (SERVER)${NC}"
echo -e "${CYAN}${CU_MAGIC_COMMANDS}${NC}"
echo -e "${YELLOW}====================================================${NC}"

read -p "Press [Enter] to do performance tests..."

echo -e "\n${CYAN}[5/5] Running Performance Benchmarks...${NC}"
sleep 2 
for i in {1..6}; do run_benchmarks "rfsim5g-end-ue-$i"; done

# 彙整報告
echo -e "\n${YELLOW}====================================================${NC}"
echo -e "${YELLOW}           全網 IAB Performance Benchmarks       ${NC}"
echo -e "${YELLOW}====================================================${NC}"

if [ $NET_UE_COUNT -gt 0 ]; then
    final_lat=$(echo "scale=2; $G_LAT_SUM / $NET_UE_COUNT" | bc)
    final_tdl=$(echo "scale=2; $G_TCP_DL_SUM / $NET_UE_COUNT" | bc)
    final_tul=$(echo "scale=2; $G_TCP_UL_SUM / $NET_UE_COUNT" | bc)
    final_udl=$(echo "scale=2; $G_UDP_DL_SUM / $NET_UE_COUNT" | bc)
    final_uul=$(echo "scale=2; $G_UDP_UL_SUM / $NET_UE_COUNT" | bc)

    echo -e "UE Count: $NET_UE_COUNT"
    echo -e "全網 Avg.Latency:     ${CYAN}$final_lat ms${NC}"
    echo -e "全網 Avg.Throughput(TCP-DL): ${CYAN}$final_tdl Mbps${NC}"
    echo -e "全網 Avg.Throughput(TCP-UL): ${CYAN}$final_tul Mbps${NC}"
    echo -e "全網 Avg.Throughput(UDP-DL): ${CYAN}$final_udl Mbps${NC}"
    echo -e "全網 Avg.Throughput(UDP-UL): ${CYAN}$final_uul Mbps${NC}"
else
    echo -e "${RED}Failed${NC}"
fi
echo -e "${YELLOW}====================================================${NC}"

echo -e "\n${GREEN}IAB Client - All 6 UEs Ready and Tested!${NC}"