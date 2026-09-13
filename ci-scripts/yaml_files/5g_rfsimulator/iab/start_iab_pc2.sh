#!/bin/bash
# PC 2: IAB Client Script — Node1,3,4 (relay) + Node5,6 (access) + UE1~4 + UE17
#
# 三主機版沿革（見 HISTORY.md 2026-09-12 條目）：
#   - Node2(relay)、Node7,8(access)、UE5~8 搬到 PC1（PC2 CPU 資源競爭導致
#     relay MT 反覆斷線重連）。
#   - Node3,4(relay，含直連 UE17）從 PC3 搬到這裡（PC3 同樣的問題）；
#     Node9~12(access) 仍留在 PC3，跨主機透過 macvlan 連到這裡的 relay DU
#     （機制跟 relay 跨主機連 Donor 完全相同，不需要額外設定）。

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
declare -A ACCESS_DU_IP=( [5]="192.168.74.20" [6]="192.168.74.21" )
declare -A ACCESS_MT_NAME=( [5]="rfsim5g-iab-mt-5" [6]="rfsim5g-iab-mt-6" )
declare -A ACCESS_DU_NAME=( [5]="rfsim5g-iab-du-5" [6]="rfsim5g-iab-du-6" )

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

# GTP-U(2152/2153)/SCTP 進 iab_internal_net 的永久放行規則（DOCKER-USER 永遠先於
# Docker 自動生成的 DOCKER chain 執行，且不會被 docker compose down/up 清空或改寫）。
# 用子網（192.168.74.0/24，docker-compose-iab-pc2.yaml 裡固定寫死）比對，
# 不能用 bridge 介面名稱（br-xxxxx）比對，因為每次 docker compose down 重來
# 都會重新產生一個新的隨機 bridge 名稱，寫死介面名稱的規則下次重啟就會失效。
echo -e "${CYAN}[0/6] 套用 iab_internal_net GTP-U/SCTP 永久放行規則 (DOCKER-USER)...${NC}"
INTERNAL_SUBNET="192.168.74.0/24"
for proto_args in "udp --dport 2152" "udp --dport 2153" "sctp"; do
    if ! sudo iptables -C DOCKER-USER -d "$INTERNAL_SUBNET" -p $proto_args -j ACCEPT 2>/dev/null; then
        sudo iptables -I DOCKER-USER -d "$INTERNAL_SUBNET" -p $proto_args -j ACCEPT
    fi
done
echo -e "${GREEN}  iab_internal_net GTP-U/SCTP 放行規則已就緒${NC}"

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
declare -A RELAY_MACVLAN=( [1]="192.168.88.150" [3]="192.168.88.152" [4]="192.168.88.153" )

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

    # 主動把這個 relay 的 tunnel IP 推給 PC1（讓 PC1 加一條路由：目的地是這個
    # tunnel IP 時，走 macvlan-br 從這個 relay 的 macvlan 位址出去）——改成
    # 由拿到 IP 的這一端主動推送，PC1 不用再自己輪詢猜 PC2/PC3 什麼時候好。
    if [ "$SSH_AVAILABLE" = true ]; then
        ssh $SSH_OPTS ${PC1_USER}@${PC1_IP} \
            "sudo ip route replace ${MT_TUNNEL_IP} via ${RELAY_MACVLAN[$N]} dev macvlan-br" 2>/dev/null \
            && echo -e "   ${GREEN}[SSH] 已通知 PC1：Node${N} tunnel IP 路由已更新${NC}" \
            || echo -e "   ${RED}[SSH] 通知 PC1 路由更新失敗，請手動執行${NC}"
    fi
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

wait_for_relay_du_healthy() {
    local DU_NAME=$1
    local COUNT=0
    while [ $COUNT -lt 40 ]; do
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

reapply_dnat_rules() {
    echo -e "${CYAN}[DNAT] 重新驗證 CU DNAT 規則（防止 MT tunnel IP 飄移）...${NC}"
    local CMDS="docker exec -u 0 rfsim5g-donor-cu iptables -t nat -F OUTPUT"
    local ok=true
    for n in 5 6; do
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

echo -e "${CYAN}[2/6] Launching Relay Node1,3,4（一個一個依序啟動；Node3,4 是 2026-09-12 從 PC3 搬過來分擔負載，見 HISTORY.md）...${NC}"
for n in 1 3 4; do
    $DOCKER_COMPOSE -f $COMPOSE_FILE up -d "rfsim5g-iab-mt-${n}"
    configure_and_start_relay $n
    wait_for_relay_du_healthy "rfsim5g-iab-du-${n}"
done

echo -e "${CYAN}[3/6] Launching + Deploying Access Nodes 5,6（一個一個依序，等前一個附著完成才啟動下一個）...${NC}"
for n in 5 6; do
    $DOCKER_COMPOSE -f $COMPOSE_FILE up -d "${ACCESS_MT_NAME[$n]}"
    COUNT=0
    while ! docker exec "${ACCESS_MT_NAME[$n]}" ip -f inet addr show oaitun_ue1 2>/dev/null | grep -q "inet "; do
        sleep 5; COUNT=$((COUNT+1))
        [ $COUNT -ge 60 ] && { echo -e "   ${RED}Node${n} tunnel IP 逾時(300s)，跳過${NC}"; break; }
    done
    if docker exec "${ACCESS_MT_NAME[$n]}" ip -f inet addr show oaitun_ue1 2>/dev/null | grep -q "inet "; then
        configure_and_start_access_du "${ACCESS_MT_NAME[$n]}" "${ACCESS_DU_NAME[$n]}" "${ACCESS_DU_IP[$n]}"
    fi
done

echo -e "${CYAN}Finalizing Control Plane, waiting 15s...${NC}"
sleep 15
reapply_dnat_rules

echo -e "\n${CYAN}[5/6] Launching End-UEs 1~4 + UE17（直連 Node4，一個一個依序啟動）...${NC}"
for i in 1 2 3 4 17; do
    $DOCKER_COMPOSE -f $COMPOSE_FILE up -d "rfsim5g-end-ue-$i"
    wait_for_ue "rfsim5g-end-ue-$i"
done

reapply_dnat_rules

echo -e "\n${YELLOW}====================================================${NC}"
if [ "$SSH_AVAILABLE" = true ]; then
    echo -e "${GREEN}[AUTO] CU magic commands 已透過 SSH 自動套用至 PC1 ✓${NC}"
else
    echo -e "${YELLOW}[ACTION REQUIRED] FINAL ROUTING FIX ON PC 1 (SERVER)${NC}"
    echo -e "${CYAN}$(echo -e "$CU_MAGIC_COMMANDS")${NC}"
fi
echo -e "${YELLOW}====================================================${NC}"
echo -e "\n${GREEN}IAB PC2 - Node1,3,4(relay) + Node5,6(access) + UE1~4 + UE17 Ready!${NC}"
