#!/bin/bash
# archive_stage_data.sh — 在清空重練前，把目前 stage 的 MongoDB 經驗與 checkpoint
# 封存起來，供之後任何 stage 切換重用（例如 Stage 2 → Stage 3）。
#
# 背景：MongoDB collection 沒有內建 namespace 機制（MONGO_DB 雖可透過環境變數
# 配置，但 docker-compose-iab-server.yaml 全部服務都寫死同一個字面值
# "iab_xapp"；collection 名稱 "node{N}_experiences" 完全不可配置），所以用
# 「改名／複製」而非切換資料庫來做封存，改動範圍最小、不影響任何現有程式碼
# 讀取路徑（run_stage2_fl.sh 接下來要 drop/清空的是封存後新建的空 collection
# 與同一個 checkpoint volume 裡的內容，不會動到已經封存出去的資料）。
#
# 用法：
#   bash iab/archive_stage_data.sh stage2_avgfl_20260918
#   FL_MODE=cluster REWARD_MODE=throughput_only bash iab/run_stage2_fl.sh   # 接著啟動下一個 stage

set -e
cd "$(dirname "$0")/.."

TAG=${1:?"用法: $0 <tag>，例如 stage2_avgfl_20260918"}
ARCHIVE_DIR="experiment_results/checkpoints_archive/${TAG}"

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; NC='\033[0m'

echo -e "${CYAN}[archive] 封存目前的 checkpoint 與 MongoDB 經驗，tag=${TAG}${NC}"

mkdir -p "$ARCHIVE_DIR"
for i in $(seq 1 12); do
    if docker cp "inference-node${i}:/app/models/model_node${i}.pt" "${ARCHIVE_DIR}/model_node${i}.pt" 2>/dev/null; then
        echo -e "  ${GREEN}node${i} checkpoint 已封存${NC}"
    else
        echo -e "  ${YELLOW}警告：node${i} checkpoint 複製失敗（可能尚未產生第一份存檔）${NC}"
    fi
done

docker exec mongodb mongosh iab_xapp --quiet --eval "
for (let i = 1; i <= 12; i++) {
    const src = 'node' + i + '_experiences';
    const dst = src + '_${TAG}';
    if (db.getCollectionNames().includes(src)) {
        db[src].renameCollection(dst);
        print('  封存 ' + src + ' -> ' + dst + '（' + db[dst].countDocuments() + ' 筆）');
    } else {
        print('  ' + src + ' 不存在，跳過');
    }
}
"

echo -e "${GREEN}[archive] 完成。checkpoint -> ${ARCHIVE_DIR}/，MongoDB collection -> node{N}_experiences_${TAG}${NC}"
echo -e "${CYAN}下一步：FL_MODE=<avg|cluster> REWARD_MODE=throughput_only bash iab/run_stage2_fl.sh${NC}"
