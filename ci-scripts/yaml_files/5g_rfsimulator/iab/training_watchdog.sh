#!/bin/bash
# training_watchdog.sh — Stage 2/3 收斂訓練期間的 FlexRIC 崩潰偵測與完整重啟
#
# 背景（CLAUDE.md 第 7 節「FlexRIC 崩潰規律與重啟流程」）：長時間訓練累積約
# 2000 筆 experience 後 FlexRIC 容器可能崩潰（E2 connection 中斷，log 顯示
# "Pending event timeout. Disarming timer."），若只單獨重啟 xApp/DU 而不動
# FlexRIC 本身，DU 會以 assoc_rb_tree_extract assertion 反覆 SIGSEGV——必須
# 三台主機一起做完整乾淨重啟（FlexRIC → DU → xApp 順序）才會恢復。這支腳本
# 把這個已知的手動復原流程自動化，讓多小時到多天的無人值守訓練能撐過去。
#
# 只在 PC1 執行（全部 12 個 xapp-nodeN/inference-nodeN 容器都在 PC1，偵測
# 訊號不需要跨主機輪詢；復原動作才需要 SSH 到 PC2/PC3）。
#
# 用法：
#   nohup bash iab/training_watchdog.sh --epoch 1758000000 \
#       > /tmp/training_watchdog.log 2>&1 &
#
# --epoch 必須跟啟動 training_scenario_driver.sh 三個實例用的同一個值一致，
# 復原後用同一個值重新啟動驅動器，讓輪替表的位置照 wall clock 自我校正
# （見 training_scenario_driver.sh 的說明）。
#
# 絕對不會呼叫 run_stage2_fl.sh——那會清空 MongoDB 經驗與 checkpoint，等於
# 銷毀已經訓練好的成果。只做「讓系統活過來」，不做「重新開始訓練」。

set -u
COMPOSE_DIR=/home/lindor/openairinterface5g/ci-scripts/yaml_files/5g_rfsimulator
COMPOSE_FILE="$COMPOSE_DIR/docker-compose-iab-server.yaml"
DC="docker compose -f $COMPOSE_FILE"

POLL_INTERVAL_S=60
CRASH_CONFIRM_WAIT_S=90
RESTART_COOLDOWN_S=600

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
log()  { echo -e "${CYAN}[watchdog $(date '+%Y-%m-%d %H:%M:%S')]${NC} $*"; }
ok()   { echo -e "${GREEN}[watchdog $(date '+%Y-%m-%d %H:%M:%S')] ✓${NC} $*"; }
warn() { echo -e "${YELLOW}[watchdog $(date '+%Y-%m-%d %H:%M:%S')] ⚠${NC} $*"; }
err()  { echo -e "${RED}[watchdog $(date '+%Y-%m-%d %H:%M:%S')] ✗${NC} $*" >&2; }

EPOCH=""
REWARD_MODE_ARG="throughput_only"
MODEL_ARCH_ARG="mlp"
FL_MODE_ARG="avg"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --epoch) EPOCH="$2"; shift 2 ;;
        --reward-mode) REWARD_MODE_ARG="$2"; shift 2 ;;
        --model-arch) MODEL_ARCH_ARG="$2"; shift 2 ;;
        --fl-mode) FL_MODE_ARG="$2"; shift 2 ;;
        *) err "未知參數: $1"; exit 1 ;;
    esac
done
if [[ -z "$EPOCH" ]]; then
    err "用法: $0 --epoch <unix_timestamp> [--reward-mode throughput_only] [--model-arch mlp] [--fl-mode avg]"
    exit 1
fi

cd "$COMPOSE_DIR" || { err "cd 到 $COMPOSE_DIR 失敗"; exit 1; }

# ⚠ 重要陷阱記錄（2026-09-18 現場踩過一次）：start_iab_server.sh 內部有一段
# 對 inference-nodeN 的 plain `docker compose up -d` 呼叫，它不知道
# REWARD_MODE/MODEL_ARCH，若呼叫時這支腳本自己的 shell 環境沒有帶上這些變數，
# docker compose 會用 compose 檔裡的預設值（REWARD_MODE 預設是 lagrangian，
# 不是這次要的 throughput_only！）悄悄重建容器，訓練資料的 reward 語意會在
# 不知不覺間被污染。下面 full_recovery() 的每一步都把這些變數帶進呼叫的
# shell 環境，並在收尾額外用 --force-recreate 重新蓋一次 inference-nodeN，
# 確保不管 start_iab_server.sh 內部怎麼跑，最後狀態一定是正確的——不能只
# 假設「應該不會被重設」，要主動覆蓋確認。
log "本次收斂訓練的環境變數：REWARD_MODE=$REWARD_MODE_ARG MODEL_ARCH=$MODEL_ARCH_ARG FL_MODE=$FL_MODE_ARG"

