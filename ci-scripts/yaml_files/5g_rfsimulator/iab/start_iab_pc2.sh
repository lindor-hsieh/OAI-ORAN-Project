#!/bin/bash
# PC 2: IAB Client Script — Node5,6,7,8 (access) + UE1~8
#
# 2026-09-22 節點重分配（見 CLAUDE.md 第 1 節、HISTORY.md 對應條目）：
#   - Node1,3,4(relay) 搬去 PC1，跟 Node2 一起全部集中到 Donor 所在主機。
#   - Node7,8(access)+UE5~8 從 PC1 搬過來這裡，跟原本就在 PC2 的
#     Node5,6(access)+UE1~4 合併成同一台主機四個 access 節點。internal-
#     bridge IP 沿用 Node7,8 原本在 PC1 時的末碼、只換網段前綴：
#     Node7=192.168.74.12/.22、Node8=192.168.74.13/.23（跟 PC2 現有的
#     Node5=.10/.20、Node6=.11/.21 同一個 192.168.74.0/24 子網，不衝突）。
#   - UE17 搬去 PC3（跟它邏輯掛的 Node4 relay 現在跑在 PC1 不同機，見
#     scenarios/traffic_scenario.py 的 UE_HOST_OVERRIDE 特例處理）。
#   - 這台主機現在不再啟動任何 relay 節點，只剩 access 節點，原本的
#     configure_and_start_relay()/SSH 通知 PC1 路由機制整層移除。

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
    # [2026-09-14 修復] 原本這裡用 `iptables -t nat -F OUTPUT`（整條 chain 全部清空）
    # 當作「CU 容器已就緒」的探測指令，副作用是無條件清空整條 OUTPUT chain——如果
    # PC3（Node9~12）的 DNAT 規則已經先寫進去，會被這裡整批砍掉，且沒有人會補回來，
    # 造成「哪個主機的腳本最後跑完，其他主機的 access 節點就斷資料面」這個会隨執行
    # 順序隨機發生、難以重現定位的 race condition（2026-09-13/14 除錯多次才定位到）。
    # 改用不具破壞性的純狀態檢查，NAT 規則的新增/覆蓋交給下面
    # configure_and_start_access_du() 用「先刪除同目的地的舊規則、再插入新規則」的
    # 冪等方式處理（見該函式），不再需要在這裡整批清空。
    until ssh $SSH_OPTS ${PC1_USER}@${PC1_IP} \
        "docker exec -u 0 rfsim5g-donor-cu true" 2>/dev/null; do
        sleep 3; _wait=$((_wait+3))
        echo -ne "\r  等待 CU... ${_wait}s"
        if [ $_wait -ge 180 ]; then
            echo -e "\n${RED}[SSH] 等待 CU 超時！請確認 PC1 的 start_iab_server.sh 已先執行${NC}"
            exit 1
        fi
    done
    echo -e "${GREEN}[SSH] rfsim5g-donor-cu 已就緒（不再清空 CU NAT OUTPUT table，避免跟其他主機互相打架）${NC}"
    ssh $SSH_OPTS ${PC1_USER}@${PC1_IP} \
        "docker exec -u 0 rfsim5g-donor-cu conntrack -F 2>/dev/null || true" 2>/dev/null
    ssh $SSH_OPTS ${PC1_USER}@${PC1_IP} \
        'docker exec -u 0 rfsim5g-donor-cu bash -c "ip route show | awk \"/^12\\.1\\.1\\.[0-9]+ /{print \$1}\" | while read r; do ip route del \$r 2>/dev/null; done"' 2>/dev/null
    echo -e "${GREEN}[SSH] CU conntrack / stale routes 已清空${NC}"
fi

# 函式：access 節點（需要 DNAT trap）
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
    # [2026-09-14 修復] 這裡原本開頭是 `iptables -t nat -F OUTPUT`（整條 chain 清空），
    # 是這一整段「PC2 主機重啟後其他主機 access 節點資料面斷線」race condition 真正的
    # 主要來源——這是 PC2 腳本執行到最後才跑的步驟，如果 PC1/PC3 已經先把自己的
    # DNAT 規則寫好，會被這裡整批砍光，且沒有人會補回來。拿掉 flush，只用
    # `-I OUTPUT 1`（插入到最前面）確保「這次真正量到的 tunnel IP」永遠贏過任何
    # 殘留的舊規則，不需要整批清空（見 configure_and_start_access_du() 的同款修法）。
    local CMDS=""
    local ok=true
    for n in 5 6 7 8; do
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
    if [ "$SSH_AVAILABLE" = true ]; then
        ssh $SSH_OPTS ${PC1_USER}@${PC1_IP} "$CMDS" 2>/dev/null \
            && echo -e "   ${GREEN}[DNAT] 全部規則已更新 ✓${NC}" \
            || echo -e "   ${RED}[DNAT] SSH 更新失敗，請手動執行${NC}"
    fi
}

