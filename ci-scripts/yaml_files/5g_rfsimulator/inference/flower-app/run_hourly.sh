#!/bin/bash
# run_hourly.sh — Phase 5 FL 排程 wrapper
#
# `flwr run` 一次只提交一組有限輪數的 job，執行完就結束——不是常駐迴圈。
# SuperLink/SuperNode 是常駐服務（見 docker-compose 的 flower-superlink/
# flower-supernode-nodeN），這支腳本才是負責「每隔一段時間送出一輪 FL job」
# 的排程器，刻意放在 ServerApp 程式碼外面，不用 time.sleep(3600) 塞進
# server_app.py。

set -u

FEDERATION="${FL_FEDERATION:-local-deployment}"
INTERVAL_S="${FL_ROUND_INTERVAL_S:-3600}"
APP_PATH="${FL_APP_PATH:-/app/flower-app}"

echo "[fl-scheduler] 啟動，federation=${FEDERATION} interval=${INTERVAL_S}s app=${APP_PATH}"

while true; do
    echo "[fl-scheduler] $(date '+%F %T') 提交 FL 輪次 (federation=${FEDERATION})..."
    flwr run "${APP_PATH}" "${FEDERATION}" \
        || echo "[fl-scheduler] $(date '+%F %T') flwr run 失敗，下個週期重試"
    echo "[fl-scheduler] 睡眠 ${INTERVAL_S} 秒..."
    sleep "${INTERVAL_S}"
done
