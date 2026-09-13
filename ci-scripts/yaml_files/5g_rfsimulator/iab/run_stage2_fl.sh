#!/bin/bash
# run_stage2_fl.sh — Stage 2 (avg FL) 起、Global 層固定的重跑輔助腳本
#
# 假設 PC1/PC2/PC3 的 RAN 基礎設施（run_local_pc{1,2,3}.sh）已經跑起來、
# 13/13 E2、12/12 xApp 皆已就緒。這支腳本只處理「切換 stage」需要的部分：
#   1. 停止全部 12 個 inference-nodeN 容器
#   2. 清空 MongoDB 經驗（node{1..12}_experiences）與模型 checkpoint
#      （用實際 Docker volume 名稱 iab-xapp-model-nodeN，不是 compose 內部
#      key 名稱 inference_models_nodeN——兩者不同，用錯會清到不存在的
#      volume，見 CLAUDE.md 第 3 節 Stage 2 踩坑記錄）
#   3. 用指定的 REWARD_MODE 重新啟動 12 個 inference-nodeN 容器
#   4. 啟動 Global xApp + Flower FL 服務（profiles: stage2-fl）
#
# 用法：
#   REWARD_MODE=throughput_only bash iab/run_stage2_fl.sh
#
# Stage 3/4 只需要改 flower-app/iab_fl/server_app.py 的聚合邏輯，
# REWARD_MODE 維持 throughput_only，此腳本不用改直接重跑即可。
# Stage 5 改用 REWARD_MODE=lagrangian 重跑。

set -e
cd "$(dirname "$0")/.."

REWARD_MODE="${REWARD_MODE:-throughput_only}"
COMPOSE_FILE="docker-compose-iab-server.yaml"
DC="docker compose -f $COMPOSE_FILE"

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; NC='\033[0m'

echo -e "${CYAN}[run_stage2_fl] REWARD_MODE=${REWARD_MODE}${NC}"

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

echo -e "${CYAN}[3/4] 用 REWARD_MODE=${REWARD_MODE} 重新啟動 inference-nodeN...${NC}"
REWARD_MODE="$REWARD_MODE" $DC up -d --force-recreate inference-node{1..12}

echo -e "${CYAN}[4/4] 啟動 Global xApp + Flower FL 服務...${NC}"
$DC --profile stage2-fl up -d global-xapp flower-superlink flower-supernode-node{1..12} flower-scheduler

echo -e "${GREEN}==================================================${NC}"
echo -e "${GREEN} Stage 2+ 服務已就緒（REWARD_MODE=${REWARD_MODE}）${NC}"
echo -e "${GREEN}==================================================${NC}"
echo -e "${YELLOW}下一步（量測前，務必先做，見 CLAUDE.md 第 3 節）：${NC}"
echo "  1. bash scenarios/setup_iperf_servers.sh"
echo "  2. 對全部 17 個 UE 做一次現場 ping 測試，確認 0% 封包遺失"
echo "  3. python3 scenarios/traffic_scenario.py --scenario R --seed 20260914 --host {pc1,pc2,pc3} --phase-duration 60 --num-phases 15"
echo "  4. python3 iab/measure_stage.py --host {pc1,pc2,pc3} --duration 900 --interval 5 --out /tmp/stage_{host}.csv"
