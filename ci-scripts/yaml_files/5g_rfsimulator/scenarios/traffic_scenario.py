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
  python3 traffic_scenario.py --scenario R --seed 42 --num-phases 20 --phase-duration 30
                                                              # 真實隨機場景，可重現，跑 20 個相位後自動結束
  python3 traffic_scenario.py --calibrate --node 3          # 對 Node 3 執行 CQI 校正

場景設計（每個 Node 各 2 個 UE）：
  A) CQI 差異化  : 等流量 10 Mbps，每 Node 兩 UE 的 CQI 差距大（15 vs 5）
  B) 流量不均    : 等 CQI (~12)，UE 間流量比例為 5:1
  C) 最差公平性  : 高流量 + 差 CQI vs 低流量 + 好 CQI（壓測公平性）
  D) 動態訓練   : 每 60 秒隨機改變流量與 CQI，最大化狀態多樣性（推薦）
  R) 真實隨機   : 面積均勻抽樣 path_loss（模擬真實細胞幾何：邊緣 UE 較多）+ lognormal
                  重尾抽樣頻寬（模擬真實流量需求：多數適中、少數高需求）+ P_IDLE 機率
                  整個相位完全閒置（真停 iperf3，模擬 burst→idle→burst 間歇使用）。
                  支援 --seed 重現同一串隨機條件，供 PF vs DRL 的 paired comparison 使用；
                  --num-phases 可設定跑滿 N 個相位後自動結束（預設不限，Ctrl+C 手動停）。
                  訓練與評估都可使用此場景，不切割（見 DRL_DESIGN.md 討論）。

UE ↔ Node 對應：
  Node 3 (telnet :9091): UE1 (ue_id=0), UE2 (ue_id=1)
  Node 4 (telnet :9092): UE3 (ue_id=0), UE4 (ue_id=1)
  Node 5 (telnet :9093): UE5 (ue_id=0), UE6 (ue_id=1)
