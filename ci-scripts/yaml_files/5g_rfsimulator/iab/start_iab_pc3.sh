#!/bin/bash
# PC 3: IAB Client Script — Node3,4 (relay) + Node9,10,11,12 (access) + UE9~17
#
# UE17 是額外那顆直接掛在 Node4(relay)底下的 UE，不經過任何 access node，
# 也不需要 DNAT trap（relay 的 DU 位址就是 MT 自己的 tunnel IP，PC1 端的
# start_iab_server.sh 已經處理好路由，PC3 這邊只要正常啟動 UE17 容器即可）。

COMPOSE_FILE="docker-compose-iab-pc3.yaml"
IFACE_NAME="enxc84d4427aa8f"

CLIENT_IP="192.168.88.3"
SERVER_IP="192.168.88.1"
CN_SUBNET="192.168.71.0/24"
UE_SUBNET="12.1.1.0/24"
UPF_DN_IP="192.168.72.135"
DN_SUBNET="192.168.72.0/24"
RIC_IP="192.168.88.141"
IAB_INTERNAL_GW="192.168.75.1"   # PC3 專屬 internal bridge（跟 PC2 的 192.168.74.0/24 分開）

declare -A ACCESS_DU_IP=( [9]="192.168.75.20" [10]="192.168.75.21" [11]="192.168.75.22" [12]="192.168.75.23" )
declare -A ACCESS_MT_NAME=( [9]="rfsim5g-iab-mt-9" [10]="rfsim5g-iab-mt-10" [11]="rfsim5g-iab-mt-11" [12]="rfsim5g-iab-mt-12" )
declare -A ACCESS_DU_NAME=( [9]="rfsim5g-iab-du-9" [10]="rfsim5g-iab-du-10" [11]="rfsim5g-iab-du-11" [12]="rfsim5g-iab-du-12" )

PC1_USER="lindor"
PC1_IP="192.168.88.1"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=5 -o BatchMode=yes"

if docker compose version &> /dev/null; then DOCKER_COMPOSE="docker compose"; else DOCKER_COMPOSE="docker-compose"; fi

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; NC='\033[0m'

SSH_AVAILABLE=false
CU_MAGIC_COMMANDS="docker exec -u 0 rfsim5g-donor-cu iptables -t nat -F OUTPUT"

echo -e "${CYAN}[0/7] Loading Kernel Modules & Macvlan Prep...${NC}"
sudo modprobe sctp
sudo modprobe nf_conntrack_sctp 2>/dev/null || sudo modprobe nf_conntrack_proto_sctp 2>/dev/null

sudo ethtool -K $IFACE_NAME rx off tx off gso off tso off gro off lro off 2>/dev/null
sudo ip link set $IFACE_NAME promisc on
sudo ip link set $IFACE_NAME mtu 1350
sudo ip link set $IFACE_NAME up
sudo ip addr flush dev $IFACE_NAME 2>/dev/null || true

sudo ip link add macvlan-br link $IFACE_NAME type macvlan mode bridge 2>/dev/null || true
sudo ip addr add 192.168.88.3/24 dev macvlan-br 2>/dev/null || true
sudo ip link set macvlan-br mtu 1350
sudo ip link set macvlan-br up
sudo ip route replace 192.168.88.128/25 dev macvlan-br
echo -e "${GREEN}  macvlan-br 192.168.88.3/24 已就緒${NC}"

echo -e "${CYAN}[SSH] 等待 PC1 SSH 連線...${NC}"
_wait=0
until ssh $SSH_OPTS ${PC1_USER}@${PC1_IP} "exit" 2>/dev/null; do
    sleep 3; _wait=$((_wait+3))
    echo -ne "\r  等待 PC1 SSH... ${_wait}s"
    if [ $_wait -ge 120 ]; then
        echo -e "\n${YELLOW}[SSH] 無法連線 PC1，將改為印出 CU magic commands 供手動執行${NC}"
        break
    fi
done

