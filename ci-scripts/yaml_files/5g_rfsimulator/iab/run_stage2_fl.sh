#!/bin/bash
# run_stage2_fl.sh — Stage 2 起、Global 層的通用重跑輔助腳本
#
# 檔名沿用歷史命名（避免破壞既有文件引用），但服務範圍是「Stage 2 起任何
# FL 模式」，不只 Stage 2——用 FL_MODE 切換聚合邏輯（avg=Stage 2 標準
# FedAvg／cluster=Stage 3 Soft/Weighted Clustered FL，見 CLAUDE.md 第 3 節）。
#
# 假設 PC1/PC2/PC3 的 RAN 基礎設施（run_local_pc{1,2,3}.sh）已經跑起來、
# 13/13 E2、12/12 xApp 皆已就緒。這支腳本只處理「切換 stage」需要的部分：
#   1. 停止全部 12 個 inference-nodeN 容器
#   2. 清空 MongoDB 經驗（node{1..12}_experiences）與模型 checkpoint
#      （用實際 Docker volume 名稱 iab-xapp-model-nodeN，不是 compose 內部
#      key 名稱 inference_models_nodeN——兩者不同，用錯會清到不存在的
#      volume，見 CLAUDE.md 第 3 節 Stage 2 踩坑記錄）
#   3. 用指定的 REWARD_MODE 重新啟動 12 個 inference-nodeN 容器
#   4. 用指定的 FL_MODE 啟動/重建 Global xApp + Flower FL 服務（profiles: stage2-fl）
#
# 用法：
#   REWARD_MODE=throughput_only bash iab/run_stage2_fl.sh                        # Stage 2（avg FL，預設，MODEL_ARCH=mlp）
#   FL_MODE=cluster REWARD_MODE=throughput_only bash iab/run_stage2_fl.sh        # Stage 3（cluster FL，MODEL_ARCH=mlp）
#   MODEL_ARCH=gru REWARD_MODE=throughput_only bash iab/run_stage2_fl.sh         # 切回 GRU 架構（保留供未來用，見 drl_agent.py）
#
# Stage 4 只需要改 flower-app/iab_fl/server_app.py 的自訂聚合邏輯，此腳本
# 不用再改直接重跑即可；Stage 5 改用 REWARD_MODE=lagrangian 重跑。
#
# MODEL_ARCH（mlp｜gru，預設 mlp）：Local DRL 的 Actor/Critic 網路架構開關
# （見 inference/drl_agent.py 模組 docstring），比照 REWARD_MODE/FL_MODE 同一
# 套模式。切換 MODEL_ARCH 前一定要清空 checkpoint（本腳本第 2 步已處理）——
# 兩種架構的 state_dict key 不相容，DRLAgent.load() 會偵測到 arch 不符而拒絕
# 載入、退回隨機初始化，但這代表沒清乾淨也不會「悄悄」載入錯誤權重，只是會
# 白白浪費一次已經在磁碟上的訓練成果，所以仍然要主動清空。
#
# 注意：改過 server_app.py/client_app.py 的原始碼後，這支腳本不會自動重建
# image——因為 inference/Dockerfile 是用 COPY 把原始碼烤進
# local-xapp-inference:latest（不是 bind mount），改完程式碼要先手動
# `docker build -t local-xapp-inference:latest ./inference` 再跑這支腳本，
# 否則容器裡還是舊程式碼。

set -e
cd "$(dirname "$0")/.."

REWARD_MODE="${REWARD_MODE:-throughput_only}"
FL_MODE="${FL_MODE:-avg}"
MODEL_ARCH="${MODEL_ARCH:-mlp}"
COMPOSE_FILE="docker-compose-iab-server.yaml"
DC="docker compose -f $COMPOSE_FILE"

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; NC='\033[0m'

echo -e "${CYAN}[run_stage2_fl] REWARD_MODE=${REWARD_MODE} FL_MODE=${FL_MODE} MODEL_ARCH=${MODEL_ARCH}${NC}"