"""

from __future__ import annotations

import argparse
import logging
import math
import random
import re
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

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

# ── Scenario R（真實隨機）參數 ──────────────────────────────────────────────
# path_loss：面積均勻分佈的正規化半徑 r=sqrt(U) 映射到 [0, PATHLOSS_SAFE_MAX_DB]，
# 重用校正掃描已驗證安全的上限（超過會斷線）。
PATHLOSS_SAFE_MAX_DB: float = 25.0

# 頻寬：MIN + Lognormal(mu, sigma)，只在上界裁切（lognormal 支撐從 0 開始，下界
# 天然 >= MIN，不會有下界堆積問題）。mu=ln(6) 讓 median(bw) = MIN + 6 Mbps。
#
# 下限修正記錄（2026-07-09）：原本 MIN=25 是沿用 Scenario D 的舊假設「頻寬必須遠
# 超過 DU 在 100ms 窗口能服務的量，確保 buffer 不清空」，但現場實測（單一 UE、最佳
# 通道 path_loss=0dB、目標頻寬 5~200Mbps 全掃過）發現：實測吞吐量天花板卡在
# ~8-12Mbps，不管目標頻寬設多高都一樣（單一 TCP 串流 + RTT 的物理限制，不是 DU
# 排程問題；同時也證實了「超過 50Mbps 會讓 DU 崩潰」的舊說法不成立，測到 200Mbps
# DU 全程穩定）。既然目標頻寬只要略高於這個天花板就足以讓 buffer 持續堆積，MIN=25
# 遠比實際需要的高，反而排除了真實世界常見的低需求情境（視訊通話、輕度瀏覽）。
# 下修至 MIN=5，讓分佈涵蓋「真閒置（P_IDLE）→ 低需求 → 中等需求 → 偶爾高需求」
# 更完整的真實使用光譜。上界 50 維持不變（DU 不會崩潰，但拉高上界只會讓
# buffer 堆積更誇張，沒有讓「實際吞吐量」更極端，且會撞上已校準的 drl_agent.py
# MAX_BUF_INFO 常數，不值得為此重新校準+重訓）。
#
# 現場模擬驗證（20 萬筆抽樣，MIN=5, MAX=50, mu=ln(6), sigma=0.6）：
#   median ≈ 11.0 Mbps，mean ≈ 12.2 Mbps
#   P(5-10)=38.0%  P(10-20)=55.6%  P(20-30)=5.5%  P(30-50)=0.8%  裁切@50=0.04%
REALISTIC_BW_MIN_MBPS: float = 5.0
REALISTIC_BW_MAX_MBPS: float = 50.0
REALISTIC_BW_LOGNORM_MU: float = math.log(6.0)
REALISTIC_BW_LOGNORM_SIGMA: float = 0.6

# 時間維度真實性：每個 UE 每個相位有 P_IDLE 的機率完全閒置（真停 iperf3，
# 讓 RLC buffer 真正歸零），模擬 burst→idle→burst 的間歇性使用型態。
# 0.25：約四分之一時間閒置，貼近真實間歇使用，同時保留足夠訓練信號密度
# （訓練與評估都用這個場景，不切割，見規劃文件的討論）。
P_IDLE: float = 0.25

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
    path_loss_db: Optional[float] = None   # Scenario R 專用：連續 path_loss 真值（非 None 時
                                            # target_cqi 只是反查最近對照表值的 log 顯示標籤）
    is_idle: bool = False                  # Scenario R 專用：True 時 iperf3 真的停止（非低頻寬）
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
    在 UE 容器內執行單次 iperf3 TCP 下行客戶端（-R reverse mode）。

    不使用 while loop：shell loop 在容器內被 SIGKILL 時，其子進程（iperf3）
    被 container PID 1 接管而繼續存活，導致多個 iperf3 並發搶同一 server port。
    改為單次 iperf3，由 Python supervisor（watchdog loop 的 poll()）負責重啟，
    stop = kill docker exec，docker exec 退出時 iperf3 是其直接子進程，一起終止。
    """
    stop_iperf_client(ue)

    ue_ip = _get_ue_ip(ue.container)
    if not ue_ip:
        log.warning("%s: oaitun_ue1 IP 尚未就緒，等下次 watchdog 重試", ue.container)
        return False

    cmd = [
        "docker", "exec", ue.container,
        "timeout", str(IPERF_DURATION + 30),
        "iperf3", "-c", EXT_DN_IP,
        "-b", f"{ue.bandwidth_mbps:.0f}M",
        "-R", "-t", str(IPERF_DURATION),
        "-p", str(ue.iperf_port),
        "-B", ue_ip,
        "--forceflush",
    ]
    try:
        ue._iperf_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        ue._last_rx_bytes = 0
        ue._last_rx_check = time.time()
        log.info("iperf3 start: %s → %s:%d @ %.0fMbps (bind=%s)",
                 ue.container, EXT_DN_IP, ue.iperf_port, ue.bandwidth_mbps, ue_ip)
        return True
    except FileNotFoundError:
        log.error("找不到 docker 指令")
        return False


