#!/bin/bash

# 1. 啟動 IAB 系統 (如果已經啟動，這行只會確保它在跑)
echo "🚀 Starting IAB System..."
docker compose -f docker-compose-iab.yaml up -d

# 2. 等待 10 秒，確保 UPF 容器已經完全啟動
echo "⏳ Waiting for UPF to initialize (10s)..."
sleep 10

# 3. 自動執行 NAT (開啟 IP 偽裝)
echo "🔧 Configuring NAT (IP Masquerading)..."
docker exec rfsim5g-oai-upf iptables -t nat -A POSTROUTING -s 12.1.1.0/24 -o eth0 -j MASQUERADE

# 4. 顯示完成訊息
echo "✅ Done! 5G IAB is live with Internet Access."
echo "   End-UE IP: 12.1.1.3"
echo "   Try ping: docker exec -it rfsim5g-end-ue ping -I oaitun_ue1 -c 4 8.8.8.8"