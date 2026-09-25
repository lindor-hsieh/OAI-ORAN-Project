#!/bin/bash
# clean_env.sh — 整套重啟前的環境清理與驗證（在 PC1 執行，透過 ssh 一併清理 PC2/PC3）
#
# 規則：每次重啟整個系統前，先跑這支腳本確認環境乾淨，再依序啟動
# （start_iab_server.sh → run_local_pc2.sh → run_local_pc3.sh → run_local_pc1.sh --skip-server）。
#
# 清掉：所有 RAN/核心網/xApp/inference/FL 容器（docker volume 不動，MongoDB 資料與模型
#       checkpoint 保留）、殘留的 traffic_scenario/measure_stage/training_* 行程、
#       遺留 iperf3、/tmp/iperf_client_*.log、/tmp/scenario_invalid_* 標記與 *.invalid。
# 註：作業系統的 systemd iperf3.service（/usr/bin/iperf3 --server，開機即在、跟實驗無關）不算殘留。
# 驗證：三台都沒有殘留容器、nr-softmodem/nr-uesoftmodem/iperf3/場景行程、暫存檔；
#       任一台不乾淨 → exit 1（不要在不乾淨的環境上重啟）。
set -u

CONTAINER_RE='^(rfsim5g-|xapp-node|inference-node|flower-|global-xapp|flexric|mongodb)'
PROC_RE='[t]raffic_scenario.py|[m]easure_stage.py|[t]raining_scenario_driver|[t]raining_watchdog|[n]r-softmodem|[n]r-uesoftmodem|[i]perf3'

clean_local() {
    # 先殺行程再刪容器，避免場景 supervisor 把 iperf3 又拉起來
    ps -eo pid,args | grep -E "[t]raffic_scenario.py|[m]easure_stage.py|[t]raining_scenario_driver|[t]raining_watchdog" \
        | grep -v clean_env | awk '{print $1}' | xargs -r kill 2>/dev/null
    sleep 1
    local names
    names=$(docker ps -aq --filter "name=rfsim5g-" --filter "name=xapp-node" --filter "name=inference-node" \
            --filter "name=flower-" --filter "name=global-xapp" --filter "name=flexric" --filter "name=mongodb")
    [ -n "$names" ] && docker rm -f $names >/dev/null 2>&1
    rm -f /tmp/iperf_client_*.log /tmp/scenario_invalid_*.txt /tmp/*.csv.invalid
}

verify_local() {
    local bad=0 c p f
    c=$(docker ps -a --format '{{.Names}}' | grep -E "$CONTAINER_RE")
    [ -n "$c" ] && { echo "  殘留容器: $(echo $c | tr '\n' ' ')"; bad=1; }
    p=$(ps -eo pid,args | grep -E "$PROC_RE" | grep -v -E "clean_env|grep|/usr/bin/iperf3 --server" | head -3)
    [ -n "$p" ] && { echo "  殘留行程: $p"; bad=1; }
    f=$(ls /tmp/iperf_client_*.log /tmp/scenario_invalid_*.txt 2>/dev/null | head -3)
    [ -n "$f" ] && { echo "  殘留暫存檔: $f"; bad=1; }
    return $bad
}

FUNCS="$(declare -f clean_local verify_local); CONTAINER_RE='$CONTAINER_RE'; PROC_RE='$PROC_RE'"
rc=0
for h in local pc2 pc3; do
    echo "[clean_env] === $h ==="
    if [ "$h" = local ]; then
        clean_local; verify_local || rc=1
    else
        ssh "$h" "$FUNCS; clean_local; verify_local" || rc=1
    fi
done

if [ $rc -eq 0 ]; then
    echo "[clean_env] ✔ 三台環境皆已乾淨，可以依序重啟"
else
    echo "[clean_env] ✘ 仍有殘留（見上），請處理後重跑；不要在不乾淨的環境上重啟" >&2
fi
exit $rc
