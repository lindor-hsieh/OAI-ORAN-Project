#!/usr/bin/env bash
# 設定 IAB 實驗室骨幹網路 (USB3.0 -> RJ45 網卡, 三台主機共接一台 switch)
#
#   PC1 = 192.168.88.1 (CN5G / FlexRIC / Donor / Node1,2)
#   PC2 = 192.168.88.2
#   PC3 = 192.168.88.3
#
# 用法（在該台主機本機執行）：
#     bash setup_lab_net.sh <1|2|3>
#
# 動作：
#   1. 自動偵測 USB 網卡 (IFACE_NAME)
#   2. 建立 NetworkManager 靜態設定檔 iab-lab，保留原本對外預設路由
#   3. 安裝並啟用 openssh-server
#   4. 寫入 PC1 的公鑰，讓 PC1 可免密碼 ssh 進來
#   5. 測試與其他兩台的連線
set -uo pipefail

HOST_ID="${1:-}"
case "$HOST_ID" in
  1|2|3) ;;
  *) echo "用法: bash $0 <1|2|3>   # 1=PC1, 2=PC2, 3=PC3"; exit 1 ;;
esac

SUBNET="192.168.88"
MY_IP="${SUBNET}.${HOST_ID}"
CON_NAME="iab-lab"
# PC1 (lindor@lindor-ubuntu) 的公鑰 —— PC2/PC3 需要它才能被 PC1 免密碼登入
PC1_PUBKEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIL7tLGnHq20UDU3/rI9mrz4AZouMqXJkiG4PhrQMp5N9 lindor@lindor-ubuntu'

# ---------- 1. 偵測 USB 網卡 ----------
IFACE=""
for d in /sys/class/net/*; do
  n=$(basename "$d")
  [ "$n" = "lo" ] && continue
  [ -d "$d/wireless" ] && continue          # 排除無線網卡
  if readlink -f "$d/device" 2>/dev/null | grep -q '/usb[0-9]'; then
    IFACE="$n"; break
  fi
done

if [ -z "$IFACE" ]; then
  echo "錯誤：找不到 USB 網卡。請確認轉接卡已插上。目前介面："
  ip -br link
  exit 1
fi

DRV=$(ethtool -i "$IFACE" 2>/dev/null | awk '/^driver:/{print $2}')
MAC=$(cat "/sys/class/net/$IFACE/address")
echo "偵測到 USB 網卡: IFACE_NAME=$IFACE  driver=${DRV:-?}  mac=$MAC"
echo "設定靜態位址: $MY_IP/24  (PC$HOST_ID)"
echo

# ---------- 2. 靜態 IP ----------
sudo nmcli con delete "$CON_NAME" >/dev/null 2>&1
sudo nmcli con add type ethernet con-name "$CON_NAME" ifname "$IFACE" \
  ipv4.method manual \
  ipv4.addresses "${MY_IP}/24" \
  ipv4.never-default yes \
  ipv4.ignore-auto-dns yes \
  ipv6.method link-local \
  connection.autoconnect yes \
  connection.autoconnect-priority 10
sudo nmcli con up "$CON_NAME"
sleep 2

# ---------- 3. SSH server ----------
if ! dpkg -s openssh-server >/dev/null 2>&1; then
  echo "安裝 openssh-server ..."
  sudo apt-get update -qq && sudo apt-get install -y openssh-server
fi
sudo systemctl enable --now ssh 2>/dev/null || sudo systemctl enable --now sshd 2>/dev/null

# ufw 若啟用，放行實驗網段
if command -v ufw >/dev/null 2>&1 && sudo ufw status 2>/dev/null | grep -q "Status: active"; then
  sudo ufw allow from "${SUBNET}.0/24" >/dev/null 2>&1 && echo "ufw：已放行 ${SUBNET}.0/24"
fi

# ---------- 4. 寫入 PC1 公鑰 ----------
if [ "$HOST_ID" != "1" ]; then
  mkdir -p ~/.ssh && chmod 700 ~/.ssh
  touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
  grep -qF "$PC1_PUBKEY" ~/.ssh/authorized_keys || echo "$PC1_PUBKEY" >> ~/.ssh/authorized_keys
  echo "已寫入 PC1 公鑰到 ~/.ssh/authorized_keys"
fi

# ---------- 5. 結果 ----------
echo
echo "============ 結果 ============"
ip -br addr show "$IFACE"
echo "--- 對外預設路由（應維持原本的對外網卡）---"
ip route | grep '^default' || echo "(無預設路由)"
echo "--- 與其他主機連線測試 ---"
for peer in 1 2 3; do
  [ "$peer" = "$HOST_ID" ] && continue
  printf "  PC%s (%s.%s): " "$peer" "$SUBNET" "$peer"
  ping -c1 -W1 "${SUBNET}.${peer}" >/dev/null 2>&1 && echo "OK" || echo "不通（對方可能尚未設定）"
done
echo
echo "===> 請把下面這行回報給 PC1："
echo "===> PC$HOST_ID user=$(whoami) host=$(hostname) iface=$IFACE mac=$MAC ip=$MY_IP sshd=$(systemctl is-active ssh 2>/dev/null || systemctl is-active sshd 2>/dev/null)"
