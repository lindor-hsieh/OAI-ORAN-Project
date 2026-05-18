"""
traffic_scenario.py — UE 流量場景控制器（DRL 訓練用）

部署環境：在 PC 2 執行
  - 控制 6 個 UE 容器的 iperf3 下行流量（ext-dn → UE，-R reverse，填滿 gNB DL buffer）
  - 透過 channelmod telnet 改變各 DU 對 UE 的通道條件（CQI）
  - 場景提供多樣化的 (BSR, CQI) 組合，驅動 DRL 訓練

前置條件：
  1. PC 1 已執行 setup_iperf_servers.sh，ext-dn 中的 iperf3 server 正在監聽
  2. docker-compose-iab-client.yaml 已加入 --telnetsrv，DU 容器已重新啟動
  3. 所有 UE 容器已啟動，oaitun_ue1 介面已取得 12.1.1.x IP

使用方法：
  python3 traffic_scenario.py --scenario D                  # 動態訓練（預設，持續執行）
  python3 traffic_scenario.py --scenario A --duration 1800  # 固定場景 A，執行 30 分鐘
  python3 traffic_scenario.py --calibrate --node 3          # 對 Node 3 執行 CQI 校正

場景設計（每個 Node 各 2 個 UE）：
  A) CQI 差異化  : 等流量 10 Mbps，每 Node 兩 UE 的 CQI 差距大（15 vs 5）
  B) 流量不均    : 等 CQI (~12)，UE 間流量比例為 5:1
  C) 最差公平性  : 高流量 + 差 CQI vs 低流量 + 好 CQI（壓測公平性）
  D) 動態訓練   : 每 60 秒隨機改變流量與 CQI，最大化狀態多樣性（推薦）

UE ↔ Node 對應：
  Node 3 (telnet :9091): UE1 (ue_id=0), UE2 (ue_id=1)
  Node 4 (telnet :9092): UE3 (ue_id=0), UE4 (ue_id=1)
  Node 5 (telnet :9093): UE5 (ue_id=0), UE6 (ue_id=1)
"""

from __future__ import annotations

import argparse
import logging
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from channelmod_ctrl import ChannelModController

# =============================================================================
# 全域設定
# =============================================================================

EXT_DN_IP = "192.168.72.135"       # ext-dn 在 traffic_net 的 IP
IPERF_DURATION = 300               # iperf3 每次 session 持續時間 (s)；定期循環以刷新連線狀態
IPERF_BIND_IF = "oaitun_ue1"      # UE PDN 介面名稱
FLOW_WATCHDOG_INTERVAL = 30        # 每 30s 檢查一次 DL flow 是否凍結

# iperf3 server port 分配（PC 1 setup_iperf_servers.sh 必須一致）
UE_IPERF_PORTS = {
    "rfsim5g-end-ue-1": 5201,
    "rfsim5g-end-ue-2": 5202,
    "rfsim5g-end-ue-3": 5203,
    "rfsim5g-end-ue-4": 5204,
    "rfsim5g-end-ue-5": 5205,
    "rfsim5g-end-ue-6": 5206,
}

# Node → (DU telnet port, [(ue_container, ue_id), ...])
NODE_CONFIG = {
    3: (9091, [("rfsim5g-end-ue-1", 0), ("rfsim5g-end-ue-2", 1)]),
    4: (9092, [("rfsim5g-end-ue-3", 0), ("rfsim5g-end-ue-4", 1)]),
    5: (9093, [("rfsim5g-end-ue-5", 0), ("rfsim5g-end-ue-6", 1)]),
}

# Node → DU 容器名稱（校正時用來讀取 nrMAC_stats.log）
NODE_TO_DU_CONTAINER: dict[int, str] = {
    3: "rfsim5g-iab-du-3",
    4: "rfsim5g-iab-du-4",
    5: "rfsim5g-iab-du-5",
}

# 校正掃描的 path_loss 值（單位 dB）；上限 25dB，超過會斷線
CALIBRATE_LOSS_VALUES: list[float] = [0.0, 5.0, 10.0, 14.0, 18.0, 21.0, 23.0, 25.0]

# _DEFAULT_CQI_TO_PATHLOSS 的標準 CQI 鍵值集合
_STANDARD_CQIS: list[int] = [15, 12, 10, 8, 6, 4, 2, 1]

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("traffic_scenario")


# =============================================================================
# UE 狀態容器
# =============================================================================

