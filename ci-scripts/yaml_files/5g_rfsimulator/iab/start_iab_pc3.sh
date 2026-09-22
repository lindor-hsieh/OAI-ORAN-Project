#!/bin/bash
# PC 3: IAB Client Script — Node9,10,11,12 (access) + UE9~16
#
# 三主機版沿革（見 HISTORY.md 2026-09-12 條目）：
#   Node3,4(relay，含直連 UE17) 已搬到 PC2（PC3 CPU 資源競爭導致 relay MT
#   反覆斷線重連）。PC3 現在只剩 4 個 access 節點 + 它們的 UE，全部 access
#   MT 都跨主機連到 PC2 的 relay DU（透過 macvlan，機制跟 relay 跨主機連
#   Donor 完全相同——access MT 的 rfsimulator serveraddr 本來就是網段共用的
#   macvlan IP，不因為 parent relay 實際跑在哪台主機而改變，不需要額外設定）。

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

# 這 4 個 access 節點的 parent relay（Node3, Node4）現在跑在 PC2，
# 啟動前要先確認它們在 PC2 上已經穩定運作，不能只靠本機檢查。
declare -A PARENT_RELAY_OF=( [9]="rfsim5g-iab-du-3" [10]="rfsim5g-iab-du-3" [11]="rfsim5g-iab-du-4" [12]="rfsim5g-iab-du-4" )

PC1_USER="lindor"
PC1_IP="192.168.88.1"
PC2_USER="mcalab"
PC2_IP="192.168.88.2"
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
sudo ip addr add 192.168.88.3/24 dev macvlan-br 2>/dev/null || true
sudo ip link set macvlan-br mtu 1350
sudo ip link set macvlan-br up
sudo ip route replace 192.168.88.128/25 dev macvlan-br
echo -e "${GREEN}  macvlan-br 192.168.88.3/24 已就緒${NC}"

# GTP-U(2152/2153)/SCTP 進 iab_internal_net 的永久放行規則（DOCKER-USER 永遠先於
# Docker 自動生成的 DOCKER chain 執行，且不會被 docker compose down/up 清空或改寫）。
# 用子網（192.168.75.0/24，docker-compose-iab-pc3.yaml 裡固定寫死）比對，
# 不能用 bridge 介面名稱（br-xxxxx）比對，因為每次 docker compose down 重來
# 都會重新產生一個新的隨機 bridge 名稱，寫死介面名稱的規則下次重啟就會失效。
echo -e "${CYAN}[0/6] 套用 iab_internal_net GTP-U/SCTP 永久放行規則 (DOCKER-USER)...${NC}"
INTERNAL_SUBNET="192.168.75.0/24"
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

echo -e "${CYAN}[SSH] 等待 PC2 的 relay Node3,4 就緒（parent relay 現在跑在 PC2）...${NC}"
_wait=0
until ssh $SSH_OPTS ${PC2_USER}@${PC2_IP} \
    "docker inspect -f '{{.State.Status}}' rfsim5g-iab-du-3 rfsim5g-iab-du-4 2>/dev/null | grep -qv running && exit 1 || exit 0" 2>/dev/null; do
    sleep 5; _wait=$((_wait+5))
    echo -ne "\r  等待 PC2 relay Node3,4... ${_wait}s"
    if [ $_wait -ge 180 ]; then
        echo -e "\n${YELLOW}[SSH] 等待逾時，PC2 的 relay 可能還沒就緒，access 節點啟動後可能要多花時間才能附著${NC}"
        break
    fi
done
echo -e "${GREEN}  PC2 relay Node3,4 檢查完成${NC}"