# 只追蹤 PC1 本機的關鍵容器（donor-cu/donor-du/flexric）——這三個是崩潰鏈的
# 起點（見上方說明），PC2/PC3 上的 DU 容器崩潰也會反映在這三者的 RestartCount
# 或 E2 連線數變化上，不需要額外對 PC2/PC3 做 SSH 輪詢才能偵測。
CRITICAL_CONTAINERS=(rfsim5g-donor-cu rfsim5g-donor-du flexric)

# [2026-09-18 新增] Relay/access DU（Node1~12）的 Random Access process pool
# 耗盡（OAI 內部固定 4 格陣列，gNB_scheduler_RA.c:719 "no free RA process"）
# 是一種獨立於上面三個訊號的崩潰模式——只會卡住該 DU 底下的子節點連通性，
# 不一定會觸發 FlexRIC pending timeout、xApp 重連風暴、或 CU/DU(donor)/
# flexric 的 RestartCount 變化，過去曾經讓 full_recovery() 的 UE 連通性驗證
# 卡住重試 5 次後失敗（見 HISTORY.md 2026-09-18 Node2 案例、本次 session
# PC3 Node9~12 案例）。修法很單純（重啟該 DU 容器即可，5~10 秒恢復），不需要
# 驚動三主機完整重啟，獨立於 detect_crash_signal()/full_recovery() 之外，
# 每個主迴圈週期都輕量檢查一次。
declare -A DU_HOST=( [1]=pc2 [2]=pc1 [3]=pc2 [4]=pc2 [5]=pc2 [6]=pc2 [7]=pc1 [8]=pc1 [9]=pc3 [10]=pc3 [11]=pc3 [12]=pc3 )
declare -A RA_HEAL_LAST_TS
RA_HEAL_COOLDOWN_S=300

# heal_ra_exhaustion <node_id> — 檢查 rfsim5g-iab-du-<node_id>（依 DU_HOST 對照，
# PC1 本機直接執行，PC2/PC3 透過 ssh）最近 90 秒的 log 有沒有出現 RA process
# pool 耗盡，有就重啟該 DU；每個 DU 有獨立的 300 秒冷卻，避免反覆重啟同一個
# DU 反而讓它自己變成不穩定源。
heal_ra_exhaustion() {
    local node=$1
    local du="rfsim5g-iab-du-${node}"
    local host="${DU_HOST[$node]}"
    local now
    now=$(date +%s)
    local last=${RA_HEAL_LAST_TS[$node]:-0}
    if (( now - last < RA_HEAL_COOLDOWN_S )); then
        return 1
    fi
    local check="docker logs --since 90s $du 2>&1 | grep -c 'no free RA process' || true"
    local hits
    if [[ "$host" == "pc1" ]]; then
        hits=$(eval "$check" 2>/dev/null)
    else
        hits=$(ssh "$host" "$check" 2>/dev/null)
    fi
    hits=${hits:-0}
    if [[ "$hits" -gt 0 ]]; then
        warn "Node${node}（$du，$host）偵測到 RA process pool 耗盡（${hits} 次 in 90s），重啟該 DU..."
        if [[ "$host" == "pc1" ]]; then
            docker restart "$du" >/dev/null 2>&1
        else
            ssh "$host" "docker restart $du" >/dev/null 2>&1
        fi
        RA_HEAL_LAST_TS[$node]=$now
        ok "Node${node} 的 $du 已重啟（RA process pool 自我修復）"
        return 0
    fi
    return 1
}

check_and_heal_ra_all() {
    for node in "${!DU_HOST[@]}"; do
        heal_ra_exhaustion "$node"
    done
}

restart_count() {
    docker inspect --format '{{.RestartCount}}' "$1" 2>/dev/null || echo -1
}

declare -A BASELINE_RESTARTS
for c in "${CRITICAL_CONTAINERS[@]}"; do
    BASELINE_RESTARTS[$c]=$(restart_count "$c")
