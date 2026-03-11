#!/bin/bash
# IAB Performance Benchmark Script (Standalone)
# Usage: ./iab_perf_test.sh [Optional: Iterations]

# ==========================================
# 1. 參數設定
# ==========================================
UPF_DN_IP="192.168.72.135"   # 核心網 Traffic Server IP
DEFAULT_ITERATIONS=3         # 預設每個 UE 測幾次

# 如果使用者有輸入參數，就用輸入的次數，否則用預設值
ITERATIONS=${1:-$DEFAULT_ITERATIONS}

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
        # 使用 -t 2 (2秒) 快速採樣，避免拖太久
        local dl_raw=$(docker exec $UE_NAME iperf3 -c $UPF_DN_IP -t 2 -R 2>/dev/null)
        local dl_val=$(echo "$dl_raw" | grep "receiver" | awk '{print $7}')
        if [ ! -z "$dl_val" ] && [ "$dl_val" != "0.00" ]; then 
            u_tcp_dl=$(echo "$u_tcp_dl + $dl_val" | bc)
            u_tcp_dl_c=$((u_tcp_dl_c+1))
        fi

        # 3. TCP Uplink
        local ul_raw=$(docker exec $UE_NAME iperf3 -c $UPF_DN_IP -t 2 2>/dev/null)
        local ul_val=$(echo "$ul_raw" | grep "receiver" | awk '{print $7}')
        if [ ! -z "$ul_val" ] && [ "$ul_val" != "0.00" ]; then 
            u_tcp_ul=$(echo "$u_tcp_ul + $ul_val" | bc)
            u_tcp_ul_c=$((u_tcp_ul_c+1))
        fi

        # 4. UDP Downlink (頻寬設為 20M 測試瓶頸)
        local udl_raw=$(docker exec $UE_NAME iperf3 -c $UPF_DN_IP -u -b 20M -t 2 -R 2>/dev/null)
        local udl_val=$(echo "$udl_raw" | grep "receiver" | awk '{print $7}')
        if [ ! -z "$udl_val" ] && [ "$udl_val" != "0.00" ]; then 
            u_udp_dl=$(echo "$u_udp_dl + $udl_val" | bc)
            u_udp_dl_c=$((u_udp_dl_c+1))
        fi
        
        # 5. UDP Uplink
        local uul_raw=$(docker exec $UE_NAME iperf3 -c $UPF_DN_IP -u -b 20M -t 2 2>/dev/null)
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

    # UDP DL 處理
    if [ $u_udp_dl_c -gt 0 ]; then
        local a_udl=$(echo "scale=2; $u_udp_dl / $u_udp_dl_c" | bc)
        echo -e "   Avg.UDP Downlink:       ${GREEN}${a_udl} Mbps${NC}"
        G_UDP_DL_SUM=$(echo "$G_UDP_DL_SUM + $a_udl" | bc)
    fi

    NET_UE_COUNT=$((NET_UE_COUNT + 1))
}

# ==========================================
# 3. 執行測試
# ==========================================

echo -e "${YELLOW}Starting IAB Network Benchmark...${NC}"
echo -e "Target Server: $UPF_DN_IP"
echo -e "Iterations per UE: $ITERATIONS"

# 掃描並測試 UE 1 ~ UE 6
for i in {1..6}; do 
    run_benchmarks "rfsim5g-end-ue-$i"
done

# ==========================================
# 4. 全網總結報告
# ==========================================
echo -e "\n${YELLOW}====================================================${NC}"
echo -e "${YELLOW}           全網 IAB Performance Summary            ${NC}"
echo -e "${YELLOW}====================================================${NC}"

if [ $NET_UE_COUNT -gt 0 ]; then
    final_lat=$(echo "scale=2; $G_LAT_SUM / $NET_UE_COUNT" | bc)
    final_tdl=$(echo "scale=2; $G_TCP_DL_SUM / $NET_UE_COUNT" | bc)
    final_udl=$(echo "scale=2; $G_UDP_DL_SUM / $NET_UE_COUNT" | bc)

    echo -e "Active UEs Tested:  ${CYAN}$NET_UE_COUNT${NC}"
    echo -e "Network Latency:    ${CYAN}$final_lat ms${NC}"
    echo -e "Avg TCP Throughput: ${CYAN}$final_tdl Mbps${NC} (Downlink)"
    echo -e "Avg UDP Throughput: ${CYAN}$final_udl Mbps${NC} (Downlink)"
else
    echo -e "${RED}No active UEs found to benchmark.${NC}"
fi
echo -e "${YELLOW}====================================================${NC}"