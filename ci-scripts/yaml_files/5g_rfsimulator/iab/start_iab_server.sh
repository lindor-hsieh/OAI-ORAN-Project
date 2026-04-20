#!/bin/bash
# PC 1: IAB Server Script 

COMPOSE_FILE="docker-compose-iab-server.yaml" 
IFACE_NAME="enxc84d44350030" 

# IP 設定 (需與 PC 2 同步)
PC2_IP="192.168.88.2"
UPF_IP="192.168.88.134"
UE_TUNNEL_SUBNET="12.1.1.0/24" 
IAB_INTERNAL_SUBNET="192.168.74.0/24"
DN_CONTAINER="rfsim5g-oai-ext-dn"

if command -v docker-compose &> /dev/null; then DOCKER_COMPOSE="docker-compose"; else DOCKER_COMPOSE="docker compose"; fi

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

# ==========================================
# 1. 核心清理與 Docker 網路環境修復
# ==========================================
echo -e "${CYAN}[1/8] Clean Up...${NC}"

# 先停止所有容器，避免網路介面被佔用
$DOCKER_COMPOSE -f $COMPOSE_FILE down 2>/dev/null

# [核心修正] 針對性清理防火牆規則，禁止使用 iptables -X 或 iptables -F (全清)
# 這樣可以保留 Docker 的 DOCKER-FORWARD 等鏈，避免建立網路失敗
sudo iptables -P INPUT ACCEPT
sudo iptables -P FORWARD ACCEPT
sudo iptables -P OUTPUT ACCEPT
sudo iptables -F INPUT
sudo iptables -F FORWARD
sudo iptables -F OUTPUT

# 解決 "No chain/target/match by that name" 錯誤：重啟 Docker 引擎重建鏈
echo -e "${YELLOW}Restarting Docker to rebuild internal chains...${NC}"
sudo systemctl restart docker
sleep 5

# 殺死所有可能卡住端口的殭屍行程
sudo fuser -k 38472/sctp 2>/dev/null
sudo fuser -k 36421/sctp 2>/dev/null

# 網卡效能與模式調優
sudo ethtool -K $IFACE_NAME rx off tx off gso off tso off gro off lro off 2>/dev/null
sudo ip link set $IFACE_NAME promisc on
sudo ip link set $IFACE_NAME mtu 1350

# ==========================================
# 2. 設定 Macvlan 與魔法路由 (解決跨機連線問題)
# ==========================================
echo -e "${CYAN}[2/8] Configuring Macvlan Bridge & Magic Route...${NC}"
sudo ip link set $IFACE_NAME up

# 清除實體網卡上的 IP，避免雙網卡衝突
sudo ip addr flush dev $IFACE_NAME 2>/dev/null || true

# 建立 macvlan-br，並將 IP「唯一」綁定在虛擬網卡上
sudo ip link add macvlan-br link $IFACE_NAME type macvlan mode bridge 2>/dev/null || true
sudo ip addr add 192.168.88.1/24 dev macvlan-br 2>/dev/null || true
sudo ip link set macvlan-br mtu 1350
sudo ip link set macvlan-br up

# 魔法路由：告訴 PC 1，要去 PC 2 內部的 .74 網段，必須透過 macvlan-br 找 192.168.88.2
sudo ip route replace $IAB_INTERNAL_SUBNET via $PC2_IP dev macvlan-br
sudo ip route replace 192.168.88.128/25 dev macvlan-br

# ==========================================
# 3. 啟動 O-RAN 核心網與 Donor-CU
# ==========================================
echo -e "${CYAN}[3/8] Starting OAI Core, FlexRIC & MongoDB...${NC}"
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d mysql oai-amf oai-smf oai-upf oai-ext-dn oai-flexric mongodb

