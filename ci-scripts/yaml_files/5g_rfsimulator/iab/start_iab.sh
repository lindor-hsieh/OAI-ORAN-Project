#!/bin/bash

# IAB 5G 網路啟動腳本 - 1-2-1 Triangle Topology
# 架構: Donor -> IAB Node 1 & IAB Node 2 -> End UE*3

COMPOSE_FILE="docker-compose-iab.yaml"

# 自動判斷 docker compose 指令
if command -v docker-compose &> /dev/null; then
    DOCKER_COMPOSE="docker-compose"
else
    DOCKER_COMPOSE="docker compose"
fi

echo "[1/7] Clean up the Docker environment"
$DOCKER_COMPOSE -f $COMPOSE_FILE down
# 清除舊的宿主機路由 (避免衝突)
sudo ip route del 12.1.1.0/24 2>/dev/null || true

echo "[2/7] Run Core Network and Donor (Root Node)"
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d mysql oai-amf oai-smf oai-upf oai-ext-dn rfsim5g-donor-cu rfsim5g-donor-du oai-flexric

echo "Waiting for Core/Donor Initialization (25s)..."
sleep 25

# IAB Node 1 (Relay Node / PCI 1)
echo "[3/7] Start IAB Node 1 (Relay Node)"
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-mt

echo "Waiting for IAB-MT-1 to get IP..."
MT1_IP=""
MAX_RETRIES=60
COUNT=0

while [ -z "$MT1_IP" ]; do
    sleep 2
    MT1_IP=$(docker exec rfsim5g-iab-mt ip -f inet addr show oaitun_ue1 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}')
    COUNT=$((COUNT+1))
    if [ $COUNT -ge $MAX_RETRIES ]; then
        echo "Exceed time: IAB-MT-1 connect failed"
        exit 1
    fi
    echo -n "."
done
echo -e "\nSuccess! IAB-MT-1 IP: $MT1_IP"

# 更新 Node 1 設定檔並啟動
sed -i "s/local_n_address *= *\".*\";/local_n_address = \"$MT1_IP\";/" ./conf/iab_du.conf
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-du

# IAB Node 2 (Access Node / PCI 2)
echo "[4/7] Start IAB Node 2 (Access Node)"
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-mt-2

echo "Waiting for IAB-MT-2 to get IP..."
MT2_IP=""
COUNT=0

while [ -z "$MT2_IP" ]; do
    sleep 2
    # !! 這裡抓的是 rfsim5g-iab-mt-2 的 IP
    MT2_IP=$(docker exec rfsim5g-iab-mt-2 ip -f inet addr show oaitun_ue1 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}')
    COUNT=$((COUNT+1))
    if [ $COUNT -ge $MAX_RETRIES ]; then
        echo "Exceed time: IAB-MT-2 connect failed"
        exit 1
    fi
    echo -n "."
done
echo -e "\nSuccess! IAB-MT-2 IP: $MT2_IP"

# 更新 Node 2 設定檔 (iab_du_2.conf) 並啟動
sed -i "s/local_n_address *= *\".*\";/local_n_address = \"$MT2_IP\";/" ./conf/iab_du_2.conf
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-du-2

# Routing & NAT Setup (For BOTH Nodes)
echo "[5/7] Routing Rules and NAT Setup"

# IAB Node Setup
setup_iab_node() {
    NODE_NAME=$1
    NODE_IP=$2
    echo "Configuring Routing for $NODE_NAME ($NODE_IP)..."
    
    # 預備工具
    # docker exec -u 0 $NODE_NAME bash -c "command -v iptables >/dev/null 2>&1 || (apt-get update && apt-get install -y iptables iproute2)"
    
    # 1. 開啟轉發 & 關閉過濾
    docker exec -u 0 $NODE_NAME sysctl -w net.ipv4.ip_forward=1 >/dev/null
    docker exec -u 0 $NODE_NAME sysctl -w net.ipv4.conf.all.rp_filter=0 >/dev/null
    docker exec -u 0 $NODE_NAME sysctl -w net.ipv4.conf.oaitun_ue1.rp_filter=0 >/dev/null
    
    # 2. 設定 NAT (Masquerade)
    docker exec -u 0 $NODE_NAME iptables -t nat -F
    docker exec -u 0 $NODE_NAME iptables -t nat -A POSTROUTING -o oaitun_ue1 -j MASQUERADE
    
    # 3. 設定去往 CU 的路由
    docker exec -u 0 $NODE_NAME ip route del 192.168.71.140 2>/dev/null || true
    docker exec -u 0 $NODE_NAME ip route add 192.168.71.140 dev oaitun_ue1 src $NODE_IP
}

# 設定 Node 1
setup_iab_node "rfsim5g-iab-mt" "$MT1_IP"

# 設定 Node 2
setup_iab_node "rfsim5g-iab-mt-2" "$MT2_IP"


