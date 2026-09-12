"""
traffic_scenario.py — UE 流量場景控制器（三主機 12-node/17-UE 拓樸版）

部署環境：PC1、PC2、PC3 各自執行一份（`--host pc1` / `--host pc2` / `--host pc3`），
只控制該主機本地擁有的 UE 容器與 DU telnet 通道（channelmod port 只在
`127.0.0.1` 監聽，無法跨網路連過去，見 CLAUDE.md 第 1 節網路拓樸）：
  - 控制本地 UE 容器的 iperf3 流量（ext-dn → UE，-R reverse，填滿 gNB DL buffer）
  - 透過 channelmod telnet 改變本地 DU 對 UE 的通道條件（path loss / CQI）
  - 場景提供多樣化的 (path_loss, 頻寬, 協定, 閒置) 組合，模擬真實劣化環境

前置條件：
  1. PC 1 已執行 setup_iperf_servers.sh，ext-dn 中的 iperf3 server 正在監聽（17 個 port）
  2. 三主機基礎設施已啟動（run_local_pc1.sh/pc2.sh/pc3.sh），DU 容器已帶 --telnetsrv
  3. 所有 UE 容器已啟動，oaitun_ue1 介面已取得 12.1.1.x IP

使用方法：
  # PC1 控制 UE5~8（Node2/7/8 子樹），PC2 控制 UE1~4，PC3 控制 UE9~17（含 UE17），
  # 三邊用同一個 --seed 保持場景同步
  python3 traffic_scenario.py --scenario R --seed 42 --host pc1
  python3 traffic_scenario.py --scenario R --seed 42 --host pc2
  python3 traffic_scenario.py --scenario R --seed 42 --host pc3

  python3 traffic_scenario.py --scenario A --duration 1800 --host pc2   # 固定場景 A
  python3 traffic_scenario.py --scenario R --seed 42 --num-phases 20 --phase-duration 30 --host pc2
  python3 traffic_scenario.py --calibrate --node 5 --host pc2          # 對 Node 5 執行 CQI 校正

場景設計：
  A) CQI 差異化  : 等流量，同 Node 內兩 UE 的 CQI 差距大（15 vs 5）
  B) 流量不均    : 等 CQI，UE 間流量比例不對等
  C) 最差公平性  : 高流量 + 差 CQI vs 低流量 + 好 CQI（壓測公平性）
  D) 動態訓練   : 每 60 秒隨機改變流量與 CQI，均勻隨機（無統計依據，已被 R 取代，保留供比較）
  R) 真實隨機   : 面積均勻抽樣 path_loss（模擬真實細胞幾何：邊緣 UE 較多）+ 每 UE 持久化
                  使用者 profile（heavy-streaming / light-browsing / bursty-iot，決定其
                  lognormal 頻寬分佈與閒置機率範圍，而非每個相位重新獨立抽樣）+ TCP/UDP
                  協定混合 + 間歇閒置（真停 iperf3，模擬 burst→idle→burst 使用型態）。
                  支援 --seed 重現同一串隨機條件，且用 (seed, ue_global_id, phase_index)
                  三元組導出每個 UE 每個相位的獨立 RNG——PC1/PC2/PC3 三邊各自執行也能
                  天然保持同步，不需要跨主機即時通訊（phase_index 用絕對時間換算）。

跨主機拓樸（見 CLAUDE.md 第 1 節）：
  relay Node1~4（telnet 9089~9092），access Node5~12（telnet 9093~9100）。
  Node2 + 其 access 子節點 Node7,8 + UE5~8 在 PC1；Node1,3,4 + Node5,6 + UE1~4,17 在
  PC2；Node9~12 + UE9~16 在 PC3（2026-09-12 分兩輪為分擔 CPU 負載搬遷，見
  CLAUDE.md——Node9~12 這 4 個 access 節點沒動，但它們的 parent relay Node3,4 搬到
  PC2 了，變成跨主機連線）。UE17 直接掛在 relay Node4 底下（不經過 access 層，
  channelmod ue_id 依連線順序固定為 2——Node4 的 DU 先接 Node11/Node12 兩個 access
  MT（ue_id 0,1，這兩個 MT 現在是跨主機從 PC3 連過來），UE17 最後連線取得 ue_id 2）。
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import math
import random
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from channelmod_ctrl import ChannelModController

# =============================================================================
# 全域設定
# =============================================================================

EXT_DN_IP = "192.168.72.135"       # ext-dn 在 traffic_net 的 IP
IPERF_DURATION = 300               # iperf3 每次 session 持續時間 (s)；定期循環以刷新連線狀態
IPERF_BIND_IF = "oaitun_ue1"      # UE PDN 介面名稱
FLOW_WATCHDOG_INTERVAL = 30        # 每 30s 檢查一次 DL flow 是否凍結（僅 TCP 適用，UDP 無 rx_bytes 累積保證）

# iperf3 server port 分配（PC 1 setup_iperf_servers.sh 必須一致）：UE1~17 → 5201~5217
UE_IPERF_PORTS: dict[str, int] = {
    f"rfsim5g-end-ue-{i}": 5200 + i for i in range(1, 18)
}

# Node → (DU telnet port, [(ue_container, ue_id), ...])
# telnet port 公式：relay/access 統一 9088 + node_id（見 CLAUDE.md IP/ID 配置表）。
# relay Node1~3 沒有直連 UE（子節點都是 access node，走各自 access 的 telnet port
# 控制），故 UE 清單為空；relay Node4 額外多一個直連 UE17。
NODE_CONFIG: dict[int, tuple[int, list[tuple[str, int]]]] = {
    1:  (9089, []),
    2:  (9090, []),
    3:  (9091, []),
    4:  (9092, [("rfsim5g-end-ue-17", 2)]),   # ue_id=2：Node11/12 的 MT 先連線取走 0,1
    5:  (9093, [("rfsim5g-end-ue-1", 0), ("rfsim5g-end-ue-2", 1)]),
    6:  (9094, [("rfsim5g-end-ue-3", 0), ("rfsim5g-end-ue-4", 1)]),
    7:  (9095, [("rfsim5g-end-ue-5", 0), ("rfsim5g-end-ue-6", 1)]),
    8:  (9096, [("rfsim5g-end-ue-7", 0), ("rfsim5g-end-ue-8", 1)]),
    9:  (9097, [("rfsim5g-end-ue-9", 0), ("rfsim5g-end-ue-10", 1)]),
    10: (9098, [("rfsim5g-end-ue-11", 0), ("rfsim5g-end-ue-12", 1)]),
    11: (9099, [("rfsim5g-end-ue-13", 0), ("rfsim5g-end-ue-14", 1)]),
    12: (9100, [("rfsim5g-end-ue-15", 0), ("rfsim5g-end-ue-16", 1)]),
}

# Node → DU 容器名稱（校正時用來讀取 nrMAC_stats.log；relay/access 命名一致）
NODE_TO_DU_CONTAINER: dict[int, str] = {n: f"rfsim5g-iab-du-{n}" for n in range(1, 13)}

# Node → 所屬主機（決定 --host 篩選哪些節點；telnet chanmod port 只在該節點
# DU 容器實際運作的那台主機的 127.0.0.1 監聽，所以這裡要填「DU 實際跑在哪」，
# 不是「它的 access 子節點在哪」——Node3,4 的 DU 搬到 pc2 後，即使 Node9~12
# 這些 access 子節點還留在 pc3，Node3,4 這兩列也要跟著改成 pc2）。
# 2026-09-12 兩輪搬遷（見 CLAUDE.md）：
#   1) Node2 + 其 access 子節點 Node7,8 從 pc2 搬到 pc1。
#   2) Node3,4（relay）從 pc3 搬到 pc2；Node9~12（access）留在 pc3 不動。
HOST_OF_NODE: dict[int, str] = {
    2: "pc1", 7: "pc1", 8: "pc1",
    1: "pc2", 3: "pc2", 4: "pc2", 5: "pc2", 6: "pc2",
    9: "pc3", 10: "pc3", 11: "pc3", 12: "pc3",
}

# 校正掃描的 path_loss 值（單位 dB）；上限 25dB，超過會斷線
CALIBRATE_LOSS_VALUES: list[float] = [0.0, 5.0, 10.0, 14.0, 18.0, 21.0, 23.0, 25.0]

# _DEFAULT_CQI_TO_PATHLOSS 的標準 CQI 鍵值集合
_STANDARD_CQIS: list[int] = [15, 12, 10, 8, 6, 4, 2, 1]

# ── Scenario R（真實隨機）參數 ──────────────────────────────────────────────
# path_loss：面積均勻分佈的正規化半徑 r=sqrt(U) 映射到 [0, PATHLOSS_SAFE_MAX_DB]，
# 重用校正掃描已驗證安全的上限（超過會斷線）。
PATHLOSS_SAFE_MAX_DB: float = 25.0

# 每 UE 持久化使用者 profile：開場抽一次、整個執行期間不變，比「每個 phase 完全
# 獨立同分布抽樣」更貼近真實世界「同一個用戶的行為模式有慣性」。三種原型：
#   heavy    ：重度串流／下載，頻寬需求高、很少閒置
#   light    ：輕度瀏覽，頻寬需求低、常常閒置
#   bursty   ：間歇性高峰（視訊通話／遊戲），頻寬變化大、閒置機率中等偏高
# lognorm_mu/sigma 疊加在 min_mbps 之上（只在上界裁切），p_idle 為該 profile 的
# 閒置機率。權重決定抽到各 profile 的機率（加總為 1，不必嚴格相等，貼近真實
# 世界「重度用戶是少數」的分布）。
UE_PROFILES: dict[str, dict[str, float]] = {
    "heavy":  {"weight": 0.25, "min_mbps": 15.0, "max_mbps": 80.0,
               "lognorm_mu": math.log(20.0), "lognorm_sigma": 0.5, "p_idle": 0.08},
    "light":  {"weight": 0.45, "min_mbps": 2.0,  "max_mbps": 20.0,
               "lognorm_mu": math.log(3.0),  "lognorm_sigma": 0.5, "p_idle": 0.45},
    "bursty": {"weight": 0.30, "min_mbps": 5.0,  "max_mbps": 60.0,
               "lognorm_mu": math.log(8.0),  "lognorm_sigma": 0.9, "p_idle": 0.30},
}

# TCP/UDP 協定混合：大部分真實流量仍是 TCP（網頁/檔案/串流走 adaptive bitrate
# over TCP），少數是 UDP（即時語音/視訊通話、遊戲）。UDP 走固定目標頻寬（iperf3
# -u -b），不像 TCP 受壅塞控制影響，兩者混合更貼近真實流量組成、也讓 DRL 學習
# 對不同壅塞行為的流量做資源分配。
P_UDP: float = 0.25

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
    global_id: int = 0                     # 跨主機唯一序號（UE1~17 對應 1~17），供
                                            # (seed, global_id, phase_index) 決定式抽樣用
    profile: str = "light"                 # Scenario R 專用：持久化使用者 profile
    target_cqi: int = 12
    bandwidth_mbps: float = 10.0
    protocol: str = "tcp"                  # "tcp" 或 "udp"
    path_loss_db: Optional[float] = None   # Scenario R 專用：連續 path_loss 真值（非 None 時
                                            # target_cqi 只是反查最近對照表值的 log 顯示標籤）
    is_idle: bool = False                  # Scenario R 專用：True 時 iperf3 真的停止（非低頻寬）
    _iperf_proc: Optional[subprocess.Popen] = field(default=None, repr=False)
    _iperf_log: Optional[Any] = field(default=None, repr=False)   # iperf3 client stdout 檔案控制代碼
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
    在 UE 容器內執行單次 iperf3 下行客戶端（-R reverse mode），TCP 或 UDP。

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
    if ue.protocol == "udp":
        cmd.insert(cmd.index("-R"), "-u")
    try:
        # iperf3 client 的即時輸出（每秒一筆進度 + 結尾摘要）寫進固定路徑的
        # log 檔（覆寫模式，每次重啟這個 UE 的 session 就重新開始），供外部
        # 量測取樣腳本（見 iab/measure_stage.py）讀取「實際達成吞吐量」，
        # 而不是這裡設定的目標頻寬 ue.bandwidth_mbps。這是唯一的職責擴充，
        # traffic_scenario.py 本身不做任何量測/記錄邏輯，維持給全部 5 個
        # stage 共用的環境產生器角色不變。
        ue._iperf_log = open(f"/tmp/iperf_client_{ue.container}.log", "w")
        ue._iperf_proc = subprocess.Popen(
            cmd,
            stdout=ue._iperf_log,
            stderr=subprocess.DEVNULL,
        )
        ue._last_rx_bytes = 0
        ue._last_rx_check = time.time()
        log.info("iperf3 start: %s → %s:%d @ %.0fMbps/%s (bind=%s)",
                 ue.container, EXT_DN_IP, ue.iperf_port, ue.bandwidth_mbps,
                 ue.protocol.upper(), ue_ip)
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
    if ue._iperf_log:
        try:
            ue._iperf_log.close()
        except Exception:
            pass
        ue._iperf_log = None

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
    new_protocol: Optional[str] = None,
) -> None:
    """套用新的通道、頻寬與協定設定到單一 UE。

    通道變更：透過 channelmod telnet 即時設定，無需重啟 iperf3。
      - new_cqi：走既有的 8 點對照表吸附路徑（set_target_cqi）。
      - new_path_loss_db：Scenario R 專用，直接連續值送 set_path_loss()，繞過對照表；
        與 new_cqi 互斥（給了 new_path_loss_db 就忽略 new_cqi）。target_cqi 仍會反查
        對照表最近值填入，但那只是給 log 看的粗略標籤，不是分析用的真值——
        真值看 ue.path_loss_db。
    BW/協定變更：兩者都烘焙進 iperf3 指令，必須重啟 iperf3 loop 才能套用新值
      （~0.3s 短暫無流量）。
      - new_bw <= 0.0：閒置 sentinel（Scenario R 專用）。真的呼叫 stop_iperf_client()
        停掉流量，讓 RLC buffer 真正歸零，而不是把頻寬設到接近 0。
      - new_protocol 改變時（即使 BW 沒變）也要重啟 iperf3，因為 TCP/UDP 是不同的
        iperf3 invocation。
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

    protocol_changed = new_protocol is not None and new_protocol != ue.protocol
    if new_bw is not None:
        going_idle = new_bw <= 0.0
        if going_idle:
            if not ue.is_idle:
                stop_iperf_client(ue)
                ue.bandwidth_mbps = 0.0
                ue.is_idle = True
                changed = True
        elif ue.is_idle or abs(new_bw - ue.bandwidth_mbps) > 0.5 or protocol_changed:
            ue.bandwidth_mbps = new_bw
            ue.is_idle = False
            if new_protocol is not None:
                ue.protocol = new_protocol
            # BW/協定烘焙進 iperf3 指令，必須重啟 loop 才能套用新值。
            # stop_iperf_client 在 start_iperf_client 內部處理；
            # 短暫 2~3s 的無流量期 DRL 可容忍，各 UE 間有 100ms 錯開。
            start_iperf_client(ue)
            changed = True
    elif protocol_changed and not ue.is_idle:
        ue.protocol = new_protocol
        start_iperf_client(ue)
        changed = True

    if changed:
        if ue.is_idle:
            if ue.path_loss_db is not None:
                log.info("  %-28s IDLE（無流量）ploss=%.1fdB", ue.container, ue.path_loss_db)
            else:
                log.info("  %-28s IDLE（無流量）", ue.container)
        elif ue.path_loss_db is not None:
            log.info("  %-28s [%s] CQI≈%2d (ploss=%.1fdB)  BW=%.1fMbps  proto=%s",
                     ue.container, ue.profile, ue.target_cqi, ue.path_loss_db,
                     ue.bandwidth_mbps, ue.protocol.upper())
        else:
            log.info("  %-28s CQI=%2d  BW=%.0fMbps  proto=%s",
                     ue.container, ue.target_cqi, ue.bandwidth_mbps, ue.protocol.upper())


