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
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

from channelmod_ctrl import ChannelModController

# =============================================================================
# 全域設定
# =============================================================================

EXT_DN_IP = "192.168.72.135"       # ext-dn 在 traffic_net 的 IP
IPERF_DURATION = 86400             # iperf3 每次啟動持續時間 (s)，夠長以持續執行
IPERF_BIND_IF = "oaitun_ue1"      # UE PDN 介面名稱

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

    @property
    def iperf_port(self) -> int:
        return UE_IPERF_PORTS[self.container]


# =============================================================================
# iperf3 控制
# =============================================================================

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
    在 UE 容器中啟動 iperf3 UDP 上行客戶端（UE → ext-dn，UL 方向）。

    使用 UL 流量讓 UE 的 MAC 層產生非零 BSR（Buffer Status Report），
    xApp 讀取 BSR 作為 DRL 狀態輸入。若使用 -R（DL），UE 無 UL 資料，
    BSR 恆為 0，DRL 無法學習。

    重啟順序：先取得 UE IP（舊 iperf3 仍在跑，oaitun_ue1 必然存在），
    再 pkill 舊進程並立刻啟動新進程，將流量空隙壓縮至毫秒級，
    避免 gNB inactivity timer 釋放 RRC 連線導致 UE crash。
    """
    ue_ip = _get_ue_ip(ue.container)
    if ue_ip is None:
        log.error("%s: oaitun_ue1 不存在，跳過 iperf3 啟動", ue.container)
        return False

    cmd = [
        "docker", "exec", "-d", ue.container,
        "iperf3",
        "-c", EXT_DN_IP,
        "-u",                           # UDP
        "-b", f"{ue.bandwidth_mbps:.0f}M",   # UL：UE → ext-dn，產生 BSR
        "-t", str(IPERF_DURATION),
        "-p", str(ue.iperf_port),
        "-B", ue_ip,                    # 綁定 PDN 介面 IP，確保流量走 5G 路徑
        "--forceflush",
    ]

    try:
        # 停舊進程後立刻啟動新進程，縮短無流量的空窗期
        stop_iperf_client(ue)
        ue._iperf_proc = subprocess.Popen(cmd)
        log.info("iperf3 start: %s → %s:%d @ %.0fMbps",
                 ue.container, EXT_DN_IP, ue.iperf_port, ue.bandwidth_mbps)
        return True
    except FileNotFoundError:
        log.error("找不到 docker 指令")
        return False


def stop_iperf_client(ue: UEConfig) -> None:
    """停止 UE 容器中所有 iperf3 進程。"""
    subprocess.run(
        ["docker", "exec", ue.container, "pkill", "-f", "iperf3"],
        capture_output=True,
    )
    if ue._iperf_proc:
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
    """套用新的 CQI 與頻寬設定到單一 UE。"""
    changed = False

    if new_cqi is not None and new_cqi != ue.target_cqi:
        ue.target_cqi = new_cqi
        ctrl.set_target_cqi(ue.ue_id, new_cqi)
        changed = True

    if new_bw is not None and abs(new_bw - ue.bandwidth_mbps) > 0.5:
        ue.bandwidth_mbps = new_bw
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
    """
    assert len(configs) == len(ues), "configs 長度必須等於 UE 數量"
    log.info("── 套用場景相位 ──")
    for ue, (cqi, bw) in zip(ues, configs):
        ctrl = ctrls[ue.node_id]
        apply_ue_config(ue, ctrl, new_cqi=cqi, new_bw=bw)


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
    cqi_choices = [1, 2, 4, 6, 8, 10, 12, 15]
    bw_choices = [1.0, 2.0, 5.0, 8.0, 10.0, 15.0, 20.0, 25.0]
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

            time.sleep(phase_duration)
    except KeyboardInterrupt:
        log.info("收到中斷，停止動態場景（共執行 %d 個相位）", phase)
    finally:
        stop_all_iperf(ues)
        for node_id, ctrl in ctrls.items():
            for ue in ues:
                if ue.node_id == node_id:
                    ctrl.reset_channel(ue.ue_id)


def run_calibration(node_id: int) -> None:
    """對指定 Node 的 DU 執行 CQI 校正掃描。"""
    telnet_port = NODE_CONFIG[node_id][0]
    ctrl = ChannelModController("127.0.0.1", telnet_port)

    print(f"\n=== Node {node_id} CQI 校正（telnet port {telnet_port}）===")
    print("請確認至少有一個 UE 連線到此 Node，且正在傳輸流量")
    input("按 Enter 繼續...")

    ctrl.calibrate_cqi(
        ue_id=0,
        loss_values=[0.0, 40.0, 50.0, 55.0, 58.0, 62.0, 66.0, 70.0, 75.0],
        wait_s=5.0,
    )


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
