#!/bin/bash
# PC 2: IAB Client Script (Based on User's Successful Logic + Macvlan Prep)

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

RIC_IP="192.168.88.141"
DN_SUBNET="192.168.72.0/24" # UPF 所在的資料網段

# PC1 SSH 設定（用於自動執行 CU magic commands，省去手動複製貼上）
PC1_USER="lindor"
PC1_IP="192.168.88.1"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=5 -o BatchMode=yes"

if docker compose version &> /dev/null; then DOCKER_COMPOSE="docker compose"; else DOCKER_COMPOSE="docker-compose"; fi

RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
NC='\033[0m'

DONE_NODE3=0; DONE_NODE4=0; DONE_NODE5=0
# fallback 用：若 SSH 不通，仍累積指令供手動貼上
CU_MAGIC_COMMANDS="docker exec -u 0 rfsim5g-donor-cu iptables -t nat -F OUTPUT"
SSH_AVAILABLE=false

# 檢查 SSH 連線是否可用
if ssh $SSH_OPTS ${PC1_USER}@${PC1_IP} "exit" 2>/dev/null; then
    SSH_AVAILABLE=true
    echo -e "${GREEN}[SSH] PC1 SSH 連線可用，CU magic commands 將自動執行${NC}"
    # 先清空 OUTPUT NAT table
    ssh $SSH_OPTS ${PC1_USER}@${PC1_IP} \
        "docker exec -u 0 rfsim5g-donor-cu iptables -t nat -F OUTPUT" 2>/dev/null \
        && echo -e "${GREEN}[SSH] OUTPUT NAT table 已清空${NC}" \
        || echo -e "${YELLOW}[SSH] 清空 NAT table 失敗，請確認 rfsim5g-donor-cu 已啟動${NC}"
else
    echo -e "${YELLOW}[SSH] 無法連線 PC1，將改為印出 CU magic commands 供手動執行${NC}"
    echo -e "${YELLOW}[SSH] 若要啟用自動模式，請在 PC2 執行：${NC}"
    echo -e "${CYAN}       ssh-copy-id ${PC1_USER}@${PC1_IP}${NC}"
fi

echo -e "${CYAN}[0/5] Loading Kernel Modules & Macvlan Prep...${NC}"
sudo modprobe sctp
sudo modprobe nf_conntrack_sctp 2>/dev/null || sudo modprobe nf_conntrack_proto_sctp 2>/dev/null

# 加入 PC 2 Macvlan 與實體網卡調優 (防 IQ 封包掉包)
sudo ethtool -K $IFACE_NAME rx off tx off gso off tso off gro off lro off 2>/dev/null
sudo ip link set $IFACE_NAME promisc on
sudo ip link set $IFACE_NAME mtu 1350

# 清除實體網卡上的 IP，避免雙網卡衝突 (rp_filter 丟包元兇)
sudo ip link set $IFACE_NAME up
sudo ip addr flush dev $IFACE_NAME 2>/dev/null || true

# 建立 macvlan-br，並將 IP「唯一」綁定在虛擬網卡上
sudo ip link add macvlan-br link $IFACE_NAME type macvlan mode bridge 2>/dev/null || true
sudo ip addr add 192.168.88.2/24 dev macvlan-br 2>/dev/null || true
sudo ip link set macvlan-br mtu 1350
sudo ip link set macvlan-br up
sudo ip route replace 192.168.88.128/25 dev macvlan-br