def apply_scenario_phase(
    ues: list[UEConfig],
    ctrls: dict[int, ChannelModController],
    configs: list[tuple],
    raw_path_loss: bool = False,
) -> None:
    """
    套用一個場景相位的設定到所有 UE（僅限本主機負責的 UE 子集）。

    configs 長度必須等於 ues 長度。元素為 (target_cqi_or_path_loss_db, bandwidth_mbps)
    或 (target_cqi_or_path_loss_db, bandwidth_mbps, protocol) 三元組（Scenario R 用）。
    各 UE 之間插入 100ms 間隔給 channelmod telnet 指令回應。

    raw_path_loss=True 時，configs 的第一個元素視為連續 path_loss_db（Scenario R），
    否則視為 target_cqi（Scenario A/B/C/D 既有行為，預設）。
    """
    assert len(configs) == len(ues), "configs 長度必須等於 UE 數量"
    log.info("── 套用場景相位 ──")
    for ue, cfg in zip(ues, configs):
        a, bw = cfg[0], cfg[1]
        proto = cfg[2] if len(cfg) > 2 else None
        ctrl = ctrls[ue.node_id]
        if raw_path_loss:
            apply_ue_config(ue, ctrl, new_path_loss_db=a, new_bw=bw, new_protocol=proto)
        else:
            apply_ue_config(ue, ctrl, new_cqi=int(a), new_bw=bw, new_protocol=proto)
        time.sleep(0.1)


