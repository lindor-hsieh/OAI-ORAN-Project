#!/bin/bash
# PC 1: IAB Server Script (1 donor + 4 relay + 8 access + 17 UE 三主機版)
#
# 跟舊版(雙主機)最大的差異：Node1~4(relay) 全部搬到 PC2/PC3，PC1 不再啟動任何
# relay MT/DU 容器，只負責 CN5G + FlexRIC + MongoDB + Donor CU/DU + 12 組
# xApp/inference 容器(集中在 PC1，理由見 CLAUDE.md)。Donor DU 需要跨主機接受
# 4 個 relay 節點的 rfsim client 連線，且 Donor CU 需要能路由到這 4 個 relay
# 節點動態取得的 12.1.1.x tunnel IP —— 這跟舊版「CU DNAT 陷阱」是同一類問題，
# 只是換成 relay 層、且 tunnel IP 現在要透過 SSH 去 PC2/PC3 查詢。

COMPOSE_FILE="docker-compose-iab-server.yaml"
IFACE_NAME="enxc84d44350030"

# IP 設定
PC2_IP="192.168.88.2"
PC3_IP="192.168.88.3"
UPF_IP="192.168.88.134"
UE_TUNNEL_SUBNET="12.1.1.0/24"
PC2_INTERNAL_SUBNET="192.168.74.0/24"   # Node5~8 (access, PC2)
PC3_INTERNAL_SUBNET="192.168.75.0/24"   # Node9~12 (access, PC3)
DN_CONTAINER="rfsim5g-oai-ext-dn"

PC2_USER="mcalab"
PC3_USER="lindor"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=5 -o BatchMode=yes"

if command -v docker-compose &> /dev/null; then DOCKER_COMPOSE="docker-compose"; else DOCKER_COMPOSE="docker compose"; fi

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

# ==========================================
# 1. 核心清理與 Docker 網路環境修復
# ==========================================
echo -e "${CYAN}[1/7] Clean Up...${NC}"

$DOCKER_COMPOSE -f $COMPOSE_FILE down 2>/dev/null

sudo iptables -P INPUT ACCEPT
sudo iptables -P FORWARD ACCEPT
sudo iptables -P OUTPUT ACCEPT
sudo iptables -F INPUT
sudo iptables -F FORWARD
sudo iptables -F OUTPUT

echo -e "${YELLOW}Restarting Docker to rebuild internal chains...${NC}"
sudo systemctl restart docker
sleep 5

sudo fuser -k 38472/sctp 2>/dev/null
sudo fuser -k 36421/sctp 2>/dev/null

sudo ethtool -K $IFACE_NAME rx off tx off gso off tso off gro off lro off 2>/dev/null
sudo ip link set $IFACE_NAME promisc on
sudo ip link set $IFACE_NAME mtu 1350

# ==========================================
# 2. 設定 Macvlan 與魔法路由 (PC2/PC3 各自的 internal 子網分開路由)
# ==========================================
echo -e "${CYAN}[2/7] Configuring Macvlan Bridge & Magic Routes...${NC}"
sudo ip link set $IFACE_NAME up
sudo ip addr flush dev $IFACE_NAME 2>/dev/null || true

sudo ip link del macvlan-br 2>/dev/null || true
sudo ip link add macvlan-br link $IFACE_NAME type macvlan mode bridge
sudo ip addr add 192.168.88.1/24 dev macvlan-br
sudo ip link set macvlan-br mtu 1350
sudo ip link set macvlan-br up

# 兩條 internal 子網各自指向對應主機（PC2/PC3 用不同子網，見 docker-compose-iab-pc3.yaml 註解）
sudo ip route replace $PC2_INTERNAL_SUBNET via $PC2_IP dev macvlan-br
sudo ip route replace $PC3_INTERNAL_SUBNET via $PC3_IP dev macvlan-br
sudo ip route replace 192.168.88.128/25 dev macvlan-br

# ==========================================
# 3. 啟動 O-RAN 核心網與 Donor-CU/DU
# ==========================================
echo -e "${CYAN}[3/7] Starting OAI Core, FlexRIC & MongoDB...${NC}"
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d mysql oai-amf oai-smf oai-upf oai-ext-dn oai-flexric mongodb

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
# 4. 啟動 Python 推論伺服器 (Node 1~12，全部集中在 PC1)
# ==========================================
echo -e "${CYAN}[4/7] Starting Inference Servers (Node 1~12)...${NC}"
NODES=$(seq 1 12)
INF_SERVICES=""
for i in $NODES; do INF_SERVICES="$INF_SERVICES inference-node${i}"; done
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d $INF_SERVICES
sleep 3