# 函式：設定網路與啟動 DU 
configure_and_start_du() {
    local MT_NAME=$1
    local DU_NAME=$2
    local DU_DOCKER_IP=$3

    echo -e "\n${GREEN}[Action] Setting up network for $DU_NAME ($DU_DOCKER_IP)${NC}"
    
    local MT_TUNNEL_IP=$(docker exec $MT_NAME ip -f inet addr show oaitun_ue1 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}')
    
    # CU magic command：在 donor-cu 容器加 DNAT 規則，讓回程封包能找到 MT tunnel IP
    local CU_CMD="docker exec -u 0 rfsim5g-donor-cu iptables -t nat -A OUTPUT -d $DU_DOCKER_IP -p udp --dport 2152 -j DNAT --to-destination $MT_TUNNEL_IP"
    CU_MAGIC_COMMANDS+="\n${CU_CMD}"

    if [ "$SSH_AVAILABLE" = true ]; then
        ssh $SSH_OPTS ${PC1_USER}@${PC1_IP} "$CU_CMD" 2>/dev/null \
            && echo -e "   ${GREEN}[SSH] CU DNAT rule applied: $DU_DOCKER_IP → $MT_TUNNEL_IP${NC}" \
            || echo -e "   ${RED}[SSH] 套用失敗，請手動執行：${CU_CMD}${NC}"
    fi

    docker exec -u 0 $MT_NAME sysctl -w net.ipv4.ip_forward=1 >/dev/null
    docker exec -u 0 $MT_NAME iptables -t nat -F PREROUTING
    docker exec -u 0 $MT_NAME iptables -t nat -F POSTROUTING
    docker exec -u 0 $MT_NAME iptables -t nat -A POSTROUTING -s $DU_DOCKER_IP -o oaitun_ue1 -j MASQUERADE
    docker exec -u 0 $MT_NAME iptables -t nat -A PREROUTING -i oaitun_ue1 -p sctp -j DNAT --to-destination $DU_DOCKER_IP
    docker exec -u 0 $MT_NAME iptables -t nat -A PREROUTING -i oaitun_ue1 -p udp --dport 2152 -j DNAT --to-destination $DU_DOCKER_IP
    
    # 讓 MT 知道核心網網段與資料網段怎麼走
    docker exec -u 0 $MT_NAME ip route replace $CN_SUBNET via 12.1.1.1 dev oaitun_ue1
    docker exec -u 0 $MT_NAME ip route replace $DN_SUBNET via 12.1.1.1 dev oaitun_ue1
    
    docker exec -u 0 $MT_NAME ethtool -K oaitun_ue1 tx off 2>/dev/null || true
    docker exec -u 0 $MT_NAME ip link set oaitun_ue1 mtu 1300 2>/dev/null

    docker exec -u 0 $MT_NAME ip route del default 2>/dev/null || true
    docker exec -u 0 $MT_NAME ip route add default via 12.1.1.1 dev oaitun_ue1

    echo "   -> [Docker] Starting DU: $DU_NAME"
    $DOCKER_COMPOSE -f $COMPOSE_FILE up -d --force-recreate $DU_NAME

    # 等待 DU 容器真正可執行指令（sleep 2 不夠，DU3/DU4 首批啟動較慢導致 docker exec 失敗）
    local _wait=0
    until docker exec -u 0 "$DU_NAME" true 2>/dev/null; do
        sleep 1; _wait=$((_wait+1))
        if [ $_wait -ge 20 ]; then
            echo -e "   ${RED}警告：$DU_NAME 等待逾時，路由可能未設定成功${NC}"
            break
        fi
    done

    local MT_INTERNAL_IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{"\n"}}{{end}}' $MT_NAME | grep '192.168.74' | head -n 1 | xargs)

    # [核心修正] 解決 F1AP 非對稱路由問題 (CU only)
    # 注意：RIC_IP (FlexRIC) 不設 bridge 路由，讓 E2AP 走 macvlan 直接路徑
    # FlexRIC container 無法透過 macvlan-br (192.168.88.1) 回覆 bridge IP 的封包
    # 若設此路由，SCTP COOKIE_WAIT 會永久卡住 (macvlan host isolation 限制)
    docker exec -u 0 $DU_NAME ip route replace $SERVER_IP via 192.168.74.1 2>/dev/null

    # [核心修正] 讓 DU 知道資料面流量要丟給 MT (包含核心網與 UPF 網段)
    docker exec -u 0 $DU_NAME ip route replace $CN_SUBNET via $MT_INTERNAL_IP 2>/dev/null
    docker exec -u 0 $DU_NAME ip route replace $DN_SUBNET via $MT_INTERNAL_IP 2>/dev/null
    docker exec -u 0 $DU_NAME ip route replace $UE_SUBNET via $MT_INTERNAL_IP 2>/dev/null

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
# --no-benchmark：跳過互動式 read 與 benchmark（供 run_phase4_pc2.sh 自動呼叫）
NO_BENCHMARK=false
for arg in "$@"; do [ "$arg" = "--no-benchmark" ] && NO_BENCHMARK=true; done

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
if [ "$SSH_AVAILABLE" = true ]; then
    echo -e "${GREEN}[AUTO] CU magic commands 已透過 SSH 自動套用至 PC1 ✓${NC}"
    echo -e "${YELLOW}驗證指令（在 PC1 執行）：${NC}"
    echo -e "${CYAN}docker logs flexric 2>&1 | grep -c 'E2 SETUP-REQUEST'  # 預期: 6${NC}"
else
    echo -e "${YELLOW}[ACTION REQUIRED] FINAL ROUTING FIX ON PC 1 (SERVER)${NC}"
    echo -e "${CYAN}$(echo -e "$CU_MAGIC_COMMANDS")${NC}"
fi
echo -e "${YELLOW}====================================================${NC}"

if [ "$NO_BENCHMARK" = true ]; then
    echo -e "\n${GREEN}IAB Client - All 6 UEs Ready! (benchmark skipped, run iab_perf_test.sh separately)${NC}"
    exit 0
fi

read -p "Press [Enter] to do performance tests..."

echo -e "\n${CYAN}[5/5] Running Performance Benchmarks...${NC}"
sleep 2
for i in {1..6}; do run_benchmarks "rfsim5g-end-ue-$i"; done

# 彙整報告
echo -e "\n${YELLOW}====================================================${NC}"
echo -e "${YELLOW}           全網 IAB Performance Benchmarks       ${NC}"
echo -e "${YELLOW}====================================================${NC}"

if [ "$NET_UE_COUNT" -gt 0 ]; then
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
    echo -e "${RED}Failed: No UE connected.${NC}"
fi
echo -e "${YELLOW}====================================================${NC}"

echo -e "\n${GREEN}IAB Client - All 6 UEs Ready and Tested!${NC}"