@dataclass
class UEConfig:
    container: str
    ue_id: int            # channelmod ue_id（0-based，對應 rfsimulator 連線索引）
    node_id: int
    target_cqi: int = 12
    bandwidth_mbps: float = 10.0
    _iperf_proc: Optional[subprocess.Popen] = field(default=None, repr=False)
    _last_rx_bytes: int = field(default=0, repr=False)       # watchdog: 上次量到的 oaitun_ue1 rx bytes
    _last_rx_check: float = field(default=0.0, repr=False)   # watchdog: 上次檢查的 timestamp

    @property
    def iperf_port(self) -> int:
        return UE_IPERF_PORTS[self.container]


# =============================================================================
# iperf3 控制
# =============================================================================

def _get_oaitun_rx_bytes(container: str) -> int:
    """讀取 UE 容器 oaitun_ue1 介面的 RX byte 計數（用於 DL flow watchdog）。"""
    try:
        result = subprocess.run(
            ["docker", "exec", container,
             "cat", "/sys/class/net/oaitun_ue1/statistics/rx_bytes"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            return int(result.stdout.strip())
    except Exception:
        pass
    return 0


def _get_ue_ip(container: str) -> Optional[str]:
    """取得 UE 容器 oaitun_ue1 介面的 IP 位址。"""
    try:
        result = subprocess.run(
            ["docker", "exec", container,
             "ip", "addr", "show", IPERF_BIND_IF],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("inet "):
                ip = line.split()[1].split("/")[0]
                return ip
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError) as exc:
        log.warning("取得 %s IP 失敗: %s", container, exc)
    return None


def start_iperf_client(ue: UEConfig) -> bool:
    """
    在 UE 容器內以 while loop 持續執行 iperf3 TCP 下行客戶端（-R reverse mode）。

    不使用 docker exec -d（detached）：-d 會讓 docker exec 立刻以 rc=0 返回，
    導致 _iperf_proc.poll() 馬上非 None，watchdog 誤判死亡並不斷重啟。

    改為前台執行 sh -c "while true; do iperf3 ...; sleep 2; done"：
    - docker exec 持續存活，poll() 恆為 None → watchdog 不誤觸發
    - loop 每次迭代前動態查詢 oaitun_ue1 的 IP：UE 暫時斷線重連後可自動恢復
    - 若介面尚未就緒（IP 為空），每 5 秒輪詢一次，待 IP 出現後立即重啟 iperf3
    - 使用 TCP 而非 UDP：TCP 擁塞控制可自適應 DRL PRB 震盪，不因 TCP 控制通道中斷而斷線
    """
    # timeout {IPERF_DURATION+30} 確保即使 iperf3 卡死也會被強制結束，
    # 避免 while loop 永遠等不到下一次迭代。
    loop_cmd = (
        f"trap 'pkill -f iperf3; exit 0' TERM INT; "
        f"while true; do "
        f"UE_IP=$(ip addr show {IPERF_BIND_IF} 2>/dev/null | grep 'inet ' | awk '{{print $2}}' | cut -d/ -f1); "
        f"if [ -z \"$UE_IP\" ]; then sleep 5; continue; fi; "
        f"timeout {IPERF_DURATION + 30} "
        f"iperf3 -c {EXT_DN_IP} -b {ue.bandwidth_mbps:.0f}M -R "
        f"-t {IPERF_DURATION} -p {ue.iperf_port} -B $UE_IP --forceflush; "
        f"sleep 2; "
        f"done"
    )
    cmd = ["docker", "exec", ue.container, "sh", "-c", loop_cmd]

    try:
        stop_iperf_client(ue)
        ue._iperf_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        ue._last_rx_bytes = 0
        ue._last_rx_check = time.time()
        log.info("iperf3 loop start: %s → %s:%d @ %.0fMbps",
                 ue.container, EXT_DN_IP, ue.iperf_port, ue.bandwidth_mbps)
        return True
    except FileNotFoundError:
        log.error("找不到 docker 指令")
        return False


def stop_iperf_client(ue: UEConfig) -> None:
    """停止 UE 容器中的 iperf3 loop（先殺容器內程序，再終止 docker exec）。"""
    # 用 SIGKILL（-9）直接殺，避免 SIGTERM trap handler 慢速執行造成 race condition：
    # SIGTERM 會觸發 sh loop 的 trap，trap 內又 pkill，導致 stop_iperf_client.wait(5) timeout，
    # wait timeout 後只 kill docker exec（PC 端），容器內 sh loop 仍殘存 → 多 loop 並發。
    # SIGKILL 則立即終止，不給 trap 機會，確保容器內只有一個 loop。
    try:
        subprocess.run(
            ["docker", "exec", ue.container, "sh", "-c",
             "pkill -9 -f iperf3 2>/dev/null; pkill -9 -f 'while true' 2>/dev/null; true"],
            capture_output=True, timeout=5,
        )
        time.sleep(0.3)   # 給 kernel 時間清理進程，避免 Popen 啟動時舊 loop 仍存在
    except subprocess.TimeoutExpired:
        log.warning("stop_iperf_client: %s docker exec 超時（容器可能重啟中），跳過 pkill",
                    ue.container)
    except Exception as exc:
        log.warning("stop_iperf_client: %s 異常: %s", ue.container, exc)
    if ue._iperf_proc:
        try:
            ue._iperf_proc.terminate()
            ue._iperf_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            ue._iperf_proc.kill()
            ue._iperf_proc.wait()
        ue._iperf_proc = None


def stop_all_iperf(ues: list[UEConfig]) -> None:
    for ue in ues:
        stop_iperf_client(ue)


# =============================================================================
# 場景應用
# =============================================================================

def apply_ue_config(
    ue: UEConfig,
    ctrl: ChannelModController,
    new_cqi: Optional[int] = None,
    new_bw: Optional[float] = None,
) -> None:
    """套用新的 CQI 與頻寬設定到單一 UE。

    CQI 變更：透過 channelmod telnet 即時設定，無需重啟 iperf3。
    BW 變更：BW 烘焙進 loop_cmd，必須重啟 iperf3 loop 才能套用新值（~0.3s 短暫無流量）。
    """
    changed = False

    if new_cqi is not None and new_cqi != ue.target_cqi:
        ue.target_cqi = new_cqi
        ctrl.set_target_cqi(ue.ue_id, new_cqi)
        changed = True

    if new_bw is not None and abs(new_bw - ue.bandwidth_mbps) > 0.5:
        ue.bandwidth_mbps = new_bw
        # BW 烘焙進 loop_cmd，必須重啟 loop 才能套用新值。
        # stop_iperf_client 在 start_iperf_client 內部處理；
        # 短暫 2~3s 的無流量期 DRL 可容忍，各 UE 間有 100ms 錯開。
        start_iperf_client(ue)
        changed = True

    if changed:
        log.info("  %-28s CQI=%2d  BW=%.0fMbps",
                 ue.container, ue.target_cqi, ue.bandwidth_mbps)


def apply_scenario_phase(
    ues: list[UEConfig],
    ctrls: dict[int, ChannelModController],
    configs: list[tuple[int, float]],  # [(target_cqi, bandwidth_mbps), ...]
) -> None:
    """
    套用一個場景相位的設定到所有 UE。

    configs 長度必須為 6，對應 UE1~UE6。
    各 UE 之間插入 100ms 間隔給 channelmod telnet 指令回應。
    """
    assert len(configs) == len(ues), "configs 長度必須等於 UE 數量"
    log.info("── 套用場景相位 ──")
    for ue, (cqi, bw) in zip(ues, configs):
        ctrl = ctrls[ue.node_id]
        apply_ue_config(ue, ctrl, new_cqi=cqi, new_bw=bw)
        time.sleep(0.1)


# =============================================================================
# 預定義場景
# =============================================================================

def scenario_a(ues: list[UEConfig], ctrls: dict[int, ChannelModController]) -> list[tuple[int, float]]:
    """
    場景 A：CQI 差異化
    目標：訓練 DRL 識別通道品質，優先分配 PRB 給高 CQI UE（最大化吞吐量）。
    每個 Node 內 UE 對比：CQI 15 vs CQI 5，流量相等。
    """
    return [
        (15, 10.0),  # UE1 @ Node3: 好通道
        (5,  10.0),  # UE2 @ Node3: 差通道
        (15, 10.0),  # UE3 @ Node4: 好通道
        (5,  10.0),  # UE4 @ Node4: 差通道
        (15, 10.0),  # UE5 @ Node5: 好通道
        (5,  10.0),  # UE6 @ Node5: 差通道
    ]


def scenario_b(ues: list[UEConfig], ctrls: dict[int, ChannelModController]) -> list[tuple[int, float]]:
    """
    場景 B：流量不均（Jain's Fairness 壓測）
    目標：訓練 DRL 在相同 CQI 下公平分配 PRB，避免高流量 UE 飢餓低流量 UE。
    """
    return [
        (12, 25.0),  # UE1 @ Node3: 高需求
        (12,  5.0),  # UE2 @ Node3: 低需求
        (12, 20.0),  # UE3 @ Node4: 高需求
        (12,  4.0),  # UE4 @ Node4: 低需求
        (12, 22.0),  # UE5 @ Node5: 高需求
        (12,  3.0),  # UE6 @ Node5: 低需求
    ]


def scenario_c(ues: list[UEConfig], ctrls: dict[int, ChannelModController]) -> list[tuple[int, float]]:
    """
    場景 C：最惡公平性（高流量 + 差通道 vs 低流量 + 好通道）
    目標：讓 DRL 學習在「頻譜效率低但需求大」的 UE 與「效率高但需求小」的 UE 間取得平衡。
    """
    return [
        (4,  25.0),  # UE1 @ Node3: 差通道高需求 ← 挑戰排程器
        (14,  3.0),  # UE2 @ Node3: 好通道低需求
        (4,  20.0),  # UE3 @ Node4: 差通道高需求
        (13,  4.0),  # UE4 @ Node4: 好通道低需求
        (5,  22.0),  # UE5 @ Node5: 差通道高需求
        (14,  2.0),  # UE6 @ Node5: 好通道低需求
    ]


def scenario_d_random(ues: list[UEConfig]) -> list[tuple[int, float]]:
    """
    場景 D：隨機相位（每 60 秒呼叫一次，生成新的隨機設定）
    目標：最大化訓練資料多樣性，讓 DRL 學習通用策略。
    """
    # path_loss 表已壓縮至 0~25dB 安全範圍，所有 CQI 值皆可使用
    cqi_choices = list(range(1, 16))   # 1~15 完整覆蓋
    # BW 最低 5 Mbps，最高 50 Mbps（106 PRB 兩 UE 共享上限約 40Mbps，50 留餘量）
    # 超過 50Mbps 會讓 rfsim TCP buffer 滿載，導致 DU 排程延遲與 DL 路徑崩潰。
    bw_choices = [float(x) for x in range(5, 55, 5)]
    configs = []
    for _ in ues:
        cqi = random.choice(cqi_choices)
        bw = random.choice(bw_choices)
        configs.append((cqi, bw))
    return configs


# =============================================================================
# 主控邏輯
# =============================================================================

def build_ue_list() -> list[UEConfig]:
    ues = []
    for node_id, (_, ue_list) in NODE_CONFIG.items():
        for container, ue_id in ue_list:
            ues.append(UEConfig(container=container, ue_id=ue_id, node_id=node_id))
    return ues


def build_controllers() -> dict[int, ChannelModController]:
    return {
        node_id: ChannelModController("127.0.0.1", telnet_port)
        for node_id, (telnet_port, _) in NODE_CONFIG.items()
    }


def wait_for_ue_interfaces(ues: list[UEConfig], max_wait: int = 120) -> bool:
    """等待所有 UE oaitun_ue1 介面就緒，最多等 max_wait 秒。"""
    log.info("等待 UE PDN 介面就緒...")
    deadline = time.time() + max_wait
    while time.time() < deadline:
        ready = [ue for ue in ues if _get_ue_ip(ue.container) is not None]
        log.info("  已就緒: %d/%d", len(ready), len(ues))
        if len(ready) == len(ues):
            return True
        time.sleep(5)
    log.warning("部分 UE 介面未就緒，繼續執行（可能影響部分 UE 流量）")
    return False


def run_fixed_scenario(
    scenario_name: str,
    ues: list[UEConfig],
    ctrls: dict[int, ChannelModController],
    duration: int,
) -> None:
    """執行固定場景，持續 duration 秒後結束。"""
    scenario_fn = {
        "A": scenario_a,
        "B": scenario_b,
        "C": scenario_c,
    }[scenario_name]

    log.info("=== 場景 %s 開始，持續 %d 秒 ===", scenario_name, duration)
    configs = scenario_fn(ues, ctrls)
    apply_scenario_phase(ues, ctrls, configs)

    # 啟動所有 iperf3（apply_scenario_phase 已處理 BW 變更，這裡補起初次啟動）
    for ue in ues:
        if ue._iperf_proc is None:
            start_iperf_client(ue)

    try:
        time.sleep(duration)
    except KeyboardInterrupt:
        log.info("收到中斷，停止場景")
    finally:
        log.info("=== 場景 %s 結束，清理 iperf3 ===", scenario_name)
        stop_all_iperf(ues)
        for node_id, ctrl in ctrls.items():
            for ue in ues:
                if ue.node_id == node_id:
                    ctrl.reset_channel(ue.ue_id)


def run_dynamic_scenario(
    ues: list[UEConfig],
    ctrls: dict[int, ChannelModController],
    phase_duration: int = 60,
) -> None:
    """
    場景 D：動態訓練模式，每 phase_duration 秒隨機切換一次。

    持續執行直到 Ctrl+C。
    """
    log.info("=== 場景 D 動態訓練開始，相位間隔 %d 秒 ===", phase_duration)
    phase = 0
    try:
        while True:
            phase += 1
            configs = scenario_d_random(ues)
            log.info("── 相位 %d ──", phase)
            apply_scenario_phase(ues, ctrls, configs)

            # 確保所有 iperf3 在第一個相位啟動
            for ue in ues:
                if ue._iperf_proc is None:
                    start_iperf_client(ue)

            # 每 10 秒輪詢一次：
            #   1. 容器級別失敗：docker exec 退出 → 重啟整個 loop
            #   2. DL flow watchdog：oaitun_ue1 rx_bytes 30s 未增加 → pkill iperf3
            #      （while loop 在容器內自行 sleep 2 後重啟新 session）
            deadline = time.time() + phase_duration
            while time.time() < deadline:
                time.sleep(10)
                now = time.time()
                for ue in ues:
                    # ── 容器級別失敗 ──────────────────────────────────────────
                    if ue._iperf_proc is not None and ue._iperf_proc.poll() is not None:
                        log.warning("iperf3 supervisor 退出 %s (rc=%d)，重啟 loop...",
                                    ue.container, ue._iperf_proc.returncode)
                        ue._iperf_proc = None
                        start_iperf_client(ue)
                        continue
                    # ── DL flow watchdog ──────────────────────────────────────
                    if ue._iperf_proc is None:
                        continue
                    if now - ue._last_rx_check < FLOW_WATCHDOG_INTERVAL:
                        continue
                    rx = _get_oaitun_rx_bytes(ue.container)
                    if ue._last_rx_bytes > 0 and rx == ue._last_rx_bytes:
                        log.warning(
                            "DL frozen: %s rx_bytes=%d 超過 %ds 未增加，強制重啟 iperf3 session",
                            ue.container, rx, FLOW_WATCHDOG_INTERVAL,
                        )
                        # 殺完立即重啟（不等下一輪 poll 偵測 _iperf_proc.poll()）
                        start_iperf_client(ue)
                        ue._last_rx_bytes = 0   # 重置計數，新 session 從 0 開始判斷
                    else:
                        ue._last_rx_bytes = rx
                    ue._last_rx_check = now
    except KeyboardInterrupt:
        log.info("收到中斷，停止動態場景（共執行 %d 個相位）", phase)
    finally:
        stop_all_iperf(ues)
        for node_id, ctrl in ctrls.items():
            for ue in ues:
                if ue.node_id == node_id:
                    ctrl.reset_channel(ue.ue_id)


def _build_full_cqi_mapping(observed: list[tuple[float, int]]) -> dict[int, float]:
    """
    從 (path_loss, observed_cqi) 觀測序列建構 {standard_cqi: path_loss} 映射。

    對每個標準 CQI 鍵，選取觀測 CQI 最接近的點；
    差距相同時選 path_loss 最高的（讓通道差異最大化）。
    """
    result: dict[int, float] = {}
    for target in _STANDARD_CQIS:
        best = min(observed, key=lambda lc: (abs(lc[1] - target), -lc[0]))
        result[target] = best[0]
    return result


def _write_cqi_pathloss_table(mapping: dict[int, float]) -> None:
    """
    將 mapping 寫回 channelmod_ctrl.py 的 _DEFAULT_CQI_TO_PATHLOSS。
    使用 regex 原地替換，保留其他程式碼不變。
    """
    ctrl_file = Path(__file__).parent / "channelmod_ctrl.py"
    content = ctrl_file.read_text()

    entries = "{\n"
    for cqi in sorted(mapping.keys(), reverse=True):
        entries += f"    {cqi}: {mapping[cqi]:.1f},\n"
    entries += "}"

    new_content = re.sub(
        r'_DEFAULT_CQI_TO_PATHLOSS: dict\[int, float\] = \{[^}]*\}',
        f'_DEFAULT_CQI_TO_PATHLOSS: dict[int, float] = {entries}',
        content,
        flags=re.DOTALL,
    )
    if new_content == content:
        print("[警告] 找不到 _DEFAULT_CQI_TO_PATHLOSS，請手動更新 channelmod_ctrl.py")
        return
    ctrl_file.write_text(new_content)
    log.info("channelmod_ctrl.py _DEFAULT_CQI_TO_PATHLOSS 已更新")


def run_calibration(node_id: int) -> None:
    """
    全自動 CQI 校正：
      1. 掃描 path_loss 0~25dB（每步等 5s 穩定）
      2. 自動從 DU 容器的 nrMAC_stats.log 讀取實測 CQI
      3. 建構 {standard_cqi: path_loss} 映射
      4. 寫回 channelmod_ctrl.py（Node 3 負責寫檔；Node 4/5 共用相同通道特性）
    """
    telnet_port = NODE_CONFIG[node_id][0]
    du_container = NODE_TO_DU_CONTAINER[node_id]
    ctrl = ChannelModController("127.0.0.1", telnet_port)

    print(f"\n=== Node {node_id} 全自動 CQI 校正 ===")
    print(f"  telnet port : {telnet_port}")
    print(f"  DU 容器     : {du_container}")
    print(f"  掃描範圍    : {CALIBRATE_LOSS_VALUES[0]}~{CALIBRATE_LOSS_VALUES[-1]} dB")
    print("請確認至少有一個 UE 連線到此 Node（oaitun_ue1 已取得 IP）")
    input("按 Enter 開始（約需 40 秒）...")

    observed = ctrl.calibrate_auto(
        ue_id=0,
        du_container=du_container,
        loss_values=CALIBRATE_LOSS_VALUES,
        wait_s=5.0,
    )

    if not observed:
        print("\n[校正失敗] 未讀取到任何 CQI 值。")
        print("  可能原因：DU 容器未運行、nrMAC_stats.log 尚未生成、UE 未連線。")
        return

    mapping = _build_full_cqi_mapping(observed)

    print(f"\n校正結果（標準 CQI → path_loss）：")
    for cqi in sorted(mapping.keys(), reverse=True):
        print(f"  CQI {cqi:2d}  →  {mapping[cqi]:.1f} dB")

    # 立即生效（更新 class 變數）
    ChannelModController.cqi_to_pathloss.update(mapping)

    # Node 3 作為代表寫回 channelmod_ctrl.py（rfsimulator 各節點通道特性相同）
    if node_id == 3:
        _write_cqi_pathloss_table(mapping)
        print("\n[完成] channelmod_ctrl.py _DEFAULT_CQI_TO_PATHLOSS 已自動更新。")
        print("       下次啟動不需重新校正（除非修改硬體/參數）。")
    else:
        print(f"\n[完成] Node {node_id} 校正結果已套用至本次執行（Node 3 的結果已寫入檔案）。")


# =============================================================================
# 入口
# =============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="IAB DRL 訓練流量場景控制器"
    )
    parser.add_argument(
        "--scenario", choices=["A", "B", "C", "D"], default="D",
        help="場景選擇：A=CQI差異, B=流量不均, C=最差公平性, D=動態訓練（預設）",
    )
    parser.add_argument(
        "--duration", type=int, default=1800,
        help="固定場景 A/B/C 的執行秒數（預設 1800s = 30min）",
    )
    parser.add_argument(
        "--phase-duration", type=int, default=60,
        help="場景 D 每個相位的秒數（預設 60s）",
    )
    parser.add_argument(
        "--calibrate", action="store_true",
        help="執行 CQI 校正模式（需搭配 --node）",
    )
    parser.add_argument(
        "--node", type=int, choices=[3, 4, 5], default=3,
        help="校正模式：指定要校正的 Node（3/4/5）",
    )
    parser.add_argument(
        "--no-wait", action="store_true",
        help="跳過等待 UE 介面就緒的步驟",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.calibrate:
        run_calibration(args.node)
        return

    ues = build_ue_list()
    ctrls = build_controllers()

    if not args.no_wait:
        wait_for_ue_interfaces(ues)

    if args.scenario == "D":
        run_dynamic_scenario(ues, ctrls, phase_duration=args.phase_duration)
    else:
        run_fixed_scenario(args.scenario, ues, ctrls, duration=args.duration)


if __name__ == "__main__":
    main()
