#!/bin/bash
# training_scenario_driver.sh — Stage 2/3 收斂訓練期間的訓練用流量場景輪替
#
# 目的：訓練場景必須跟固定的測試場景（Scenario R, seed=20260914，Stage 1/2/3
# 既有量測方法論沿用）不同，避免訓練/測試同分佈；同時要夠多樣化，涵蓋 CQI
# 對比（A）、流量不均（B）、最差公平性（C）、均勻隨機（D）、多組不同 seed 的
# 真實隨機分佈（R），見 CLAUDE.md 五階段路線圖與這次的收斂訓練計畫。
#
# 設計（靠 wall-clock epoch 自我校正，不需要狀態檔、不需要跨主機通訊）：
#   三台主機各自跑同一支腳本、同一個 --epoch，各自只用 --host 控制自己負責
#   的 UE/Node 子集（跟 traffic_scenario.py 既有的 Scenario R 跨主機同步手法
#   完全一樣）。任何時刻的「目前該跑哪個 slot」都是從 (now - epoch) 算出來
#   的，watchdog 殺掉這支腳本重開，或單純這支腳本自己的子行程跑完自然換下
#   一輪，都會自動接續到 wall clock 當下該在的位置，不需要額外狀態同步。
#
# 用法：
#   nohup bash iab/training_scenario_driver.sh --host pc1 --epoch 1758000000 \
#       > /tmp/driver_stage2_pc1.log 2>&1 &
#
# 三台主機的 --epoch 必須是同一個值（由 coordinator 在啟動時 `date +%s` 抓一次，
# 分別傳給 PC1/PC2/PC3 各自的呼叫，見 iab/training_watchdog.sh 的重啟邏輯）。
#
# --protocol {tcp,udp}（選填）：原樣傳給每個 traffic_scenario.py 呼叫，全部 slot 的
# 全部 UE 統一用該協定（UDP 版訓練場景）。未指定時各場景維持原本行為（T/A/B/C=tcp、
# R=TCP/UDP 混合）。三台主機必須一致；watchdog 用 --protocol 啟動時會在復原後
# 用同樣的值重啟驅動器，不會悄悄變回 TCP。
#
# 安全性：全程只呼叫 scenarios/traffic_scenario.py 既有的公開 --scenario 介面，
# 不直接碰 channelmod_ctrl.py，PATHLOSS_SAFE_MAX_DB=25.0 上限由該腳本的既有
# 程式碼結構保證不會超過（Scenario A/B/C 的固定 CQI 對照表、D/R 的
# `r * PATHLOSS_SAFE_MAX_DB` 連續抽樣，兩者皆已內建上限）。

set -u
COMPOSE_DIR=/home/lindor/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
log()  { echo -e "${CYAN}[driver $(date '+%H:%M:%S')]${NC} $*"; }
warn() { echo -e "${YELLOW}[driver $(date '+%H:%M:%S')] ⚠${NC} $*"; }
err()  { echo -e "${RED}[driver $(date '+%H:%M:%S')] ✗${NC} $*" >&2; }

HOST=""
EPOCH=""
PROTOCOL=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --host) HOST="$2"; shift 2 ;;
        --epoch) EPOCH="$2"; shift 2 ;;
        --protocol) PROTOCOL="$2"; shift 2 ;;
        *) err "未知參數: $1"; exit 1 ;;
    esac
done
if [[ -z "$HOST" || -z "$EPOCH" ]]; then
    err "用法: $0 --host {pc1,pc2,pc3} --epoch <unix_timestamp> [--protocol {tcp,udp}]"
    exit 1
fi
case "$PROTOCOL" in
    ""|tcp|udp) ;;
    *) err "--protocol 必須是 tcp 或 udp（收到: $PROTOCOL）"; exit 1 ;;
esac
case "$HOST" in
    pc1|pc2|pc3) ;;
    *) err "--host 必須是 pc1、pc2 或 pc3（收到: $HOST）"; exit 1 ;;
esac

cd "$COMPOSE_DIR" || { err "cd 到 $COMPOSE_DIR 失敗"; exit 1; }

