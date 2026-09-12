#!/bin/bash
# IAB Performance Benchmark Script（三主機版，UE 編號改用明確列表而非範圍）
#
# 用法：./iab_perf_test.sh <iterations> <ue_id1> [ue_id2] [ue_id3] ...
#   例如三主機各自對自己管的 UE 跑（見 CLAUDE.md 第 1 節拓樸）：
#     PC1: ./iab_perf_test.sh 3 5 6 7 8
#     PC2: ./iab_perf_test.sh 3 1 2 3 4 17
#     PC3: ./iab_perf_test.sh 3 9 10 11 12 13 14 15 16
#   之所以用明確列表而非 <start> <end> 範圍，是因為 PC2 現在管的 UE 不連續
#   （1~4 加上 UE17，UE17 直接掛在 Node4 底下，見 CLAUDE.md 拓樸圖）。

# ==========================================
# 1. 參數設定
# ==========================================
UPF_DN_IP="192.168.72.135"   # 核心網 Traffic Server IP

if [ $# -lt 2 ]; then
    echo "Usage: $0 <iterations> <ue_id1> [ue_id2] ..."
    echo "  例如: $0 3 5 6 7 8"
    exit 1
fi

ITERATIONS=$1
shift
UE_IDS=("$@")

# 顏色定義
RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
NC='\033[0m'

# 全網統計變數初始化
G_LAT_SUM=0
G_TCP_DL_SUM=0; G_TCP_UL_SUM=0
G_UDP_DL_SUM=0; G_UDP_UL_SUM=0
NET_UE_COUNT=0

# ==========================================
# 2. 核心測試函式
# ==========================================
run_benchmarks() {
    local UE_NAME=$1
    local UE_IP=$(docker exec $UE_NAME ip -f inet addr show oaitun_ue1 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}')

    # 檢查 UE 是否活著
    if [ -z "$UE_IP" ]; then
        echo -e "${RED}[Skip] $UE_NAME is not running or no IP assigned.${NC}"
        return
    fi

    # 用這個 UE 專屬的 iperf3 port（5200+UE編號，跟 setup_iperf_servers.sh／
    # traffic_scenario.py 的 UE_IPERF_PORTS 一致），避免跟同時在跑的 Scenario R
    # 流量搶同一個 port（不指定 port 會全部落在 iperf3 預設的 5201，即 UE1 的
    # 專屬 port，其他 UE 測試會跟 UE1 的場景流量互相干擾）。
    local UE_NUM=$(echo "$UE_NAME" | grep -oP '\d+$')
    local PORT=$((5200 + UE_NUM))

    echo -e "\n${CYAN}======================================${NC}"
    echo -e "${CYAN} Analyzing $UE_NAME ($UE_IP) ${NC}"
    echo -e "${CYAN}======================================${NC}"

    # 該 UE 的局部累加器
    local u_lat=0; local u_lat_c=0
    local u_tcp_dl=0; local u_tcp_dl_c=0
    local u_tcp_ul=0; local u_tcp_ul_c=0
    local u_udp_dl=0; local u_udp_dl_c=0
    local u_udp_ul=0; local u_udp_ul_c=0

    for ((i=1; i<=ITERATIONS; i++)); do
        echo -ne "   Test [$i/$ITERATIONS] ... "

        # 1. Latency (Ping) - 取平均值
        local lat=$(docker exec $UE_NAME ping -c 2 -W 1 $UPF_DN_IP 2>/dev/null | grep "avg" | awk -F'/' '{print $5}')
        if [ ! -z "$lat" ]; then
            u_lat=$(echo "$u_lat + $lat" | bc)
            u_lat_c=$((u_lat_c+1))
        fi

        # 2. TCP Downlink (-R reverse mode)
        # 使用 -t 2 (2秒) 快速採樣，避免拖太久；timeout 10 防止 server 忙時永久卡住
        local dl_raw=$(timeout 10 docker exec $UE_NAME iperf3 -c $UPF_DN_IP -p $PORT -t 2 -R 2>/dev/null)
        local dl_val=$(echo "$dl_raw" | grep "receiver" | awk '{print $7}')
        if [ ! -z "$dl_val" ] && [ "$dl_val" != "0.00" ]; then
            u_tcp_dl=$(echo "$u_tcp_dl + $dl_val" | bc)
            u_tcp_dl_c=$((u_tcp_dl_c+1))
        fi

        # 3. TCP Uplink
        local ul_raw=$(timeout 10 docker exec $UE_NAME iperf3 -c $UPF_DN_IP -p $PORT -t 2 2>/dev/null)
        local ul_val=$(echo "$ul_raw" | grep "receiver" | awk '{print $7}')
        if [ ! -z "$ul_val" ] && [ "$ul_val" != "0.00" ]; then
            u_tcp_ul=$(echo "$u_tcp_ul + $ul_val" | bc)
            u_tcp_ul_c=$((u_tcp_ul_c+1))
        fi

        # 4. UDP Downlink (頻寬設為 20M 測試瓶頸)
        local udl_raw=$(timeout 10 docker exec $UE_NAME iperf3 -c $UPF_DN_IP -p $PORT -u -b 20M -t 2 -R 2>/dev/null)
        local udl_val=$(echo "$udl_raw" | grep "receiver" | awk '{print $7}')
        if [ ! -z "$udl_val" ] && [ "$udl_val" != "0.00" ]; then
            u_udp_dl=$(echo "$u_udp_dl + $udl_val" | bc)
            u_udp_dl_c=$((u_udp_dl_c+1))
        fi

        # 5. UDP Uplink
        local uul_raw=$(timeout 10 docker exec $UE_NAME iperf3 -c $UPF_DN_IP -p $PORT -u -b 20M -t 2 2>/dev/null)
        local uul_val=$(echo "$uul_raw" | grep "receiver" | awk '{print $7}')
        if [ ! -z "$uul_val" ] && [ "$uul_val" != "0.00" ]; then
            u_udp_ul=$(echo "$u_udp_ul + $uul_val" | bc)
            u_udp_ul_c=$((u_udp_ul_c+1))
        fi

        echo "Done."
    done

    # 計算單一 UE 平均並顯示
    echo -e "   ----------------Result----------------"

    # Latency 處理
    if [ $u_lat_c -gt 0 ]; then
        local a_lat=$(echo "scale=2; $u_lat / $u_lat_c" | bc)
        echo -e "   Avg.Latency:            ${YELLOW}${a_lat} ms${NC}"
        G_LAT_SUM=$(echo "$G_LAT_SUM + $a_lat" | bc)
    fi

    # TCP DL 處理
    if [ $u_tcp_dl_c -gt 0 ]; then
        local a_tdl=$(echo "scale=2; $u_tcp_dl / $u_tcp_dl_c" | bc)
        echo -e "   Avg.TCP Downlink:       ${GREEN}${a_tdl} Mbps${NC}"
        G_TCP_DL_SUM=$(echo "$G_TCP_DL_SUM + $a_tdl" | bc)
    fi

    # TCP UL 處理
    if [ $u_tcp_ul_c -gt 0 ]; then
        local a_tul=$(echo "scale=2; $u_tcp_ul / $u_tcp_ul_c" | bc)
        echo -e "   Avg.TCP Uplink:         ${GREEN}${a_tul} Mbps${NC}"
        G_TCP_UL_SUM=$(echo "$G_TCP_UL_SUM + $a_tul" | bc)
    fi

    # UDP DL 處理
    if [ $u_udp_dl_c -gt 0 ]; then
        local a_udl=$(echo "scale=2; $u_udp_dl / $u_udp_dl_c" | bc)
        echo -e "   Avg.UDP Downlink:       ${GREEN}${a_udl} Mbps${NC}"
        G_UDP_DL_SUM=$(echo "$G_UDP_DL_SUM + $a_udl" | bc)
    fi

    # UDP UL 處理
    if [ $u_udp_ul_c -gt 0 ]; then
        local a_uul=$(echo "scale=2; $u_udp_ul / $u_udp_ul_c" | bc)
        echo -e "   Avg.UDP Uplink:         ${GREEN}${a_uul} Mbps${NC}"
        G_UDP_UL_SUM=$(echo "$G_UDP_UL_SUM + $a_uul" | bc)
    fi

    NET_UE_COUNT=$((NET_UE_COUNT + 1))
}

# ==========================================
# 3. 執行測試
# ==========================================

echo -e "${YELLOW}Starting IAB Network Benchmark...${NC}"
echo -e "Target Server: $UPF_DN_IP"
echo -e "Iterations per UE: $ITERATIONS"
echo -e "UE list: ${UE_IDS[*]}"

for i in "${UE_IDS[@]}"; do
    run_benchmarks "rfsim5g-end-ue-$i"
done

# ==========================================
# 4. 全網總結報告（本機這幾個 UE 的平均，三主機各自跑完後手動彙整）
# ==========================================
echo -e "\n${YELLOW}====================================================${NC}"
echo -e "${YELLOW}           本機 IAB Performance Summary            ${NC}"
echo -e "${YELLOW}====================================================${NC}"

if [ $NET_UE_COUNT -gt 0 ]; then
    final_lat=$(echo "scale=2; $G_LAT_SUM / $NET_UE_COUNT" | bc)
    final_tdl=$(echo "scale=2; $G_TCP_DL_SUM / $NET_UE_COUNT" | bc)
    final_tul=$(echo "scale=2; $G_TCP_UL_SUM / $NET_UE_COUNT" | bc)
    final_udl=$(echo "scale=2; $G_UDP_DL_SUM / $NET_UE_COUNT" | bc)
    final_uul=$(echo "scale=2; $G_UDP_UL_SUM / $NET_UE_COUNT" | bc)

    echo -e "Active UEs Tested:  ${CYAN}$NET_UE_COUNT${NC}"
    echo -e "Avg Latency:        ${CYAN}$final_lat ms${NC}"
    echo -e "Avg TCP Downlink:   ${CYAN}$final_tdl Mbps${NC}"
    echo -e "Avg TCP Uplink:     ${CYAN}$final_tul Mbps${NC}"
    echo -e "Avg UDP Downlink:   ${CYAN}$final_udl Mbps${NC}"
    echo -e "Avg UDP Uplink:     ${CYAN}$final_uul Mbps${NC}"
else
    echo -e "${RED}No active UEs found to benchmark.${NC}"
fi
echo -e "${YELLOW}====================================================${NC}"