configure_and_start_access_du() {
    local MT_NAME=$1
    local DU_NAME=$2
    local DU_DOCKER_IP=$3

    echo -e "\n${GREEN}[Action] Setting up network for $DU_NAME ($DU_DOCKER_IP)${NC}"

    local MT_TUNNEL_IP=$(docker exec $MT_NAME ip -f inet addr show oaitun_ue1 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}')

    local CU_CMD="docker exec -u 0 rfsim5g-donor-cu iptables -t nat -I OUTPUT 1 -d $DU_DOCKER_IP -p udp --dport 2152 -j DNAT --to-destination $MT_TUNNEL_IP"
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
docker exec -u 0 rfsim5g-donor-cu iptables -t nat -I OUTPUT 1 -d ${ACCESS_DU_IP[$n]} -p udp --dport 2152 -j DNAT --to-destination ${ip}"
    done
    if [ "$ok" = false ]; then
        echo -e "   ${RED}[DNAT] 有 MT tunnel IP 缺失${NC}"
        return 1
    fi
    # 注意：不 flush OUTPUT table，只用 -I OUTPUT 1 插入到最前面補規則——2026-09-14
    # 已確認 PC2 端原本會在 start_iab_pc2.sh 的 reapply_dnat_rules() 整批 flush OUTPUT
    # table，是「哪個主機腳本最後跑完，其他主機 access 節點就斷資料面」這個 race
    # condition 的主要來源，PC2 端已同步修掉（見 start_iab_pc2.sh 對應註解），三個
    # 主機現在都只補規則、不做整批清空。
    if [ "$SSH_AVAILABLE" = true ]; then
        ssh $SSH_OPTS ${PC1_USER}@${PC1_IP} "$CMDS" 2>/dev/null \
            && echo -e "   ${GREEN}[DNAT] 全部規則已更新 ✓${NC}" \
            || echo -e "   ${RED}[DNAT] SSH 更新失敗，請手動執行${NC}"
    fi
}

reassert_mt_routes() {
    # [2026-09-14 新增] 同 start_iab_pc2.sh 的說明：MT 的 oaitun_ue1 tunnel 偶爾會在
    # 初次設定完成後自發性重新建立 PDU session，把先前設好的 71/72/default 自訂
    # 路由重置掉，不是腳本沒跑到，是跑完之後又被重置，需要重新斷言。
    for n in 9 10 11 12; do
        docker exec -u 0 ${ACCESS_MT_NAME[$n]} ip route replace $CN_SUBNET via 12.1.1.1 dev oaitun_ue1 2>/dev/null
        docker exec -u 0 ${ACCESS_MT_NAME[$n]} ip route replace $DN_SUBNET via 12.1.1.1 dev oaitun_ue1 2>/dev/null
        docker exec -u 0 ${ACCESS_MT_NAME[$n]} ip route del default 2>/dev/null
        docker exec -u 0 ${ACCESS_MT_NAME[$n]} ip route add default via 12.1.1.1 dev oaitun_ue1 2>/dev/null
    done
}

# [2026-09-18 新增] 見 start_iab_pc2.sh 同名函式的說明：UE 自己的預設路由
# 偶爾會在 PDU session 自發重建後消失，reassert_mt_routes/reapply_dnat_rules
# 都不會動到 UE 容器本身，補進自我修復迴圈。
fix_ue_default_routes() {
    for i in "$@"; do
        docker exec -u 0 "rfsim5g-end-ue-${i}" ip route replace default via 12.1.1.1 dev oaitun_ue1 2>/dev/null
    done
}

