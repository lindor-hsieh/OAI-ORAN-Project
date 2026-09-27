#!/bin/bash
# sync_pc23.sh — 把 PC1 上的修改同步到 PC2/PC3（原始碼、腳本、文件），並檢查編譯產物是否比原始碼新（在 PC1 執行）
#
# 用法：bash iab/sync_pc23.sh          # 只比對（dry-run），列出 PC2/PC3 與 PC1 內容不同的檔案
#       bash iab/sync_pc23.sh --apply  # 同步（以校驗碼比對、保留時間戳）+ 再比對 + 產物時間戳檢查
#
# 規則（CLAUDE.md 第 7 節）：所有修改先在 PC1 完成再傳給 PC2/PC3；同步後 C 原始碼必須在 PC2/PC3 各自重編，
# 否則行為停留在舊版。本腳本不代為編譯，只在最後指出「產物比原始碼舊」的項目與該下的編譯指令。
# 不同步：.git、build 目錄、__pycache__、conf/iab_du_node*.conf（啟動腳本執行期會覆寫的佔位值）。
set -u
B=/home/lindor/openairinterface5g
cd "$B" || exit 1
EXC=(--exclude=.git/ --exclude='cmake_targets/ran_build/' --exclude='cmake_targets/*/build/' --exclude=build/
     --exclude=__pycache__/ --exclude='*.pyc' --exclude=checkpoints_archive/ --exclude='conf/iab_du_node*.conf')

diff_list() { rsync -rnci "${EXC[@]}" ./ "$1:$B/" 2>&1 | grep -E '^[<>c.][fd]' | grep -v '^\.d' | awk '{print $2}'; }

if [ "${1:-}" = "--apply" ]; then
  for h in pc2 pc3; do echo "== rsync → $h"; rsync -a --checksum "${EXC[@]}" ./ "$h:$B/" || echo "rsync $h 失敗"; done
fi

rc=0
for h in pc2 pc3; do
  n=$(diff_list $h | wc -l)
  if [ "$n" -eq 0 ]; then echo "[$h] 與 PC1 一致（校驗碼比對 0 個差異）"
  else echo "[$h] 有 $n 個檔案與 PC1 不同："; diff_list $h | head -40; rc=1; fi
done
[ "${1:-}" = "--apply" ] || { echo "（dry-run，未修改任何檔案；要同步請加 --apply）"; exit $rc; }

# 產物 vs 原始碼時間戳（只檢查有明確對應的組合）
CHK='B=/home/lindor/openairinterface5g; R=$B/cmake_targets/ran_build/build; bad=0
chk(){ src=$1; shift; s=$(stat -c %Y $B/$src); for bin in "$@"; do b=$(stat -c %Y $R/$bin 2>/dev/null || echo 0); [ $b -ge $s ] || { echo "  ✘ $bin 比 $(basename $src) 舊"; bad=1; }; done; }
chk common/utils/telnetsrv/telnetsrv_bhload.c nr-softmodem nr-uesoftmodem
chk openair2/LAYER2/NR_MAC_gNB/nr_mac_gNB_backhaul_poll.c nr-softmodem
chk openair2/E2AP/RAN_FUNCTION/CUSTOMIZED/ran_func_mac.c nr-softmodem
chk radio/rfsimulator/simulator.c librfsimulator.so
chk radio/rfsimulator/apply_channelmod.c librfsimulator.so
[ $bad = 0 ] && echo "  ✔ 編譯產物都不比對應原始碼舊"'
for h in local pc2 pc3; do
  echo "[產物檢查 $h]"; if [ $h = local ]; then out=$(bash -c "$CHK"); else out=$(ssh $h "$CHK"); fi; echo "$out"
  echo "$out" | grep -q "✘" && rc=1
done
[ $rc = 1 ] && echo "→ 有產物過舊：在該主機執行  cd $B/cmake_targets/ran_build/build && sudo ninja nr-softmodem nr-uesoftmodem rfsimulator telnetsrv"
exit $rc
