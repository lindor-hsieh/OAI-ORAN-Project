#!/bin/bash
# PC 1: IAB Server Script (1 donor + 4 relay + 8 access + 17 UE 三主機版)
#
# 2026-09-22 節點重分配：Node1~4(relay) 全部集中到 PC1（跟 Donor 同機），
# 用來消除「跟 Donor 同主機的分支吞吐量系統性偏高」這個量測 confound（見
# CLAUDE.md 第 1 節、HISTORY.md 對應條目）。PC1 現在不再啟動任何 access
# 節點或 UE 容器（Node7,8+UE5~8 搬去 PC2），只負責 CN5G + FlexRIC +
# MongoDB + Donor CU/DU + 全部 4 個 relay(Node1~4) + 12 組 xApp/inference
# 容器。四個 relay 現在全部跟 CU 同機，設定 tunnel IP 路由完全是本機操作，
# 不需要任何 SSH（舊版只有 Node2 是這樣，Node1/3/4 原本要靠 PC2/PC3 SSH
# 通知 PC1，現在整層 SSH 通知機制都不需要了）。

COMPOSE_FILE="docker-compose-iab-server.yaml"
IFACE_NAME="enxc84d44350030"

# IP 設定
PC2_IP="192.168.88.2"
PC3_IP="192.168.88.3"
UPF_IP="192.168.88.134"
UE_TUNNEL_SUBNET="12.1.1.0/24"
PC2_INTERNAL_SUBNET="192.168.74.0/24"   # Node5,6,7,8 (access, PC2)
PC3_INTERNAL_SUBNET="192.168.75.0/24"   # Node9~12 (access, PC3)
DN_CONTAINER="rfsim5g-oai-ext-dn"

if command -v docker-compose &> /dev/null; then DOCKER_COMPOSE="docker-compose"; else DOCKER_COMPOSE="docker compose"; fi

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

# ==========================================
# 1. 核心清理與 Docker 網路環境修復
# ==========================================
echo -e "${CYAN}[1/6] Clean Up...${NC}"

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
echo -e "${CYAN}[2/6] Configuring Macvlan Bridge & Magic Routes...${NC}"
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
echo -e "${CYAN}[3/6] Starting OAI Core, FlexRIC & MongoDB...${NC}"
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
# 3.5 本機 RAN 節點：Node1,2,3,4（全部 relay，2026-09-22 集中到 PC1）
#   CU 就在本機，四個 relay 的 tunnel IP 路由設定全部是本機操作，不需要
#   SSH。一個一個依序啟動並等待穩定，避免「relay 還沒真的穩定，下一個就
#   搶著啟動」造成 CU stale F1 association 骨牌效應（同 PC2/PC3 舊版的
#   教訓，見 CLAUDE.md 第 7 節排查指南）。
# ==========================================
echo -e "${CYAN}[3.5/6] Launching Local Relay Nodes: Node1,2,3,4...${NC}"

declare -A RELAY_MACVLAN=( [1]="192.168.88.150" [2]="192.168.88.151" [3]="192.168.88.152" [4]="192.168.88.153" )