# =============================================================================
# 預定義場景（A/B/C：對每個 access node 的頭兩個 UE 施加對比條件；
#             泛化到 12 節點，UE 清單長度隨 build_ue_list() 而定）
# =============================================================================

def scenario_a(ues: list[UEConfig], ctrls: dict[int, ChannelModController]) -> list[tuple[int, float]]:
    """場景 A：CQI 差異化——同 Node 內兩 UE 對比 CQI 15 vs 5，流量相等。"""
    configs = []
    for i, _ in enumerate(ues):
        configs.append((15, 30.0) if i % 2 == 0 else (5, 30.0))
    return configs


def scenario_b(ues: list[UEConfig], ctrls: dict[int, ChannelModController]) -> list[tuple[int, float]]:
    """場景 B：流量不均（Jain's Fairness 壓測）——等 CQI，流量比例不對等。"""
    configs = []
    for i, _ in enumerate(ues):
        configs.append((12, 45.0) if i % 2 == 0 else (12, 25.0))
    return configs


def scenario_c(ues: list[UEConfig], ctrls: dict[int, ChannelModController]) -> list[tuple[int, float]]:
    """場景 C：最惡公平性——差通道高需求 vs 好通道低需求。"""
    configs = []
    for i, _ in enumerate(ues):
        configs.append((4, 50.0) if i % 2 == 0 else (14, 25.0))
    return configs


