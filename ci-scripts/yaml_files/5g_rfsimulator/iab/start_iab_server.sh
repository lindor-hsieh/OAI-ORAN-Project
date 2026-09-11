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
PC2_INTERNAL_SUBNET="192.168.74.0/24"   # Node5,6 (access, PC2)
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
# 3.5 本機 RAN 節點：Node2(relay) + Node7,8(access) + UE5~8
#   2026-09-12 因 PC2 CPU 資源競爭導致 relay MT 反覆斷線重連（見
#   HISTORY.md），把這組子樹從 PC2 搬來 PC1 分擔負載。跟 PC2/PC3 的
#   configure_and_start_relay()/configure_and_start_access_du() 邏輯完全
#   相同，差別只在於 CU 就在本機，所有原本要 SSH 過去下的指令改成直接
#   本機執行，不需要 SSH。
# ==========================================
echo -e "${CYAN}[3.5/7] Launching Local RAN Nodes: Node2(relay) + Node7,8(access) + UE5~8...${NC}"

configure_and_start_local_relay() {
    local N=$1
    local MACVLAN_IP=$2
    local MT_NAME="rfsim5g-iab-mt-${N}"
    local DU_NAME="rfsim5g-iab-du-${N}"

    echo -e "\n${GREEN}[Action] relay Node${N}: 等待 MT tunnel IP...${NC}"
    local MT_TUNNEL_IP=""
    local COUNT=0
    while [ -z "$MT_TUNNEL_IP" ]; do
        MT_TUNNEL_IP=$(docker exec $MT_NAME ip -f inet addr show oaitun_ue1 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}')
        [ -z "$MT_TUNNEL_IP" ] && { sleep 2; COUNT=$((COUNT+1)); }
        [ $COUNT -ge 60 ] && { echo -e "${RED}逾時${NC}"; return 1; }
    done
    echo -e "  Node${N} tunnel IP: ${GREEN}${MT_TUNNEL_IP}${NC}"

    docker exec -u 0 $MT_NAME iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE 2>/dev/null

    sed -i "s|local_n_address = \"[0-9.]*\"|local_n_address = \"$MT_TUNNEL_IP\"|" ./conf/iab_du_node${N}.conf
    echo "   -> [Docker] Starting DU: $DU_NAME"
    $DOCKER_COMPOSE -f $COMPOSE_FILE up -d --force-recreate $DU_NAME
    sleep 5

    # CU 就在本機，直接下路由指令，不需要 SSH 通知
    sudo ip route replace ${MT_TUNNEL_IP} via ${MACVLAN_IP} dev macvlan-br 2>/dev/null \
        && echo -e "   ${GREEN}本機路由已更新：${MT_TUNNEL_IP} via ${MACVLAN_IP}${NC}"
}

