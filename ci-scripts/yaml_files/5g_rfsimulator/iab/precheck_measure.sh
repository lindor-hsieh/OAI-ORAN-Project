#!/bin/bash
# precheck_measure.sh — 量測前檢查（在 PC1 執行）：13/13 E2、xApp 數量（EXPECT_XAPP，PF=0、Stage 2~5=12）、CU/DU/FlexRIC 與各 relay/access RestartCount、
# UE 連通性（0% loss；UE1~8 在 PC2、UE9~16 在 PC3、relay 直連 UE17~24 在 PC1）、對應數量的 iperf3 server、三台 S 一致。
# RELAY_UES=0 時只檢查 16 個 UE（2026-10-01 前的拓樸）。任一項不過 → 最後印 PRECHECK FAIL。
ok=1
NUE=24; [ "${RELAY_UES:-1}" = 0 ] && NUE=16
e2=$(docker logs flexric 2>&1 | grep -c "E2 SETUP-REQUEST"); echo "E2 SETUP-REQUEST = $e2 (應 13)"; [ "$e2" = 13 ] || ok=0
xa=$(docker ps --format '{{.Names}}' | grep -c '^xapp-node'); echo "運行中 xApp 容器 = $xa (應 ${EXPECT_XAPP:-0})"; [ "$xa" = "${EXPECT_XAPP:-0}" ] || ok=0
for c in rfsim5g-donor-cu rfsim5g-donor-du flexric; do rc=$(docker inspect --format '{{.RestartCount}}' $c); echo "$c RestartCount=$rc"; [ "$rc" = 0 ] || ok=0; done
for n in 1 2 3 4; do for k in mt du; do rc=$(docker inspect --format '{{.RestartCount}}' rfsim5g-iab-$k-$n); [ "$rc" = 0 ] || { echo "relay $k-$n RestartCount=$rc"; ok=0; }; done; done
if [ $NUE = 24 ]; then
  for u in $(seq 17 24); do rc=$(docker inspect --format '{{.RestartCount}}' rfsim5g-end-ue-$u 2>/dev/null || echo missing); [ "$rc" = 0 ] || { echo "relay UE$u RestartCount=$rc"; ok=0; }; done
fi
for h in pc2 pc3; do
  bad=$(ssh $h 'for c in $(docker ps -a --format "{{.Names}}" | grep -E "^rfsim5g-(iab-mt|iab-du|end-ue)-"); do rc=$(docker inspect --format "{{.RestartCount}}" $c); [ "$rc" = 0 ] || echo "$c=$rc"; done')
  [ -z "$bad" ] || { echo "$h 有重啟過的容器: $bad"; ok=0; }
done
lost=0
for u in $(seq 1 $NUE); do
  if [ $u -gt 16 ]; then
    r=$(docker exec rfsim5g-end-ue-$u ping -c3 -W3 192.168.72.135 2>&1 | grep -oE '[0-9]+% packet loss')
  else
    h=pc2; [ $u -gt 8 ] && h=pc3
    r=$(ssh $h "docker exec rfsim5g-end-ue-$u ping -c3 -W3 192.168.72.135 2>&1 | grep -oE '[0-9]+% packet loss'")
  fi
  [ "$r" = "0% packet loss" ] || { echo "UE$u: $r"; lost=1; }
done
[ $lost = 0 ] && echo "$NUE/$NUE UE ping 0% loss" || ok=0
np=$(docker exec rfsim5g-oai-ext-dn ss -tln | grep -cE ':52(0[1-9]|1[0-9]|2[0-4])\b'); echo "ext-dn iperf3 listening ports (5201~5224) = $np (應 $NUE)"; [ "$np" = $NUE ] || ok=0
SF=/home/lindor/openairinterface5g/cmake_targets/ran_build/build/rfsim_speed.txt
s1=$(cat $SF); s2=$(ssh pc2 cat $SF); s3=$(ssh pc3 cat $SF); echo "S@pc1=$s1 S@pc2=$s2 S@pc3=$s3"
{ [ "$s1" = "$s2" ] && [ "$s1" = "$s3" ]; } || { echo "三台 S 不一致"; ok=0; }
[ $ok = 1 ] && echo "PRECHECK PASS" || echo "PRECHECK FAIL"