def scenario_d_random(ues: list[UEConfig]) -> list[tuple[int, float]]:
    """場景 D：均勻隨機相位（無統計依據，已被 Scenario R 取代，保留供比較）。"""
    cqi_choices = list(range(1, 16))
    bw_choices = [float(x) for x in range(25, 55, 5)]
    configs = []
    for _ in ues:
        cqi = random.choice(cqi_choices)
        bw = random.choice(bw_choices)
        configs.append((cqi, bw))
    return configs


def _rng_for(seed: int, global_id: int, phase_index: int) -> random.Random:
    """
    決定式導出 (seed, global_id, phase_index) 專屬的 RNG 實例。

    不用單一共用序列依序消耗（那需要所有 UE 在同一個 process 裡按固定順序抽樣），
    改用雜湊導出獨立種子——任一台主機、只知道自己負責的 UE 子集，也能算出跟另一
    台主機完全一致的抽樣結果，不需要跨主機通訊或啟動時間精準對齊。
    """
    h = hashlib.sha256(f"{seed}:{global_id}:{phase_index}".encode()).digest()
    return random.Random(int.from_bytes(h[:8], "big"))


def scenario_r_realistic(
    ues: list[UEConfig],
    seed: int,
    phase_index: int,
) -> list[tuple[float, float, str]]:
    """
    場景 R：真實隨機相位——用有統計依據的分佈 + 持久化使用者 profile 取代 D 的均勻隨機。

    通道品質：真實蜂巢網路裡 UE 均勻分布在細胞「面積」上，而非均勻分布在 CQI
    值上——面積隨半徑平方成長，代表更多 UE 落在訊號較差的邊緣區域。抽樣
    r = sqrt(U)，U ~ Uniform(0,1)，使 r 的機率密度 f(r) = 2r（隨半徑線性增加，
    對應環狀面積 ∝ r），再線性映射到 [0, PATHLOSS_SAFE_MAX_DB] dB，直接送
    set_path_loss()（繞過 8 點 CQI 對照表，連續值，通道解析度比 A/B/C/D 都細）。

    流量需求：每個 UE 開場已抽定一個持久化 profile（見 UE_PROFILES），本函式依
    該 UE 的 profile 參數做 Lognormal 抽樣，而不是全體 UE 共用同一組參數——不同
    profile 的 UE 呈現出明顯不同的流量特徵，比「所有 UE 同分布」更貼近真實世界
    「不同用戶行為模式不同」的異質性。

    協定：每個 UE 每個相位額外抽一個協定（TCP/UDP 混合，見 P_UDP），modeling
    真實流量裡少數即時語音/視訊/遊戲走 UDP、多數網頁/串流走 TCP 的組成。

    時間動態：每個 UE 有其 profile 對應的 p_idle 機率整個相位完全閒置（bw=0.0
    sentinel，apply_ue_config() 見到會真的停止 iperf3，不是把頻寬設到接近 0），
    模擬 burst→idle→burst 的真實間歇使用型態。path_loss 不受閒置影響，仍照常
    抽樣套用——通道條件是物理環境的屬性，跟有沒有資料在傳輸無關。

    seed/phase_index：每個 UE 用 `_rng_for(seed, ue.global_id, phase_index)` 導出
    獨立 RNG，任一台主機都能只算自己負責的 UE、不需要知道其他 UE 被抽到什麼。
    """
    configs: list[tuple[float, float, str]] = []
    for ue in ues:
        rng = _rng_for(seed, ue.global_id, phase_index)
        prof = UE_PROFILES[ue.profile]

        r = math.sqrt(rng.random())
        path_loss = r * PATHLOSS_SAFE_MAX_DB

        if rng.random() < prof["p_idle"]:
            bw = 0.0
            proto = ue.protocol  # 閒置時協定無意義，維持原值即可
        else:
            bw = prof["min_mbps"] + rng.lognormvariate(prof["lognorm_mu"], prof["lognorm_sigma"])
            bw = min(bw, prof["max_mbps"])
            proto = "udp" if rng.random() < P_UDP else "tcp"

        configs.append((path_loss, bw, proto))
    return configs