configure_and_start_local_access_du() {
    local MT_NAME=$1
    local DU_NAME=$2
    local DU_DOCKER_IP=$3

    echo -e "\n${GREEN}[Action] Setting up network for $DU_NAME ($DU_DOCKER_IP)${NC}"

    local MT_TUNNEL_IP=$(docker exec $MT_NAME ip -f inet addr show oaitun_ue1 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}')

    # CU 就在本機，直接下 DNAT 指令，不需要 SSH 通知
    docker exec -u 0 rfsim5g-donor-cu iptables -t nat -A OUTPUT -d $DU_DOCKER_IP -p udp --dport 2152 -j DNAT --to-destination $MT_TUNNEL_IP \
        && echo -e "   ${GREEN}CU DNAT rule applied: $DU_DOCKER_IP → $MT_TUNNEL_IP${NC}"

    docker exec -u 0 $MT_NAME sysctl -w net.ipv4.ip_forward=1 >/dev/null
    docker exec -u 0 $MT_NAME iptables -t nat -F PREROUTING
    docker exec -u 0 $MT_NAME iptables -t nat -F POSTROUTING
    docker exec -u 0 $MT_NAME conntrack -F 2>/dev/null || true
    docker exec -u 0 $MT_NAME iptables -t nat -A POSTROUTING -s $DU_DOCKER_IP -o oaitun_ue1 -j MASQUERADE
    docker exec -u 0 $MT_NAME iptables -t nat -A PREROUTING -i oaitun_ue1 -p sctp -j DNAT --to-destination $DU_DOCKER_IP
    docker exec -u 0 $MT_NAME iptables -t nat -A PREROUTING -i oaitun_ue1 -p udp --dport 2152 -j DNAT --to-destination $DU_DOCKER_IP

    docker exec -u 0 $MT_NAME ip route replace 192.168.71.0/24 via 12.1.1.1 dev oaitun_ue1
    docker exec -u 0 $MT_NAME ip route replace 192.168.72.0/24 via 12.1.1.1 dev oaitun_ue1

    docker exec -u 0 $MT_NAME ethtool -K oaitun_ue1 tx off 2>/dev/null || true
    docker exec -u 0 $MT_NAME ip link set oaitun_ue1 mtu 1300 2>/dev/null

    docker exec -u 0 $MT_NAME ip route del default 2>/dev/null || true
    docker exec -u 0 $MT_NAME ip route add default via 12.1.1.1 dev oaitun_ue1

    echo "   -> [Docker] Starting DU: $DU_NAME"
    $DOCKER_COMPOSE -f $COMPOSE_FILE up -d --force-recreate $DU_NAME

    local _wait=0
    until docker exec -u 0 "$DU_NAME" true 2>/dev/null; do
        sleep 1; _wait=$((_wait+1))
        [ $_wait -ge 20 ] && { echo -e "   ${RED}警告：$DU_NAME 等待逾時${NC}"; break; }
    done

    local MT_INTERNAL_IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{"\n"}}{{end}}' $MT_NAME | grep '192.168.76' | head -n 1 | xargs)

    docker exec -u 0 $DU_NAME ip route replace 192.168.88.1 via 192.168.76.1 2>/dev/null
    docker exec -u 0 $DU_NAME ip route replace 192.168.71.0/24 via $MT_INTERNAL_IP 2>/dev/null
    docker exec -u 0 $DU_NAME ip route replace 192.168.72.0/24 via $MT_INTERNAL_IP 2>/dev/null
    docker exec -u 0 $DU_NAME ip route replace 12.1.1.0/24 via $MT_INTERNAL_IP 2>/dev/null

    echo -e "${YELLOW} Waiting 10s for CU F1AP stability...${NC}"
    sleep 10
}

wait_for_local_relay_du_healthy() {
    local DU_NAME=$1
    local COUNT=0
    while [ $COUNT -lt 20 ]; do
        local STATUS=$(docker inspect -f '{{.State.Status}}' "$DU_NAME" 2>/dev/null)
        local RESTARTS=$(docker inspect -f '{{.RestartCount}}' "$DU_NAME" 2>/dev/null)
        if [ "$STATUS" = "running" ]; then
            sleep 3
            local STATUS2=$(docker inspect -f '{{.State.Status}}' "$DU_NAME" 2>/dev/null)
            local RESTARTS2=$(docker inspect -f '{{.RestartCount}}' "$DU_NAME" 2>/dev/null)
            if [ "$STATUS2" = "running" ] && [ "$RESTARTS" = "$RESTARTS2" ]; then
                echo -e "   ${GREEN}$DU_NAME 已穩定運作（RestartCount=$RESTARTS2）${NC}"
                return 0
            fi
        fi
        echo -e "   ${YELLOW}$DU_NAME 尚未穩定（status=$STATUS, restarts=$RESTARTS），等待中...${NC}"
        sleep 3
        COUNT=$((COUNT+1))
    done
    echo -e "   ${RED}$DU_NAME 逾時仍未穩定，access node 可能連不上，請檢查${NC}"
    return 1
}