done
log "初始 RestartCount 基準：$(for c in "${CRITICAL_CONTAINERS[@]}"; do echo -n "$c=${BASELINE_RESTARTS[$c]} "; done)"

LAST_RECOVERY_TS=0
CRASH_COUNT=0

detect_crash_signal() {
    # 訊號 1：FlexRIC log 出現 pending event timeout
    if docker logs --since "${POLL_INTERVAL_S}s" flexric 2>&1 | grep -q "Pending event timeout"; then
        echo "flexric_pending_timeout"
        return 0
    fi
    # 訊號 2：多節點同時重連風暴（單一節點重連是雜訊，多節點同時才是系統性崩潰）
    local reconnecting=0
    for n in $(seq 1 12); do
        if docker logs --since "${POLL_INTERVAL_S}s" "xapp-node${n}" 2>&1 | grep -q "Resending Setup Request"; then
            reconnecting=$((reconnecting + 1))
        fi
    done
    if [[ $reconnecting -ge 3 ]]; then
        echo "xapp_reconnect_storm(${reconnecting}/12)"
        return 0
    fi
    # 訊號 3：關鍵容器 RestartCount 較基準增加
    for c in "${CRITICAL_CONTAINERS[@]}"; do
        local rc
        rc=$(restart_count "$c")
        if [[ "$rc" != "-1" && "$rc" != "${BASELINE_RESTARTS[$c]}" ]]; then
            echo "restart_count_regression(${c}:${BASELINE_RESTARTS[$c]}->${rc})"
            return 0
        fi
    done
    return 1
}

stop_scenario_driver() {
    log "停止三主機的 training_scenario_driver.sh..."
    pkill -f training_scenario_driver.sh 2>/dev/null
    ssh pc2 "pkill -f training_scenario_driver.sh" 2>/dev/null
    ssh pc3 "pkill -f training_scenario_driver.sh" 2>/dev/null
    sleep 3
}

start_scenario_driver() {
    log "重新啟動三主機的 training_scenario_driver.sh（epoch=$EPOCH，自動接續到 wall clock 當下位置）..."
    nohup bash "$COMPOSE_DIR/iab/training_scenario_driver.sh" --host pc1 --epoch "$EPOCH" \
        > /tmp/driver_stage_pc1.log 2>&1 < /dev/null &
    disown
    # 2026-09-18 現場踩過的坑：`ssh host "cmd &"` 這個寫法不可靠——遠端 shell
    # 把指令丟進背景後，SSH session 有時會在背景行程真的 fork 完成前就先關閉，
    # 導致遠端行程從未真正啟動（第一次上線時 PC3 的驅動器就是這樣悄悄沒起來，
    # 直到下次健康檢查才發現）。改用 `ssh -f`（ssh 自己先 fork 到背景、確認
    # session 建立後才把控制權交還本地端，不依賴遠端 shell 的 `&` 語意）。
    ssh -f pc2 "cd $COMPOSE_DIR && nohup bash iab/training_scenario_driver.sh --host pc2 --epoch $EPOCH > /tmp/driver_stage_pc2.log 2>&1 < /dev/null" 2>/dev/null
    ssh -f pc3 "cd $COMPOSE_DIR && nohup bash iab/training_scenario_driver.sh --host pc3 --epoch $EPOCH > /tmp/driver_stage_pc3.log 2>&1 < /dev/null" 2>/dev/null
    sleep 3
    local missing=""
    pgrep -f "training_scenario_driver.sh --host pc1" >/dev/null || missing="$missing pc1"
    ssh pc2 "pgrep -f training_scenario_driver.sh" >/dev/null 2>&1 || missing="$missing pc2"
    ssh pc3 "pgrep -f training_scenario_driver.sh" >/dev/null 2>&1 || missing="$missing pc3"
    if [[ -n "$missing" ]]; then
        err "場景驅動器沒有在這些主機上起來：$missing —— 重試一次"
        for h in $missing; do
            if [[ "$h" == "pc1" ]]; then
                nohup bash "$COMPOSE_DIR/iab/training_scenario_driver.sh" --host pc1 --epoch "$EPOCH" \
                    > /tmp/driver_stage_pc1.log 2>&1 < /dev/null &
                disown
            else
                ssh -f "$h" "cd $COMPOSE_DIR && nohup bash iab/training_scenario_driver.sh --host $h --epoch $EPOCH > /tmp/driver_stage_${h}.log 2>&1 < /dev/null" 2>/dev/null
            fi
        done
        sleep 3
    fi
    ok "場景驅動器已在三主機重新啟動"
}

