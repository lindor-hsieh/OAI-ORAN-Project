#!/bin/bash

# ==========================================
# 1. 環境準備：安裝 iptables
# ==========================================
echo "[IAB-MT] Installing iptables..."
# 更新 package list 並安裝 iptables (因為 develop 版映像檔通常很精簡)
apt-get update && apt-get install -y iptables iproute2

# ==========================================
# 2. 等待網卡並設定路由
# ==========================================
echo "[IAB-MT] Waiting for oaitun_ue1 interface..."

# 這裡我們用一個背景迴圈來等待網卡出現
# 因為網卡是 UE 程式啟動後連上基站才會產生的
(
    while ! ip link show oaitun_ue1 > /dev/null 2>&1; do
        sleep 1
    done
    
    echo "[IAB-MT] Interface oaitun_ue1 detected! Configuring NAT..."
    sysctl -w net.ipv4.ip_forward=1
    iptables -t nat -A POSTROUTING -o oaitun_ue1 -j MASQUERADE
    echo "[IAB-MT] Routing configured successfully."
) &  # 注意：這個括號和 & 符號讓它在背景執行，不卡住後面的 UE 啟動

# ==========================================
# 3. 啟動 UE (IAB-MT)
# ==========================================
echo "[IAB-MT] Starting nr-uesoftmodem with develop parameters..."

# 這裡我們把所有參數「寫死」，確保 develop 版一定吃得到頻率設定
# 解決 Assertion failed (0 Hz) 的問題
exec /opt/oai-nr-ue/bin/nr-uesoftmodem \
    -E --rfsim -r 106 --numerology 1 \
    -C 3319680000 \
    --uicc0.imsi 208990100001100 \
    --rfsimulator.serveraddr 192.168.71.140 \
    --log_config.global_log_options level,nocolor,time