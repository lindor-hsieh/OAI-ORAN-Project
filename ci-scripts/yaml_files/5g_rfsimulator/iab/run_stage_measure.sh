#!/bin/bash
# run_stage_measure.sh — 兩狀態 Scenario T 量測（在 PC1 執行，PC2/PC3 同時起跑）
# usage: OUT_DIR=<輸出目錄> bash iab/run_stage_measure.sh <tcp|udp> <tag>
# 相位 110 s × 11 個 = 一個完整壅塞週期（5/11 壅塞）；兩台主機用同一個 --phase-origin；量測 1230 s、每 5 秒取樣。
# 完成後 CSV/log 在 $OUT_DIR，再跑 python3 iab/analyze_stage.py $OUT_DIR <tag>。
P=$1; TAG=$2
ORIGIN=$(($(date +%s)+15))
S=${OUT_DIR:-/tmp/stage_run}; mkdir -p $S
D=/home/lindor/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator
for h in pc2 pc3; do
  nohup ssh $h "cd $D && python3 scenarios/traffic_scenario.py --scenario T --protocol $P --host $h --phase-duration 110 --num-phases 11 --phase-origin $ORIGIN; echo SCN_EXIT=\$?" > $S/${TAG}_scn_$h.log 2>&1 &
  nohup ssh $h "cd $D && python3 iab/measure_stage.py --host $h --duration 1230 --interval 5 --out /tmp/${TAG}_$h.csv; echo MEAS_EXIT=\$?" > $S/${TAG}_meas_$h.log 2>&1 &
done
wait
for h in pc2 pc3; do scp -q $h:/tmp/${TAG}_$h.csv $S/${TAG}_$h.csv; grep -h -E "EXIT" $S/${TAG}_scn_$h.log $S/${TAG}_meas_$h.log; done
echo RUN DONE
