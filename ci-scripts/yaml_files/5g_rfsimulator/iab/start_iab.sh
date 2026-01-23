#!/bin/bash

# IAB 5G 網路啟動腳本 (路由與轉發)
COMPOSE_FILE="docker-compose-iab.yaml"

# 自動判斷 docker compose 指令
if command -v docker-compose &> /dev/null; then
    DOCKER_COMPOSE="docker-compose"
else
    DOCKER_COMPOSE="docker compose"
fi

echo "[1/6] Clean up the Docker environment"
$DOCKER_COMPOSE -f $COMPOSE_FILE down

# 清除舊的宿主機路由 (避免衝突)
sudo ip route del 12.1.1.0/24 2>/dev/null

echo "[2/6] Run Core Network and Donor "
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d mysql oai-amf oai-smf oai-upf oai-ext-dn rfsim5g-donor-cu rfsim5g-donor-du oai-flexric

echo "System Initialization"
sleep 25

echo "[3/6] Run IAB-MT  "
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-mt

echo "Waiting for IAB-MT to get IP "
MT_IP=""
MAX_RETRIES=60
COUNT=0

while [ -z "$MT_IP" ]; do
    sleep 2
    MT_IP=$(docker exec rfsim5g-iab-mt ip -f inet addr show oaitun_ue1 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}')
    COUNT=$((COUNT+1))
    if [ $COUNT -ge $MAX_RETRIES ]; then
        echo "Exceed time:IAB-MT connect failed"
        exit 1
    fi
    echo -n "."
done
echo -e "\nSuccess! MT IP: $MT_IP"

echo "[4/6] Update IAB-DU IP and Start IAB-DU"

# 只更新 IP，不動 Port，也不動其他設定
sed -i "s/local_n_address *= *\".*\";/local_n_address = \"$MT_IP\";/" ./conf/iab_du.conf

# 啟動 IAB-DU
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-iab-du

echo "[5/6] Routing Rules and NAT Setup"

# 預備動作：確保 IAB-MT 有 iptables 工具 
docker exec -u 0 rfsim5g-iab-mt bash -c "command -v iptables >/dev/null 2>&1 || (apt-get update && apt-get install -y iptables iproute2)"

# A. IAB-MT 側設定 (負責將 IAB-DU 的流量送往 Donor)
echo " Configure IAB-MT: Enable forwarding, disable filtering, and configure NAT..."

# 1. 開啟 Linux 核心封包轉發功能
docker exec -u 0 rfsim5g-iab-mt sysctl -w net.ipv4.ip_forward=1 >/dev/null

# 2. 關閉反向路徑過濾 (RP Filter)
# 重要！因為 OAI 的封包是從 Tunnel 出去，Linux 預設會認為這是偽造來源而丟棄，必須關閉
docker exec -u 0 rfsim5g-iab-mt sysctl -w net.ipv4.conf.all.rp_filter=0 >/dev/null
docker exec -u 0 rfsim5g-iab-mt sysctl -w net.ipv4.conf.oaitun_ue1.rp_filter=0 >/dev/null

# 3. 設定 NAT (偽裝)
# 讓從 IAB-DU 出來的封包，看起來像是 IAB-MT 發出的，這樣 Donor 回信時才找得到人。
docker exec -u 0 rfsim5g-iab-mt iptables -t nat -F
docker exec -u 0 rfsim5g-iab-mt iptables -t nat -A POSTROUTING -o oaitun_ue1 -j MASQUERADE

# 4. 設定上行路由 (Uplink Route)
# 告訴 MT：要去 CU (192.168.71.140) 的話，請走無線介面 (oaitun_ue1)，下一跳是 Donor
docker exec -u 0 rfsim5g-iab-mt ip route del 192.168.71.140 2>/dev/null
docker exec -u 0 rfsim5g-iab-mt ip route add 192.168.71.140 dev oaitun_ue1 src $MT_IP

# B. Donor-CU 側設定 (負責回信給 IAB-DU)
echo "Configure Donor-CU: Add return route..."

# 1. 設定下行路由 (Downlink Route)
# 告訴 CU：要去 12.1.1.x (IAB-DU/UE 網段) 的話，請把信交給 UPF (192.168.71.134) 轉送。
# 這是解決 "Connection Refused/Timeout" 的關鍵。
docker exec -u 0 rfsim5g-donor-cu ip route del 12.1.1.0/24 2>/dev/null
docker exec -u 0 rfsim5g-donor-cu ip route add 12.1.1.0/24 via 192.168.71.134 dev eth0 2>/dev/null || true

# C. UPF 側設定 (負責 Internet NAT)
echo "Configure UPF: Enable Internet NAT..."

# 1. 確保 UPF 開啟轉發
docker exec -u 0 rfsim5g-oai-upf sysctl -w net.ipv4.ip_forward=1 >/dev/null

