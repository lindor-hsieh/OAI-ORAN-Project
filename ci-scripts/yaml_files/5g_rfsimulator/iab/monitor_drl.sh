#!/bin/bash
# monitor_drl.sh — DRL 訓練狀態監控儀表板（PC1 執行）
#
# 合併四個監控終端為單一儀表板，每 INTERVAL 秒自動刷新：
#   - E2 連線數 / ZMQ socket 狀態
#   - MongoDB experience 累積量（各 Node 進度條）
#   - 各 Node 推論伺服器最新狀態（啟發式 / DRL / actor_loss）
#   - fallback 告警
#   - channelmod telnet 連線狀態
#   - xApp watchdog：ZMQ 凍結超過 FROZEN_THRESHOLD 輪自動重啟 xApp
#
# 使用方式：
#   bash monitor_drl.sh          # 預設 15 秒刷新
#   bash monitor_drl.sh 30       # 30 秒刷新

INTERVAL=${1:-15}

# ZMQ 凍結偵測：連續 N 輪 delta=0 → 自動重啟 xApp（N × INTERVAL ≈ 45s）
FROZEN_THRESHOLD=3

PC2_USER="lindor"
PC2_IP="192.168.88.2"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=3 -o BatchMode=yes"

INFERENCE_SRC=~/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator/inference/reward_calculator.py

GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

# ── 輔助函式 ────────────────────────────────────────────────

check_e2() {
    docker logs flexric 2>&1 | grep -c "E2 SETUP-REQUEST" 2>/dev/null || echo "0"
}

check_zmq() {
    ls /tmp/zmq_node*_inference.ipc 2>/dev/null | wc -l
}

check_experiences() {
    docker exec mongodb mongosh --quiet iab_xapp --eval \
        'print(JSON.stringify({
            n1: db.node1_experiences.countDocuments(),
            n2: db.node2_experiences.countDocuments(),
            n3: db.node3_experiences.countDocuments(),
            n4: db.node4_experiences.countDocuments(),
            n5: db.node5_experiences.countDocuments()
        }))' 2>/dev/null | grep "^{" | tail -1
}

check_inference_status() {
    local node=$1
    docker logs "inference-node${node}" 2>&1 \
        | grep -E "actor_loss|DRL 推論模式|啟發式|推論統計|經驗數量不足|已綁定" \
        | tail -1 2>/dev/null
}

check_fallback() {
    local node=$1
    docker logs "xapp-node${node}" 2>&1 2>/dev/null | grep -c "fallback" || echo "0"
}

check_channelmod() {
    local node=$1
    local port=$((9088 + node))
    local container="rfsim5g-iab-du"
    [ "$node" -gt 1 ] && container="rfsim5g-iab-du-${node}"

    if [ "$node" -le 2 ]; then
        timeout 2 docker exec "$container" bash -c \
            "exec 3<>/dev/tcp/127.0.0.1/${port} && exec 3>&-" 2>/dev/null \
            && echo "OK" || echo "NO"
    else
        ssh $SSH_OPTS ${PC2_USER}@${PC2_IP} \
            "timeout 2 docker exec $container bash -c 'exec 3<>/dev/tcp/127.0.0.1/${port} && exec 3>&-' 2>/dev/null" \
            2>/dev/null && echo "OK" || echo "NO"
    fi
}

# xApp watchdog：重啟所有 xApp 並補 reward_calculator.py
restart_xapps() {
    local reason=$1
    echo -e "\n${RED}[WATCHDOG $(date '+%H:%M:%S')] ${reason}，重啟所有 xApp...${NC}"
    for n in 1 2 3 4 5; do
        docker restart "xapp-node${n}" >/dev/null 2>&1 && \
            echo -e "  ${YELLOW}xapp-node${n} restarted${NC}" || \
            echo -e "  ${RED}xapp-node${n} restart 失敗${NC}"
    done
    # FlexRIC 重啟後 inference 容器的 reward_calculator.py 會被 image 覆蓋，需重新 cp
    sleep 3
    for n in 1 2 3 4 5; do
        docker cp "$INFERENCE_SRC" "inference-node${n}:/app/reward_calculator.py" >/dev/null 2>&1
    done
    echo -e "  ${GREEN}reward_calculator.py 已重新 cp 至所有 inference 容器${NC}"
}