def stop_iperf_client(ue: UEConfig) -> None:
    """終止 docker exec wrapper 並殺掉容器內的 iperf3 進程。"""
    # Step 1：終止 docker exec wrapper
    if ue._iperf_proc:
        try:
            ue._iperf_proc.kill()
            ue._iperf_proc.wait(timeout=3)
        except Exception:
            pass
        ue._iperf_proc = None

    # Step 2：殺容器內殘留的 iperf3（按 port 精確匹配）
    try:
        subprocess.run(
            ["docker", "exec", ue.container, "pkill", "-9", "-f",
             f"iperf3.*-p {ue.iperf_port}"],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass

    # 等待 server 端重置 TCP 連線後重啟，避免新 client 遇到 "server is busy"
    time.sleep(1.5)


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
    new_path_loss_db: Optional[float] = None,
) -> None:
    """套用新的通道與頻寬設定到單一 UE。

    通道變更：透過 channelmod telnet 即時設定，無需重啟 iperf3。
      - new_cqi：走既有的 8 點對照表吸附路徑（set_target_cqi）。
      - new_path_loss_db：Scenario R 專用，直接連續值送 set_path_loss()，繞過對照表；
        與 new_cqi 互斥（給了 new_path_loss_db 就忽略 new_cqi）。target_cqi 仍會反查
        對照表最近值填入，但那只是給 log 看的粗略標籤，不是分析用的真值——
        真值看 ue.path_loss_db。
    BW 變更：BW 烘焙進 loop_cmd，必須重啟 iperf3 loop 才能套用新值（~0.3s 短暫無流量）。
      - new_bw <= 0.0：閒置 sentinel（Scenario R 專用）。真的呼叫 stop_iperf_client()
        停掉流量，讓 RLC buffer 真正歸零，而不是把頻寬設到接近 0——現有 25 Mbps
        下限本身就是為了避免 buffer 空，真閒置必須完全停止 iperf3。
    """
    changed = False

    if new_path_loss_db is not None:
        if ue.path_loss_db is None or abs(new_path_loss_db - ue.path_loss_db) > 0.05:
            ue.path_loss_db = new_path_loss_db
            ctrl.set_path_loss(ue.ue_id, new_path_loss_db)
            table = ctrl.cqi_to_pathloss
            ue.target_cqi = min(table, key=lambda c: abs(table[c] - new_path_loss_db))
            changed = True
    elif new_cqi is not None and new_cqi != ue.target_cqi:
        ue.target_cqi = new_cqi
        ctrl.set_target_cqi(ue.ue_id, new_cqi)
        changed = True

    if new_bw is not None:
        going_idle = new_bw <= 0.0
        if going_idle:
            if not ue.is_idle:
                stop_iperf_client(ue)
                ue.bandwidth_mbps = 0.0
                ue.is_idle = True
                changed = True
        elif ue.is_idle or abs(new_bw - ue.bandwidth_mbps) > 0.5:
            ue.bandwidth_mbps = new_bw
            ue.is_idle = False
            # BW 烘焙進 loop_cmd，必須重啟 loop 才能套用新值。
            # stop_iperf_client 在 start_iperf_client 內部處理；
            # 短暫 2~3s 的無流量期 DRL 可容忍，各 UE 間有 100ms 錯開。
            start_iperf_client(ue)
            changed = True

    if changed:
        if ue.is_idle:
            if ue.path_loss_db is not None:
                log.info("  %-28s IDLE（無流量）ploss=%.1fdB", ue.container, ue.path_loss_db)
            else:
                log.info("  %-28s IDLE（無流量）", ue.container)
        elif ue.path_loss_db is not None:
            log.info("  %-28s CQI≈%2d (ploss=%.1fdB)  BW=%.1fMbps",
                     ue.container, ue.target_cqi, ue.path_loss_db, ue.bandwidth_mbps)
        else:
            log.info("  %-28s CQI=%2d  BW=%.0fMbps",
                     ue.container, ue.target_cqi, ue.bandwidth_mbps)


def apply_scenario_phase(
    ues: list[UEConfig],
    ctrls: dict[int, ChannelModController],
    configs: list[tuple[float, float]],  # [(target_cqi_or_path_loss_db, bandwidth_mbps), ...]
    raw_path_loss: bool = False,
) -> None:
    """
    套用一個場景相位的設定到所有 UE。

    configs 長度必須為 6，對應 UE1~UE6。
    各 UE 之間插入 100ms 間隔給 channelmod telnet 指令回應。

    raw_path_loss=True 時，configs 的第一個元素視為連續 path_loss_db（Scenario R），
    否則視為 target_cqi（Scenario A/B/C/D 既有行為，預設）。
    """
    assert len(configs) == len(ues), "configs 長度必須等於 UE 數量"
    log.info("── 套用場景相位 ──")
    for ue, (a, bw) in zip(ues, configs):
        ctrl = ctrls[ue.node_id]
        if raw_path_loss:
            apply_ue_config(ue, ctrl, new_path_loss_db=a, new_bw=bw)
        else:
            apply_ue_config(ue, ctrl, new_cqi=int(a), new_bw=bw)
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
        (15, 30.0),  # UE1 @ Node3: 好通道
        (5,  30.0),  # UE2 @ Node3: 差通道
        (15, 30.0),  # UE3 @ Node4: 好通道
        (5,  30.0),  # UE4 @ Node4: 差通道
        (15, 30.0),  # UE5 @ Node5: 好通道
        (5,  30.0),  # UE6 @ Node5: 差通道
    ]