if ssh $SSH_OPTS ${PC1_USER}@${PC1_IP} "exit" 2>/dev/null; then
    SSH_AVAILABLE=true
    echo -e "${GREEN}[SSH] PC1 SSH 連線可用${NC}"

    echo -e "${CYAN}[SSH] 等待 rfsim5g-donor-cu 就緒...${NC}"
    _wait=0
    until ssh $SSH_OPTS ${PC1_USER}@${PC1_IP} \
        "docker exec -u 0 rfsim5g-donor-cu true" 2>/dev/null; do
        sleep 3; _wait=$((_wait+3))
        echo -ne "\r  等待 CU... ${_wait}s"
        if [ $_wait -ge 180 ]; then
            echo -e "\n${RED}[SSH] 等待 CU 超時！請確認 PC1 的 start_iab_server.sh 已先執行${NC}"
            exit 1
        fi
    done
    # 注意：這裡不再 flush OUTPUT table（PC2 的腳本已經做過一次；連續 flush
    # 兩次沒有額外壞處，但為了避免跟 PC2 的清空時序互相打架，PC3 只補規則不 flush）
    echo -e "${GREEN}[SSH] PC1 CU 已就緒${NC}"
fi

configure_and_start_relay() {
    local N=$1
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
}

configure_and_start_access_du() {
    local MT_NAME=$1
    local DU_NAME=$2
    local DU_DOCKER_IP=$3

    echo -e "\n${GREEN}[Action] Setting up network for $DU_NAME ($DU_DOCKER_IP)${NC}"

    local MT_TUNNEL_IP=$(docker exec $MT_NAME ip -f inet addr show oaitun_ue1 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}')

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
    docker exec -u 0 $MT_NAME conntrack -F 2>/dev/null || true
    docker exec -u 0 $MT_NAME iptables -t nat -A POSTROUTING -s $DU_DOCKER_IP -o oaitun_ue1 -j MASQUERADE
    docker exec -u 0 $MT_NAME iptables -t nat -A PREROUTING -i oaitun_ue1 -p sctp -j DNAT --to-destination $DU_DOCKER_IP
    docker exec -u 0 $MT_NAME iptables -t nat -A PREROUTING -i oaitun_ue1 -p udp --dport 2152 -j DNAT --to-destination $DU_DOCKER_IP

    docker exec -u 0 $MT_NAME ip route replace $CN_SUBNET via 12.1.1.1 dev oaitun_ue1
    docker exec -u 0 $MT_NAME ip route replace $DN_SUBNET via 12.1.1.1 dev oaitun_ue1

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

    local MT_INTERNAL_IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{"\n"}}{{end}}' $MT_NAME | grep '192.168.75' | head -n 1 | xargs)

    docker exec -u 0 $DU_NAME ip route replace $SERVER_IP via $IAB_INTERNAL_GW 2>/dev/null
    docker exec -u 0 $DU_NAME ip route replace $CN_SUBNET via $MT_INTERNAL_IP 2>/dev/null
    docker exec -u 0 $DU_NAME ip route replace $DN_SUBNET via $MT_INTERNAL_IP 2>/dev/null
    docker exec -u 0 $DU_NAME ip route replace $UE_SUBNET via $MT_INTERNAL_IP 2>/dev/null

    echo -e "${YELLOW} Waiting 10s for CU F1AP stability...${NC}"
    sleep 10
}

