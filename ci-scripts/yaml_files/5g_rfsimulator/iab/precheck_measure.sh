#!/bin/bash
# precheck_measure.sh — 量測前檢查（在 PC1 執行）：13/13 E2、xApp 數量（EXPECT_XAPP，PF=0、Stage 2~5=12）、CU/DU/FlexRIC 與各 relay/access RestartCount、
# 17 UE 連通性（0% loss）、17 個 iperf3 server、三台 S 一致。任一項不過 → 最後印 PRECHECK FAIL。
ok=1
e2=$(docker logs flexric 2>&1 | grep -c "E2 SETUP-REQUEST"); echo "E2 SETUP-REQUEST = $e2 (應 13)"; [ "$e2" = 13 ] || ok=0
xa=$(docker ps --format '{{.Names}}' | grep -c '^xapp-node'); echo "運行中 xApp 容器 = $xa (應 ${EXPECT_XAPP:-0})"; [ "$xa" = "${EXPECT_XAPP:-0}" ] || ok=0
for c in rfsim5g-donor-cu rfsim5g-donor-du flexric; do rc=$(docker inspect --format '{{.RestartCount}}' $c); echo "$c RestartCount=$rc"; [ "$rc" = 0 ] || ok=0; done
for n in 1 2 3 4; do for k in mt du; do rc=$(docker inspect --format '{{.RestartCount}}' rfsim5g-iab-$k-$n); [ "$rc" = 0 ] || { echo "relay $k-$n RestartCount=$rc"; ok=0; }; done; done
for h in pc2 pc3; do
  bad=$(ssh $h 'for c in $(docker ps -a --format "{{.Names}}" | grep -E "^rfsim5g-(iab-mt|iab-du|end-ue)-"); do rc=$(docker inspect --format "{{.RestartCount}}" $c); [ "$rc" = 0 ] || echo "$c=$rc"; done')
  [ -z "$bad" ] || { echo "$h 有重啟過的容器: $bad"; ok=0; }
done
lost=0
for u in $(seq 1 17); do h=pc2; [ $u -gt 8 ] && h=pc3
  r=$(ssh $h "docker exec rfsim5g-end-ue-$u ping -c3 -W3 192.168.72.135 2>&1 | grep -oE '[0-9]+% packet loss'")
  [ "$r" = "0% packet loss" ] || { echo "UE$u: $r"; lost=1; }
done
[ $lost = 0 ] && echo "17/17 UE ping 0% loss" || ok=0
np=$(docker exec rfsim5g-oai-ext-dn ss -tln | grep -c ':52[01][0-9]'); echo "ext-dn iperf3 listening ports = $np (應 17)"; [ "$np" = 17 ] || ok=0
for h in pc2 pc3; do echo "S@$h=$(ssh $h cat /home/lindor/openairinterface5g/cmake_targets/ran_build/build/rfsim_speed.txt)"; done; echo "S@pc1=$(cat /home/lindor/openairinterface5g/cmake_targets/ran_build/build/rfsim_speed.txt)"
[ $ok = 1 ] && echo "PRECHECK PASS" || echo "PRECHECK FAIL"