wait_for_local_ue() {
    local UE_NAME=$1
    echo -n "   -> Checking $UE_NAME... "
    local IP=""; local COUNT=0
    while [ -z "$IP" ]; do
        sleep 2
        IP=$(docker exec $UE_NAME ip -f inet addr show oaitun_ue1 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}')
        echo -n "."
        COUNT=$((COUNT+1))
        [ $COUNT -ge 30 ] && break
    done
    if [ ! -z "$IP" ]; then
        echo -e "${GREEN} Attached! ($IP)${NC}"
        docker exec -u 0 $UE_NAME ip link set oaitun_ue1 mtu 1200 2>/dev/null
        docker exec -u 0 $UE_NAME ip route replace default via 12.1.1.1 dev oaitun_ue1 2>/dev/null
    else
        echo -e "${RED} Not Found${NC}"
    fi
}

$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-mt-2
configure_and_start_local_relay 2 192.168.88.151
wait_for_local_relay_du_healthy rfsim5g-iab-du-2

declare -A LOCAL_ACCESS_DU_IP=( [7]="192.168.76.12" [8]="192.168.76.13" )
declare -A LOCAL_ACCESS_MT_NAME=( [7]="rfsim5g-iab-mt-7" [8]="rfsim5g-iab-mt-8" )
declare -A LOCAL_ACCESS_DU_NAME=( [7]="rfsim5g-iab-du-7" [8]="rfsim5g-iab-du-8" )

for n in 7 8; do
    $DOCKER_COMPOSE -f $COMPOSE_FILE up -d "${LOCAL_ACCESS_MT_NAME[$n]}"
    COUNT=0
    while ! docker exec "${LOCAL_ACCESS_MT_NAME[$n]}" ip -f inet addr show oaitun_ue1 2>/dev/null | grep -q "inet "; do
        sleep 5; COUNT=$((COUNT+1))
        [ $COUNT -ge 24 ] && { echo -e "   ${RED}Node${n} tunnel IP 逾時(120s)，跳過${NC}"; break; }
    done
    if docker exec "${LOCAL_ACCESS_MT_NAME[$n]}" ip -f inet addr show oaitun_ue1 2>/dev/null | grep -q "inet "; then
        configure_and_start_local_access_du "${LOCAL_ACCESS_MT_NAME[$n]}" "${LOCAL_ACCESS_DU_NAME[$n]}" "${LOCAL_ACCESS_DU_IP[$n]}"
    fi
done

for i in 5 6 7 8; do
    $DOCKER_COMPOSE -f $COMPOSE_FILE up -d "rfsim5g-end-ue-$i"
    wait_for_local_ue "rfsim5g-end-ue-$i"
done

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
#
#   這條路由改成由 PC2/PC3 的 configure_and_start_relay()「主動推送」
#   （拿到 tunnel IP 的當下透過 SSH 通知 PC1，見該腳本），不再由 PC1 這裡
#   反過來被動輪詢猜 PC2/PC3 什麼時候準備好——舊做法會在 PC2/PC3 還沒啟動
#   時卡住最多 4 個節點 × 180s = 12 分鐘的無意義等待。PC1 這裡不需要再做
#   任何事，繼續往下執行即可。
# ==========================================

# ==========================================
# 6. 最後路由與 UPF/FlexRIC 修正
# ==========================================
echo -e "${CYAN}[6/7] Finalizing Network & UPF/FlexRIC Routing...${NC}"

docker exec -u 0 rfsim5g-oai-upf bash -c "sysctl -w net.ipv4.ip_forward=1 && iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE"
sudo sysctl -w net.ipv4.ip_forward=1 > /dev/null
sudo ip route replace $UE_TUNNEL_SUBNET via $UPF_IP dev macvlan-br 2>/dev/null

# FlexRIC 回程路由：三條 internal 子網都要能回得去（PC2/PC3 各自的，
# 加上 PC1 本機新增的 Node7,8 internal 子網 192.168.76.0/24）
docker exec -u 0 flexric ip route add $PC2_INTERNAL_SUBNET via 192.168.88.1 2>/dev/null || true
docker exec -u 0 flexric ip route add $PC3_INTERNAL_SUBNET via 192.168.88.1 2>/dev/null || true
docker exec -u 0 flexric ip route add 192.168.76.0/24 via 192.168.88.1 2>/dev/null || true

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
