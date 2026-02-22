#!/bin/bash
# PC 1: IAB Server Script (5G Core, Donor, Node 1, Node 2, iperf3 Server)

COMPOSE_FILE="docker-compose-iab-server.yaml" 
IFACE_NAME="enxc84d44350030" 

# PC 2 
PC2_PHYSICAL_IP="192.168.88.2"
PC2_DOCKER_SUBNET="192.168.74.0/24" 

# 核心網 IP 與網段
UPF_IP="192.168.71.134"
UE_TUNNEL_SUBNET="12.1.1.0/24" # End-UE 與 MT 的隧道網段

if command -v docker-compose &> /dev/null; then DOCKER_COMPOSE="docker-compose"; else DOCKER_COMPOSE="docker compose"; fi

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; NC='\033[0m'

echo -e "${CYAN}[0/6] Loading Kernel Modules...${NC}"
sudo modprobe sctp
sudo modprobe nf_conntrack_sctp 2>/dev/null || sudo modprobe nf_conntrack_proto_sctp 2>/dev/null

echo -e "${CYAN}[1/6] Configuring Host Network...${NC}"
# 穩定 USB 網卡傳輸
sudo ethtool -K $IFACE_NAME tx off rx off 2>/dev/null
sudo nmcli dev set $IFACE_NAME managed no 2>/dev/null || true

# 固定 IP 設定
sudo ip link set $IFACE_NAME up
sleep 1
sudo ip addr flush dev $IFACE_NAME 2>/dev/null || true
sudo ip addr add 192.168.88.1/24 dev $IFACE_NAME 2>/dev/null || true

# 強迫流量走 5G 核心網與無線隧道，而非乙太網捷徑
sudo ip route del $PC2_DOCKER_SUBNET 2>/dev/null || true

sudo sysctl -w net.ipv4.ip_forward=1 > /dev/null
sudo iptables -P FORWARD ACCEPT
sudo iptables -I DOCKER-USER -j ACCEPT
sudo iptables -I INPUT -p sctp -j ACCEPT

$DOCKER_COMPOSE -f $COMPOSE_FILE down

# 2. 啟動服務與核心
echo -e "${CYAN}[2/6] Starting Core & Donor...${NC}"
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d mysql oai-amf oai-smf oai-upf oai-ext-dn rfsim5g-donor-cu rfsim5g-donor-du oai-flexric

echo -e "${YELLOW}Waiting 25s for stabilization...${NC}"
sleep 25

# 為 CU 安裝工具並設定 5G 隧道出口
docker exec -u 0 rfsim5g-donor-cu apt-get update >/dev/null 2>&1
docker exec -u 0 rfsim5g-donor-cu apt-get install -y iptables >/dev/null 2>&1
docker exec -u 0 rfsim5g-donor-cu ip route replace $UE_TUNNEL_SUBNET via $UPF_IP dev eth0 2>/dev/null || true

# 3. 啟動 Ext-DN 伺服器測速模式
echo -e "${CYAN}[3/6] Initializing DN Benchmark Server Mode...${NC}"
# 關鍵：告訴 Ext-DN 回應封包要送回 UPF (72.134)
docker exec -u 0 rfsim5g-oai-ext-dn ip route replace $UE_TUNNEL_SUBNET via 192.168.72.134 dev eth0 2>/dev/null || true

# 清理舊進程並啟動伺服器
docker exec -u 0 rfsim5g-oai-ext-dn pkill iperf3 2>/dev/null || true
docker exec -d rfsim5g-oai-ext-dn iperf3 -s
echo -e "${GREEN}   -> iperf3 server ready on Ext-DN (192.168.72.135)${NC}"

# 4. 啟動 Node 1 (Local)
echo -e "${CYAN}[4/6] Starting Node 1...${NC}"

wait_for_ip() {
    local CONTAINER=$1; local VAR_NAME=$2; local IP=""
    echo -n "Waiting for $CONTAINER..."
    while [ -z "$IP" ]; do
        sleep 2; echo -n "."
        IP=$(docker exec $CONTAINER ip -f inet addr show oaitun_ue1 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}')
    done
    echo " IP: $IP"
    eval "$VAR_NAME='$IP'"
    docker exec -u 0 $CONTAINER sysctl -w net.ipv4.ip_forward=1 >/dev/null
    docker exec -u 0 $CONTAINER iptables -t nat -A POSTROUTING -o oaitun_ue1 -j MASQUERADE
}

$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-mt
wait_for_ip "rfsim5g-iab-mt" MT1_IP
sed -i "s/local_n_address *= *\".*\";/local_n_address = \"$MT1_IP\";/" ./conf/iab_du.conf
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-du

# 5. 啟動 Node 2 (Local)
echo -e "${CYAN}[5/6] Starting Node 2...${NC}"
sleep 5
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-mt-2
wait_for_ip "rfsim5g-iab-mt-2" MT2_IP
sed -i "s/local_n_address *= *\".*\";/local_n_address = \"$MT2_IP\";/" ./conf/iab_du_2.conf
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-du-2

# 6. 最終檢查
echo -e "${CYAN}[6/6] Finalizing Routing...${NC}"
# 確保 UPF 開啟 NAT 轉發
docker exec -u 0 rfsim5g-oai-upf bash -c "sysctl -w net.ipv4.ip_forward=1 && iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE"
# 確保宿主機可以找得到 UE 隧道網段
sudo ip route replace $UE_TUNNEL_SUBNET via $UPF_IP 2>/dev/null || true

echo -e "${GREEN}Server Setup Complete !${NC}"
echo -e "Layer 1 IPs: Node1=$MT1_IP, Node2=$MT2_IP"
echo -e "Benchmark Target: ${YELLOW}192.168.72.135${NC}"