reassert_mt_routes() {
    # [2026-09-14 新增] MT 的 oaitun_ue1 tunnel 偶爾會在初次設定完成後自發性
    # 重新建立 PDU session（tunnel IP 換掉、核心自動加回的路由只剩
    # `12.1.1.0/24 dev oaitun_ue1` 這條，先前用 configure_and_start_access_du()
    # 設好的 71/72/default 自訂路由會跟著消失）——這不是「腳本沒跑到」，是
    # 跑完之後又被重置，所以要在後面加一個「重新斷言」步驟，不是只加長等待時間。
    for n in 5 6 7 8; do
        docker exec -u 0 ${ACCESS_MT_NAME[$n]} ip route replace $CN_SUBNET via 12.1.1.1 dev oaitun_ue1 2>/dev/null
        docker exec -u 0 ${ACCESS_MT_NAME[$n]} ip route replace $DN_SUBNET via 12.1.1.1 dev oaitun_ue1 2>/dev/null
        docker exec -u 0 ${ACCESS_MT_NAME[$n]} ip route del default 2>/dev/null
        docker exec -u 0 ${ACCESS_MT_NAME[$n]} ip route add default via 12.1.1.1 dev oaitun_ue1 2>/dev/null
    done
}

# [2026-09-18 新增] UE 自己的預設路由偶爾會在 PDU session 自發重建後消失
# （`ip route show` 只剩 12.1.1.0/24 這條 kernel scope 路由，default 不見），
# 跟 MT/DU 端的 tunnel IP 飄移是同一類「自發重建但沒人跟著補」問題，但這個
# 是 UE 自己這一側缺路由，reassert_mt_routes/reapply_dnat_rules 都不會動到
# UE 容器本身，不會修到這個。這裡補進自我修復迴圈，不用每次都靠人工補。
fix_ue_default_routes() {
    for i in "$@"; do
        docker exec -u 0 "rfsim5g-end-ue-${i}" ip route replace default via 12.1.1.1 dev oaitun_ue1 2>/dev/null
    done
}

# [2026-09-18 新增] Random Access process pool 耗盡（OAI 內部固定 4 格陣列，
# gNB_scheduler_RA.c:719 "no free RA process"）是一種路由/DNAT 重新斷言完全
# 救不回來的獨立崩潰模式——子節點會卡在 PRACH/RAR 重試迴圈，直到該 DU 被
# 重啟為止（見 HISTORY.md 2026-09-18 Node2 案例、PC3 Node9~12 案例）。這裡
# 本機直接檢查 Node5,6,7,8(access) 四個 DU 的 log，有就重啟，5~10 秒即可恢復。
heal_ra_exhaustion_local() {
    for du in rfsim5g-iab-du-5 rfsim5g-iab-du-6 rfsim5g-iab-du-7 rfsim5g-iab-du-8; do
        local hits
        hits=$(docker logs --since 90s "$du" 2>&1 | grep -c "no free RA process" || true)
        if [ "${hits:-0}" -gt 0 ]; then
            echo -e "   ${YELLOW}[RA-HEAL] $du 偵測到 RA process pool 耗盡（${hits} 次），重啟...${NC}"
            docker restart "$du" >/dev/null 2>&1
            sleep 10
        fi
    done
}

verify_and_heal_ues() {
    # 自我修復迴圈：ping 全部本機負責的 UE，任何一個失敗就重新斷言 MT 路由 +
    # 重新套用 CU DNAT 規則 + UE 自己的預設路由，最多重試 5 次（每次間隔 15
    # 秒）。目的是讓 run_local_pc2.sh 這一次執行就把「MT tunnel 重建導致路由
    # 消失」這種瞬時不穩定自己修好，不需要每次都靠外部重新整個三主機重啟才會通。
    local ues=(1 2 3 4 5 6 7 8)
    local ext_dn_ip="192.168.72.135"
    for attempt in 1 2 3 4 5; do
        local all_ok=true
        for i in "${ues[@]}"; do
            if ! docker exec rfsim5g-end-ue-$i ping -c 1 -W 2 $ext_dn_ip >/dev/null 2>&1; then
                all_ok=false
            fi
        done
        if [ "$all_ok" = true ]; then
            echo -e "   ${GREEN}[HEAL] 全部 UE1~8 連通性正常（第 ${attempt} 次檢查）${NC}"
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

echo -e "${CYAN}[2/6] Launching + Deploying Access Nodes 5,6,7,8（一個一個依序，等前一個附著完成才啟動下一個）...${NC}"
for n in 5 6 7 8; do
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

echo -e "\n${CYAN}[3/6] Launching End-UEs 1~8（一個一個依序啟動）...${NC}"
for i in 1 2 3 4 5 6 7 8; do
    $DOCKER_COMPOSE -f $COMPOSE_FILE up -d "rfsim5g-end-ue-$i"
    wait_for_ue "rfsim5g-end-ue-$i"
done

reapply_dnat_rules

echo -e "\n${CYAN}[4~6/6] 驗證 + 自我修復 UE1~8 連通性...${NC}"
verify_and_heal_ues

echo -e "\n${YELLOW}====================================================${NC}"
if [ "$SSH_AVAILABLE" = true ]; then
    echo -e "${GREEN}[AUTO] CU magic commands 已透過 SSH 自動套用至 PC1 ✓${NC}"
else
    echo -e "${YELLOW}[ACTION REQUIRED] FINAL ROUTING FIX ON PC 1 (SERVER)${NC}"
    echo -e "${CYAN}$(echo -e "$CU_MAGIC_COMMANDS")${NC}"
fi
echo -e "${YELLOW}====================================================${NC}"
echo -e "\n${GREEN}IAB PC2 - Node5,6,7,8(access) + UE1~8 Ready!${NC}"
