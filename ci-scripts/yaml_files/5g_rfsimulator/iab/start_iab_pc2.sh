#!/bin/bash
# PC 2: IAB Client Script — Node1,2 (relay) + Node5,6,7,8 (access) + UE1~8
#
# 三主機版：PC2 現在同時扛 2 個 relay 節點(直連 Donor，不需要 DNAT，因為
# relay 的 DU 跟 MT 共用同一個 netns/IP，沒有另一個要轉換的位址) 與 4 個
# access 節點(需要 DNAT，邏輯跟舊版 Node3/4/5 完全一樣)。

COMPOSE_FILE="docker-compose-iab-pc2.yaml"
IFACE_NAME="enxc84d44350008"

# IP 設定
CLIENT_IP="192.168.88.2"
SERVER_IP="192.168.88.1"
CN_SUBNET="192.168.71.0/24"
UE_SUBNET="12.1.1.0/24"
UPF_DN_IP="192.168.72.135"
DN_SUBNET="192.168.72.0/24"
RIC_IP="192.168.88.141"

# Access node DU 的 internal-bridge IP（需要 CU DNAT trap）
declare -A ACCESS_DU_IP=( [5]="192.168.74.20" [6]="192.168.74.21" [7]="192.168.74.22" [8]="192.168.74.23" )
declare -A ACCESS_MT_NAME=( [5]="rfsim5g-iab-mt-5" [6]="rfsim5g-iab-mt-6" [7]="rfsim5g-iab-mt-7" [8]="rfsim5g-iab-mt-8" )
declare -A ACCESS_DU_NAME=( [5]="rfsim5g-iab-du-5" [6]="rfsim5g-iab-du-6" [7]="rfsim5g-iab-du-7" [8]="rfsim5g-iab-du-8" )

# PC1 SSH 設定
PC1_USER="lindor"
PC1_IP="192.168.88.1"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=5 -o BatchMode=yes"

if docker compose version &> /dev/null; then DOCKER_COMPOSE="docker compose"; else DOCKER_COMPOSE="docker-compose"; fi

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; NC='\033[0m'

SSH_AVAILABLE=false
CU_MAGIC_COMMANDS="docker exec -u 0 rfsim5g-donor-cu iptables -t nat -F OUTPUT"

echo -e "${CYAN}[0/6] Loading Kernel Modules & Macvlan Prep...${NC}"
sudo modprobe sctp
sudo modprobe nf_conntrack_sctp 2>/dev/null || sudo modprobe nf_conntrack_proto_sctp 2>/dev/null

sudo ethtool -K $IFACE_NAME rx off tx off gso off tso off gro off lro off 2>/dev/null
sudo ip link set $IFACE_NAME promisc on
sudo ip link set $IFACE_NAME mtu 1350
sudo ip link set $IFACE_NAME up
sudo ip addr flush dev $IFACE_NAME 2>/dev/null || true

sudo ip link add macvlan-br link $IFACE_NAME type macvlan mode bridge 2>/dev/null || true
sudo ip addr add 192.168.88.2/24 dev macvlan-br 2>/dev/null || true
sudo ip link set macvlan-br mtu 1350
sudo ip link set macvlan-br up
sudo ip route replace 192.168.88.128/25 dev macvlan-br
echo -e "${GREEN}  macvlan-br 192.168.88.2/24 已就緒${NC}"

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
        "docker exec -u 0 rfsim5g-donor-cu iptables -t nat -F OUTPUT" 2>/dev/null; do
        sleep 3; _wait=$((_wait+3))
        echo -ne "\r  等待 CU... ${_wait}s"
        if [ $_wait -ge 180 ]; then
            echo -e "\n${RED}[SSH] 等待 CU 超時！請確認 PC1 的 start_iab_server.sh 已先執行${NC}"
            exit 1
        fi
    done
    echo -e "${GREEN}[SSH] CU NAT OUTPUT table 已清空${NC}"
    ssh $SSH_OPTS ${PC1_USER}@${PC1_IP} \
        "docker exec -u 0 rfsim5g-donor-cu conntrack -F 2>/dev/null || true" 2>/dev/null
    ssh $SSH_OPTS ${PC1_USER}@${PC1_IP} \
        'docker exec -u 0 rfsim5g-donor-cu bash -c "ip route show | awk \"/^12\\.1\\.1\\.[0-9]+ /{print \$1}\" | while read r; do ip route del \$r 2>/dev/null; done"' 2>/dev/null
    echo -e "${GREEN}[SSH] CU conntrack / stale routes 已清空${NC}"
