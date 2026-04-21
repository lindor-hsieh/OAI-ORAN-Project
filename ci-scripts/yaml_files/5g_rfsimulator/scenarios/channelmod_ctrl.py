"""
channelmod_ctrl.py — OAI rfsimulator channelmod telnet controller

OAI telnetsrv 接受 channelmod 指令：
  channelmod show config
  channelmod show params <ue_id>
  channelmod modify <ue_id> <param> <value>

key parameters:
  path_loss_dB    : 信號路徑損耗（越高 → SNR 越低 → CQI 越低）
  noise_power_dB  : 附加雜訊功率（越高 → SNR 越低 → CQI 越低）
  max_Doppler     : 都卜勒頻率 (Hz)，增加時變通道效果

path_loss_dB → 預計 wb_cqi 對照表（需依實際環境校正）：
  配置前提：max_pdschReferenceSignalPower = -27, att_tx = 0
  實測安全上限：path_loss > 25~30dB → UE 斷線（MCS max=28，系統 SNR 極高）
  ─────────────────────────────
   path_loss_dB │ 預計 CQI  │ 備註
  ─────────────────────────────
        0       │  15       │ 理想通道
        5       │  12~15    │ 需校正
       10       │  10~12    │ 需校正
       14       │   8~10    │ 需校正
       18       │   6~8     │ 需校正
       21       │   4~6     │ 需校正
       23       │   2~4     │ 需校正
       25       │   1~2     │ 安全下限（勿超過）
  ─────────────────────────────
  請執行 calibrate_cqi() 取得實際對應值並更新 _DEFAULT_CQI_TO_PATHLOSS。
"""

from __future__ import annotations

import logging
import re
import socket
import subprocess
import time
from typing import Optional

log = logging.getLogger("channelmod_ctrl")

# 預設 path_loss_dB 對應各目標 CQI（第一次校正前使用）
# 實測安全上限：path_loss > 25~30dB 會導致 UE 斷線（MCS 最高 28，系統 SNR 高）
# 所有值壓縮在 0~25dB 範圍內，校正後以實測值取代
_DEFAULT_CQI_TO_PATHLOSS: dict[int, float] = {
    15: 0.0,
    12: 5.0,
    10: 10.0,
    8:  14.0,
    6:  18.0,
    4:  21.0,
    2:  23.0,
    1:  25.0,
}