def assign_ue_profiles(ues: list[UEConfig], seed: int) -> None:
    """開場一次性抽定每個 UE 的持久化 profile（決定式，依 seed 導出，跨主機一致）。"""
    names = list(UE_PROFILES.keys())
    weights = [UE_PROFILES[n]["weight"] for n in names]
    for ue in ues:
        rng = _rng_for(seed, ue.global_id, phase_index=-1)   # phase_index=-1 保留給 profile 抽樣
        ue.profile = rng.choices(names, weights=weights, k=1)[0]
        log.info("  %-28s profile=%s", ue.container, ue.profile)


# =============================================================================
# 主控邏輯
# =============================================================================

def _my_hostname_role() -> Optional[str]:
    """從 hostname 猜測 --host 值（PC2/PC3 的實際 hostname，供未指定 --host 時 fallback）。"""
    hn = socket.gethostname().lower()
    if "mcalab" in hn:
        return "pc2"
    return None  # PC3 的 hostname (lindor-hsieh) 跟 PC1 (lindor-ubuntu) 都含 "lindor"，不做猜測


def build_ue_list(host: Optional[str] = None) -> list[UEConfig]:
    """建立 UE 清單。host 指定時只回傳該主機負責的 UE 子集（見 HOST_OF_NODE）。"""
    ues = []
    for node_id, (_, ue_list) in NODE_CONFIG.items():
        if host is not None and HOST_OF_NODE.get(node_id) != host:
            continue
        for container, ue_id in ue_list:
            global_id = int(container.rsplit("-", 1)[-1])  # "rfsim5g-end-ue-17" → 17
            ues.append(UEConfig(container=container, ue_id=ue_id, node_id=node_id, global_id=global_id))
    return ues