def scenario_b(ues: list[UEConfig], ctrls: dict[int, ChannelModController]) -> list[tuple[int, float]]:
    """
    場景 B：流量不均（Jain's Fairness 壓測）
    目標：訓練 DRL 在相同 CQI 下公平分配 PRB，避免高流量 UE 飢餓低流量 UE。
    """
    return [
        (12, 45.0),  # UE1 @ Node3: 高需求
        (12, 25.0),  # UE2 @ Node3: 低需求（仍確保 bsr > 0）
        (12, 45.0),  # UE3 @ Node4: 高需求
        (12, 25.0),  # UE4 @ Node4: 低需求
        (12, 45.0),  # UE5 @ Node5: 高需求
        (12, 25.0),  # UE6 @ Node5: 低需求
    ]


def scenario_c(ues: list[UEConfig], ctrls: dict[int, ChannelModController]) -> list[tuple[int, float]]:
    """
    場景 C：最惡公平性（高流量 + 差通道 vs 低流量 + 好通道）
    目標：讓 DRL 學習在「頻譜效率低但需求大」的 UE 與「效率高但需求小」的 UE 間取得平衡。
    """
    return [
        (4,  50.0),  # UE1 @ Node3: 差通道高需求 ← 挑戰排程器
        (14, 25.0),  # UE2 @ Node3: 好通道低需求（仍確保 bsr > 0）
        (4,  50.0),  # UE3 @ Node4: 差通道高需求
        (13, 25.0),  # UE4 @ Node4: 好通道低需求
        (5,  50.0),  # UE5 @ Node5: 差通道高需求
        (14, 25.0),  # UE6 @ Node5: 好通道低需求
    ]


def scenario_d_random(ues: list[UEConfig]) -> list[tuple[int, float]]:
    """
    場景 D：隨機相位（每 60 秒呼叫一次，生成新的隨機設定）
    目標：最大化訓練資料多樣性，讓 DRL 學習通用策略。
    """
    # path_loss 表已壓縮至 0~25dB 安全範圍，所有 CQI 值皆可使用
    cqi_choices = list(range(1, 16))   # 1~15 完整覆蓋
    # BW 最低 25 Mbps，最高 50 Mbps。
    # 下限 25 Mbps：確保每個 UE 的 iperf3 需求超過 DU 在 100ms 窗口能服務的量，
    # 使 DL buffer 持續有資料（bsr 恆 > 0），消除 bsr=0 帶來的 idle 噪訊。
    # 上限 50 Mbps：超過此值會讓 rfsim TCP buffer 滿載，導致 DU 排程延遲與 DL 路徑崩潰。
    bw_choices = [float(x) for x in range(25, 55, 5)]
    configs = []
    for _ in ues:
        cqi = random.choice(cqi_choices)
        bw = random.choice(bw_choices)
        configs.append((cqi, bw))
    return configs