# 等待 MongoDB healthy 再繼續
echo -e "${YELLOW}Waiting for MongoDB to be ready...${NC}"
until [ "$($DOCKER_COMPOSE -f $COMPOSE_FILE ps -q mongodb | xargs docker inspect -f '{{.State.Health.Status}}' 2>/dev/null)" = "healthy" ]; do
    echo -n "."; sleep 3
done
echo -e " ${GREEN}MongoDB ready${NC}"

sleep 5
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-donor-cu
echo -e "${YELLOW}Waiting for CU initialization (15s)...${NC}"
sleep 15
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-donor-du

# ==========================================
# 4. 啟動 Python 推論伺服器 (Node 1~5)
# ==========================================
echo -e "${CYAN}[4/8] Starting Inference Servers (Node 1~5)...${NC}"
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d \
    inference-node1 inference-node2 inference-node3 \
    inference-node4 inference-node5
sleep 3

# 確認全部 ZMQ socket 已綁定
for i in 1 2 3 4 5; do
    STATUS=$(docker logs inference-node${i} 2>&1 | grep "已綁定" | tail -1)
    if [ -n "$STATUS" ]; then
        echo -e "  Node${i}: ${GREEN}ZMQ OK${NC}"
    else
        echo -e "  Node${i}: ${YELLOW}waiting...${NC}"
    fi
done

# ==========================================
# 函式：等待隧道 IP
# ==========================================
wait_for_ip() {
    local CONTAINER=$1; local VAR_NAME=$2; local IP=""
    echo -n "Waiting for $CONTAINER..."
    while [ -z "$IP" ]; do
        sleep 2; echo -n "."
        IP=$(docker exec $CONTAINER ip -f inet addr show oaitun_ue1 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}')
    done
    echo " IP: $IP"
    eval "$VAR_NAME='$IP'"
}

# ==========================================
# 4. 啟動本地 Node 1 (ID: 3585)
# ==========================================
echo -e "${CYAN}[5/8] Launching Local Node 1...${NC}"
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-mt
wait_for_ip "rfsim5g-iab-mt" MT1_TUNNEL_IP

MT1_MACVLAN_IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' rfsim5g-iab-mt | grep 192.168.88)
MT1_MAC=$(docker exec rfsim5g-iab-mt cat /sys/class/net/eth0/address)

docker exec -u 0 rfsim5g-iab-mt iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE
sudo arp -s $MT1_MACVLAN_IP $MT1_MAC -i macvlan-br
sudo ip route replace $MT1_TUNNEL_IP via $MT1_MACVLAN_IP dev macvlan-br

# 動態更新 DU-1 conf 的 local_n_address 為 MT-1 實際取得的 tunnel IP
sed -i "s|local_n_address = \"[0-9.]*\"|local_n_address = \"$MT1_TUNNEL_IP\"|" ./conf/iab_du.conf
echo -e "  Node1 DU local_n_address updated to ${GREEN}$MT1_TUNNEL_IP${NC}"

$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-du
sleep 3
docker exec -d rfsim5g-iab-du /opt/oai-gnb/bin/nr-softmodem -O /opt/oai-gnb/etc/gnb.conf --rfsim --SCTP.local_portc 38473 --telnetsrv --telnetsrv.listenport 9089 --log_config.global_log_level info

# ==========================================
# 5. 啟動本地 Node 2 (ID: 3586)
# ==========================================
echo -e "${CYAN}[6/8] Launching Local Node 2...${NC}"
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-mt-2
wait_for_ip "rfsim5g-iab-mt-2" MT2_TUNNEL_IP

MT2_MACVLAN_IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' rfsim5g-iab-mt-2 | grep 192.168.88)
MT2_MAC=$(docker exec rfsim5g-iab-mt-2 cat /sys/class/net/eth0/address)

docker exec -u 0 rfsim5g-iab-mt-2 iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE
sudo arp -s $MT2_MACVLAN_IP $MT2_MAC -i macvlan-br
sudo ip route replace $MT2_TUNNEL_IP via $MT2_MACVLAN_IP dev macvlan-br

