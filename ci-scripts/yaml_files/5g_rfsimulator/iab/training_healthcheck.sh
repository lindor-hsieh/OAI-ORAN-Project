#!/bin/bash
# training_healthcheck.sh — Stage 2/3 收斂訓練期間的人工定期檢查彙總報告
#
# 只在 PC1 執行（MongoDB／12 個 inference-nodeN 都在 PC1）。這不是自動化迴圈
# ——是 coordinator 每隔幾小時手動跑一次的快照報告，不會對系統做任何變更
# （唯一的副作用是把 checkpoint mtime 的觀測值寫進 /tmp 的小狀態檔，供下次
# 執行時算出「這段期間有沒有真的發生 FL 聚合」）。
#
# 用法：
#   bash iab/training_healthcheck.sh [--since 3600]
#   --since：檢查「PRB 是否確實生效」時，docker logs 回看的秒數，預設 3600
#            （建議跟實際檢查間隔數量級一致，例如每 2 小時檢查一次就用 7200）

set -u
COMPOSE_DIR=/home/lindor/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator
STATE_FILE=/tmp/training_healthcheck_ckpt_mtimes.txt

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
section() { echo -e "\n${BOLD}${CYAN}== $* ==${NC}"; }
ok()   { echo -e "${GREEN}  ✓${NC} $*"; }
warn() { echo -e "${YELLOW}  ⚠${NC} $*"; }
err()  { echo -e "${RED}  ✗${NC} $*"; }

SINCE=3600
while [[ $# -gt 0 ]]; do
    case "$1" in
        --since) SINCE="$2"; shift 2 ;;
        *) echo "未知參數: $1" >&2; exit 1 ;;
    esac
done

cd "$COMPOSE_DIR" || { echo "cd 到 $COMPOSE_DIR 失敗" >&2; exit 1; }

echo -e "${BOLD}訓練健康檢查報告 — $(date '+%Y-%m-%d %H:%M:%S')${NC}"

# =============================================================================
# A. 資料乾淨度：經驗數量、維度、reward 有效性、REWARD_MODE 污染、重複記錄
# =============================================================================
section "A. 資料乾淨度"

REWARD_MODE_NODE1=$(docker exec inference-node1 env 2>/dev/null | grep -oP '(?<=REWARD_MODE=).*' || echo "unknown")
echo "  目前 REWARD_MODE（以 node1 為代表，理論上 12 個節點應一致）：$REWARD_MODE_NODE1"

docker exec mongodb mongosh iab_xapp --quiet --eval '
for (let i = 1; i <= 12; i++) {
    const col = db["node" + i + "_experiences"];
    const count = col.countDocuments();
    const recent = col.find({}).sort({timestamp: -1}).limit(20).toArray();
    let badDim = 0, badReward = 0, hasLambda = 0;
    recent.forEach(d => {
        if (!d.state_vec || d.state_vec.length !== 50) badDim++;
        if (!d.next_state_vec || d.next_state_vec.length !== 50) badDim++;
        if (typeof d.reward !== "number" || !isFinite(d.reward)) badReward++;
        if (d.lambda_applied !== undefined) hasLambda++;
    });
    const dupWindow = new Date(Date.now() - 5 * 60 * 1000);
    const dupGroups = col.aggregate([
        {$match: {timestamp: {$gte: dupWindow}}},
        {$group: {_id: "$timestamp", n: {$sum: 1}}},
        {$match: {n: {$gt: 1}}},
        {$count: "n"}
    ]).toArray();
    const dupCount = dupGroups.length ? dupGroups[0].n : 0;
    print(`node${i} count=${count} sample=${recent.length} bad_dim=${badDim} bad_reward=${badReward} has_lambda_applied=${hasLambda} dup_timestamp_groups=${dupCount}`);
}
' 2>/dev/null | while read -r line; do
    if echo "$line" | grep -qE "bad_dim=[1-9]|bad_reward=[1-9]|dup_timestamp_groups=[1-9]"; then
        err "$line"
    elif echo "$line" | grep -q "has_lambda_applied=[1-9]" && [[ "$REWARD_MODE_NODE1" == "throughput_only" ]]; then
        err "$line  (REWARD_MODE=throughput_only 不應該有 lambda_applied 欄位)"
    else
        ok "$line"
    fi