full_recovery() {
    local ts_dir="/tmp/training_watchdog_recovery_$(date '+%Y%m%d_%H%M%S')"
    mkdir -p "$ts_dir"
    log "===== 開始完整系統重啟（記錄於 $ts_dir） ====="

    stop_scenario_driver

    # [2026-09-19 新增] DEGRADED 累計「軟性」失敗（UE heal 重試 5 次仍有連不通、
    # E2 連線數不足 13），跟「腳本本身跑不完／SSH 不通」這種硬性失敗分開處理。
    # 教訓：舊版任何一步驟只要偵測到「重試 5 次後仍有 UE 連不通」就立刻
    # return 1，導致 [4/4] 的 xApp 啟動步驟永遠不會被執行到——2026-09-19 這次
    # 現場真的踩到（PC2 卡在 Node5 一個 access node，PC3/PC1 的 xApp 因此被
    # 晾在「已被 down 掉、沒人重新啟動」的狀態長達近一小時，直到人工介入）。
    # 軟性失敗通常只影響少數幾個節點、且往往在後續步驟或稍後自然/手動修復，
    # 讓全部 12 個 xApp 繼續掛著不啟動的代價遠大於「先把 xApp 啟動、個別節點
    # 連通性之後再修」。硬性失敗（SSH 不通、腳本本身非 0 結束）代表問題可能
    # 更根本，仍然直接中止、不冒進。
    local DEGRADED=0
    local DEGRADED_DETAIL=""

    log "[1/4] PC1 基礎設施（start_iab_server.sh）..."
    REWARD_MODE="$REWARD_MODE_ARG" MODEL_ARCH="$MODEL_ARCH_ARG" FL_MODE="$FL_MODE_ARG" \
        bash iab/start_iab_server.sh > "$ts_dir/pc1_server.log" 2>&1
    if [[ $? -ne 0 ]]; then
        err "PC1 基礎設施腳本本身執行失敗（非 0 結束），見 $ts_dir/pc1_server.log"
        return 1
    fi
    if grep -q "重試 5 次後仍有 UE 連不通" "$ts_dir/pc1_server.log"; then
        warn "PC1 基礎設施重啟後仍有 UE 連不通（軟性失敗，繼續往下走，見 $ts_dir/pc1_server.log）"
        DEGRADED=$((DEGRADED + 1)); DEGRADED_DETAIL="$DEGRADED_DETAIL pc1"
    else
        ok "PC1 基礎設施完成"
    fi

    log "[2/4] PC2（ssh pc2 run_local_pc2.sh）..."
    ssh pc2 "cd $COMPOSE_DIR && bash iab/run_local_pc2.sh" > "$ts_dir/pc2.log" 2>&1
    if [[ $? -ne 0 ]]; then
        err "PC2 重啟腳本本身執行失敗（非 0 結束，可能 SSH 不通），見 $ts_dir/pc2.log"
        return 1
    fi
    if grep -q "重試 5 次後仍有 UE 連不通" "$ts_dir/pc2.log"; then
        warn "PC2 重啟後仍有 UE 連不通（軟性失敗，繼續往下走，見 $ts_dir/pc2.log）"
        DEGRADED=$((DEGRADED + 1)); DEGRADED_DETAIL="$DEGRADED_DETAIL pc2"
    else
        ok "PC2 完成"
    fi

    log "[3/4] PC3（ssh pc3 run_local_pc3.sh）..."
    ssh pc3 "cd $COMPOSE_DIR && bash iab/run_local_pc3.sh" > "$ts_dir/pc3.log" 2>&1
    if [[ $? -ne 0 ]]; then
        err "PC3 重啟腳本本身執行失敗（非 0 結束，可能 SSH 不通），見 $ts_dir/pc3.log"
        return 1
    fi
    if grep -q "重試 5 次後仍有 UE 連不通" "$ts_dir/pc3.log"; then
        warn "PC3 重啟後仍有 UE 連不通（軟性失敗，繼續往下走，見 $ts_dir/pc3.log）"
        DEGRADED=$((DEGRADED + 1)); DEGRADED_DETAIL="$DEGRADED_DETAIL pc3"
    else
        ok "PC3 完成"
    fi

    # 三主機的軟性失敗都發生時，才視為問題可能更根本（例如 CU/FlexRIC 本身
    # 沒起來），這種情況下繼續硬闖沒有意義，停下來讓人工介入。
    if [[ $DEGRADED -ge 3 ]]; then
        err "PC1/PC2/PC3 三主機同時回報 UE 連不通，可能是更根本的問題（CU/FlexRIC？），停止並需要人工介入"
        return 1
    fi

    log "[4/4] 回 PC1：等待 13/13 E2、啟動 xApp（run_local_pc1.sh --skip-server）..."
    bash iab/run_local_pc1.sh --skip-server > "$ts_dir/pc1_xapp.log" 2>&1
    E2=$(docker logs flexric 2>&1 | grep -c "E2 SETUP-REQUEST" 2>/dev/null || echo 0)
    if [[ "$E2" -lt 13 ]]; then
        warn "重啟後 E2 連線只有 ${E2}/13（軟性失敗，xApp 仍已對已連線節點啟動，見 $ts_dir/pc1_xapp.log）"
        DEGRADED=$((DEGRADED + 1)); DEGRADED_DETAIL="$DEGRADED_DETAIL e2(${E2}/13)"
    else
        ok "13/13 E2 連線已恢復"
    fi
    if [[ $DEGRADED -gt 0 ]]; then
        warn "本次復原完成，但有降級項目：$DEGRADED_DETAIL——建議之後找時間人工複查這些主機/節點的連通性"
    fi

    # 2026-09-18 現場踩過的坑（見 HISTORY.md 同日條目「訂正」）：PC1 本機
    # Node7/8（UE5~8）的健康檢查只在 start_iab_server.sh 自己執行過程中跑
    # 一次，時間點在整個依序流程的最前面；PC2/PC3 接下來要跑數分鐘，這段
    # 期間如果 MT7/8 的 tunnel 自發性重建（已知現象），PC1 端完全沒有東西
    # 會重新檢查或補上 DNAT，直到最後才被發現斷線。這裡在全部四步跑完後，
    # 對 PC1 本機的 UE5~8 做最後一次獨立驗證＋必要時重新斷言 DNAT／路由，
    # 補上這段時機缺口，不再只依賴 start_iab_server.sh 內部那次過早的檢查。
    verify_and_fix_local_ue5to8() {
        local ext_dn_ip="192.168.72.135"
        for attempt in 1 2 3; do
            local all_ok=true
            for i in 5 6 7 8; do
                docker exec "rfsim5g-end-ue-${i}" ping -c 1 -W 2 "$ext_dn_ip" >/dev/null 2>&1 || all_ok=false
            done
            [[ "$all_ok" == true ]] && { ok "PC1 本機 UE5~8 最終驗證通過（第 ${attempt} 次）"; return 0; }

            warn "PC1 本機 UE5~8 最終驗證第 ${attempt} 次發現異常，重新斷言 Node7/8 的 DNAT／路由..."
            declare -A du_ip=( [7]="192.168.76.12" [8]="192.168.76.13" )
            declare -A mt_name=( [7]="rfsim5g-iab-mt-7" [8]="rfsim5g-iab-mt-8" )
            for n in 7 8; do
                local mt_ip
                mt_ip=$(docker exec "${mt_name[$n]}" ip -f inet addr show oaitun_ue1 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}')
                [[ -z "$mt_ip" ]] && continue
                docker exec -u 0 rfsim5g-donor-cu iptables -t nat -I OUTPUT 1 -d "${du_ip[$n]}" -p udp --dport 2152 -j DNAT --to-destination "$mt_ip"
                docker exec -u 0 "${mt_name[$n]}" ip route del default 2>/dev/null
                docker exec -u 0 "${mt_name[$n]}" ip route add default via 12.1.1.1 dev oaitun_ue1 2>/dev/null
            done
            for i in 5 6 7 8; do
                docker exec -u 0 "rfsim5g-end-ue-${i}" ip route replace default via 12.1.1.1 dev oaitun_ue1 2>/dev/null
            done
            sleep 10
        done
        err "PC1 本機 UE5~8 最終驗證重試 3 次後仍有異常，需要人工檢查"
        return 1
    }
    if ! verify_and_fix_local_ue5to8; then
        # [2026-09-19 新增] 同上方理由：這只是 PC1 本機 UE5~8 的最後一道保險，
        # 此時 xApp／E2 已經在 [4/4] 啟動過了，不應該因為這裡沒過就整個
        # return 1（會連帶跳過下面「確認 Stage 2+ 服務」與重啟場景驅動器，
        # 讓已經恢復的訓練迴圈又被晾著）。降級記錄、繼續往下走。
        warn "PC1 本機 UE5~8 最終驗證未通過（軟性失敗，繼續往下走，訓練/FL 服務不受影響）"
        DEGRADED=$((DEGRADED + 1)); DEGRADED_DETAIL="$DEGRADED_DETAIL pc1-ue5to8"
    fi

    # [2026-09-20 新增] UE17（直連 Node4 relay，機制跟 access 節點不同，見
    # CLAUDE.md 第 1 節）不在 start_iab_pc2.sh 的 verify_and_heal_ues() 範圍內
    # （該腳本自己的註解明講：「UE17 不在 verify_and_heal_ues() 的 ping 重試
    # 範圍內」），現場觀察到連續 4 次 full_recovery() 之後 UE17 100% 會斷線，
    # 原因有二，每次都復發：(1) DU4 的 F1-U 本地綁定位址（conf 檔
    # local_n_address）在 DU4 啟動當下寫入，但 MT4 的 oaitun_ue1 tunnel IP
    # 之後常會自發性重建换掉，DU4 綁定的舊位址就從介面上消失，需要在 MT4
    # netns 補回一個該舊位址的別名（不需重啟 DU4 本身）；(2) UE17 自己的
    # default route 在容器重建後有時會停留在 eth0（macvlan，只用來連
    # rfsimulator RF 控制通道），沒有正確指向 oaitun_ue1（PDU session
    # tunnel），需要重新斷言。兩者都在 PC2，透過 ssh 執行。
    verify_and_fix_ue17() {
        local ext_dn_ip="192.168.72.135"
        for attempt in 1 2 3; do
            if ssh pc2 "docker exec rfsim5g-end-ue-17 ping -c 1 -W 2 $ext_dn_ip" >/dev/null 2>&1; then
                ok "UE17 最終驗證通過（第 ${attempt} 次）"
                return 0
            fi
            warn "UE17 最終驗證第 ${attempt} 次發現異常，重新斷言 DU4 F1-U 別名位址／UE17 預設路由..."
            local bind_ip
            bind_ip=$(ssh pc2 "grep -oP '(?<=local_n_address = \")[0-9.]+' $COMPOSE_DIR/conf/iab_du_node4.conf" 2>/dev/null)
            if [[ -n "$bind_ip" ]]; then
                ssh pc2 "docker exec -u 0 rfsim5g-iab-mt-4 ip addr add ${bind_ip}/32 dev oaitun_ue1" 2>/dev/null
            fi
            ssh pc2 "docker exec -u 0 rfsim5g-end-ue-17 ip route replace default via 12.1.1.1 dev oaitun_ue1" 2>/dev/null
            sleep 10
        done
        err "UE17 最終驗證重試 3 次後仍有異常，需要人工檢查"
        return 1
    }
    if ! verify_and_fix_ue17; then
        warn "UE17 最終驗證未通過（軟性失敗，繼續往下走，訓練/FL 服務不受影響）"
        DEGRADED=$((DEGRADED + 1)); DEGRADED_DETAIL="$DEGRADED_DETAIL ue17"
    fi

    log "確認 Stage 2+ 服務（inference-nodeN／global-xapp／flower-*）仍在跑..."
    $DC up -d inference-node{1..12} >> "$ts_dir/stage2_services.log" 2>&1
    if $DC ps global-xapp 2>/dev/null | grep -q "global-xapp"; then
        # [2026-09-19 修復] 同 run_stage2_fl.sh 的同款修法：這裡原本沒帶
        # REWARD_MODE，只帶 FL_MODE/MODEL_ARCH，導致 flower-supernode-nodeN
        # 每次經過這裡都悄悄吃到 compose 檔預設值 lagrangian（現場實測發現：
        # 已訓練數小時的 Stage 3 checkpoint 的 lambda 欄位持續在漂移，追查到
        # 全部 12 個 flower-supernode-nodeN 的 REWARD_MODE 都是 lagrangian）。
        REWARD_MODE="$REWARD_MODE_ARG" FL_MODE="$FL_MODE_ARG" MODEL_ARCH="$MODEL_ARCH_ARG" \
            $DC --profile stage2-fl up -d --force-recreate global-xapp flower-superlink flower-supernode-node{1..12} flower-scheduler \
            >> "$ts_dir/stage2_services.log" 2>&1
    fi

    # 上面的 plain `up -d`（無 --force-recreate）本身不該改變已存在容器的環境
    # 變數，但 start_iab_server.sh 內部那段 plain `up -d $INF_SERVICES` 呼叫
    # 已經證實會在某些情況下觸發 recreate（2026-09-18 現場觀察，原因未完全
    # 查清，懷疑跟 mongodb depends_on 的健康狀態轉換有關）——不管原因是什麼，
    # 這裡主動用 --force-recreate 再蓋一次、明確帶上正確的環境變數，把「最終
    # 狀態一定正確」的保證建立在覆蓋動作上，而不是「上面應該沒事」的假設上。
    log "主動用正確的環境變數再覆蓋一次 inference-nodeN（防止 start_iab_server.sh 內部靜默 recreate 成預設值）..."
    REWARD_MODE="$REWARD_MODE_ARG" MODEL_ARCH="$MODEL_ARCH_ARG" \
        $DC up -d --force-recreate inference-node{1..12} >> "$ts_dir/stage2_services.log" 2>&1

    mismatched=0
    for n in $(seq 1 12); do
        rm=$(docker exec "inference-node${n}" env 2>/dev/null | grep -oP '(?<=REWARD_MODE=).*')
        ma=$(docker exec "inference-node${n}" env 2>/dev/null | grep -oP '(?<=MODEL_ARCH=).*')
        if [[ "$rm" != "$REWARD_MODE_ARG" || "$ma" != "$MODEL_ARCH_ARG" ]]; then
            err "inference-node${n} 環境變數不符：REWARD_MODE=$rm(應為 $REWARD_MODE_ARG) MODEL_ARCH=$ma(應為 $MODEL_ARCH_ARG)"
            mismatched=$((mismatched + 1))
        fi
    done
    if [[ $mismatched -gt 0 ]]; then
        err "$mismatched 個節點的環境變數不符，需要人工檢查"
        return 1
    fi
    ok "服務狀態已確認，12/12 節點環境變數正確"

    for c in "${CRITICAL_CONTAINERS[@]}"; do
        BASELINE_RESTARTS[$c]=$(restart_count "$c")
    done
    LAST_RECOVERY_TS=$(date +%s)
    CRASH_COUNT=$((CRASH_COUNT + 1))
    ok "===== 完整重啟成功（累計第 ${CRASH_COUNT} 次），基準已重設 ====="

    start_scenario_driver
    return 0
}

