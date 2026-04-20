#!/bin/bash
# monitor_drl.sh — DRL 訓練狀態監控儀表板（PC1 執行）
#
# 合併四個監控終端為單一儀表板，每 INTERVAL 秒自動刷新：
#   - E2 連線數 / ZMQ socket 狀態
#   - MongoDB experience 累積量（各 Node 進度條）
#   - 各 Node 推論伺服器最新狀態（啟發式 / DRL / actor_loss）
#   - fallback 告警
#   - channelmod telnet 連線狀態
#
# 使用方式：
#   bash monitor_drl.sh          # 預設 15 秒刷新
#   bash monitor_drl.sh 30       # 30 秒刷新

INTERVAL=${1:-15}

# Node 3/4/5 的 channelmod telnetsrv 在 PC2 的容器內
PC2_USER="lindor"
PC2_IP="192.168.88.2"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=3 -o BatchMode=yes"

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

# 回傳 JSON：{n1:N, n2:N, ...}
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

# 各 Node 推論 log 最後一條有意義的行
check_inference_status() {
    local node=$1
    docker logs "inference-node${node}" 2>&1 \
        | grep -E "actor_loss|DRL 推論模式|啟發式|推論統計|經驗數量不足|已綁定" \
        | tail -1 2>/dev/null
}

# 累計 fallback 次數
check_fallback() {
    local node=$1
    docker logs "xapp-node${node}" 2>&1 2>/dev/null | grep -c "fallback" || echo "0"
}

# channelmod 連線
# Node 1 (9089) / Node 2 (9090)：PC1 本機，直接查
# Node 3 (9091) / Node 4 (9092) / Node 5 (9093)：SSH 到 PC2 容器內查
check_channelmod() {
    local node=$1
    local port=$((9088 + node))   # node1→9089, node2→9090, node3→9091, node4→9092, node5→9093
    local container="rfsim5g-iab-du-${node}"
    if [ "$node" -le 2 ]; then
        bash -c "exec 3<>/dev/tcp/127.0.0.1/${port} && echo 'channelmod show config' >&3 && sleep 0.5 && cat <&3" 2>/dev/null \
            | grep -q . && echo "OK" || echo "NO"
    else
        ssh $SSH_OPTS ${PC2_USER}@${PC2_IP} \
            "docker exec $container bash -c 'exec 3<>/dev/tcp/127.0.0.1/${port} && echo \"channelmod show config\" >&3 && sleep 0.5 && cat <&3' 2>/dev/null | grep -q ." 2>/dev/null \
            && echo "OK" || echo "NO"
    fi
}

# ── 主迴圈 ──────────────────────────────────────────────────

PREV_EXP_FILE="/tmp/monitor_drl_prev_exp.json"
echo "{}" > "$PREV_EXP_FILE"

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

    # ── Experience 累積 ───────────────────────────────────────
    echo -e "${YELLOW}── Experience 累積 ──────────────────────────────────────────${NC}"

    EXP_JSON=$(check_experiences)
    if [ -n "$EXP_JSON" ]; then
        echo "$EXP_JSON" | python3 -c "
import json, sys
try:
    data = json.loads(sys.stdin.read().strip())
    with open('$PREV_EXP_FILE') as f:
        prev = json.loads(f.read())
    for k in sorted(data.keys()):
        v = data[k]
        node_num = k[1]
        delta = v - prev.get(k, 0)
        delta_str = f'+{delta}' if delta >= 0 else str(delta)
        print(f'  Node{node_num}: {v:>7,} 筆  ({delta_str}/刷新)')
    with open('$PREV_EXP_FILE', 'w') as f:
        f.write(json.dumps(data))
except Exception as e:
    print(f'  (解析失敗: {e})')
"
    else
        echo -e "  ${RED}(MongoDB 未回應，確認 mongodb 容器狀態)${NC}"
    fi
    echo ""

    # ── 推論狀態 ─────────────────────────────────────────────
    echo -e "${YELLOW}── 推論狀態 ─────────────────────────────────────────────────${NC}"
    for i in 1 2 3 4 5; do
        STATUS=$(check_inference_status $i)
        FB=$(check_fallback $i)

        # 根據關鍵字著色
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

    # ── 底部提示 ─────────────────────────────────────────────
    echo -e "${DIM}  刷新間隔：${INTERVAL}s | Ctrl+C 離開 | bash monitor_drl.sh [秒數] 可調整${NC}"
    echo -e "${DIM}  $(date -d "+${INTERVAL} seconds" '+下次刷新：%H:%M:%S')${NC}"

    sleep "$INTERVAL"
done