def scenario_r_realistic(
    ues: list[UEConfig],
    rng: random.Random,
) -> list[tuple[float, float]]:
    """
    場景 R：真實隨機相位——用有統計依據的分佈取代 D 的均勻隨機。

    通道品質：真實蜂巢網路裡 UE 均勻分布在細胞「面積」上，而非均勻分布在 CQI
    值上——面積隨半徑平方成長，代表更多 UE 落在訊號較差的邊緣區域。抽樣
    r = sqrt(U)，U ~ Uniform(0,1)，使 r 的機率密度 f(r) = 2r（隨半徑線性增加，
    對應環狀面積 ∝ r），再線性映射到 [0, PATHLOSS_SAFE_MAX_DB] dB，直接送
    set_path_loss()（繞過 8 點 CQI 對照表，連續值，通道解析度比 A/B/C/D 都細）。

    流量需求：真實流量需求是重尾分佈（多數使用者需求適中、少數使用者需求很高），
    用 Lognormal(REALISTIC_BW_LOGNORM_MU, REALISTIC_BW_LOGNORM_SIGMA) 疊加在
    REALISTIC_BW_MIN_MBPS 之上，只在上界裁切（詳見常數區塊註解的百分位數字）。

    時間動態：每個 UE 有 P_IDLE 的機率整個相位完全閒置（bw=0.0 sentinel，
    apply_ue_config() 見到會真的停止 iperf3，不是把頻寬設到接近 0），模擬
    burst→idle→burst 的真實間歇使用型態。path_loss 不受閒置影響，仍照常抽樣
    套用——通道條件是物理環境的屬性，跟有沒有資料在傳輸無關。

    rng：呼叫端傳入的 random.Random 實例（非全域 random 模組），供 --seed 重現。
    """
    configs: list[tuple[float, float]] = []
    for _ in ues:
        r = math.sqrt(rng.random())
        path_loss = r * PATHLOSS_SAFE_MAX_DB
        if rng.random() < P_IDLE:
            bw = 0.0
        else:
            bw = REALISTIC_BW_MIN_MBPS + rng.lognormvariate(
                REALISTIC_BW_LOGNORM_MU, REALISTIC_BW_LOGNORM_SIGMA
            )
            bw = min(bw, REALISTIC_BW_MAX_MBPS)
        configs.append((path_loss, bw))
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
    phase_fn: Callable[[list[UEConfig]], list[tuple[float, float]]] = scenario_d_random,
    raw_path_loss: bool = False,
    scenario_label: str = "D",
    max_phases: Optional[int] = None,
) -> None:
    """
    場景 D/R：動態模式，每 phase_duration 秒切換一次相位。

    phase_fn 決定每個相位怎麼抽樣（預設 scenario_d_random；Scenario R 傳入綁定了
    rng 的 scenario_r_realistic 閉包）。raw_path_loss 須與 phase_fn 的輸出格式
    一致（True 時 phase_fn 必須回傳 (path_loss_db, bw) 而非 (cqi, bw)）。

    max_phases 為 None 時持續執行直到 Ctrl+C（既有 D 的行為）；設定時跑滿 N 個
    相位後自動結束，不需人工介入（供 Scenario R 的 paired comparison 使用）。
    """
    log.info("=== 場景 %s 動態訓練開始，相位間隔 %d 秒 ===", scenario_label, phase_duration)
    phase = 0
    try:
        while True:
            phase += 1
            configs = phase_fn(ues)
            log.info("── 相位 %d ──", phase)
            apply_scenario_phase(ues, ctrls, configs, raw_path_loss=raw_path_loss)

            # 確保所有 iperf3 在第一個相位啟動（跳過刻意閒置的 UE，否則會
            # 立刻把 Scenario R 剛停掉的閒置 UE 重新啟動，閒置機制形同虛設）
            for ue in ues:
                if ue._iperf_proc is None and not ue.is_idle:
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

            if max_phases is not None and phase >= max_phases:
                log.info("已達 max_phases=%d，自動結束場景 %s", max_phases, scenario_label)
                break
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
        "--scenario", choices=["A", "B", "C", "D", "R"], default="D",
        help="場景選擇：A=CQI差異, B=流量不均, C=最差公平性, D=動態訓練（預設）, "
             "R=真實隨機（面積均勻 path_loss + lognormal 頻寬，支援 --seed 重現）",
    )
    parser.add_argument(
        "--duration", type=int, default=1800,
        help="固定場景 A/B/C 的執行秒數（預設 1800s = 30min）",
    )
    parser.add_argument(
        "--phase-duration", type=int, default=60,
        help="場景 D/R 每個相位的秒數（預設 60s）",
    )
    parser.add_argument(
        "--seed", type=int, default=None,
        help="場景 R 專用：亂數種子。未指定則自動產生並印在 log 裡，"
             "供之後用同一個種子重現同一串隨機條件（PF vs DRL paired comparison）",
    )
    parser.add_argument(
        "--num-phases", type=int, default=None,
        help="場景 R 專用：跑滿 N 個相位後自動結束（預設不限，Ctrl+C 手動停）",
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
    elif args.scenario == "R":
        seed = args.seed if args.seed is not None else secrets.randbits(32)
        log.info("Scenario R seed=%d（用 --seed %d 可重現同一串隨機條件）", seed, seed)
        rng = random.Random(seed)
        run_dynamic_scenario(
            ues, ctrls,
            phase_duration=args.phase_duration,
            phase_fn=lambda u: scenario_r_realistic(u, rng),
            raw_path_loss=True,
            scenario_label="R",
            max_phases=args.num_phases,
        )
    else:
        run_fixed_scenario(args.scenario, ues, ctrls, duration=args.duration)


if __name__ == "__main__":
    main()