echo "Configure Donor-CU & UPF..."
# Donor-CU 路由: 告訴 CU 12.1.1.0/24 整個網段都在 UPF 後面 (這條規則適用所有 IAB Nodes)
docker exec -u 0 rfsim5g-donor-cu ip route del 12.1.1.0/24 2>/dev/null || true
docker exec -u 0 rfsim5g-donor-cu ip route add 12.1.1.0/24 via 192.168.71.134 dev eth0 2>/dev/null || true

# UPF NAT: 允許 UE 上網
docker exec -u 0 rfsim5g-oai-upf sysctl -w net.ipv4.ip_forward=1 >/dev/null
docker exec -u 0 rfsim5g-oai-upf iptables -t nat -A POSTROUTING -s 12.1.1.0/24 -o eth0 -j MASQUERADE 2>/dev/null || true

# End UE & Testing
echo "[6/7] Run All End-UEs (UE 1, UE 2, UE 3)"
# 一次啟動所有 UE 容器
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-end-ue rfsim5g-end-ue-2 rfsim5g-end-ue-3

echo "Waiting for UEs to attach..."

# 定義一個函數來檢查 UE IP，避免重複寫程式碼
check_ue_ip() {
    UE_NAME=$1
    echo -n "Checking $UE_NAME... "
    UE_IP=""
    MAX_RETRIES=60
    COUNT=0
    while [ -z "$UE_IP" ]; do
        UE_IP=$(docker exec $UE_NAME ip -f inet addr show oaitun_ue1 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}')
        if [ ! -z "$UE_IP" ]; then
            echo " Attached! IP: $UE_IP"
            break
        fi
        sleep 2
        COUNT=$((COUNT+1))
        if [ $COUNT -ge $MAX_RETRIES ]; then
            echo "❌ Timeout: $UE_NAME failed to attach."
            return 1
        fi
    done
}

# 依序檢查三台 UE 是否都拿到 IP
check_ue_ip "rfsim5g-end-ue"
check_ue_ip "rfsim5g-end-ue-2"
check_ue_ip "rfsim5g-end-ue-3"

# 給一點緩衝時間讓路由穩定
echo "Wait 5s for routing stability..."
sleep 5

echo "[7/7] Connectivity & Topology Test with Hop-by-Hop Diagnostics"

# 定義固定節點 IP 
DONOR_CU_IP="192.168.71.140"
DONOR_DU_IP="192.168.71.144"
GOOGLE_DNS="8.8.8.8"

# 定義測試函數 (自動判定 PASS/FAIL)
ping_hop() {
    SRC_CONTAINER=$1
    TARGET_NAME=$2
    TARGET_IP=$3

    echo -n "   -> Ping $TARGET_NAME ($TARGET_IP): "
    # -c 3: ping 3次
    # -W 2: 等待超時 2秒 (避免卡死)
    docker exec -it $SRC_CONTAINER ping -I oaitun_ue1 -c 3 -W 2 $TARGET_IP > /dev/null 2>&1

    if [ $? -eq 0 ]; then
        echo -e "\033[32mPASS\033[0m" # 綠色 PASS
    else
        echo -e "\033[31mFAIL\033[0m" # 紅色 FAIL
    fi
}

echo -e "\n 1. Hop-by-Hop Diagnostics (逐跳診斷) "

echo "PATH A: UE 1 -> IAB Node 2 -> Donor -> Internet"
# 使用腳本前面抓到的 $MT2_IP 變數
if [ -z "$MT2_IP" ]; then MT2_IP="192.168.71.152"; fi # 防呆預設值

ping_hop "rfsim5g-end-ue" "IAB Node 2 (Gateway)" "$MT2_IP"
ping_hop "rfsim5g-end-ue" "Donor DU (Backhaul)" "$DONOR_DU_IP"
ping_hop "rfsim5g-end-ue" "Donor CU (Core Edge)" "$DONOR_CU_IP"
ping_hop "rfsim5g-end-ue" "Internet (Google)" "$GOOGLE_DNS"

echo "PATH B: UE 3 -> IAB Node 1 -> Donor -> Internet"
# 使用腳本前面抓到的 $MT1_IP 變數
if [ -z "$MT1_IP" ]; then MT1_IP="192.168.71.150"; fi # 防呆預設值

ping_hop "rfsim5g-end-ue-3" "IAB Node 1 (Gateway)" "$MT1_IP"
ping_hop "rfsim5g-end-ue-3" "Donor DU (Backhaul)" "$DONOR_DU_IP"
ping_hop "rfsim5g-end-ue-3" "Donor CU (Core Edge)" "$DONOR_CU_IP"
ping_hop "rfsim5g-end-ue-3" "Internet (Google)" "$GOOGLE_DNS"

echo -e "\n Diagnostics Completed."