# 動態更新 DU-2 conf 的 local_n_address 為 MT-2 實際取得的 tunnel IP
sed -i "s|local_n_address = \"[0-9.]*\"|local_n_address = \"$MT2_TUNNEL_IP\"|" ./conf/iab_du_2.conf
echo -e "  Node2 DU local_n_address updated to ${GREEN}$MT2_TUNNEL_IP${NC}"

$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-du-2
sleep 3
docker exec -d rfsim5g-iab-du-2 /opt/oai-gnb/bin/nr-softmodem -O /opt/oai-gnb/etc/gnb.conf --rfsim --SCTP.local_portc 38474 --telnetsrv --telnetsrv.listenport 9090 --log_config.global_log_level info

# ==========================================
# 6. 最後路由與 UPF/FlexRIC 修正
# ==========================================
echo -e "${CYAN}[7/8] Finalizing Network & UPF/FlexRIC Routing...${NC}"

# UPF 轉發設定
docker exec -u 0 oai-upf bash -c "sysctl -w net.ipv4.ip_forward=1 && iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE"
sudo sysctl -w net.ipv4.ip_forward=1 > /dev/null
sudo ip route replace $UE_TUNNEL_SUBNET via $UPF_IP dev macvlan-br 2>/dev/null

# [核心修正] FlexRIC 回程路由
# 讓 FlexRIC 容器知道如何把 E2-Response 回傳給 PC 2 的內部網段
# 注意：如果容器內沒有 ip 指令，這行會報錯，但我們加上 || true 確保腳本不中斷
docker exec -u 0 flexric ip route add $IAB_INTERNAL_SUBNET via 192.168.88.1 2>/dev/null || true

# [核心修正] 處理 DN (iperf3 Server) 容器
echo -e "${YELLOW}Setting up iperf3 server in $DN_CONTAINER...${NC}"
docker start $DN_CONTAINER 2>/dev/null
# 啟動 iperf3 監聽服務端 (背景執行)
docker exec -d $DN_CONTAINER iperf3 -s
# 補上 DN 內部的回程路由，讓資料能回傳給 UE
docker exec -u 0 $DN_CONTAINER ip route replace $UE_TUNNEL_SUBNET via $UPF_IP 2>/dev/null || true

# 確保 SCTP 端口在 FORWARD 鏈是被允許的
sudo iptables -A FORWARD -p sctp --dport 36421 -j ACCEPT
sudo iptables -A FORWARD -p sctp --dport 38472 -j ACCEPT

echo -e "${GREEN}PC 1 Server Setup Complete!${NC}"

echo ""
echo -e "${CYAN}====================================================${NC}"
echo -e "${CYAN} 基礎設施已就緒，請手動啟動 xApp${NC}"
echo -e "${CYAN}====================================================${NC}"
echo -e " 1. 確認 PC 2 的 start_iab_client.sh 已執行完畢"
echo -e " 2. 確認所有 6 個 E2 SETUP 已完成："
echo -e "${YELLOW}    docker logs flexric 2>&1 | grep -c 'E2 SETUP-REQUEST'${NC}"
echo -e " 3. 逐一啟動 xApp（建議每個間隔 2~3 秒）："
echo -e "${YELLOW}    docker compose -f $COMPOSE_FILE up -d node1-l-xapp${NC}"
echo -e "${YELLOW}    docker compose -f $COMPOSE_FILE up -d node2-l-xapp${NC}"
echo -e "${YELLOW}    docker compose -f $COMPOSE_FILE up -d node3-l-xapp${NC}"
echo -e "${YELLOW}    docker compose -f $COMPOSE_FILE up -d node4-l-xapp${NC}"
echo -e "${YELLOW}    docker compose -f $COMPOSE_FILE up -d node5-l-xapp${NC}"
echo -e "${CYAN}====================================================${NC}"