log "開始監控（每 ${POLL_INTERVAL_S}s 輪詢一次，疑似崩潰後等 ${CRASH_CONFIRM_WAIT_S}s 二次確認，冷卻窗 ${RESTART_COOLDOWN_S}s）"

while true; do
    sleep "$POLL_INTERVAL_S"

    # RA process pool 耗盡的輕量自我修復，跟下面的崩潰偵測/完整重啟路徑完全
    # 獨立——每個週期都跑，發現就直接修，不需要二次確認也不影響冷卻窗判斷。
    check_and_heal_ra_all

    signal=$(detect_crash_signal) || signal=""
    if [[ -z "$signal" ]]; then
        continue
    fi

    warn "疑似崩潰訊號：$signal，等待 ${CRASH_CONFIRM_WAIT_S}s 二次確認..."
    sleep "$CRASH_CONFIRM_WAIT_S"

    signal2=$(detect_crash_signal) || signal2=""
    if [[ -z "$signal2" ]]; then
        log "二次確認未再觀察到崩潰訊號，判斷為瞬斷雜訊，繼續監控"
        continue
    fi

    now=$(date +%s)
    since_last=$((now - LAST_RECOVERY_TS))
    if [[ $LAST_RECOVERY_TS -ne 0 && $since_last -lt $RESTART_COOLDOWN_S ]]; then
        err "確認崩潰（$signal2），但距離上次重啟只有 ${since_last}s（< 冷卻窗 ${RESTART_COOLDOWN_S}s）——"
        err "代表恢復沒有生效，不再自動重試。停止 watchdog 與場景驅動器，需要人工介入。"
        stop_scenario_driver
        exit 1
    fi

    warn "確認崩潰（$signal2），開始完整重啟..."
    if ! full_recovery; then
        err "完整重啟失敗，停止 watchdog 與場景驅動器，需要人工介入。"
        stop_scenario_driver
        exit 1
    fi
done