fi

# 函式：relay 節點（MT+DU 共用 netns，不需要 DNAT，只需要把 tunnel IP 動態寫回 DU conf）
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

# 函式：access 節點（跟舊版 Node3/4/5 邏輯相同，需要 DNAT trap）
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

    local MT_INTERNAL_IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{"\n"}}{{end}}' $MT_NAME | grep '192.168.74' | head -n 1 | xargs)

    docker exec -u 0 $DU_NAME ip route replace $SERVER_IP via 192.168.74.1 2>/dev/null
    docker exec -u 0 $DU_NAME ip route replace $CN_SUBNET via $MT_INTERNAL_IP 2>/dev/null
    docker exec -u 0 $DU_NAME ip route replace $DN_SUBNET via $MT_INTERNAL_IP 2>/dev/null
    docker exec -u 0 $DU_NAME ip route replace $UE_SUBNET via $MT_INTERNAL_IP 2>/dev/null

    echo -e "${YELLOW} Waiting 10s for CU F1AP stability...${NC}"
    sleep 10
}

reapply_dnat_rules() {
    echo -e "${CYAN}[DNAT] 重新驗證 CU DNAT 規則（防止 MT tunnel IP 飄移）...${NC}"
    local CMDS="docker exec -u 0 rfsim5g-donor-cu iptables -t nat -F OUTPUT"
    local ok=true
    for n in 5 6 7 8; do
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
echo -e "${CYAN}[1/6] Host Network Prep...${NC}"
sudo sysctl -w net.ipv4.ip_forward=1 > /dev/null

$DOCKER_COMPOSE -f $COMPOSE_FILE down

echo -e "${CYAN}[2/6] Launching Relay Node1, Node2 (直連 Donor)...${NC}"
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-mt-1 rfsim5g-iab-mt-2
configure_and_start_relay 1
configure_and_start_relay 2
echo -e "${YELLOW}Waiting 10s for relay DU F1AP stability...${NC}"
sleep 10

echo -e "${CYAN}[3/6] Launching Access MTs (Node5,6,7,8)...${NC}"
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-mt-5 rfsim5g-iab-mt-6 rfsim5g-iab-mt-7 rfsim5g-iab-mt-8

echo -e "${CYAN}[4/6] Deploying Access DUs Sequentially...${NC}"
DONE=(0 0 0 0); NODES=(5 6 7 8)
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

echo -e "\n${CYAN}[5/6] Launching All 8 End-UEs...${NC}"
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-end-ue-1 rfsim5g-end-ue-2 rfsim5g-end-ue-3 rfsim5g-end-ue-4 rfsim5g-end-ue-5 rfsim5g-end-ue-6 rfsim5g-end-ue-7 rfsim5g-end-ue-8
for i in {1..8}; do wait_for_ue "rfsim5g-end-ue-$i"; done

reapply_dnat_rules

echo -e "\n${YELLOW}====================================================${NC}"
if [ "$SSH_AVAILABLE" = true ]; then
    echo -e "${GREEN}[AUTO] CU magic commands 已透過 SSH 自動套用至 PC1 ✓${NC}"
else
    echo -e "${YELLOW}[ACTION REQUIRED] FINAL ROUTING FIX ON PC 1 (SERVER)${NC}"
    echo -e "${CYAN}$(echo -e "$CU_MAGIC_COMMANDS")${NC}"
fi
echo -e "${YELLOW}====================================================${NC}"
echo -e "\n${GREEN}IAB PC2 - Node1,2(relay) + Node5,6,7,8(access) + UE1~8 Ready!${NC}"