# ── 輪替表（刻意排除 seed=20260914，那是固定測試場景保留的） ───────────
# scenario / seed（0=不適用）/ 這個 slot 的秒數。總長 18000 秒 = 5 小時一輪，
# 無限循環。
#
# [2026-09-18 重新設計] 原本 R 佔多數（65%），但現場發現 R 的 profile 設計
# （heavy/light/bursty，light/bursty 佔比高、閒置機率不低）長時間訓練下
# reward 訊號量級普遍偏小、對雜訊敏感（見 HISTORY.md 同日條目）——沒有任何
# 既有場景真正把頻寬塞滿到「需求超過供給」的壅塞狀態，A/B/C 之間的對比也都
# 在溫和範圍內。改成以新的 Scenario T（低/中/高流量 × 低/中/高路徑損耗 3x3
# 交叉設計，見 scenarios/traffic_scenario.py::scenario_t_tiered()）為主力
# （53%），確保系統性覆蓋整個負載/通道品質空間、包含真正的高負載壅塞狀態；
# R 降到 20% 保留其真實隨機分佈的多樣性；A/B/C 各自保留其特定邊界案例
# 價值（CQI 極端對比／流量嚴重不均／最差公平性）；D 移除（已被 T 的系統性
# 覆蓋取代，D 本來就標記「無統計依據，已被 R 取代」，這次直接一併移除）。
SLOT_SCENARIO=(T R T A T B T C R)
SLOT_SEED=(0 130001 0 0 0 0 0 0 130002)
SLOT_DURATION_S=(2400 1800 2400 1500 2400 1500 2400 1800 1800)

N_SLOTS=${#SLOT_SCENARIO[@]}
CYCLE_LEN_S=0
for d in "${SLOT_DURATION_S[@]}"; do CYCLE_LEN_S=$((CYCLE_LEN_S + d)); done

log "啟動：host=$HOST epoch=$EPOCH protocol=${PROTOCOL:-預設} cycle_len=${CYCLE_LEN_S}s (${N_SLOTS} slots)"

CHILD_PID=""
cleanup() {
    if [[ -n "$CHILD_PID" ]] && kill -0 "$CHILD_PID" 2>/dev/null; then
        log "收到終止訊號，停止目前的 traffic_scenario.py 子行程 (pid=$CHILD_PID)..."
        kill "$CHILD_PID" 2>/dev/null
        wait "$CHILD_PID" 2>/dev/null
    fi
    exit 0
}
trap cleanup SIGTERM SIGINT

while true; do
    now=$(date +%s)
    elapsed=$(( (now - EPOCH) % CYCLE_LEN_S ))
    # 找出目前落在哪個 slot、這個 slot 還剩幾秒
    cursor=0
    slot_idx=-1
    remaining=0
    for i in "${!SLOT_DURATION_S[@]}"; do
        d=${SLOT_DURATION_S[$i]}
        if (( elapsed < cursor + d )); then
            slot_idx=$i
            remaining=$(( cursor + d - elapsed ))
            break
        fi
        cursor=$((cursor + d))
    done
    if (( slot_idx < 0 )); then
        # 理論上不會發生（elapsed < CYCLE_LEN_S 恆成立），防禦性 fallback
        slot_idx=0
        remaining=${SLOT_DURATION_S[0]}
    fi
    # 剩餘時間太短（<30 秒）就直接跳過，等下一個 slot 開始，避免啟動一個
    # 幾乎立刻又要被換掉的 traffic_scenario.py 行程
    if (( remaining < 30 )); then
        sleep "$remaining"
        continue
    fi

    scenario=${SLOT_SCENARIO[$slot_idx]}
    seed=${SLOT_SEED[$slot_idx]}

    log "slot=$slot_idx scenario=$scenario remaining=${remaining}s"

    case "$scenario" in
        R)
            num_phases=$(( (remaining + 59) / 60 ))
            python3 scenarios/traffic_scenario.py --scenario R --seed "$seed" \
                --host "$HOST" --phase-duration 60 --num-phases "$num_phases" \
                ${PROTOCOL:+--protocol "$PROTOCOL"} &
            ;;
        T)
            num_phases=$(( (remaining + 59) / 60 ))
            python3 scenarios/traffic_scenario.py --scenario T \
                --host "$HOST" --phase-duration 60 --num-phases "$num_phases" \
                ${PROTOCOL:+--protocol "$PROTOCOL"} &
            ;;
        A|B|C)
            python3 scenarios/traffic_scenario.py --scenario "$scenario" \
                --host "$HOST" --duration "$remaining" \
                ${PROTOCOL:+--protocol "$PROTOCOL"} &
            ;;
        *)
            err "未知的 slot scenario: $scenario"
            sleep "$remaining"
            continue
            ;;
    esac
    CHILD_PID=$!
    wait "$CHILD_PID"
    CHILD_PID=""
done