echo -e "${CYAN}[1/4] 停止全部 12 個 inference-nodeN 容器...${NC}"
$DC stop inference-node{1..12}

echo -e "${CYAN}[2/4] 清空 checkpoint（實際 volume 名稱 iab-xapp-model-nodeN）與 MongoDB 經驗...${NC}"
for i in $(seq 1 12); do
    docker run --rm -v "iab-xapp-model-node${i}:/models" alpine \
        sh -c 'rm -f /models/*.pt /models/*.tmp.* 2>/dev/null || true'
done
docker exec mongodb mongosh iab_xapp --quiet --eval \
    'for(let i=1;i<=12;i++){db["node"+i+"_experiences"].drop()}; db.getCollectionNames()'
echo -e "${GREEN}  checkpoint / 經驗已清空${NC}"

echo -e "${CYAN}[3/4] 用 REWARD_MODE=${REWARD_MODE} MODEL_ARCH=${MODEL_ARCH} 重新啟動 inference-nodeN...${NC}"
REWARD_MODE="$REWARD_MODE" MODEL_ARCH="$MODEL_ARCH" $DC up -d --force-recreate inference-node{1..12}

echo -e "${CYAN}[4/4] 用 FL_MODE=${FL_MODE} MODEL_ARCH=${MODEL_ARCH} REWARD_MODE=${REWARD_MODE} 啟動/重建 Global xApp + Flower FL 服務...${NC}"
# --force-recreate 是必要的：flower-superlink 的 FL_MODE/MODEL_ARCH 環境變數
# 可能跟上次執行不同（例如從 avg 切到 cluster、或從 mlp 切到 gru），若容器
# 已存在，`up -d` 不會重建既有容器、環境變數不會生效。
#
# [2026-09-19 修復] 這裡原本沒有帶 REWARD_MODE，只帶 FL_MODE/MODEL_ARCH——
# `flower-supernode-nodeN` 服務在 compose 檔裡雖然有 `REWARD_MODE:
# "${REWARD_MODE:-lagrangian}"` 的預設值寫法，但那個 `${REWARD_MODE:-...}`
# 是在「執行這行 docker compose 指令的當下 shell 環境」裡展開，這裡的呼叫
# 沒有把 REWARD_MODE 帶進這個 shell 環境，所以永遠吃到預設值 lagrangian，
# 跟 2026-09-14 修過的那個 bug（HISTORY.md）是同一類但沒修乾淨——當時只驗證
# 了透過這支腳本啟動當下那次沒問題（因為手動有 export），沒有把 REWARD_MODE
# 也明確寫進這行指令本身，導致往後只要重跑這行（例如 watchdog 的
# full_recovery()）就會重新踩到。這裡明確補上，兩處呼叫都要一致。
REWARD_MODE="$REWARD_MODE" FL_MODE="$FL_MODE" MODEL_ARCH="$MODEL_ARCH" $DC --profile stage2-fl up -d --force-recreate \
    global-xapp flower-superlink flower-supernode-node{1..12} flower-scheduler

echo -e "${GREEN}==================================================${NC}"
echo -e "${GREEN} Stage 2+ 服務已就緒（REWARD_MODE=${REWARD_MODE}, FL_MODE=${FL_MODE}, MODEL_ARCH=${MODEL_ARCH}）${NC}"
echo -e "${GREEN}==================================================${NC}"
echo -e "${YELLOW}下一步（量測前，務必先做，見 CLAUDE.md 第 3 節）：${NC}"
echo "  1. bash scenarios/setup_iperf_servers.sh"
echo "  2. 對全部 17 個 UE 做一次現場 ping 測試，確認 0% 封包遺失"
echo "  3. python3 scenarios/traffic_scenario.py --scenario R --seed 20260914 --host {pc1,pc2,pc3} --phase-duration 60 --num-phases 15"
echo "  4. python3 iab/measure_stage.py --host {pc1,pc2,pc3} --duration 900 --interval 5 --out /tmp/stage_{host}.csv"