def build_controllers(host: Optional[str] = None) -> dict[int, ChannelModController]:
    """建立 channelmod controller。host 指定時只回傳該主機負責的節點子集。"""
    return {
        node_id: ChannelModController("127.0.0.1", telnet_port)
        for node_id, (telnet_port, _) in NODE_CONFIG.items()
        if host is None or HOST_OF_NODE.get(node_id) == host
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
    phase_fn: Callable[[list[UEConfig]], list[tuple]] = scenario_d_random,
    raw_path_loss: bool = False,
    scenario_label: str = "D",
    max_phases: Optional[int] = None,
    epoch: float = 0.0,
) -> None:
    """
    場景 D/R：動態模式，每 phase_duration 秒切換一次相位。

    phase_fn 決定每個相位怎麼抽樣（預設 scenario_d_random；Scenario R 傳入綁定了
    seed 的 scenario_r_realistic 閉包）。raw_path_loss 須與 phase_fn 的輸出格式
    一致（True 時 phase_fn 必須回傳 (path_loss_db, bw[, protocol]) 而非 (cqi, bw)）。

    epoch：Scenario R 專用，phase_index 用 `int((time.time() - epoch) // phase_duration)`
    絕對時間換算（不是「跑了幾輪迴圈」），讓 PC2/PC3 兩邊各自的 process 只要系統
    時鐘沒有嚴重飄移，就會自動落在同一個 phase_index、抽出一致的隨機值，不需要
    手動同步啟動時刻或跨主機通訊。

    max_phases 為 None 時持續執行直到 Ctrl+C（既有 D 的行為）；設定時跑滿 N 個
    相位後自動結束，不需人工介入（供 Scenario R 的 paired comparison 使用）。
    """
    log.info("=== 場景 %s 動態訓練開始，相位間隔 %d 秒 ===", scenario_label, phase_duration)
    phase = 0
    try:
        while True:
            phase += 1
            if epoch:
                phase_index = int((time.time() - epoch) // phase_duration)
                configs = phase_fn(ues, phase_index)
            else:
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
            #      （while loop 在容器內自行 sleep 2 後重啟新 session；UDP 沒有壅塞
            #      控制回退問題，但仍可能因 DU 端無資料可送而長期無 rx，watchdog
            #      同樣適用）
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
      4. 寫回 channelmod_ctrl.py（RF 參數/config 對全部 12 個節點都相同，任一節點
         校正一次即可代表全部，預設由執行校正的那個節點負責寫檔）
    """
    telnet_port = NODE_CONFIG[node_id][0]
    du_container = NODE_TO_DU_CONTAINER[node_id]
    ctrl = ChannelModController("127.0.0.1", telnet_port)

    print(f"\n=== Node {node_id} 全自動 CQI 校正 ===")
    print(f"  telnet port : {telnet_port}")
    print(f"  DU 容器     : {du_container}")
    print(f"  掃描範圍    : {CALIBRATE_LOSS_VALUES[0]}~{CALIBRATE_LOSS_VALUES[-1]} dB")
    print("請確認至少有一個 UE/MT 連線到此 Node（channelmod ue_id=0 對應第一個連線）")
    input("按 Enter 開始（約需 40 秒）...")

    observed = ctrl.calibrate_auto(
        ue_id=0,
        du_container=du_container,
        loss_values=CALIBRATE_LOSS_VALUES,
        wait_s=5.0,
    )

    if not observed:
        print("\n[校正失敗] 未讀取到任何 CQI 值。")
        print("  可能原因：DU 容器未運行、nrMAC_stats.log 尚未生成、UE/MT 未連線。")
        return

    mapping = _build_full_cqi_mapping(observed)

    print(f"\n校正結果（標準 CQI → path_loss）：")
    for cqi in sorted(mapping.keys(), reverse=True):
        print(f"  CQI {cqi:2d}  →  {mapping[cqi]:.1f} dB")

    # 立即生效（更新 class 變數）
    ChannelModController.cqi_to_pathloss.update(mapping)
    _write_cqi_pathloss_table(mapping)
    print("\n[完成] channelmod_ctrl.py _DEFAULT_CQI_TO_PATHLOSS 已自動更新。")
    print("       下次啟動不需重新校正（除非修改硬體/參數）。")


# =============================================================================
# 入口
# =============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="IAB 流量+路徑損耗場景控制器（三主機 12-node/17-UE 版）"
    )
    parser.add_argument(
        "--host", choices=["pc1", "pc2", "pc3"], default=None,
        help="只控制該主機負責的 UE/Node 子集（見 CLAUDE.md HOST_OF_NODE）。"
             "未指定時嘗試從 hostname 猜測，猜不出來則控制全部 17 個 UE（單機測試用）。",
    )
    parser.add_argument(
        "--scenario", choices=["A", "B", "C", "D", "R"], default="R",
        help="場景選擇：A=CQI差異, B=流量不均, C=最差公平性, D=均勻隨機（已被R取代）, "
             "R=真實隨機（預設，area-uniform path_loss + 持久化 profile + 協定混合）",
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
             "供之後用同一個種子重現同一串隨機條件（PF vs DRL paired comparison，"
             "PC1/PC2/PC3 三邊務必用同一個 --seed 才會產生一致的場景）",
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
        "--node", type=int, choices=list(range(1, 13)), default=5,
        help="校正模式：指定要校正的 Node（1~12，任一節點皆可，RF 參數對全部節點相同）",
    )
    parser.add_argument(
        "--no-wait", action="store_true",
        help="跳過等待 UE 介面就緒的步驟",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    host = args.host or _my_hostname_role()
    if host:
        log.info("--host=%s：只控制該主機負責的 UE/Node 子集", host)
    else:
        log.warning("未指定 --host 且無法從 hostname 猜測，將控制全部 17 個 UE（單機測試用）")

    if args.calibrate:
        run_calibration(args.node)
        return

    ues = build_ue_list(host)
    ctrls = build_controllers(host)
    if not ues:
        log.error("此 --host=%s 沒有任何 UE，請確認 HOST_OF_NODE/NODE_CONFIG 設定", host)
        sys.exit(1)

    if not args.no_wait:
        wait_for_ue_interfaces(ues)

    if args.scenario == "D":
        run_dynamic_scenario(ues, ctrls, phase_duration=args.phase_duration)
    elif args.scenario == "R":
        import secrets
        seed = args.seed if args.seed is not None else secrets.randbits(32)
        log.info("Scenario R seed=%d（PC1/PC2/PC3 三邊用同一個 --seed %d 才會場景一致）", seed, seed)
        assign_ue_profiles(ues, seed)
        # 固定 epoch：兩台主機各自的 process 只要系統時鐘沒有嚴重飄移就會落在同一個
        # phase_index，不需要任何跨主機通訊或啟動時刻同步。
        FIXED_EPOCH = 1700000000.0  # 2023-11-14，純粹當作絕對時間的錨點，無特殊意義
        run_dynamic_scenario(
            ues, ctrls,
            phase_duration=args.phase_duration,
            phase_fn=lambda u, phase_index: scenario_r_realistic(u, seed, phase_index),
            raw_path_loss=True,
            scenario_label="R",
            max_phases=args.num_phases,
            epoch=FIXED_EPOCH,
        )
    else:
        run_fixed_scenario(args.scenario, ues, ctrls, duration=args.duration)


if __name__ == "__main__":
    main()