# [2026-09-18 新增] Random Access process pool 耗盡（OAI 內部固定 4 格陣列，
# gNB_scheduler_RA.c:719 "no free RA process"）是一種路由/DNAT 重新斷言完全
# 救不回來的獨立崩潰模式，本次 session 手動排查 PC3 Node9~12 卡住時就是這個
# 根因（連續 errno(111) connection refused，`docker restart rfsim5g-iab-du-3`/
# `rfsim5g-iab-du-4` 才真正解決）。PC3 的 4 個 access DU（rfsim5g-iab-du-9~12）
# 在本機，可以直接檢查；但它們的 parent relay（Node3,4）在 PC2，PC3 這裡連不通
# 時也可能是 PC2 端那兩個 relay DU 的 RA pool 耗盡，一併透過 SSH 檢查修復。
heal_ra_exhaustion_local() {
    for du in rfsim5g-iab-du-9 rfsim5g-iab-du-10 rfsim5g-iab-du-11 rfsim5g-iab-du-12; do
        local hits
        hits=$(docker logs --since 90s "$du" 2>&1 | grep -c "no free RA process" || true)
        if [ "${hits:-0}" -gt 0 ]; then
            echo -e "   ${YELLOW}[RA-HEAL] $du 偵測到 RA process pool 耗盡（${hits} 次），重啟...${NC}"
            docker restart "$du" >/dev/null 2>&1
            sleep 10
        fi
    done
    # parent relay Node3,4 在 PC2，跨主機檢查（跟本檔案其餘跨主機操作用同一組 SSH_OPTS）
    for du in rfsim5g-iab-du-3 rfsim5g-iab-du-4; do
        local check="docker logs --since 90s $du 2>&1 | grep -c 'no free RA process' || true"
        local hits
        hits=$(ssh $SSH_OPTS ${PC2_USER}@${PC2_IP} "$check" 2>/dev/null)
        if [ "${hits:-0}" -gt 0 ]; then
            echo -e "   ${YELLOW}[RA-HEAL] $du（PC2）偵測到 RA process pool 耗盡（${hits} 次），重啟...${NC}"
            ssh $SSH_OPTS ${PC2_USER}@${PC2_IP} "docker restart $du" 2>/dev/null
            sleep 10
        fi
    done
}

verify_and_heal_ues() {
    # 自我修復迴圈，同 start_iab_pc2.sh 的版本，這裡管 UE9~16。
    local ues=(9 10 11 12 13 14 15 16)
    local ext_dn_ip="192.168.72.135"
    for attempt in 1 2 3 4 5; do
        local all_ok=true
        for i in "${ues[@]}"; do
            if ! docker exec rfsim5g-end-ue-$i ping -c 1 -W 2 $ext_dn_ip >/dev/null 2>&1; then
                all_ok=false
            fi
        done
        if [ "$all_ok" = true ]; then
            echo -e "   ${GREEN}[HEAL] 全部 UE9~16 連通性正常（第 ${attempt} 次檢查）${NC}"
            return 0
        fi
        echo -e "   ${YELLOW}[HEAL] 第 ${attempt} 次檢查發現連通性異常，重新斷言路由/DNAT 規則後等待重試...${NC}"
        reassert_mt_routes
        reapply_dnat_rules
        fix_ue_default_routes "${ues[@]}"
        if [ "$attempt" -ge 3 ]; then
            heal_ra_exhaustion_local
        fi
        sleep 15
    done
    echo -e "   ${RED}[HEAL] 重試 5 次後仍有 UE 連不通，需要人工檢查${NC}"
    return 1
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

echo -e "${CYAN}[2/6] Launching + Deploying Access Nodes 9,10,11,12（一個一個依序，parent relay 跨主機在 PC2）...${NC}"
for n in 9 10 11 12; do
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

echo -e "\n${CYAN}[3/6] Launching End-UEs 9~16（一個一個依序啟動）...${NC}"
for i in 9 10 11 12 13 14 15 16; do
    $DOCKER_COMPOSE -f $COMPOSE_FILE up -d "rfsim5g-end-ue-$i"
    wait_for_ue "rfsim5g-end-ue-$i"
done

reapply_dnat_rules

echo -e "\n${CYAN}[6/6] 驗證 + 自我修復 UE9~16 連通性...${NC}"
verify_and_heal_ues

echo -e "\n${YELLOW}====================================================${NC}"
if [ "$SSH_AVAILABLE" = true ]; then
    echo -e "${GREEN}[AUTO] CU magic commands 已透過 SSH 自動套用至 PC1 ✓${NC}"
else
    echo -e "${YELLOW}[ACTION REQUIRED] FINAL ROUTING FIX ON PC 1 (SERVER)${NC}"
    echo -e "${CYAN}$(echo -e "$CU_MAGIC_COMMANDS")${NC}"
fi
echo -e "${YELLOW}====================================================${NC}"
echo -e "\n${GREEN}IAB PC3 - Node9,10,11,12(access) + UE9~16 Ready!${NC}"
