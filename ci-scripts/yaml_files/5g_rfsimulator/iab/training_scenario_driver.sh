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

# ── 輪替表（2026-09-26 重寫）─────────────────────────────────────────────────────
# 只用兩種場景，且都已對齊新平台的流量量級（模擬時間 Mbps，S=0.4，見 CLAUDE.md 第 8 節）：
#   TR：隨機化的兩狀態 T——同量測用 Scenario T 的檔位與 ~45% 壅塞比例，但壅塞相位排列與每 UE 的組合由 seed
#       打散，訓練分佈與固定的測試場景 T 不同（「訓練場景不可等於測試場景」的原則）。
#   R ：新版真實隨機（idle:burst:traffic = 1:2.5:6.5，TCP/UDP 混合），保留另一種隨機結構的多樣性。
# 舊表的 A/B/C 已移除：它們的流量（每 UE 25~50 Mbps 模擬時間）是重設平台前的舊量級，17 個 UE 加總 400~850，
# 遠超 CPU 平台（~100~108），會讓平台飽和、RTT 秒級、UE 崩潰。固定的 Scenario T（測試基準）也不進訓練。
# 協定：TCP／UDP 在不同 slot 混合訓練（最後量測兩種都要量）；R 的協定維持其內建 TCP/UDP 混合（75/25）。
# 每個 slot 的 seed = 基底 + 週期編號×10 + slot 序號，每一輪循環都是全新的隨機排列。
# scenario / 協定（tcp|udp|mix，mix=用場景內建）/ 這個 slot 的秒數。總長 13200 秒 ≈ 3.7 小時一輪，無限循環。
SLOT_SCENARIO=(TR  TR  R   TR  TR  R)
SLOT_PROTOCOL=(tcp udp mix udp tcp mix)
SLOT_DURATION_S=(2400 2400 1800 2400 2400 1800)
TR_SEED_BASE=140000
R_SEED_BASE=150000     # 刻意避開固定測試 seed 20260914
TR_PHASE_S=110         # 與量測用的 Scenario T 相同的相位長度
R_PHASE_S=60
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
    cycle_no=$(( (now - EPOCH) / CYCLE_LEN_S ))
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
    # 這個 slot 的起點與相位原點：三台主機用同一個 EPOCH 算，天然一致，--phase-origin 讓相位邊界固定、編號連號
    slot_start=$(( now - (elapsed - cursor) ))
    # 協定：--protocol 明確指定則全部 slot 覆寫（相容舊用法）；否則用該 slot 的設定（mix = 場景內建）
    proto=${PROTOCOL:-${SLOT_PROTOCOL[$slot_idx]}}
    proto_arg=()
    [[ "$proto" != "mix" ]] && proto_arg=(--protocol "$proto")

    # 這個 slot 還要跑幾個相位：第一個相位可能是「進行到一半」（watchdog 重啟後接續），只算剩餘的部分
    phase_len=$TR_PHASE_S; [[ "$scenario" == "R" ]] && phase_len=$R_PHASE_S
    first_left=$(( phase_len - (now - slot_start) % phase_len ))
    num_phases=$(( 1 + ( (remaining > first_left ? remaining - first_left : 0) + phase_len - 1 ) / phase_len ))

    log "slot=$slot_idx scenario=$scenario proto=$proto cycle=$cycle_no remaining=${remaining}s phases=$num_phases"

    # 訓練期間 CrashGuard 只警告（--on-crash warn）：崩潰復原由 training_watchdog.sh 負責，不要讓場景行程自己退出
    case "$scenario" in
        TR)
            seed=$(( TR_SEED_BASE + cycle_no * 10 + slot_idx ))
            python3 scenarios/traffic_scenario.py --scenario TR --seed "$seed" \
                --host "$HOST" --phase-duration "$TR_PHASE_S" --num-phases "$num_phases" \
                --phase-origin "$slot_start" --on-crash warn "${proto_arg[@]}" &
            ;;
        R)
            seed=$(( R_SEED_BASE + cycle_no * 10 + slot_idx ))
            python3 scenarios/traffic_scenario.py --scenario R --seed "$seed" \
                --host "$HOST" --phase-duration "$R_PHASE_S" --num-phases "$num_phases" \
                --phase-origin "$slot_start" --on-crash warn "${proto_arg[@]}" &
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
