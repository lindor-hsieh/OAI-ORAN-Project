#!/bin/bash
# restore_stage_data.sh — archive_stage_data.sh 的反向操作：把一份先前封存的
# checkpoint／MongoDB 經驗還原成「現在正在跑」的那份，供重新載入某個 stage
# 的已訓練模型做驗證量測用（例如：先把目前 Stage N 的即時狀態封存起來，再
# 把 Stage M（早於 N）的封存資料還原回即時狀態，量測完後再用本腳本或
# archive_stage_data.sh 把兩者換回來）。
#
# 用法：
#   bash iab/restore_stage_data.sh stage2_avgfl_20260919
#
# 注意：
#   - 還原前務必先用 archive_stage_data.sh 把「現在」的即時狀態封存起來，
#     否則會被覆蓋掉、回不去。
#   - MongoDB 部分是 renameCollection（搬移，不是複製）——還原後來源
#     collection（node{N}_experiences_<tag>）就不存在了，變成即時的
#     node{N}_experiences；這是刻意的，對稱於 archive_stage_data.sh 的行為，
#     確保任何時刻只有一份「即時」資料，不會搞混。
#   - checkpoint 只是複製（docker cp 進容器），archive 目錄本身的檔案不會被
#     刪除，需要的話可以重複還原同一份。
#   - 還原後 inference-nodeN 不會自動重新載入新 checkpoint 到記憶體——呼叫端
#     需要自行 force-recreate inference-nodeN（讓它從磁碟重新啟動讀取），
#     不能依賴背景熱重載執行緒（那個是每次訓練輪次才順便檢查 mtime，可能要
#     等到下一輪，不夠即時）。

set -e
cd "$(dirname "$0")/.."

TAG=${1:?"用法: $0 <tag>，例如 stage2_avgfl_20260919"}
ARCHIVE_DIR="experiment_results/checkpoints_archive/${TAG}"

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; NC='\033[0m'

if [ ! -d "$ARCHIVE_DIR" ]; then
    echo -e "${RED}[restore] 找不到封存目錄 ${ARCHIVE_DIR}，中止${NC}"
    exit 1
fi

echo -e "${CYAN}[restore] 還原封存資料成即時狀態，tag=${TAG}${NC}"

for i in $(seq 1 12); do
    if [ -f "${ARCHIVE_DIR}/model_node${i}.pt" ]; then
        docker cp "${ARCHIVE_DIR}/model_node${i}.pt" "inference-node${i}:/app/models/model_node${i}.pt"
        echo -e "  ${GREEN}node${i} checkpoint 已還原${NC}"
    else
        echo -e "  ${YELLOW}警告：${ARCHIVE_DIR}/model_node${i}.pt 不存在，跳過${NC}"
    fi
done

docker exec mongodb mongosh iab_xapp --quiet --eval "
for (let i=1;i<=12;i++){
    const src='node'+i+'_experiences_${TAG}';
    const dst='node'+i+'_experiences';
    if (db.getCollectionNames().includes(src)) {
        if (db.getCollectionNames().includes(dst)) {
            print('  警告：node'+i+'_experiences 已存在（應該先封存過才對），跳過改名，請人工檢查');
        } else {
            db[src].renameCollection(dst);
            print('  還原 ' + src + ' -> ' + dst + '（' + db[dst].countDocuments() + ' 筆）');
        }
    } else {
        print('  ' + src + ' 不存在，跳過');
    }
}
"

echo -e "${GREEN}[restore] checkpoint／MongoDB 經驗已還原。${YELLOW}記得呼叫端要自己 force-recreate inference-nodeN 才會真正載入新 checkpoint。${NC}"