# 2. 設定 NAT (關鍵！沒有這行出不去 8.8.8.8)
# 意義：凡是來自 12.1.1.0/24 (UE網段) 的封包，要從 eth0 (Docker 網橋) 出去時,把它偽裝成 UPF 的 Docker IP，這樣 Google 才知道回信給誰
docker exec -u 0 rfsim5g-oai-upf iptables -t nat -A POSTROUTING -s 12.1.1.0/24 -o eth0 -j MASQUERADE

echo "[6/6] Run End-UE and Test..."

# 1. 啟動 UE
$DOCKER_COMPOSE -f $COMPOSE_FILE up -d rfsim5g-end-ue

echo "Waiting for End-UE to attach..."

# 定義檢查連線函數
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
            echo "Timeout: $UE_NAME failed to attach."
            return 1
        fi
    done
}

# 2. 檢查 UE 是否連上 (這時網卡才會建立)
check_ue_ip "rfsim5g-end-ue"

# 3. 設定測速路由 (必須在連上後執行)
echo "Configuring Static Routes for Throughput Test..."
# 告訴 UE 去找 Ext-DN (192.168.72.x) 必須走 oaitun_ue1
docker exec -u 0 rfsim5g-end-ue ip route add 192.168.72.128/26 dev oaitun_ue1 2>/dev/null || true

echo "Wait 5s for routing stability..."
sleep 5

echo "[7/7] Connectivity & Performance Test (Latency + Throughput)"

# 1. 定義測試參數
# 定義固定節點 IP
DONOR_CU_IP="192.168.71.140"
DONOR_DU_IP="192.168.71.144"
GOOGLE_DNS="8.8.8.8"
TRAFFIC_SERVER_IP="192.168.72.135"

# 確保 Traffic Server (Ext-DN) 已經在背景開啟 iperf3 Server 模式
docker exec -d rfsim5g-oai-ext-dn iperf3 -s > /dev/null 2>&1

# 2. 定義測試函數
# 函數 A: Ping 測試 (含 Latency)
ping_hop() {
    SRC_CONTAINER=$1
    TARGET_NAME=$2
    TARGET_IP=$3
    
    echo -n "   -> Ping $TARGET_NAME ($TARGET_IP): "
    
    # 執行 Ping，並抓取結果
    OUTPUT=$(docker exec $SRC_CONTAINER ping -I oaitun_ue1 -c 3 -W 2 $TARGET_IP 2>&1)
    EXIT_CODE=$?
    
    if [ $EXIT_CODE -eq 0 ]; then
        # 抓取平均延遲 (avg)
        AVG_LATENCY=$(echo "$OUTPUT" | grep -E "^rtt|^round-trip" | awk -F '/' '{print $5}')
        echo -e "\033[32mPASS\033[0m \033[36m(Avg: ${AVG_LATENCY} ms)\033[0m"
    else
        echo -e "\033[31mFAIL\033[0m"
    fi
}

# 函數 B: Throughput 測試 (含 Speed)
measure_throughput() {
    SRC_CONTAINER=$1
    TARGET_IP=$2
    
    echo -n "   -> Throughput Test (Downlink): "
    
    # 執行 iperf3 
    OUTPUT=$(docker exec $SRC_CONTAINER iperf3 -c $TARGET_IP -t 3 -f m -R 2>&1)
    EXIT_CODE=$?

    if [ $EXIT_CODE -eq 0 ]; then
        # 抓取最後的 Receiver Bitrate
        THROUGHPUT=$(echo "$OUTPUT" | grep "receiver" | grep "SUM" | awk '{print $6, $7}')
        if [ -z "$THROUGHPUT" ]; then
             THROUGHPUT=$(echo "$OUTPUT" | grep "receiver" | tail -n 1 | awk '{print $7, $8}')
        fi
        echo -e "\033[32mPASS\033[0m \033[35m(Speed: ${THROUGHPUT})\033[0m"
    else
        echo -e "\033[31mFAIL (Check iperf3 installed?)\033[0m"
    fi
}

# 3. 開始執行診斷
echo -e "\n1. Hop-by-Hop Diagnostics (逐跳診斷 + 測速)"
echo "PATH: UE -> IAB Node (Gateway) -> Donor -> Internet"

# 使用腳本前面抓到的 $MT_IP
if [ -z "$MT_IP" ]; then 
    echo "Warning: MT_IP variable is empty, using default."
    MT_IP="192.168.71.150"
fi

# 1. 逐跳測試
ping_hop "rfsim5g-end-ue" "IAB Node (Gateway)" "$MT_IP"
ping_hop "rfsim5g-end-ue" "Donor DU (Backhaul)" "$DONOR_DU_IP"
ping_hop "rfsim5g-end-ue" "Donor CU (Core Edge)" "$DONOR_CU_IP"
ping_hop "rfsim5g-end-ue" "Internet (Google)" "$GOOGLE_DNS"

# 2. 端對端測速
measure_throughput "rfsim5g-end-ue" $TRAFFIC_SERVER_IP

echo -e "\n Diagnostics Completed."