for i in $NODES; do
    STATUS=$(docker logs inference-node${i} 2>&1 | grep "已綁定" | tail -1)
    if [ -n "$STATUS" ]; then
        echo -e "  Node${i}: ${GREEN}ZMQ OK${NC}"
    else
        echo -e "  Node${i}: ${YELLOW}waiting...${NC}"
    fi
done

# ==========================================
# 5. 跨主機 relay tunnel IP 路由（新版 CU 陷阱：relay 層版本）
#   Node1~4 的 MT 容器在 PC2/PC3 上，取得的 12.1.1.x tunnel IP 是動態的，
#   Donor CU（在 PC1、host network）要能把 F1/GTP 封包送到這個 tunnel IP，
#   需要在 PC1 本機路由表加一條「tunnel IP 經由該 relay 的 macvlan IP」。
#   relay 的 macvlan IP 本身是固定的（.150~.153），可以直接用；
#   tunnel IP 要 SSH 去對應主機查詢。
# ==========================================
echo -e "${CYAN}[5/7] Resolving Relay Node Tunnel IPs (cross-host)...${NC}"

declare -A RELAY_MACVLAN=( [1]="192.168.88.150" [2]="192.168.88.151" [3]="192.168.88.152" [4]="192.168.88.153" )
declare -A RELAY_HOST=( [1]="$PC2_USER@$PC2_IP" [2]="$PC2_USER@$PC2_IP" [3]="$PC3_USER@$PC3_IP" [4]="$PC3_USER@$PC3_IP" )

wait_relay_tunnel_ip() {
    local N=$1
    local HOST=${RELAY_HOST[$N]}
    local IP=""
    echo -n "  Node${N} tunnel IP (via $HOST)..."
    local COUNT=0
    while [ -z "$IP" ]; do
        IP=$(ssh $SSH_OPTS $HOST "docker exec rfsim5g-iab-mt-${N} ip -f inet addr show oaitun_ue1 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}'" 2>/dev/null)
        if [ -z "$IP" ]; then
            echo -n "."
            sleep 3
            COUNT=$((COUNT+1))
            if [ $COUNT -ge 60 ]; then
                echo -e " ${RED}逾時(180s)，跳過${NC}"
                return 1
            fi
        fi
    done
    echo -e " ${GREEN}$IP${NC}"
    sudo ip route replace $IP via ${RELAY_MACVLAN[$N]} dev macvlan-br
    echo "$IP"
}

for n in 1 2 3 4; do
    wait_relay_tunnel_ip $n
done

# ==========================================
# 6. 最後路由與 UPF/FlexRIC 修正
# ==========================================
echo -e "${CYAN}[6/7] Finalizing Network & UPF/FlexRIC Routing...${NC}"

docker exec -u 0 rfsim5g-oai-upf bash -c "sysctl -w net.ipv4.ip_forward=1 && iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE"
sudo sysctl -w net.ipv4.ip_forward=1 > /dev/null
sudo ip route replace $UE_TUNNEL_SUBNET via $UPF_IP dev macvlan-br 2>/dev/null

# FlexRIC 回程路由：兩條 internal 子網都要能回得去
docker exec -u 0 flexric ip route add $PC2_INTERNAL_SUBNET via 192.168.88.1 2>/dev/null || true
docker exec -u 0 flexric ip route add $PC3_INTERNAL_SUBNET via 192.168.88.1 2>/dev/null || true

echo -e "${YELLOW}Setting up iperf3 server in $DN_CONTAINER...${NC}"
docker start $DN_CONTAINER 2>/dev/null
docker exec -d $DN_CONTAINER iperf3 -s
docker exec -u 0 $DN_CONTAINER ip route replace $UE_TUNNEL_SUBNET via $UPF_IP 2>/dev/null || true

sudo iptables -A FORWARD -p sctp --dport 36421 -j ACCEPT
sudo iptables -A FORWARD -p sctp --dport 38472 -j ACCEPT

echo -e "${GREEN}PC 1 Server Setup Complete!${NC}"

echo ""
echo -e "${CYAN}====================================================${NC}"
echo -e "${CYAN} 基礎設施已就緒，請確認 PC2/PC3 已啟動、再手動啟動 xApp${NC}"
echo -e "${CYAN}====================================================${NC}"
echo -e " 1. 確認 PC2 的 start_iab_pc2.sh、PC3 的 start_iab_pc3.sh 已執行完畢"
echo -e " 2. 確認全部 13 個 E2 SETUP 已完成（1 donor + 12 node）："
echo -e "${YELLOW}    docker logs flexric 2>&1 | grep -c 'E2 SETUP-REQUEST'${NC}"
echo -e " 3. 逐一啟動 xApp（建議每個間隔 2~3 秒）："
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    echo -e "${YELLOW}    docker compose -f $COMPOSE_FILE up -d node${i}-l-xapp${NC}"
done
echo -e "${CYAN}====================================================${NC}"