# ── 主迴圈 ──────────────────────────────────────────────────

PREV_EXP_FILE="/tmp/monitor_drl_prev_exp.json"
echo "{}" > "$PREV_EXP_FILE"

FROZEN_COUNT=0          # 連續凍結輪數
WATCHDOG_MSG=""         # 最後一次 watchdog 觸發訊息
LAST_RESTART_TIME=0     # 避免在 watchdog 觸發後馬上再觸發

while true; do
    clear
    NOW=$(date '+%Y-%m-%d %H:%M:%S')

    echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════════════╗${NC}"
    printf "${BOLD}${CYAN}║   DRL 訓練監控儀表板   %-34s║${NC}\n" "$NOW"
    echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════════╝${NC}"
    echo ""

    # ── 連線狀態 ──────────────────────────────────────────────
    E2=$(check_e2)
    ZMQ=$(check_zmq)

    if [ "$E2" -ge 6 ] 2>/dev/null; then
        E2_STR="${GREEN}${E2}/6  ✓${NC}"
    else
        E2_STR="${RED}${E2}/6  ✗ (等待中)${NC}"
    fi

    if [ "$ZMQ" -ge 5 ] 2>/dev/null; then
        ZMQ_STR="${GREEN}${ZMQ}/5  ✓${NC}"
    else
        ZMQ_STR="${RED}${ZMQ}/5  ✗ (xApp 未全數啟動)${NC}"
    fi

    echo -e "  E2 連線 : $(echo -e "$E2_STR")    ZMQ Socket : $(echo -e "$ZMQ_STR")"
    echo ""

    # ── Experience 累積 + 凍結偵測 ──────────────────────────
    echo -e "${YELLOW}── Experience 累積 ──────────────────────────────────────────${NC}"

    EXP_JSON=$(check_experiences)
    ALL_FROZEN=0

    if [ -n "$EXP_JSON" ]; then
        DELTA_OUTPUT=$(echo "$EXP_JSON" | python3 -c "
import json, sys
try:
    data = json.loads(sys.stdin.read().strip())
    with open('$PREV_EXP_FILE') as f:
        prev = json.loads(f.read())
    total_delta = 0
    for k in sorted(data.keys()):
        v = data[k]
        node_num = k[1]
        delta = v - prev.get(k, 0)
        total_delta += delta
        delta_str = f'+{delta}' if delta >= 0 else str(delta)
        print(f'  Node{node_num}: {v:>7,} 筆  ({delta_str}/刷新)')
    with open('$PREV_EXP_FILE', 'w') as f:
        f.write(json.dumps(data))
    # 輸出 total_delta 供 bash 判斷
    print(f'__TOTAL_DELTA__:{total_delta}')
except Exception as e:
    print(f'  (解析失敗: {e})')
    print('__TOTAL_DELTA__:-1')
" 2>/dev/null)

        # 擷取 total_delta
        TOTAL_DELTA=$(echo "$DELTA_OUTPUT" | grep "__TOTAL_DELTA__" | cut -d: -f2)
        echo "$DELTA_OUTPUT" | grep -v "__TOTAL_DELTA__"

        # 凍結判斷：全部 delta=0 且 ZMQ socket 數量正常（確認 xApp 確實在跑）
        NOW_TS=$(date +%s)
        SINCE_RESTART=$((NOW_TS - LAST_RESTART_TIME))
        if [ "${TOTAL_DELTA:-0}" -eq 0 ] && [ "$ZMQ" -ge 5 ] && [ "$SINCE_RESTART" -gt 60 ]; then
            FROZEN_COUNT=$((FROZEN_COUNT + 1))
            ALL_FROZEN=1
        else
            FROZEN_COUNT=0
        fi
    else
        echo -e "  ${RED}(MongoDB 未回應，確認 mongodb 容器狀態)${NC}"
        FROZEN_COUNT=0
    fi

    # 凍結狀態顯示
    if [ "$ALL_FROZEN" -eq 1 ]; then
        REMAINING=$((FROZEN_THRESHOLD - FROZEN_COUNT))
        if [ "$REMAINING" -le 0 ]; then
            echo -e "  ${RED}⚠ ZMQ 凍結 $((FROZEN_COUNT * INTERVAL))s！${NC}"
        else
            echo -e "  ${YELLOW}⚠ ZMQ 凍結偵測中（${FROZEN_COUNT}/${FROZEN_THRESHOLD} 輪，再 $((REMAINING * INTERVAL))s 觸發重啟）${NC}"
        fi
    fi

    # ── Watchdog 觸發 ────────────────────────────────────────
    if [ "$FROZEN_COUNT" -ge "$FROZEN_THRESHOLD" ]; then
        WATCHDOG_MSG="ZMQ 凍結 $((FROZEN_COUNT * INTERVAL))s（FlexRIC 可能重啟過）"
        restart_xapps "$WATCHDOG_MSG"
        FROZEN_COUNT=0
        LAST_RESTART_TIME=$(date +%s)
    fi

    echo ""

    # ── 推論狀態 ─────────────────────────────────────────────
    echo -e "${YELLOW}── 推論狀態 ─────────────────────────────────────────────────${NC}"
    for i in 1 2 3 4 5; do
        STATUS=$(check_inference_status $i)
        FB=$(check_fallback $i)

        if echo "$STATUS" | grep -qE "actor_loss|DRL 推論模式"; then
            STATUS_STR="${GREEN}${STATUS}${NC}"
        elif echo "$STATUS" | grep -q "啟發式"; then
            STATUS_STR="${YELLOW}${STATUS}${NC}"
        elif [ -z "$STATUS" ]; then
            STATUS_STR="${RED}(尚無 log，確認 inference-node${i} 容器)${NC}"
        else
            STATUS_STR="${DIM}${STATUS}${NC}"
        fi

        printf "  Node%-1s: " "$i"
        echo -e "$STATUS_STR"

        if [ "$FB" -gt 0 ] 2>/dev/null; then
            echo -e "         ${RED}⚠ fallback 累計 ${FB} 次（Python 端無回應）${NC}"
        fi
    done
    echo ""

    # ── channelmod 連線 ───────────────────────────────────────
    echo -e "${YELLOW}── channelmod 連線 ──────────────────────────────────────────${NC}"
    for node in 1 2 3 4 5; do
        CM=$(check_channelmod "$node")
        port=$((9088 + node))
        if [ "$CM" = "OK" ]; then
            echo -e "  Node${node} (:${port}): ${GREEN}OK ✓${NC}"
        else
            echo -e "  Node${node} (:${port}): ${RED}NO ✗${NC}"
        fi
    done
    echo ""

    # ── Watchdog 歷史 ─────────────────────────────────────────
    if [ -n "$WATCHDOG_MSG" ]; then
        echo -e "${DIM}  [上次 watchdog] ${WATCHDOG_MSG}${NC}"
    fi

    # ── 底部提示 ─────────────────────────────────────────────
    echo -e "${DIM}  刷新間隔：${INTERVAL}s | Ctrl+C 離開 | bash monitor_drl.sh [秒數] 可調整${NC}"
    echo -e "${DIM}  xApp watchdog：凍結 $((FROZEN_THRESHOLD * INTERVAL))s 自動重啟 | $(date -d "+${INTERVAL} seconds" '+下次刷新：%H:%M:%S')${NC}"

    sleep "$INTERVAL"
done