done

# =============================================================================
# B. PRB 是否確實生效（AI 決策 vs Fallback，最近 ${SINCE}s）
# =============================================================================
section "B. PRB 分配確實生效（最近 ${SINCE}s，AI 決策 / Fallback）"

for n in $(seq 1 12); do
    ai=$(docker logs --since "${SINCE}s" "xapp-node${n}" 2>&1 | grep -c "AI 決策" || echo 0)
    fb=$(docker logs --since "${SINCE}s" "xapp-node${n}" 2>&1 | grep -c "Fallback" || echo 0)
    if [[ "$ai" -eq 0 ]]; then
        warn "node${n}: AI 決策=0 Fallback=${fb}（若剛啟動不久是正常的，持續為 0 才需要留意）"
    elif [[ "$fb" -gt 0 ]]; then
        warn "node${n}: AI 決策=${ai} Fallback=${fb}（有 fallback，檢查 ZMQ round-trip 是否偶爾逾時）"
    else
        ok "node${n}: AI 決策=${ai} Fallback=0"
    fi
done

# =============================================================================
# C. Local DRL 收斂（Track 1）
# =============================================================================
section "C. Local DRL 收斂判準（Track 1，MongoDB 版，跨容器重啟持續累積）"
python3 iab/check_convergence_mongo.py --nodes 1 2 3 4 5 6 7 8 9 10 11 12 \
    --window-minutes 120 --bucket-minutes 5 --min-buckets 8 2>&1 | sed 's/^/  /'
echo "  （舊版 docker-logs 版本仍可用：python3 iab/check_convergence.py --nodes 1..12 --window 20——"
echo "   但長時間收斂訓練期間容器頻繁因崩潰復原重建，log 歷史會被砍掉，這裡不再預設執行）"

# =============================================================================
# D. Global FL 真實聚合輪數（Track 2，checkpoint mtime 變化）
# =============================================================================
section "D. Global FL 真實聚合（Track 2，checkpoint mtime 變化）"

declare -A CURR_MTIME
for n in $(seq 1 12); do
    mt=$(docker run --rm -v "iab-xapp-model-node${n}:/models" alpine \
        sh -c 'stat -c %Y /models/model_node'"${n}"'.pt 2>/dev/null || echo 0')
    CURR_MTIME[$n]=$mt
done

CHANGED=0
if [[ -f "$STATE_FILE" ]]; then
    declare -A PREV_MTIME
    while IFS='=' read -r k v; do PREV_MTIME[$k]=$v; done < "$STATE_FILE"
    for n in $(seq 1 12); do
        prev=${PREV_MTIME[$n]:-0}
        curr=${CURR_MTIME[$n]}
        if [[ "$curr" != "0" && "$curr" != "$prev" ]]; then
            CHANGED=$((CHANGED + 1))
        fi
    done
    ok "自上次健康檢查以來，checkpoint mtime 有變化的節點數：${CHANGED}/12（至少代表這段時間內發生過 >=1 輪真實聚合）"
else
    warn "找不到上次的狀態檔（$STATE_FILE），這是第一次執行，無法比較——下次執行才看得出 delta"
fi

: > "$STATE_FILE"
for n in $(seq 1 12); do
    echo "${n}=${CURR_MTIME[$n]}" >> "$STATE_FILE"
done

echo "  輔助佐證（flower-superlink 最近 ${SINCE}s 的 log，跳過本輪聚合次數）："
SKIPPED=$(docker logs --since "${SINCE}s" flower-superlink 2>&1 | grep -c "跳過本輪聚合")
[[ -z "$SKIPPED" ]] && SKIPPED="N/A"
echo "    跳過次數（找不到容器或指令輸出格式不符時顯示 N/A，需人工確認 log 實際落在哪個容器）: $SKIPPED"

echo -e "\n${BOLD}健康檢查完成 — $(date '+%Y-%m-%d %H:%M:%S')${NC}"
