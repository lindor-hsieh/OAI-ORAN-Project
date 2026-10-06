#!/bin/bash
# start_relay_ues.sh — 在 PC1 依序啟動 relay 直連 UE（UE17~24）並驗證／自我修復（2026-10-01）
#
# relay r（Node1~4）各帶 2 個 UE：UE(15+2r)、UE(16+2r)，容器在 PC1、compose profile `relay-ue`。
# 必須在 PC2/PC3 的 access MT 都連上 relay DU 之後才啟動：relay DU 上的連線順序（chanmod ue_id）才會固定是
# MT=0,1、UE=2,3（traffic_scenario.py 的 NODE_CONFIG 依此對照）。由 run_local_pc1.sh 在等完 13/13 E2 之後呼叫；
# 也可單獨執行。RELAY_UES=0 時整支跳過（回到 16 UE 拓樸）。
#
# 驗證失敗的修復順序（同 PC2/PC3 的 UE）：重新斷言預設路由 → 重啟該 UE 容器再補路由，最多 5 輪；
# 修不好時印出「重試 5 次後仍有 UE 連不通」（training_watchdog.sh 以這句判斷軟性失敗）。

COMPOSE_DIR=/home/lindor/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator
COMPOSE_FILE="$COMPOSE_DIR/docker-compose-iab-server.yaml"
EXT_DN_IP="192.168.72.135"
RELAY_UE_IDS=(17 18 19 20 21 22 23 24)

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

if [[ "${RELAY_UES:-1}" == "0" ]]; then
    echo -e "${YELLOW}[relay UE] RELAY_UES=0，跳過 relay 直連 UE${NC}"
    exit 0
fi
if command -v docker-compose &> /dev/null; then DOCKER_COMPOSE="docker-compose"; else DOCKER_COMPOSE="docker compose"; fi

ue_ip() {
    docker exec "rfsim5g-end-ue-$1" ip -f inet addr show oaitun_ue1 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}'
}

# 等 tunnel IP（最多 90 秒）後設定 MTU 與預設路由；成功回傳 0
attach_and_route() {
    local u=$1 c="rfsim5g-end-ue-$1" ip="" k
    for k in $(seq 1 45); do
        ip=$(ue_ip "$u"); [[ -n "$ip" ]] && break
        sleep 2
    done
    [[ -z "$ip" ]] && return 1
    docker exec -u 0 "$c" ip link set oaitun_ue1 mtu 1200 2>/dev/null
    docker exec -u 0 "$c" ip route replace default via 12.1.1.1 dev oaitun_ue1 2>/dev/null
    echo "$ip"
}

ue_ok() {
    docker exec "rfsim5g-end-ue-$1" ping -c 2 -W 3 "$EXT_DN_IP" 2>/dev/null | grep -q " 0% packet loss"
}

echo -e "${CYAN}[relay UE] 依序啟動 UE${RELAY_UE_IDS[0]}~UE${RELAY_UE_IDS[-1]}（relay 直連，PC1）...${NC}"
for u in "${RELAY_UE_IDS[@]}"; do
    $DOCKER_COMPOSE -f "$COMPOSE_FILE" --profile relay-ue up -d "rfsim5g-end-ue-$u" >/dev/null 2>&1
    if ip=$(attach_and_route "$u"); then
        echo -e "   UE${u}: ${GREEN}Attached ($ip)${NC}"
    else
        echo -e "   UE${u}: ${RED}90 秒內沒有 tunnel IP${NC}"
    fi
done

echo -e "${CYAN}[relay UE] 驗證連通性（ping ext-dn）＋自我修復...${NC}"
bad=()
for u in "${RELAY_UE_IDS[@]}"; do ue_ok "$u" || bad+=("$u"); done
for round in 1 2 3 4 5; do
    [[ ${#bad[@]} -eq 0 ]] && break
    echo -e "   ${YELLOW}[HEAL] 第 ${round} 次：UE ${bad[*]} 不通${NC}"
    still=()
    for u in "${bad[@]}"; do
        docker exec -u 0 "rfsim5g-end-ue-$u" ip route replace default via 12.1.1.1 dev oaitun_ue1 2>/dev/null
        sleep 3
        if ue_ok "$u"; then continue; fi
        # 「容器存活但資料面斷線」：重啟該 UE 容器（2026-10-01 UE4 案例，重啟後即恢復）
        docker restart "rfsim5g-end-ue-$u" >/dev/null 2>&1
        attach_and_route "$u" >/dev/null || true
        sleep 3
        ue_ok "$u" || still+=("$u")
    done
    bad=("${still[@]}")
done

if [[ ${#bad[@]} -eq 0 ]]; then
    echo -e "${GREEN}[relay UE] ${#RELAY_UE_IDS[@]}/${#RELAY_UE_IDS[@]} 個 relay UE 連通${NC}"
else
    echo -e "${RED}[HEAL] 重試 5 次後仍有 UE 連不通，需要人工檢查（relay UE: ${bad[*]}）${NC}"
fi
exit 0
