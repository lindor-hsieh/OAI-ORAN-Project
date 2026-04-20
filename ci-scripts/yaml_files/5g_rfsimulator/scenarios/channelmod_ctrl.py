"""
channelmod_ctrl.py — OAI rfsimulator channelmod telnet controller

OAI telnetsrv 接受 channelmod 指令：
  channelmod show config
  channelmod show params <ue_id>
  channelmod modify <ue_id> <param> <value>

key parameters:
  path_loss_dB    : 信號路徑損耗（越高 → SNR 越低 → CQI 越低）
  noise_power_dBm : 附加雜訊功率（越高 → SNR 越低 → CQI 越低）
  max_Doppler     : 都卜勒頻率 (Hz)，增加時變通道效果

path_loss_dB → 預計 wb_cqi 對照表（需依實際環境校正）：
  配置前提：max_pdschReferenceSignalPower = -27, att_tx = 0
  ─────────────────────────────
   path_loss_dB │ SINR 估計 │ CQI
  ─────────────────────────────
        0       │  >60 dB   │  15
       40       │  ~22 dB   │  15
       50       │  ~12 dB   │ 12~13
       55       │  ~7  dB   │  10
       60       │  ~2  dB   │  7~8
       65       │  ~-3 dB   │   5
       70       │  ~-8 dB   │   3
       75       │ ~-13 dB   │   1
  ─────────────────────────────
  上表為理論估算，需執行 calibrate_cqi() 取得實際對應值。
"""

from __future__ import annotations

import logging
import socket
import time
from typing import Optional

log = logging.getLogger("channelmod_ctrl")

# 預設 path_loss_dB 對應各目標 CQI（第一次校正前使用）
_DEFAULT_CQI_TO_PATHLOSS: dict[int, float] = {
    15: 0.0,
    12: 50.0,
    10: 55.0,
    8:  58.0,
    6:  62.0,
    4:  66.0,
    2:  70.0,
    1:  75.0,
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
        resp = self._send_command(f"channelmod modify {ue_id} path_loss_dB {loss_db:.1f}")
        ok = "OK" in resp or resp.strip() != ""
        log.info("set_path_loss ue=%d loss=%.1f dB → %s", ue_id, loss_db, "ok" if ok else "no-resp")
        return ok

    def set_noise_power(self, ue_id: int, noise_dbm: float) -> bool:
        """
        設定附加雜訊功率（輔助旋鈕，與 path_loss_dB 搭配使用）。

        Args:
            ue_id     : UE 連線索引 (0-based)
            noise_dbm : 雜訊功率 dBm，建議範圍 [-120, -70]
        """
        resp = self._send_command(f"channelmod modify {ue_id} noise_power_dBm {noise_dbm:.1f}")
        ok = "OK" in resp or resp.strip() != ""
        log.info("set_noise_power ue=%d noise=%.1f dBm → %s", ue_id, noise_dbm, "ok" if ok else "no-resp")
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
        """恢復理想通道（path_loss=0, noise=-150 dBm）。"""
        self.set_path_loss(ue_id, 0.0)
        self.set_noise_power(ue_id, -150.0)
        log.info("reset_channel ue=%d → ideal", ue_id)

    # -------------------------------------------------------------------------
    # 校正輔助
    # -------------------------------------------------------------------------

    def calibrate_cqi(
        self,
        ue_id: int,
        loss_values: list[float],
        wait_s: float = 5.0,
    ) -> None:
        """
        掃描 path_loss_dB 值並列印觀測提示，協助建立準確的 CQI 對照表。

        用法：
          ctrl = ChannelModController("127.0.0.1", 9091)
          ctrl.calibrate_cqi(ue_id=0, loss_values=[0, 40, 50, 55, 60, 65, 70, 75])

        執行後，觀察 gNB log 中的 wb_cqi 值並手動更新 cqi_to_pathloss。
        """
        print(f"\n[校正] 開始掃描 ue_id={ue_id}，間隔 {wait_s}s")
        print("請同時觀察 gNB log 中的 wb_cqi 欄位\n")
        print(f"{'path_loss_dB':>14} │ 請記錄 wb_cqi")
        print("─" * 32)
        for loss in loss_values:
            self.set_path_loss(ue_id, loss)
            print(f"{loss:>14.1f} │ (等待 {wait_s}s 穩定...)", end="", flush=True)
            time.sleep(wait_s)
            print("  ← 請記錄 CQI")
        print("\n[校正完成] 請更新 ChannelModController.cqi_to_pathloss")