class ChannelModController:
    """
    透過 TCP socket 控制 OAI rfsimulator channelmod。

    每個 IAB-DU 有獨立的 telnetsrv port：
      Node 3 DU → 127.0.0.1:9091
      Node 4 DU → 127.0.0.1:9092
      Node 5 DU → 127.0.0.1:9093

    ue_id 對應 rfsimulator 內部 UE 連線索引（0-based）：
      Node 3: UE1 → ue_id=0, UE2 → ue_id=1
      Node 4: UE3 → ue_id=0, UE4 → ue_id=1
      Node 5: UE5 → ue_id=0, UE6 → ue_id=1
    """

    # 供校正後更新的 CQI → path_loss 對照表
    cqi_to_pathloss: dict[int, float] = dict(_DEFAULT_CQI_TO_PATHLOSS)

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9091,
        timeout: float = 3.0,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout

    # -------------------------------------------------------------------------
    # 底層通訊
    # -------------------------------------------------------------------------

    def _send_command(self, cmd: str) -> str:
        """
        建立連線、送出指令、讀取回應後關閉。

        OAI telnetsrv 接受 LF 結尾的文字指令，在 timeout 內讀回全部輸出。
        """
        try:
            with socket.create_connection((self.host, self.port), self.timeout) as sock:
                sock.settimeout(self.timeout)
                # 等待 welcome banner
                time.sleep(0.05)
                try:
                    sock.recv(4096)
                except socket.timeout:
                    pass

                sock.sendall((cmd + "\n").encode())
                time.sleep(0.1)

                chunks: list[bytes] = []
                sock.settimeout(0.5)
                while True:
                    try:
                        data = sock.recv(4096)
                        if not data:
                            break
                        chunks.append(data)
                    except socket.timeout:
                        break

                response = b"".join(chunks).decode(errors="replace")
                log.debug("[Node telnet:%d] cmd=%r resp=%r", self.port, cmd, response[:200])
                return response
        except (OSError, ConnectionRefusedError) as exc:
            log.warning("channelmod telnet 連線失敗 (%s:%d): %s", self.host, self.port, exc)
            return ""

    # -------------------------------------------------------------------------
    # 公開 API
    # -------------------------------------------------------------------------

    def show_config(self) -> str:
        """顯示目前 channelmod 設定（用於除錯/確認）。"""
        return self._send_command("channelmod show config")

    def show_current(self) -> str:
        """列出所有已載入的 channel model 及其索引（用於確認正確索引）。"""
        return self._send_command("channelmod show current")

    def show_params(self, ue_id: int) -> str:
        """顯示指定 UE 的 channelmod 參數。"""
        return self._send_command(f"channelmod show params {ue_id}")

    def set_path_loss(self, ue_id: int, loss_db: float) -> bool:
        """
        設定 UE 的路徑損耗（直接控制 CQI 的主要旋鈕）。

        Args:
            ue_id  : UE 連線索引 (0-based)
            loss_db: 路徑損耗 dB，範圍建議 [0, 80]

        Returns:
            True 表示指令送達（不代表 OAI 成功套用）。
        """
        resp = self._send_command(f"channelmod modify {ue_id} ploss {loss_db:.1f}")
        ok = len(resp.strip()) > 0
        log.info("set_path_loss ue=%d loss=%.1f dB → %s", ue_id, loss_db, "ok" if ok else "no-resp")
        return ok

    def set_noise_power(self, ue_id: int, noise_db: float) -> bool:
        """
        設定附加雜訊功率（輔助旋鈕，與 path_loss_dB 搭配使用）。

        Args:
            ue_id    : UE 連線索引 (0-based)
            noise_db : 雜訊功率 dB，OAI 預設 -50，越高 SNR 越低
        """
        resp = self._send_command(f"channelmod modify {ue_id} noise_power_dB {noise_db:.1f}")
        ok = len(resp.strip()) > 0
        log.info("set_noise_power ue=%d noise=%.1f dB → %s", ue_id, noise_db, "ok" if ok else "no-resp")
        return ok

    def set_doppler(self, ue_id: int, doppler_hz: float) -> bool:
        """設定都卜勒頻率，模擬移動 UE 的時變通道。"""
        resp = self._send_command(f"channelmod modify {ue_id} max_Doppler {doppler_hz:.1f}")
        ok = "OK" in resp or resp.strip() != ""
        log.info("set_doppler ue=%d doppler=%.1f Hz → %s", ue_id, doppler_hz, "ok" if ok else "no-resp")
        return ok

    def set_target_cqi(self, ue_id: int, target_cqi: int) -> bool:
        """
        根據 cqi_to_pathloss 對照表設定 path_loss_dB，逼近目標 CQI。

        target_cqi 必須為 {1,2,4,6,8,10,12,15} 之一；
        若不在表中則取最接近的值。
        """
        valid = sorted(self.cqi_to_pathloss.keys())
        nearest = min(valid, key=lambda c: abs(c - target_cqi))
        if nearest != target_cqi:
            log.debug("target_cqi=%d 不在對照表，使用最近值 %d", target_cqi, nearest)
        loss = self.cqi_to_pathloss[nearest]
        return self.set_path_loss(ue_id, loss)

    def reset_channel(self, ue_id: int) -> None:
        """恢復理想通道（path_loss=0, noise_power_dB=-50，即 OAI 預設值）。"""
        self.set_path_loss(ue_id, 0.0)
        self.set_noise_power(ue_id, -50.0)
        log.info("reset_channel ue=%d → ideal", ue_id)

    # -------------------------------------------------------------------------
    # 校正輔助
    # -------------------------------------------------------------------------

    def read_wb_cqi(self, du_container: str, retries: int = 3) -> Optional[int]:
        """
        從 DU 容器的 nrMAC_stats.log 自動讀取最低 wb_cqi。

        OAI 的 nrmac_stats_thread 每秒將 MAC 統計寫入 nrMAC_stats.log，
        格式為 "UE xxxx: CQI X, RI X, PMI (X,X)"。
        每個 Node 有 2 個 UE：ue_id=0 套用了 path_loss，CQI 較低，
        取 min() 即可得到 ue_id=0 的觀測 CQI。
        """
        for attempt in range(retries):
            # 動態找出 log 檔路徑（OAI 從執行目錄寫入，不同容器可能不同）
            find_res = subprocess.run(
                ["docker", "exec", du_container,
                 "find", "/", "-maxdepth", "6", "-name", "nrMAC_stats.log"],
                capture_output=True, text=True, timeout=10,
            )
            paths = [p.strip() for p in find_res.stdout.splitlines() if p.strip()]
            for path in paths:
                cat_res = subprocess.run(
                    ["docker", "exec", du_container, "cat", path],
                    capture_output=True, text=True, timeout=5,
                )
                if cat_res.returncode == 0 and cat_res.stdout.strip():
                    cqis = re.findall(r'\bCQI\s+(\d+)', cat_res.stdout)
                    if cqis:
                        return min(int(c) for c in cqis)
            if attempt < retries - 1:
                log.debug("read_wb_cqi: 第 %d 次未讀到 CQI，等待 2s 重試", attempt + 1)
                time.sleep(2)
        log.warning("read_wb_cqi: 無法從 %s 讀取 CQI（nrMAC_stats.log 不存在或 UE 未連線）",
                    du_container)
        return None

    def calibrate_auto(
        self,
        ue_id: int,
        du_container: str,
        loss_values: list[float],
        wait_s: float = 5.0,
    ) -> list[tuple[float, int]]:
        """
        全自動校正：掃描 path_loss，從容器 log 讀取實際 CQI，回傳觀測序列。

        回傳: [(path_loss_dB, observed_cqi), ...]
        """
        print(f"\n[自動校正] telnet:{self.port}  DU容器:{du_container}")
        print(f"{'path_loss_dB':>14} │ 實測 CQI")
        print("─" * 26)
        observed: list[tuple[float, int]] = []
        for loss in loss_values:
            self.set_path_loss(ue_id, loss)
            time.sleep(wait_s)
            cqi = self.read_wb_cqi(du_container)
            if cqi is None:
                print(f"{loss:>14.1f} │ (讀取失敗，跳過)")
                continue
            observed.append((loss, cqi))
            print(f"{loss:>14.1f} │ {cqi}")
        self.reset_channel(ue_id)
        return observed

    def calibrate_cqi(
        self,
        ue_id: int,
        loss_values: list[float],
        wait_s: float = 5.0,
    ) -> None:
        """手動校正（已由 calibrate_auto 取代）：掃描並列印提示，需人工記錄 CQI。"""
        print(f"\n[手動校正] 開始掃描 ue_id={ue_id}，間隔 {wait_s}s")
        print("請同時觀察 gNB log 中的 wb_cqi 欄位\n")
        print(f"{'path_loss_dB':>14} │ 請記錄 wb_cqi")
        print("─" * 32)
        for loss in loss_values:
            self.set_path_loss(ue_id, loss)
            print(f"{loss:>14.1f} │ (等待 {wait_s}s 穩定...)", end="", flush=True)
            time.sleep(wait_s)
            print("  ← 請記錄 CQI")
        print("\n[校正完成] 請更新 ChannelModController.cqi_to_pathloss")