configure_and_start_local_relay() {
    local N=$1
    local MACVLAN_IP="${RELAY_MACVLAN[$N]}"
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

wait_for_local_relay_du_healthy() {
    local DU_NAME=$1
    local COUNT=0
    # [2026-09-12 修復] 20 圈(每圈最多 sleep 3s，約 60s)對 relay 曾經觀察到的 CU stale
    # F1 association 重試骨牌效應（實測要 100+ 秒才自然解開）來說不夠，拉長到 40 圈
    # (~120s)，減少「relay 還沒真的穩定，但這裡已經逾時放行，導致下游 access node
    # 的 120s 倒數在 relay 準備好之前就先開始算」這種銜接失準的情況。
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
    echo -e "   ${RED}$DU_NAME 逾時仍未穩定，下游節點可能連不上，請檢查${NC}"
    return 1
}

# [2026-09-18 新增，2026-09-22 沿用] Random Access process pool 耗盡（OAI 內部
# 固定 4 格陣列，gNB_scheduler_RA.c:719 "no free RA process"）是一種路由重新
# 斷言完全救不回來的獨立崩潰模式，子節點會卡在 PRACH/RAR 重試迴圈直到該 DU
# 被重啟為止（見 HISTORY.md 2026-09-18 Node2 案例）。這裡檢查全部 4 個本機
# relay DU 的 log，有就重啟。
heal_ra_exhaustion_local() {
    for du in rfsim5g-iab-du-1 rfsim5g-iab-du-2 rfsim5g-iab-du-3 rfsim5g-iab-du-4; do
        local hits
        hits=$(docker logs --since 90s "$du" 2>&1 | grep -c "no free RA process" || true)
        if [ "${hits:-0}" -gt 0 ]; then
            echo -e "   ${YELLOW}[RA-HEAL] $du 偵測到 RA process pool 耗盡（${hits} 次），重啟...${NC}"
            docker restart "$du" >/dev/null 2>&1
            sleep 10
        fi
    done
}

for n in 1 2 3 4; do
    $DOCKER_COMPOSE -f $COMPOSE_FILE up -d "rfsim5g-iab-mt-${n}"
    configure_and_start_local_relay $n
    wait_for_local_relay_du_healthy "rfsim5g-iab-du-${n}"
done

heal_ra_exhaustion_local

# ==========================================
# 4. 啟動 Python 推論伺服器 (Node 1~12，全部集中在 PC1)
# ==========================================
echo -e "${CYAN}[4/6] Starting Inference Servers (Node 1~12)...${NC}"
NODES=$(seq 1 12)
INF_SERVICES=""
for i in $NODES; do INF_SERVICES="$INF_SERVICES inference-node${i}"; done
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d $INF_SERVICES
sleep 3

# [2026-09-18 新增] 陷阱記錄：這支腳本開頭的 `docker compose down`（無 --profile）
# 會連 stage2-fl profile 的服務（flower-superlink/supernode-nodeN/scheduler/
# global-xapp）一起清掉，但上面這行 plain `up -d $INF_SERVICES` 呼叫的 shell
# 環境如果沒有帶 REWARD_MODE/MODEL_ARCH，docker compose 會悄悄套用 compose 檔
# 裡的預設值（REWARD_MODE 預設是 lagrangian，不是訓練中實際要用的模式！），且
# stage2-fl 服務完全不會被帶回來——這支腳本本身不知道現在是哪個 stage。
# training_watchdog.sh 的 full_recovery() 原本就有一段「主動覆蓋一次」的保護，
# 但那只在透過 watchdog 呼叫時才生效；直接執行這支腳本（例如乾淨重啟）完全
# 沒有這層保護，2026-09-18 現場就因為直接執行踩到這個坑（REWARD_MODE 悄悄變
# lagrangian、污染了新寫入的經驗，且 FL 服務整層消失）。這裡比照
# training_watchdog.sh 的做法，主動用環境變數（或安全預設值）覆蓋一次，並在
# stage2-fl 服務先前有在跑（用 docker ps -a 判斷該服務容器是否存在過）時，
# 用正確的環境變數重新帶回來。
REWARD_MODE="${REWARD_MODE:-throughput_only}"
MODEL_ARCH="${MODEL_ARCH:-mlp}"
FL_MODE="${FL_MODE:-avg}"
echo -e "${CYAN}[4/6] 主動用 REWARD_MODE=${REWARD_MODE} MODEL_ARCH=${MODEL_ARCH} 覆蓋一次 inference-nodeN（防止悄悄套用 compose 預設值）...${NC}"
REWARD_MODE="$REWARD_MODE" MODEL_ARCH="$MODEL_ARCH" \
    $DOCKER_COMPOSE -f $COMPOSE_FILE up -d --force-recreate $INF_SERVICES
sleep 3

if docker ps -a --format '{{.Names}}' | grep -q '^flower-superlink$'; then
    echo -e "${CYAN}[4/6] 偵測到 stage2-fl 服務先前有跑過，用 FL_MODE=${FL_MODE} MODEL_ARCH=${MODEL_ARCH} 一併帶回來...${NC}"
    FL_MODE="$FL_MODE" MODEL_ARCH="$MODEL_ARCH" \
        $DOCKER_COMPOSE -f $COMPOSE_FILE --profile stage2-fl up -d --force-recreate \
        global-xapp flower-superlink flower-supernode-node{1..12} flower-scheduler
fi

for i in $NODES; do
    STATUS=$(docker logs inference-node${i} 2>&1 | grep "已綁定" | tail -1)
    if [ -n "$STATUS" ]; then
        echo -e "  Node${i}: ${GREEN}ZMQ OK${NC}"
    else
        echo -e "  Node${i}: ${YELLOW}waiting...${NC}"
    fi
done

# ==========================================
# 5. 最後路由與 UPF/FlexRIC 修正
# ==========================================
echo -e "${CYAN}[5/6] Finalizing Network & UPF/FlexRIC Routing...${NC}"

docker exec -u 0 rfsim5g-oai-upf bash -c "sysctl -w net.ipv4.ip_forward=1 && iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE"
sudo sysctl -w net.ipv4.ip_forward=1 > /dev/null
sudo ip route replace $UE_TUNNEL_SUBNET via $UPF_IP dev macvlan-br 2>/dev/null

# FlexRIC 回程路由：PC2/PC3 各自的 internal 子網都要能回得去。PC1 本身
# 2026-09-22 後不再有任何 access 節點/internal bridge，不需要再加本機子網路由。
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