reapply_dnat_rules() {
    echo -e "${CYAN}[DNAT] 重新驗證 CU DNAT 規則（防止 MT tunnel IP 飄移）...${NC}"
    local CMDS=""
    local ok=true
    for n in 9 10 11 12; do
        local ip=$(docker exec ${ACCESS_MT_NAME[$n]} ip -f inet addr show oaitun_ue1 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}')
        echo -e "   DU${n} (${ACCESS_DU_IP[$n]}) → MT${n} tunnel: ${ip:-MISSING}"
        [ -z "$ip" ] && ok=false
        CMDS="$CMDS
docker exec -u 0 rfsim5g-donor-cu iptables -t nat -A OUTPUT -d ${ACCESS_DU_IP[$n]} -p udp --dport 2152 -j DNAT --to-destination ${ip}"
    done
    if [ "$ok" = false ]; then
        echo -e "   ${RED}[DNAT] 有 MT tunnel IP 缺失${NC}"
        return 1
    fi
    # 注意：不 flush OUTPUT table，只補規則（PC2 那邊已經 flush 過，避免互相清掉對方的規則）
    if [ "$SSH_AVAILABLE" = true ]; then
        ssh $SSH_OPTS ${PC1_USER}@${PC1_IP} "$CMDS" 2>/dev/null \
            && echo -e "   ${GREEN}[DNAT] 全部規則已更新 ✓${NC}" \
            || echo -e "   ${RED}[DNAT] SSH 更新失敗，請手動執行${NC}"
    fi
}

wait_for_ue() {
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

# ==========================================
# 主流程
# ==========================================
echo -e "${CYAN}[1/7] Host Network Prep...${NC}"
sudo sysctl -w net.ipv4.ip_forward=1 > /dev/null

$DOCKER_COMPOSE -f $COMPOSE_FILE down

echo -e "${CYAN}[2/7] Launching Relay Node3, Node4 (直連 Donor)...${NC}"
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-mt-3 rfsim5g-iab-mt-4
configure_and_start_relay 3
configure_and_start_relay 4
echo -e "${YELLOW}Waiting 10s for relay DU F1AP stability...${NC}"
sleep 10

echo -e "${CYAN}[3/7] Launching Access MTs (Node9,10,11,12)...${NC}"
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-mt-9 rfsim5g-iab-mt-10 rfsim5g-iab-mt-11 rfsim5g-iab-mt-12

echo -e "${CYAN}[4/7] Deploying Access DUs Sequentially...${NC}"
DONE=(0 0 0 0); NODES=(9 10 11 12)
COUNT=0
while [ $COUNT -lt 60 ]; do
    all_done=true
    for i in 0 1 2 3; do
        n=${NODES[$i]}
        if [ ${DONE[$i]} -eq 0 ]; then
            if docker exec ${ACCESS_MT_NAME[$n]} ip -f inet addr show oaitun_ue1 2>/dev/null | grep -q "inet "; then
                configure_and_start_access_du "${ACCESS_MT_NAME[$n]}" "${ACCESS_DU_NAME[$n]}" "${ACCESS_DU_IP[$n]}"
                DONE[$i]=1
            else
                all_done=false
            fi
        fi
    done
    $all_done && break
    sleep 5; COUNT=$((COUNT+1))
done

echo -e "${CYAN}Finalizing Control Plane, waiting 15s...${NC}"
sleep 15
reapply_dnat_rules

echo -e "\n${CYAN}[5/7] Launching End-UEs 9~16 (access) + UE17 (直連 Node4)...${NC}"
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-end-ue-9 rfsim5g-end-ue-10 rfsim5g-end-ue-11 rfsim5g-end-ue-12 \
    rfsim5g-end-ue-13 rfsim5g-end-ue-14 rfsim5g-end-ue-15 rfsim5g-end-ue-16 rfsim5g-end-ue-17
for i in 9 10 11 12 13 14 15 16 17; do wait_for_ue "rfsim5g-end-ue-$i"; done

reapply_dnat_rules

echo -e "\n${YELLOW}====================================================${NC}"
if [ "$SSH_AVAILABLE" = true ]; then
    echo -e "${GREEN}[AUTO] CU magic commands 已透過 SSH 自動套用至 PC1 ✓${NC}"
else
    echo -e "${YELLOW}[ACTION REQUIRED] FINAL ROUTING FIX ON PC 1 (SERVER)${NC}"
    echo -e "${CYAN}$(echo -e "$CU_MAGIC_COMMANDS")${NC}"
fi
echo -e "${YELLOW}====================================================${NC}"
echo -e "\n${GREEN}IAB PC3 - Node3,4(relay) + Node9,10,11,12(access) + UE9~17 Ready!${NC}"
