#!/bin/bash
# run_stage_measure.sh — 兩狀態 Scenario T／TH／TM／TMH 量測（在 PC1 執行，PC1（relay 直連 UE）／PC2／PC3 同時起跑）
# usage: OUT_DIR=<輸出目錄> [SCENARIO=T|TH] [MEASURE_SEED=20260930] bash iab/run_stage_measure.sh <tcp|udp> <tag>
# 相位 110 s × 11 個 = 一個完整壅塞週期（5/11 壅塞）；兩台主機用同一個 --phase-origin；量測 1230 s、每 5 秒取樣。
# TH 與 T 共用完全相同的相位/壅塞結構（見 traffic_scenario.py::scenario_th_heterogeneous()），
# analyze_stage.py 不區分場景標籤，兩者輸出可用同一套分析流程。SCENARIO 預設 T，不指定時行為與過去完全相同。
# 完成後 CSV/log 在 $OUT_DIR，再跑 python3 iab/analyze_stage.py $OUT_DIR <tag>。
P=$1; TAG=$2
SCN=${SCENARIO:-T}
NPH=${NUM_PHASES:-11}; DUR=$((NPH*110+20))   # 預設 11 個相位＝1230 s（完整壅塞週期）；試驗可縮短
# TH 的每 UE 流量指派由 --seed 決定。2026-09-30 以前沒傳 --seed，兩台主機各自抽隨機 seed，每次量測的流量分配都不同，
# PF 與各 Stage 的 TH 結果無法配對比較。固定 seed 讓全部 Stage 的 TH 量測看到同一組流量（同 T 的固定輪替）。
MEASURE_SEED=${MEASURE_SEED:-20260930}
SEED_ARG=""; case "$SCN" in TH|TMH|HS|HSH|G|HS5|HSB|HSC|HSD|HSE) SEED_ARG="--seed $MEASURE_SEED";; esac
# CONGESTED_ONLY=1（2026-10-03，場景驗證用）：只跑壅塞相位（traffic_scenario.py --congested-only），NUM_PHASES 個相位全是壅塞相位
[ "${CONGESTED_ONLY:-0}" = 1 ] && SEED_ARG="$SEED_ARG --congested-only"
ORIGIN=${PHASE_ORIGIN:-$(($(date +%s)+15))}
S=${OUT_DIR:-/tmp/stage_run}; mkdir -p $S
D=/home/lindor/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator
# PC1 只有 relay 直連 UE17~24（2026-10-01 起）：場景與取樣都在本機跑；RELAY_UES=0 時跳過
if [ "${RELAY_UES:-1}" != 0 ]; then
  ( cd $D && nohup python3 scenarios/traffic_scenario.py --scenario $SCN $SEED_ARG --protocol $P --host pc1 --phase-duration 110 --num-phases $NPH --phase-origin $ORIGIN > $S/${TAG}_scn_pc1.log 2>&1; echo SCN_EXIT=$? >> $S/${TAG}_scn_pc1.log ) &
  ( cd $D && nohup python3 iab/measure_stage.py --host pc1 --duration $DUR --interval 5 --out $S/${TAG}_pc1.csv > $S/${TAG}_meas_pc1.log 2>&1; echo MEAS_EXIT=$? >> $S/${TAG}_meas_pc1.log ) &
fi
for h in pc2 pc3; do
  nohup ssh $h "cd $D && python3 scenarios/traffic_scenario.py --scenario $SCN $SEED_ARG --protocol $P --host $h --phase-duration 110 --num-phases $NPH --phase-origin $ORIGIN; echo SCN_EXIT=\$?" > $S/${TAG}_scn_$h.log 2>&1 &
  nohup ssh $h "cd $D && python3 iab/measure_stage.py --host $h --duration $DUR --interval 5 --out /tmp/${TAG}_$h.csv; echo MEAS_EXIT=\$?" > $S/${TAG}_meas_$h.log 2>&1 &
done
wait
for h in pc2 pc3; do scp -q $h:/tmp/${TAG}_$h.csv $S/${TAG}_$h.csv; grep -h -E "EXIT" $S/${TAG}_scn_$h.log $S/${TAG}_meas_$h.log; done
[ "${RELAY_UES:-1}" != 0 ] && grep -h -E "EXIT" $S/${TAG}_scn_pc1.log $S/${TAG}_meas_pc1.log
echo RUN DONE
