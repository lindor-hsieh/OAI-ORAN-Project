"""
traffic_scenario.py — UE 流量場景控制器（三主機 12-node/16-UE 拓樸版；UE17 已於 2026-09-30 移除）

部署環境：PC1、PC2、PC3 各自執行一份（`--host pc1` / `--host pc2` / `--host pc3`），
只控制該主機本地擁有的 UE 容器與 DU telnet 通道（channelmod port 只在
`127.0.0.1` 監聽，無法跨網路連過去，見 CLAUDE.md 第 1 節網路拓樸）：
  - 控制本地 UE 容器的 iperf3 流量（ext-dn → UE，-R reverse，填滿 gNB DL buffer）
  - 透過 channelmod telnet 改變本地 DU 對 UE 的通道條件（path loss / CQI）
  - 場景提供多樣化的 (path_loss, 頻寬, 協定, 閒置) 組合，模擬真實劣化環境

前置條件：
  1. PC 1 已執行 setup_iperf_servers.sh，ext-dn 中的 iperf3 server 正在監聽（16 個 port）
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
import os
import random
import re
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from channelmod_ctrl import ChannelModController, UEChannelController

# =============================================================================
# 全域設定
# =============================================================================

EXT_DN_IP = "192.168.72.135"       # ext-dn 在 traffic_net 的 IP
IPERF_DURATION = 300               # iperf3 每次 session 持續時間 (s)；定期循環以刷新連線狀態
IPERF_BIND_IF = "oaitun_ue1"      # UE PDN 介面名稱
FLOW_WATCHDOG_INTERVAL = 30        # 每 30s 檢查一次 DL flow 是否凍結（僅 TCP 適用，UDP 無 rx_bytes 累積保證）

# iperf3 server port 分配（PC 1 setup_iperf_servers.sh 必須一致）：UE1~24 → 5201~5224
# UE1~16 掛在 access 節點；UE17~24 是 relay 直連 UE（2026-10-01 起，見 RELAY_UE_IDS）。
UE_IPERF_PORTS: dict[str, int] = {
    f"rfsim5g-end-ue-{i}": 5200 + i for i in range(1, 25)
}

# Node → (DU telnet port, [(ue_container, ue_id), ...])
# telnet port 公式：relay/access 統一 9088 + node_id（見 CLAUDE.md IP/ID 配置表）。
# relay Node r 直連 2 個 UE：UE(15+2r)、UE(16+2r)（2026-10-01 起，容器在 PC1）。ue_id 是 relay DU 上的 rfsim 連線
# 順序：兩個 access MT 先連（0,1），relay UE 由 iab/start_relay_ues.sh 在 13/13 E2 之後才啟動（2,3）。
# ue_id 只用於 DU 端 chanmod（上行，場景結束時 reset）；下行惡化走 UE 端 chanmod（set_ue_dl_degradation）。
NODE_CONFIG: dict[int, tuple[int, list[tuple[str, int]]]] = {
    1:  (9089, [("rfsim5g-end-ue-17", 2), ("rfsim5g-end-ue-18", 3)]),
    2:  (9090, [("rfsim5g-end-ue-19", 2), ("rfsim5g-end-ue-20", 3)]),
    3:  (9091, [("rfsim5g-end-ue-21", 2), ("rfsim5g-end-ue-22", 3)]),
    4:  (9092, [("rfsim5g-end-ue-23", 2), ("rfsim5g-end-ue-24", 3)]),
    # [UE17 已移除 2026-09-30] 原本：4: (9092, [("rfsim5g-end-ue-17", 2)]),   # ue_id=2：Node11/12 的 MT 先連線取走 0,1
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
# 不是「它的 access 子節點在哪」）。
# 2026-09-22 節點重分配（見 CLAUDE.md 第 1 節）：全部 4 個 relay（Node1~4）
# 集中到 pc1（跟 Donor 同機，消除同主機分支吞吐量偏高的量測 confound），
# access 節點（Node5~12）平均分散到 pc2（Node5,6,7,8）/pc3（Node9~12）。
HOST_OF_NODE: dict[int, str] = {
    1: "pc1", 2: "pc1", 3: "pc1", 4: "pc1",
    5: "pc2", 6: "pc2", 7: "pc2", 8: "pc2",
    9: "pc3", 10: "pc3", 11: "pc3", 12: "pc3",
}

# UE → 所屬主機的覆寫字典，只用來處理「邏輯歸屬 Node ≠ 容器實際主機」的特例。
# UE17 邏輯上仍掛在 NODE_CONFIG[4]，但容器實際跑在 pc3（2026-09-22 起，見
# CLAUDE.md），跟 HOST_OF_NODE[4]="pc1" 不一致，需要單獨覆寫（影響
# build_ue_list()：UE17 的 iperf/ping 等 docker exec 類操作要在 pc3 執行，
# 因為容器只存在於 pc3 的 docker daemon）；其餘 16 個 UE 沒有這個特例。
# [UE17 已移除 2026-09-30] 唯一的特例就是 UE17，字典留空（機制保留，之後有同類特例可以再用）。
UE_HOST_OVERRIDE: dict[str, str] = {
    # "rfsim5g-end-ue-17": "pc3",
}

# Node → 跨主機 chanmod telnet 位址覆寫。UE17 的 traffic control 現在在 pc3
# 執行（見上），但它的 pathloss channelmod 仍必須透過 Node4 的 DU telnetsrv
# 下達，而 Node4 的 DU 實際跑在 pc1（HOST_OF_NODE[4]="pc1"）——telnet port
# 原本只綁 127.0.0.1；容器接在 macvlan 網路，compose 的 ports: 映射無效，
# 但 telnetsrv 監聽 0.0.0.0，直接連 Node4 容器自己的 macvlan IP（.153）即可跨主機。
# 只有 Node4 需要這個覆寫；其餘節點的 telnet 永遠跟自己的 DU 同機，用
# 127.0.0.1 即可，不需要出現在這個字典裡。
# [UE17 已移除 2026-09-30] 這個覆寫只為了讓 pc3 控制 UE17 的 pathloss，字典留空。
NODE_TELNET_HOST_OVERRIDE: dict[int, str] = {
    # 4: "192.168.88.153",
}

# 校正掃描的 path_loss 值（單位 dB）；上限 25dB，超過會斷線
CALIBRATE_LOSS_VALUES: list[float] = [0.0, 5.0, 10.0, 14.0, 18.0, 21.0, 23.0, 25.0]

# _DEFAULT_CQI_TO_PATHLOSS 的標準 CQI 鍵值集合
_STANDARD_CQIS: list[int] = [15, 12, 10, 8, 6, 4, 2, 1]

# ── Scenario R（真實隨機）參數 ──────────────────────────────────────────────
# path_loss：面積均勻分佈的正規化半徑 r=sqrt(U) 映射到 [0, PATHLOSS_SAFE_MAX_DB]，
# 重用校正掃描已驗證安全的上限（超過會斷線）。
PATHLOSS_SAFE_MAX_DB: float = 25.0

# ── 模擬速度 S 與「模擬時間 Mbps」（2026-09-26）──────────────────────────────
# rfsim 速度調節器讓模擬時間 = 牆鐘時間 × S（見 CLAUDE.md 第 5、8 節；S 存在各主機 build 目錄的 rfsim_speed.txt）。
# iperf3 跑在牆鐘時間，因此**場景裡的所有頻寬（Scenario T 的各狀態流量檔位、A/B/C/D/R 的 BW）一律代表「模擬時間 Mbps」**，
# 啟動 iperf3 時才乘上 S 換成牆鐘 Mbps（wall = sim × S）；這樣場景設定與 S 無關，換 S 不必改檔位。
# 量到的牆鐘吞吐量要除以 S 才是模擬時間吞吐量。SCENARIO_SPEED_S 環境變數可覆寫（測試用）。
RFSIM_SPEED_FILE = Path("/home/lindor/openairinterface5g/cmake_targets/ran_build/build/rfsim_speed.txt")
_SIM_SPEED: Optional[float] = None


def sim_speed() -> float:
    """目前的 rfsim 速度比例 S（0<S≤1；檔案不存在或無效視為 1.0＝不調節）。啟動時讀一次並快取。"""
    global _SIM_SPEED
    if _SIM_SPEED is None:
        v = os.environ.get("SCENARIO_SPEED_S")
        try:
            val = float(v) if v else float(RFSIM_SPEED_FILE.read_text().split()[0])
        except (OSError, ValueError, IndexError):
            val = 0.0
        if val <= 0.0:
            log.warning("讀不到有效的 rfsim 速度 S（%s），以 S=1.0（不換算）處理", RFSIM_SPEED_FILE)
            val = 1.0
        _SIM_SPEED = val
        log.info("rfsim 速度 S=%.2f：場景頻寬視為模擬時間 Mbps，iperf3 牆鐘頻寬 = 模擬 × S", val)
    return _SIM_SPEED

# ── 通道惡化（2026-09-26 重寫）──────────────────────────────────────────────
# 重要更正：rfsim 的 `channelmod modify ... ploss X` 直接把 X 當「增益」（pow(10, X/20)），**正值是放大、負值才是衰減**。
# 2026-09-26 前場景對 DU 端送的 0~25「路徑損耗」實際是對上行做 0~25 dB 增益，下行完全沒動（UE 端無通道模型）——
# Stage 1~3 從未有過真正的通道惡化。而且基準訊號振幅本來就低（int16 取樣），可用衰減範圍很窄，正值太大會削波、
# 負值太大（如 ploss=-15 且 noise=-6）會讓 UE 斷線且不會自動恢復（需乾淨重啟）。
#
# 場景仍用原本的 0~PATHLOSS_SAFE_MAX_DB「損耗指標」L（Scenario R 抽的連續值、T 的 3/10/18/22/23/24 檔位、
# A/B/C 2026-09-28 起直接引用 T 的同一組檔位、D 仍用 CQI 對照表），
# 只是把它視為劣化程度的純量 s=L/PATHLOSS_SAFE_MAX_DB∈[0,1]，再沿著下面這條「已實測、不斷線」的 (ploss, noise)
# 路徑套用到 UE 端下行通道（UE3/UE11 二維網格，下行 TCP MCS：s=0→28、0.25→27~28、0.5→18~21、0.75→12~13、1→3~4）。
# 終點 (-10,-6) 離會斷線的 (-15,-6) 有 5 dB 餘裕。**上行（DU 端）通道完全不動**（維持 conf 的正常通道
# ploss=0、noise=-50；不再送過去那種實為增益的正 ploss），只改變下行。
# SCENARIO_DL_DEGRADE=0 或 --no-dl-degrade 關閉下行惡化（此時上下行都是正常通道）。
DL_DEGRADE_ENABLED: bool = os.environ.get("SCENARIO_DL_DEGRADE", "1") != "0"
# (s, ploss_dB(負=衰減), noise_power_dB)
DEGRADE_PATH: list[tuple[float, float, float]] = [
    (0.00, 0.0, -50.0),
    (0.25, -5.0, -20.0),
    (0.50, -5.0, -10.0),
    (0.75, -10.0, -10.0),
    (1.00, -10.0, -6.0),
]


def degrade_settings(loss_db: float) -> tuple[float, float]:
    """場景損耗指標 L → (UE 端下行 ploss, UE 端下行 noise)。沿 DEGRADE_PATH 線性內插。"""
    s = max(0.0, min(1.0, loss_db / PATHLOSS_SAFE_MAX_DB))
    for (s0, p0, n0), (s1, p1, n1) in zip(DEGRADE_PATH, DEGRADE_PATH[1:]):
        if s <= s1:
            f = 0.0 if s1 == s0 else (s - s0) / (s1 - s0)
            dl_ploss, dl_noise = p0 + f * (p1 - p0), n0 + f * (n1 - n0)
            break
    else:
        dl_ploss, dl_noise = DEGRADE_PATH[-1][1], DEGRADE_PATH[-1][2]
    return dl_ploss, dl_noise


_UE_DL_CTRL: dict[str, UEChannelController] = {}


def _ue_dl_ctrl(container: str) -> UEChannelController:
    if container not in _UE_DL_CTRL:
        _UE_DL_CTRL[container] = UEChannelController(container)
    return _UE_DL_CTRL[container]


def set_ue_dl_degradation(ue: "UEConfig", loss_db: float) -> None:
    """在背景執行緒設定 UE 端下行通道（docker exec 每次約 1 秒，平行處理避免拖慢相位切換）。"""
    if not DL_DEGRADE_ENABLED:
        return
    dl_ploss, dl_noise = degrade_settings(loss_db)
    threading.Thread(target=_ue_dl_ctrl(ue.container).set_channel, args=(dl_ploss, dl_noise), daemon=True).start()


def reset_ue_dl_degradation(ues: list["UEConfig"]) -> None:
    if not DL_DEGRADE_ENABLED:
        return
    threads = [threading.Thread(target=_ue_dl_ctrl(ue.container).reset, daemon=True) for ue in ues]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

# ── Scenario R 的流量模型（2026-09-26 依 Scenario T 的模擬 Mbps 量級重設）──
# 每個 UE 每個相位處於三種狀態之一，比例 idle : burst : traffic = 1 : 2.5 : 6.5
# （10% : 25% : 65%；舊版 profile×p_idle 的閒置時間約 31%，空白太多）：
#   idle    ：整個相位不傳資料（bw=0.0 sentinel，真的停 iperf3）
#   burst   ：高需求突發（對應 T 的高流量檔位量級，見 R_BURST_*）
#   traffic ：一般持續流量，量級由 UE 的持久化 profile 決定（見 UE_PROFILES）
# 單位一律是「模擬時間 Mbps」（同 Scenario T；iperf3 牆鐘頻寬 = 這裡的值 × S）。
# 量級依 2026-09-26 容量實測訂定：全系統 CPU 平台 ~100 sim Mbps；期望每 UE offered ≈ 5 → 16 UE ≈ 80（UE17 已於 2026-09-30 移除，原本 17 UE ≈ 85）。
R_STATE_WEIGHTS: dict[str, float] = {"idle": 1.0, "burst": 2.5, "traffic": 6.5}   # 比例 1:2.5:6.5（自動正規化）
R_BURST_MIN_MBPS: float = 8.0
R_BURST_MAX_MBPS: float = 16.0            # 對應 T 壅塞相位的高流量（12）到舊 high（16）
R_BURST_LOGNORM_MU: float = math.log(3.0)
R_BURST_LOGNORM_SIGMA: float = 0.4
# R 的路徑損耗指標上限：不用 25（容量 5.4、近斷線邊緣），與 T 一致最高用 24
R_PATHLOSS_MAX_DB: float = 24.0

# 每 UE 持久化使用者 profile：開場抽一次、整個執行期間不變，決定 traffic 狀態下的流量量級，
# 比「每個 phase 完全獨立同分布抽樣」更貼近「同一個用戶的行為模式有慣性」。
#   heavy  ：重度用戶（串流／下載），traffic 流量較高
#   light  ：輕度瀏覽，traffic 流量低
#   bursty ：中間型（視訊通話／遊戲）
# lognorm_mu/sigma 疊加在 min_mbps 之上（只在上界裁切）。權重決定抽到各 profile 的機率。
UE_PROFILES: dict[str, dict[str, float]] = {
    "heavy":  {"weight": 0.25, "min_mbps": 3.0, "max_mbps": 8.0,
               "lognorm_mu": math.log(2.0), "lognorm_sigma": 0.4},
    "light":  {"weight": 0.45, "min_mbps": 1.0, "max_mbps": 4.0,
               "lognorm_mu": math.log(1.0), "lognorm_sigma": 0.5},
    "bursty": {"weight": 0.30, "min_mbps": 2.0, "max_mbps": 6.0,
               "lognorm_mu": math.log(1.5), "lognorm_sigma": 0.5},
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
    global_id: int = 0                     # 跨主機唯一序號（UE1~16 對應 1~16），供
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
        "-b", f"{max(ue.bandwidth_mbps * sim_speed(), 0.05):.3g}M",   # 模擬時間 Mbps × S = 牆鐘 Mbps
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
        log.info("iperf3 start: %s → %s:%d @ %.3gMbps(牆鐘，模擬 %.3gMbps × S=%.2f)/%s (bind=%s)",
                 ue.container, EXT_DN_IP, ue.iperf_port, ue.bandwidth_mbps * sim_speed(),
                 ue.bandwidth_mbps, sim_speed(), ue.protocol.upper(), ue_ip)
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

def _apply_channel(ue: "UEConfig", ctrl: ChannelModController, loss_db: float) -> None:
    """套用場景損耗指標到通道：只改下行（UE 端 ploss+noise 路徑）；上行（DU 端）維持正常通道不動。"""
    set_ue_dl_degradation(ue, loss_db)


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
            _apply_channel(ue, ctrl, new_path_loss_db)
            table = ctrl.cqi_to_pathloss
            ue.target_cqi = min(table, key=lambda c: abs(table[c] - new_path_loss_db))
            changed = True
    elif new_cqi is not None and new_cqi != ue.target_cqi:
        ue.target_cqi = new_cqi
        table = ctrl.cqi_to_pathloss
        nearest = min(table, key=lambda c: abs(c - new_cqi))
        _apply_channel(ue, ctrl, table[nearest])
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

    raw_path_loss=True 時，configs 的第一個元素視為連續 path_loss_db（Scenario R／A/B/C，
    2026-09-28 起 A/B/C 也改用這個路徑，直接引用 Scenario T 的 PLOSS_TIERS 常數），否則
    視為 target_cqi（Scenario D 既有行為，預設）。
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

# A/B/C 的通道嚴重度改直接引用 Scenario T 已實測驗證、不斷線的 NORMAL_PLOSS_TIERS／
# CONGESTED_PLOSS_TIERS 常數（見下方定義），取代各自獨立的 CQI 對照表查表（2026-09-28，
# 使用者要求對齊，避免未來只調整 T 的檔位、卻忘了 A/B/C 也要跟著改）。三個場景不再走
# apply_scenario_phase() 的 CQI 查表路徑，改用 raw_path_loss=True 直接送連續 L 值——
# 跟 Scenario R 同一種呼叫方式，run_fixed_scenario() 已同步更新。


def scenario_a(
    ues: list[UEConfig], ctrls: dict[int, ChannelModController], protocol: str = "tcp"
) -> list[tuple[float, float, str]]:
    """場景 A：通道差異化——同 Node 內兩 UE 對比好通道 vs 差通道，流量相等。

    好／差通道直接取 Scenario T 的 NORMAL_PLOSS_TIERS 低／高檔位（3.0／18.0 dB，
    定義見下方），跟原本 CQI 15 vs 5 的查表結果（L=0.0／18.0）幾乎一致，只是改成
    直接共用 T 的常數而非各自查表。

    A/B/C 的 protocol 參數（見 CLI `--protocol`）：全部 UE 統一用這個協定，通道/頻寬
    設定完全不變，只換傳輸層，供 TCP/UDP 兩版本 1:1 對照。
    """
    configs = []
    for i, _ in enumerate(ues):
        ploss = NORMAL_PLOSS_TIERS["low"] if i % 2 == 0 else NORMAL_PLOSS_TIERS["high"]
        configs.append((ploss, 30.0, protocol))
    return configs


def scenario_b(
    ues: list[UEConfig], ctrls: dict[int, ChannelModController], protocol: str = "tcp"
) -> list[tuple[float, float, str]]:
    """場景 B：流量不均（Jain's Fairness 壓測）——等通道，流量比例不對等。

    通道品質不是這個場景要測的變數，統一取 Scenario T 的 NORMAL_PLOSS_TIERS 低檔位
    （3.0 dB，良好通道），對應原本 CQI=12（L=5.0）的「還不錯」通道品質。
    """
    configs = []
    for i, _ in enumerate(ues):
        bw = 45.0 if i % 2 == 0 else 25.0
        configs.append((NORMAL_PLOSS_TIERS["low"], bw, protocol))
    return configs


def scenario_c(
    ues: list[UEConfig], ctrls: dict[int, ChannelModController], protocol: str = "tcp"
) -> list[tuple[float, float, str]]:
    """場景 C：最惡公平性——差通道高需求 vs 好通道低需求。

    要最大化對比、逼近「最惡」的場景設計意圖，好通道取 NORMAL_PLOSS_TIERS 低檔位
    （3.0 dB），差通道取 CONGESTED_PLOSS_TIERS 高檔位（24.0 dB，T 本身壅塞相位會用到
    的最深值，已驗證不斷線）——對比幅度比原本 CQI 4 vs 14（L=21.0 vs 0.0）更極端，但
    仍在 T 已驗證的安全範圍內。
    """
    configs = []
    for i, _ in enumerate(ues):
        ploss, bw = (
            (CONGESTED_PLOSS_TIERS["high"], 50.0) if i % 2 == 0
            else (NORMAL_PLOSS_TIERS["low"], 25.0)
        )
        configs.append((ploss, bw, protocol))
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


# ── Scenario T（兩狀態：正常 / 壅塞相位 × 分層交叉 低/中/高流量 × 低/中/高路徑損耗）──
# 2026-09-26 重設計（使用者要求：量測期間約 45% 的「時間」處於壅塞狀態，其餘時間為正常狀態，
# 兩種狀態內流量與通道仍然變化）。以 11 個相位為一個週期，其中 5 個為壅塞相位（45.5%）；
# 相位編號用（--phase-origin 起算的）絕對時間換算，PC2/PC3 兩邊自動一致。
# 單位：模擬時間 Mbps（iperf3 牆鐘頻寬 = 這裡的值 × S，S=0.4）；通道欄位是場景損耗指標 L
# （0~25，見 DEGRADE_PATH）。單 UE 下行容量曲線（sim Mbps，2026-09-26 實測）：
#   L=10→40、15→35.5、18→30.8、20→22.2、22→16.8、24→8.6、25→5.4（run 間變異約 ±30%）。
# 全系統 CPU 限制的「送達」總量平台約 100~108 sim Mbps。
#   正常相位：流量 1/4/8、L=3/10/18（容量 ≥30，全部送得完，總 offered ≈ 74）。
#   壅塞相位：流量 1/10/12、L=22/23/24。同節點兩 UE 合計容量（實測，sim Mbps）：L=20→31、21→28、22→23、23→17.6、24→13.2；
#     相鄰兩 UE 通常同流量檔位，成對 offered = 2/20/24，故約 5/9 的節點過載，期望送達 ≈ 105。不用 L=25（容量 5.4、1% 遺失，近斷線邊緣）。
NORMAL_TRAFFIC_TIERS: dict[str, float] = {"low": 1.0, "medium": 4.0, "high": 8.0}
NORMAL_PLOSS_TIERS: dict[str, float] = {"low": 3.0, "medium": 10.0, "high": 18.0}
CONGESTED_TRAFFIC_TIERS: dict[str, float] = {"low": 1.0, "medium": 10.0, "high": 12.0}
CONGESTED_PLOSS_TIERS: dict[str, float] = {"low": 22.0, "medium": 23.0, "high": 24.0}
T_CYCLE_PHASES = 11
T_CONGESTED_PHASES = frozenset({2, 4, 5, 7, 9})   # 週期內第幾個相位是壅塞相位（5/11=45.5%，分散排列）；跑 11 個相位 = 一整個週期
TIER_COMBOS: list[tuple[str, str]] = [
    (t, p) for t in NORMAL_TRAFFIC_TIERS for p in NORMAL_PLOSS_TIERS]


def t_phase_congested(phase_index: int) -> bool:
    """Scenario T 這個相位是否為壅塞相位。"""
    return (phase_index % T_CYCLE_PHASES) in T_CONGESTED_PHASES


def scenario_t_tiered(
    ues: list[UEConfig], phase_index: int, protocol: str = "tcp"
) -> list[tuple[float, float, str]]:
    """
    場景 T：流量（低/中/高）× 路徑損耗（低/中/高）3x3 交叉設計。

    動機：Scenario R 的 profile 設計（heavy/light/bursty）與既有 A/B/C/D 都沒有
    真正「把頻寬塞滿」的高負載條件，且 R 的 light/bursty profile 佔比高、
    閒置機率不低，長時間收斂訓練下發現 reward 訊號量級普遍偏小、對雜訊敏感
    （見 HISTORY.md 2026-09-18 條目）。這裡改用明確的 3x3 交叉設計覆蓋低/中/
    高負載 × 低/中/高通道品質的組合空間，不含閒置機率——閒置狀態的訓練資料
    交給 Scenario R 分擔，T 專注在「有真實流量時」這個子空間。

    每個 phase 把 9 種組合依 (UE 索引 + phase_index) 錯開分配給所有 UE：
    同一個 phase 內不同 UE 拿到不同組合（同時間的狀態多樣性），且隨 phase
    推進輪替（每個 UE 長期下來會經歷全部 9 種組合，不會卡在同一種）。

    protocol：全部 UE 統一用這個協定跑（"tcp" 或 "udp"，見 CLI `--protocol`）。
    2026-09-22 現場驗證發現 TCP 版本的達成吞吐量被 TCP 自身的擁塞視窗/RTT
    乘積卡死（同一個 UE、同通道、同目標，TCP 卡在 ~1Mbps，UDP 可以乾淨跑滿
    20Mbps、0% 封包遺失），代表 TCP 版本量到的其實是「這個平台 TCP 緩衝區
    設定的極限」而非排程器真正分配出來的容量，會壓縮甚至掩蓋 PF/avg FL/
    cluster FL 之間真正的排程差異。UDP 沒有壅塞窗口，量到的吞吐量更直接
    反映排程器的實際分配結果，適合當作判斷排程演算法優劣的主要指標。
    """
    configs: list[tuple[float, float, str]] = []
    n_combos = len(TIER_COMBOS)
    congested = t_phase_congested(phase_index)
    traffic_tiers = CONGESTED_TRAFFIC_TIERS if congested else NORMAL_TRAFFIC_TIERS
    ploss_tiers = CONGESTED_PLOSS_TIERS if congested else NORMAL_PLOSS_TIERS
    log.info("Scenario T 相位狀態：%s（phase_index=%d，週期內第 %d/%d 個）",
             "壅塞" if congested else "正常", phase_index, phase_index % T_CYCLE_PHASES, T_CYCLE_PHASES)
    for i, _ in enumerate(ues):
        combo_idx = (i + phase_index) % n_combos
        traffic_name, ploss_name = TIER_COMBOS[combo_idx]
        configs.append((ploss_tiers[ploss_name], traffic_tiers[traffic_name], protocol))
    return configs


# ── Scenario P（混合通道壅塞試驗，2026-09-30）─────────────────────────────────────────
# 離線模型（/home/lindor/pf16_run_20260930/upper_bound/）預測：同節點「好 UE（L=22、需求 22）＋壞 UE（L=24、需求 6）」
# 時，PF 平分 RB 會把 RB 浪費在壞 UE；把壞 UE 限在 0.3 檔可多約 11% 總吞吐量。這個固定場景用來在平台上驗證這件事
# （PF vs 靜態上限），通過後才正式改 T／TH。每個 branch 的第一個 access 節點是混合節點、第二個是輕節點，每個相位相同。
P_GOOD_UES = frozenset({1, 5, 9, 13})     # 混合節點的好 UE（L=22）
P_BAD_UES = frozenset({2, 6, 10, 14})     # 混合節點的壞 UE（L=24）
P_GOOD = (22.0, 22.0)                     # (L, 需求 sim Mbps)
P_BAD = (24.0, 6.0)
P_LIGHT = (10.0, 1.0)


def scenario_p_pilot(ues: list[UEConfig], phase_index: int, protocol: str = "tcp") -> list[tuple[float, float, str]]:
    """場景 P：固定的混合通道壅塞配置（試驗用，每個相位相同）。"""
    log.info("Scenario P 相位狀態：壅塞（phase_index=%d，固定配置）", phase_index)
    out = []
    for ue in ues:
        L, bw = P_GOOD if ue.global_id in P_GOOD_UES else P_BAD if ue.global_id in P_BAD_UES else P_LIGHT
        out.append((L, bw, protocol))
    return out


# ── Scenario TM（T-Mixed，候選新基準，2026-10-01，尚未定案）─────────────────────────
# 場景 P 試驗發現：頻域 PRB 上限在這個平台無法重新分配資源，時域遮罩可以（HISTORY.md 續四十六）；但只有「同節點通道差很多、
# 好 UE 需求高」的節點有空間。TM 保留 T 的骨架（11 相位、壅塞相位 2/4/5/7/9、正常相位完全等同 T），只把壅塞相位改成：
#   每個 branch 一個「主動節點」＋一個輕節點（2×L=10、需求 1）。4 個 branch 中 3 個的主動節點是 M 類（該遮：好 UE 中差通道高需求＋
#   壞 UE L=24/23 低需求），1 個是 N 類（不該遮：好 UE 需求小、本來就吃得飽，遮壞 UE 只會虧）。哪個 branch 是 N 類、主動節點是
#   branch 內哪一個、節點內哪個 UE 是好 UE，都隨 phase_index 輪替 → 長期對稱。N 類讓「看到低 MCS 就遮」的固定規則吃虧，
#   最佳動作隨狀態改變（好 UE 佇列是否堆積），DRL 才有東西可學。
# 離線模擬（/home/lindor/pf16_run_20260930/slotsim/，MAC 層，實際增益約 ×0.4）：M 類 +12~15%、N 類遮了 −13~−20%。
TM_M_TYPES = [((22.0, 22.0), (24.0, 6.0)),     # M22：好 UE (L, 需求 sim Mbps)、壞 UE
              ((22.0, 18.0), (24.0, 8.0)),     # M22b
              ((22.0, 20.0), (23.0, 6.0))]     # M23
TM_N_TYPES = [((10.0, 8.0), (24.0, 6.0)),      # N10：好 UE 通道好、需求小
              ((22.0, 8.0), (24.0, 6.0))]      # N22：好 UE 需求小
TM_LIGHT = (10.0, 1.0)


def _tm_congested_index(phase_index: int) -> int:
    """第幾個壅塞相位（從 0 起算）：週期編號×5 + 在週期內的序號。只對壅塞相位有意義。"""
    cyc, r = divmod(phase_index, T_CYCLE_PHASES)
    return cyc * len(T_CONGESTED_PHASES) + sorted(T_CONGESTED_PHASES).index(r)


def _tm_roles(k: int) -> list[tuple[int, tuple, int]]:
    """
    第 k 個壅塞相位、每個 branch 的角色：(主動節點在 branch 內的位置 0/1, (好 UE, 壞 UE) 類型, 好 UE 在節點內的位置 0/1)。
    2026-10-01 修正：原本各變數都直接由 phase_index 取餘數，彼此相關、又只在固定的壅塞相位取樣，長期不對稱
    （UE1/UE13 永遠是 N 類好 UE、從來不是 M 類好 UE；其他 UE 永遠碰不到 N 類）。改成以壅塞相位序號 k 做混合進位，
    每個變數獨立循環：每 96 個壅塞相位（4×2×2×3×2）所有組合各出現一次，長期每個 UE 的角色比例完全相同。
    """
    n_branch = k % 4
    out = []
    for b in range(4):
        active = ((k // 4) + b) % 2
        good_q = ((k // 8) + b) % 2
        if b == n_branch:
            typ = TM_N_TYPES[(k // 48) % len(TM_N_TYPES)]
        else:
            typ = TM_M_TYPES[((k // 16) + b) % len(TM_M_TYPES)]
        out.append((active, typ, good_q))
    return out


def _tm_assign(ues: list[UEConfig], roles: list[tuple[int, tuple, int]], protocol: str) -> list[tuple[float, float, str]]:
    out = []
    for ue in ues:
        gid = ue.global_id
        node = (gid - 1) // 2 + 5
        branch, pos, q = (node - 5) // 2, (node - 5) % 2, (gid - 1) % 2
        active, (good, bad), good_q = roles[branch]
        L, bw = TM_LIGHT if pos != active else (good if q == good_q else bad)
        out.append((L, bw, protocol))
    return out


def scenario_tm_mixed(ues: list[UEConfig], phase_index: int, protocol: str = "tcp") -> list[tuple[float, float, str]]:
    """場景 TM：正常相位＝Scenario T；壅塞相位＝混合通道配置，角色依壅塞相位序號做混合進位輪替（長期對稱）。"""
    if not t_phase_congested(phase_index):
        cfg = scenario_t_tiered(ues, phase_index, protocol=protocol)   # 正常相位完全沿用 T（含其 log）
        log.info("Scenario TM 相位狀態：正常（phase_index=%d，週期內第 %d/%d 個）",
                 phase_index, phase_index % T_CYCLE_PHASES, T_CYCLE_PHASES)
        return cfg
    log.info("Scenario TM 相位狀態：壅塞（phase_index=%d，週期內第 %d/%d 個）",
             phase_index, phase_index % T_CYCLE_PHASES, T_CYCLE_PHASES)
    return _tm_assign(ues, _tm_roles(_tm_congested_index(phase_index)), protocol)




def _tm_type_load(typ: tuple) -> float:
    """類型的吃重程度（以每秒需要的 RB 量近似：需求 / 每 RB 效率）。用來決定 TMH 裡哪個角色比較「難」。"""
    eff = {10.0: 91.0, 18.0: 31.8, 22.0: 19.2, 23.0: 12.5, 24.0: 8.8}   # 2026-10-01 PF-shadow 實測（HISTORY.md 續四十三）
    (lg, dg), (lb, db) = typ
    return dg / eff[lg] + db / eff[lb]


def scenario_tmh_heterogeneous(
    ues: list[UEConfig], seed: int, phase_index: int, protocol: str = "tcp"
) -> list[tuple[float, float, str]]:
    """
    場景 TMH：TM 的持久異質性版本（對應 TH 之於 T，2026-10-01）。
    - 正常相位：與 Scenario TH 相同（scenario_th_heterogeneous）。
    - 壅塞相位：先取 TM 在這個相位的角色組合（_tm_roles），**同一台主機的兩個 branch 之間**依固定難度重新分配：
        1. 兩個 branch 的分數＝兩個 access 節點 BRANCH_HARDSHIP 平均＋均勻隨機 ±TH_RANK_NOISE；分數高的 branch 拿較吃重的類型。
        2. branch 內兩個節點的分數＝BRANCH_HARDSHIP＋隨機；分數高的當主動節點（混合通道），另一個是輕負載節點。
        3. 主動節點內兩個 UE 的分數＝UE_HARDSHIP＋隨機；分數高的當壞 UE。
      只在同一台主機內交換 → 每台主機、每個相位的總負載與 TM 完全相同，TM 與 TMH 只差「角色怎麼分」。
    隨機部分由 _rng_for(seed, ·, phase_index) 決定式導出，PC2/PC3 一致；固定難度跟 seed 無關，長期不會平均掉。
    """
    if not t_phase_congested(phase_index):
        cfg = scenario_th_heterogeneous(ues, seed, phase_index, protocol=protocol)
        log.info("Scenario TMH 相位狀態：正常（seed=%d phase_index=%d）", seed, phase_index)
        return cfg
    log.info("Scenario TMH 相位狀態：壅塞（seed=%d phase_index=%d）", seed, phase_index)
    noise = lambda key: (2.0 * _rng_for(seed, key, phase_index).random() - 1.0) * TH_RANK_NOISE
    base = _tm_roles(_tm_congested_index(phase_index))
    roles = list(base)
    for host_branches in ((0, 1), (2, 3)):                       # PC2：branch 0/1（Node5~8）；PC3：branch 2/3（Node9~12）
        types = sorted((base[b][1] for b in host_branches), key=_tm_type_load)          # 由輕到重
        bscore = sorted(host_branches, key=lambda b: (BRANCH_HARDSHIP[2 * b + 5] + BRANCH_HARDSHIP[2 * b + 6]) / 2
                        + noise(-10 - b))                                              # 由易到難
        for b, typ in zip(bscore, types):
            n0, n1 = 2 * b + 5, 2 * b + 6
            active = 0 if BRANCH_HARDSHIP[n0] + noise(-20 - n0) >= BRANCH_HARDSHIP[n1] + noise(-20 - n1) else 1
            node = n0 + active
            g0, g1 = (node - 5) * 2 + 1, (node - 5) * 2 + 2
            bad_q = 0 if UE_HARDSHIP[g0] + noise(g0) >= UE_HARDSHIP[g1] + noise(g1) else 1
            roles[b] = (active, typ, 1 - bad_q)
    return _tm_assign(ues, roles, protocol)


# ── Scenario TH（T-Heterogeneous，訓練專用，2026-09-29）──────────────────────────────
#
# 動機：T／TR 的 combo 指派本質上是「round-robin／均勻隨機」——不管是 (i+phase_index)%9
# 還是逐 UE 獨立均勻抽樣，長期統計下來每個 UE 都會平均經歷全部 9 種（流量×通道）組合，
# 節點之間沒有真正可利用的持久結構差異。這是刻意設計（確保 PF vs DRL 比較公平，見
# CLAUDE.md 第 3 節），但也代表 Stage 3 CAPA-Fed 這類「依節點差異做個人化」的聚合機制
# 沒有素材可學——離線驗證（2026-09-29）發現即使節點有真實本地訓練，Actor 權重跟 12 節點
# 平均比也只偏離 <1%，個人化幅度小到可忽略，因為「自己的權重」跟「大家的平均」本來就
# 很接近。
#
# TH 保留 T／TR 的量級與結構完全不變（同一組 NORMAL/CONGESTED_TRAFFIC_TIERS／
# PLOSS_TIERS、同樣兩狀態壅塞週期），只改一件事：combo 指派從「長期均勻」改成
# 「持久化節點難度偏移＋隨機」，讓某些節點長期下來比其他節點更常遇到高負載／差通道
# 組合，不會像 T/TR 一樣平均下來大家一樣。**只用於訓練場景，不影響最終跨 Stage 比較用
# 的標準 Scenario T**（比照 CLAUDE.md「最終 TCP/UDP 量測場景必須維持標準 Scenario T
# 不動，只能改訓練場景」的既有原則）。
#
# 節點難度指派（結構性常數，比照 ROLE_RATIO 的精神，不是即時量測值；2026-09-29 改版，
# 取代舊版「4 個 relay 分支對半分」的粗略二分法）：**逐節點各自獨立、用固定結構性種子
# 隨機導出**，不是手動指定哪個節點難哪個節點易——避免看起來像刻意挑數字去湊結果，
# 對論文方法論的可信度更好交代，也讓每個節點的難度都不同（不會有「同分支內完全一樣」
# 的問題），異質性比二分法更細緻。`TH_HARDSHIP_SEED` 是跟訓練 seed（TR/R 用的
# 140000/150000 區段）完全無關、獨立固定的結構性種子——不隨每次訓練的 --seed 改變，
# 保證這個「哪個節點天生比較難」的指派本身也是**持久、跨訓練輪次一致**的環境屬性，
# 不是每次重跑訓練就換一批。key 是 UE 實際的 node_id（access 節點），不含 relay Node1~4 本身（沒有 UE
# 直接掛在它們的 DU 下）。[UE17 已移除 2026-09-30] 原本含 Node4（UE17 直連），已拿掉；每個節點的值由
# (TH_HARDSHIP_SEED, node_id) 獨立導出，拿掉 Node4 不影響 Node5~12 的值。
TH_HARDSHIP_SEED: int = 999999999  # 固定結構性種子，與訓練 seed 無關，不要跟著訓練變動


def _structural_rng(seed: int, key: int) -> random.Random:
    """跟下方 `_rng_for()` 完全相同的決定式導出邏輯，這裡獨立複製一份——因為
    `BRANCH_HARDSHIP` 在模組載入時就要算好（模組級常數），此時 `_rng_for()` 還沒定義
    （它在檔案後段），不能在載入時期呼叫尚未定義的函式；`_rng_for()` 定義好之後，
    兩者算出來的值必須一致（同一份雜湊公式），不要各自漂移。"""
    h = hashlib.sha256(f"{seed}:{key}".encode()).digest()
    return random.Random(int.from_bytes(h[:8], "big"))


BRANCH_HARDSHIP: dict[int, float] = {
    node_id: 2.0 * _structural_rng(TH_HARDSHIP_SEED, node_id).random() - 1.0
    for node_id in (5, 6, 7, 8, 9, 10, 11, 12)   # [UE17 已移除 2026-09-30] 原本含 4
}

# TMH 用的每 UE 固定難度（決定節點內誰是壞 UE）；與 BRANCH_HARDSHIP 同一個結構性種子、不同索引（100+global_id），跟訓練 seed 無關。
UE_HARDSHIP: dict[int, float] = {
    gid: 2.0 * _structural_rng(TH_HARDSHIP_SEED, 100 + gid).random() - 1.0 for gid in range(1, 17)
}


TH_RANK_NOISE: float = 0.5   # 排序分數的隨機幅度（±0.5）：節點難度差 >1 時幾乎固定，差距小的節點之間會輪流


def scenario_th_heterogeneous(
    ues: list[UEConfig], seed: int, phase_index: int, protocol: str = "tcp"
) -> list[tuple[float, float, str]]:
    """
    場景 TH：Scenario T 的異質性版本——**每個相位、每台主機用的（流量×通道）組合跟 T 完全相同**（同一批組合、
    同樣兩狀態壅塞週期），唯一差異是「哪個 UE 拿到哪一個組合」：

      1. 先取 T 在這台主機、這個相位會用的那批組合（T 給第 i 個 UE 的是 TIER_COMBOS[(i+phase_index)%9]），
         依組合編號由易到難排好（TIER_COMBOS 以流量為主、通道為輔排序，編號越大越吃重）。
      2. 每個 UE 算排序分數 = 所屬節點的 BRANCH_HARDSHIP + 均勻隨機 ±TH_RANK_NOISE
         （隨機部分由 _rng_for(seed, global_id, phase_index) 決定式導出，PC2/PC3 各自算出一致結果）。
      3. 分數低的 UE 拿較易的組合、分數高的拿較難的。

    效果：難度高的節點長期下來較常拿到重的組合（持久異質性，不隨時間平均掉），但每台主機、每個相位的
    **總負載與 T 逐一相等**，T 與 TH 只差「負載怎麼分給節點」，不差「總共多少負載」。

    2026-09-30 改版前的做法是把每個 UE 的組合編號往「難」的方向平移 round(hardship×4) 格：節點難度平均
    +0.375（偏正），又集中在 PC2 側，結果 TH 的總負載明顯比 T 重（seed 20260930 下壅塞相位總目標 149 對 118），
    T 與 TH 同時差了「異質性」與「總負載」兩件事，無法把差異歸因到異質性，已改掉。
    """
    configs: list[tuple[float, float, str]] = []
    n_combos = len(TIER_COMBOS)
    congested = t_phase_congested(phase_index)
    traffic_tiers = CONGESTED_TRAFFIC_TIERS if congested else NORMAL_TRAFFIC_TIERS
    ploss_tiers = CONGESTED_PLOSS_TIERS if congested else NORMAL_PLOSS_TIERS
    log.info("Scenario TH 相位狀態：%s（phase_index=%d，週期內第 %d/%d 個）",
             "壅塞" if congested else "正常", phase_index, phase_index % T_CYCLE_PHASES, T_CYCLE_PHASES)
    pool = sorted((i + phase_index) % n_combos for i in range(len(ues)))   # T 的同一批組合，由易到難
    scores = []
    for k, ue in enumerate(ues):
        noise = (2.0 * _rng_for(seed, ue.global_id, phase_index).random() - 1.0) * TH_RANK_NOISE
        scores.append((BRANCH_HARDSHIP.get(ue.node_id, 0.0) + noise, k))
    combo_of = [0] * len(ues)
    for rank, (_, k) in enumerate(sorted(scores)):
        combo_of[k] = pool[rank]
    for k in range(len(ues)):
        traffic_name, ploss_name = TIER_COMBOS[combo_of[k]]
        configs.append((ploss_tiers[ploss_name], traffic_tiers[traffic_name], protocol))
    return configs


# ── Scenario TR（隨機化的兩狀態 T，訓練專用，2026-09-26）──────────────────────────────
# 與量測基準用的固定 Scenario T 結構、量級完全相同（同樣的正常/壅塞兩組流量與通道檔位、同樣的 45% 時間壅塞），
# 但**壅塞相位的排列與每個 UE 的（流量×通道）組合都由 seed 隨機打散**，訓練資料因此跟測試場景不同分佈
# （驅動器的原則：訓練場景不可等於測試場景）。跨主機一致性：全部隨機值只由 (seed, phase_index) 導出，
# PC2/PC3 各自算出相同結果，不需要通訊。
T_RANDOM_CONGESTED_P: float = 5.0 / 11.0     # 每個相位為壅塞相位的機率（期望 45.5% 的時間壅塞，同 T）
T_RANDOM_NUM_UES: int = 16                   # 全系統 UE 數（global_id 1~16）[UE17 已移除 2026-09-30] 原本 17


def t_random_phase_congested(seed: int, phase_index: int) -> bool:
    """Scenario TR：這個相位是否為壅塞相位（由 (seed, phase_index) 決定，兩台主機一致）。"""
    return _rng_for(seed, -1, phase_index).random() < T_RANDOM_CONGESTED_P


def scenario_t_random(
    ues: list[UEConfig], seed: int, phase_index: int, protocol: str = "tcp"
) -> list[tuple[float, float, str]]:
    """
    場景 TR：隨機化的兩狀態 T（訓練用）。狀態（正常/壅塞）隨機，檔位表與 Scenario T 相同。

    每個相位把 9 種（流量×通道）組合各重複 2 次共 18 份、依 (seed, phase_index) 洗牌，再依 UE 的 global_id 分配
    （16 個 UE 各拿一份）：組合分佈與 T 一樣均衡（不會因為獨立抽樣而偶爾全部 UE 同時抽到高流量、壓垮平台的 CPU
    上限），但誰拿哪個組合每個相位都不同。
    """
    congested = t_random_phase_congested(seed, phase_index)
    traffic_tiers = CONGESTED_TRAFFIC_TIERS if congested else NORMAL_TRAFFIC_TIERS
    ploss_tiers = CONGESTED_PLOSS_TIERS if congested else NORMAL_PLOSS_TIERS
    n_combos = len(TIER_COMBOS)
    # 固定 2×9=18 份（原本寫成 T_RANDOM_NUM_UES+1，17 個 UE 時剛好 18）：UE17 移除後若改成 16+1=17 份會不平衡，
    # 也會讓 UE1~16 的洗牌結果全部改變；固定 18 份讓 UE1~16 的組合序列跟移除前完全相同
    perm = [k % n_combos for k in range(2 * n_combos)]
    _rng_for(seed, -2, phase_index).shuffle(perm)
    log.info("Scenario TR 相位狀態：%s（seed=%d phase_index=%d）",
             "壅塞" if congested else "正常", seed, phase_index)
    configs: list[tuple[float, float, str]] = []
    for ue in ues:
        traffic_name, ploss_name = TIER_COMBOS[perm[(ue.global_id - 1) % len(perm)]]
        configs.append((ploss_tiers[ploss_name], traffic_tiers[traffic_name], protocol))
    return configs


def scenario_tm_random(
    ues: list[UEConfig], seed: int, phase_index: int, protocol: str = "tcp"
) -> list[tuple[float, float, str]]:
    """
    場景 TMR：TM 的隨機化版本（訓練用，2026-10-01）。訓練不可用量測用的固定 TM（同 TR 之於 T）。
    - 相位狀態：與 TR 相同的隨機壅塞（t_random_phase_congested，約 45%）；正常相位＝TR 的正常相位。
    - 壅塞相位：節點類型只用 TM 已驗證過的類型庫（TM_M_TYPES／TM_N_TYPES／TM_LIGHT），但每個相位隨機決定
      N 類 branch 的數量（0／1／2 個，機率 1/4／1/2／1/4）與位置、每個 branch 的主動節點、M/N 類型、節點內好 UE 是哪個。
      N 類數量有變化，讓「何時該遮」不是固定比例、策略必須看狀態判斷。
    全部由 _rng_for(seed, -3, phase_index) 決定式導出，兩台主機一致。
    """
    congested = t_random_phase_congested(seed, phase_index)
    if not congested:
        cfg = scenario_t_random(ues, seed, phase_index, protocol=protocol)
        log.info("Scenario TMR 相位狀態：正常（seed=%d phase_index=%d）", seed, phase_index)
        return cfg
    rng = _rng_for(seed, -3, phase_index)
    n_n = rng.choices([0, 1, 2], weights=[1, 2, 1])[0]
    n_branches = sorted(rng.sample(range(4), n_n))
    active_pos = [rng.randrange(2) for _ in range(4)]
    good_q = [rng.randrange(2) for _ in range(4)]
    types = [rng.choice(TM_N_TYPES) if b in n_branches else rng.choice(TM_M_TYPES) for b in range(4)]
    log.info("Scenario TMR 相位狀態：壅塞（seed=%d phase_index=%d，N 類 branch=%s）", seed, phase_index, n_branches)
    out = []
    for ue in ues:
        gid = ue.global_id
        node = (gid - 1) // 2 + 5
        branch, pos, q = (node - 5) // 2, (node - 5) % 2, (gid - 1) % 2
        if pos != active_pos[branch]:
            L, bw = TM_LIGHT
        else:
            good, bad = types[branch]
            L, bw = good if q == good_q[branch] else bad
        out.append((L, bw, protocol))
    return out


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
    protocol: Optional[str] = None,
) -> list[tuple[float, float, str]]:
    """
    場景 R：真實隨機相位——用有統計依據的分佈 + 持久化使用者 profile 取代 D 的均勻隨機。

    通道品質：真實蜂巢網路裡 UE 均勻分布在細胞「面積」上，而非均勻分布在 CQI
    值上——面積隨半徑平方成長，代表更多 UE 落在訊號較差的邊緣區域。抽樣
    r = sqrt(U)，U ~ Uniform(0,1)，使 r 的機率密度 f(r) = 2r（隨半徑線性增加，
    對應環狀面積 ∝ r），再線性映射到 [0, R_PATHLOSS_MAX_DB]（=24，場景損耗指標 L，見 DEGRADE_PATH），直接送
    set_path_loss()（繞過 8 點 CQI 對照表，連續值，通道解析度比 A/B/C/D 都細）。

    流量需求：每個 UE 開場已抽定一個持久化 profile（見 UE_PROFILES），本函式依
    該 UE 的 profile 參數做 Lognormal 抽樣，而不是全體 UE 共用同一組參數——不同
    profile 的 UE 呈現出明顯不同的流量特徵，比「所有 UE 同分布」更貼近真實世界
    「不同用戶行為模式不同」的異質性。

    協定：每個 UE 每個相位額外抽一個協定（TCP/UDP 混合，見 P_UDP），modeling
    真實流量裡少數即時語音/視訊/遊戲走 UDP、多數網頁/串流走 TCP 的組成。

    時間動態：每個 UE 每個相位依 idle:burst:traffic = 1:2.5:6.5（R_STATE_WEIGHTS）抽
    三態之一：idle 整個相位完全閒置（bw=0.0 sentinel，apply_ue_config() 見到會真的停止
    iperf3，不是把頻寬設到接近 0）、burst 高需求突發、traffic 一般持續流量（量級依 profile），
    模擬 burst→idle→burst 的真實間歇使用型態。頻寬單位是模擬時間 Mbps（同 Scenario T）。path_loss 不受閒置影響，仍照常
    抽樣套用——通道條件是物理環境的屬性，跟有沒有資料在傳輸無關。

    seed/phase_index：每個 UE 用 `_rng_for(seed, ue.global_id, phase_index)` 導出
    獨立 RNG，任一台主機都能只算自己負責的 UE、不需要知道其他 UE 被抽到什麼。

    protocol：None（預設）維持上面的 TCP/UDP 混合；"tcp"/"udp" 則把所有非閒置 UE
    的協定強制覆寫成該值。協定抽樣的 rng.random() 仍照常消耗，所以同一個 seed 下
    路徑損耗/頻寬/閒置的抽樣序列跟混合版完全一致，只有傳輸層不同（供 UDP 版本跟
    既有 seed 的資料 1:1 對照）。
    """
    configs: list[tuple[float, float, str]] = []
    for ue in ues:
        rng = _rng_for(seed, ue.global_id, phase_index)
        prof = UE_PROFILES[ue.profile]

        r = math.sqrt(rng.random())
        path_loss = r * R_PATHLOSS_MAX_DB

        # 三態抽樣（idle/burst/traffic = 1:2.5:6.5）
        u = rng.random() * sum(R_STATE_WEIGHTS.values())
        if u < R_STATE_WEIGHTS["idle"]:
            state = "idle"
        elif u < R_STATE_WEIGHTS["idle"] + R_STATE_WEIGHTS["burst"]:
            state = "burst"
        else:
            state = "traffic"

        if state == "idle":
            bw = 0.0
            proto = ue.protocol  # 閒置時協定無意義，維持原值即可
        else:
            if state == "burst":
                bw = R_BURST_MIN_MBPS + rng.lognormvariate(R_BURST_LOGNORM_MU, R_BURST_LOGNORM_SIGMA)
                bw = min(bw, R_BURST_MAX_MBPS)
            else:
                bw = prof["min_mbps"] + rng.lognormvariate(prof["lognorm_mu"], prof["lognorm_sigma"])
                bw = min(bw, prof["max_mbps"])
            proto = "udp" if rng.random() < P_UDP else "tcp"
            if protocol is not None:
                proto = protocol

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
    """建立 UE 清單。host 指定時只回傳該主機負責的 UE 子集。

    逐 UE 判斷所屬主機：優先查 UE_HOST_OVERRIDE（處理 UE17 這種「邏輯掛在
    某 Node 底下，但容器實際跑在另一台主機」的特例），查不到才 fallback 到
    該 UE 所屬 Node 的 HOST_OF_NODE。其餘 16 個 UE 沒有覆寫、行為不變。
    """
    ues = []
    for node_id, (_, ue_list) in NODE_CONFIG.items():
        for container, ue_id in ue_list:
            ue_host = UE_HOST_OVERRIDE.get(container, HOST_OF_NODE.get(node_id))
            if host is not None and ue_host != host:
                continue
            global_id = int(container.rsplit("-", 1)[-1])  # "rfsim5g-end-ue-17" → 17
            ues.append(UEConfig(container=container, ue_id=ue_id, node_id=node_id, global_id=global_id))
    return ues


def build_controllers(host: Optional[str] = None) -> dict[int, ChannelModController]:
    """建立 channelmod controller。

    host 指定時，回傳的集合 = 「DU 跟這個主機同機的節點」（一般情況，走
    127.0.0.1）∪「這個主機負責 traffic control 的 UE，其所屬 Node 掛在別的
    主機」（UE_HOST_OVERRIDE 特例，跨主機走 NODE_TELNET_HOST_OVERRIDE 指定
    的位址）——沒有這一步，apply_scenario_phase() 對 UE17 呼叫
    ctrls[ue.node_id] 會在 pc3 端 KeyError，因為 Node4 平常只會出現在
    host="pc1" 的結果裡。
    """
    ctrls = {
        node_id: ChannelModController("127.0.0.1", telnet_port)
        for node_id, (telnet_port, _) in NODE_CONFIG.items()
        if host is None or HOST_OF_NODE.get(node_id) == host
    }
    if host is not None:
        for container, ue_host in UE_HOST_OVERRIDE.items():
            if ue_host != host:
                continue
            owner_node_id = next(
                nid for nid, (_, ue_list) in NODE_CONFIG.items()
                if any(c == container for c, _ in ue_list)
            )
            if owner_node_id in ctrls:
                continue
            telnet_port = NODE_CONFIG[owner_node_id][0]
            remote_host = NODE_TELNET_HOST_OVERRIDE.get(owner_node_id, "127.0.0.1")
            ctrls[owner_node_id] = ChannelModController(remote_host, telnet_port)
    return ctrls


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


# =============================================================================
# 崩潰防護（CrashGuard）
# =============================================================================
# 場景執行期間，UE / MT / DU 容器若崩潰並被 docker 自動重啟（2026-09-26 事件：
# nr-uesoftmodem 在 init_RA segfault，重啟後 IP 改變、留下失效 F1-U 位址，造成核心網
# GTP-U 封包迴圈、整台主機掉包），後續量測全是壞資料，卻沒有任何訊號。這裡在場景開始
# 時記下本機所有 RAN 容器的 (RestartCount, StartedAt)，之後定期比對；不一樣、或容器
# 不在 running 狀態，就是崩潰。只涵蓋「本機」容器（PC2/PC3 各自跑自己的 process）。

_GUARD_NAME_RE = re.compile(r"^rfsim5g-(end-ue|iab-mt|iab-du)-\d+$")


class ScenarioCrash(RuntimeError):
    """場景期間偵測到 RAN 容器崩潰/重啟，本次場景（與其量測）視為無效。"""


class CrashGuard:
    def __init__(self, abort: bool) -> None:
        self.abort = abort
        self.base: dict[str, tuple[str, str]] = {}
        self.crashed: dict[str, str] = {}

    @staticmethod
    def _snapshot() -> dict[str, tuple[str, str, str]]:
        try:
            names = subprocess.run(
                ["docker", "ps", "-a", "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=15,
            ).stdout.split()
            names = [n for n in names if _GUARD_NAME_RE.match(n)]
            if not names:
                return {}
            out = subprocess.run(
                ["docker", "inspect", "--format",
                 "{{.Name}}|{{.RestartCount}}|{{.State.StartedAt}}|{{.State.Status}}", *names],
                capture_output=True, text=True, timeout=30,
            ).stdout
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return {}
        snap: dict[str, tuple[str, str, str]] = {}
        for line in out.splitlines():
            parts = line.strip().lstrip("/").split("|")
            if len(parts) == 4:
                snap[parts[0]] = (parts[1], parts[2], parts[3])
        return snap

    def arm(self) -> None:
        snap = self._snapshot()
        self.base = {n: (v[0], v[1]) for n, v in snap.items()}
        bad = [n for n, v in snap.items() if v[2] != "running"]
        for n in bad:
            self._mark(n, "場景開始時就不在 running 狀態")
        log.info("CrashGuard 已啟用：監控本機 %d 個 RAN 容器（%s）", len(self.base),
                 "崩潰即中止場景" if self.abort else "崩潰只警告")

    def _mark(self, name: str, why: str) -> None:
        if name not in self.crashed:
            self.crashed[name] = why
            log.error("⚠ CrashGuard：%s %s", name, why)

    def check(self) -> None:
        """比對快照；發現新崩潰時標記，abort 模式下丟出 ScenarioCrash。"""
        if not self.base:
            return
        for name, (rc, started, status) in self._snapshot().items():
            b = self.base.get(name)
            if status != "running":
                self._mark(name, f"狀態為 {status}")
            elif b is not None and (rc, started) != b:
                self._mark(name, f"已被重啟（RestartCount {b[0]}→{rc}）")
        if self.crashed:
            Path(f"/tmp/scenario_invalid_{socket.gethostname()}.txt").write_text(
                "".join(f"{n}: {w}\n" for n, w in self.crashed.items()))
            if self.abort:
                raise ScenarioCrash("、".join(self.crashed))


def run_fixed_scenario(
    scenario_name: str,
    ues: list[UEConfig],
    ctrls: dict[int, ChannelModController],
    duration: int,
    protocol: str = "tcp",
    guard: Optional[CrashGuard] = None,
) -> None:
    """執行固定場景，持續 duration 秒後結束。"""
    scenario_fn = {
        "A": scenario_a,
        "B": scenario_b,
        "C": scenario_c,
    }[scenario_name]

    log.info("=== 場景 %s 開始，持續 %d 秒，protocol=%s ===", scenario_name, duration, protocol)
    configs = scenario_fn(ues, ctrls, protocol)
    # A/B/C 2026-09-28 起改直接送連續 path_loss_db（引用 Scenario T 的 PLOSS_TIERS 常數，
    # 見 scenario_a/b/c docstring），不再是 CQI 查表，raw_path_loss 須為 True（同 Scenario R）。
    apply_scenario_phase(ues, ctrls, configs, raw_path_loss=True)

    # 啟動所有 iperf3（apply_scenario_phase 已處理 BW 變更，這裡補起初次啟動）
    for ue in ues:
        if ue._iperf_proc is None:
            start_iperf_client(ue)

    try:
        end = time.time() + duration
        while time.time() < end:
            time.sleep(min(10, max(0.0, end - time.time())))
            if guard:
                guard.check()
    except KeyboardInterrupt:
        log.info("收到中斷，停止場景")
    finally:
        log.info("=== 場景 %s 結束，清理 iperf3 ===", scenario_name)
        stop_all_iperf(ues)
        for node_id, ctrl in ctrls.items():
            for ue in ues:
                if ue.node_id == node_id:
                    ctrl.reset_channel(ue.ue_id)
        reset_ue_dl_degradation(ues)


def run_dynamic_scenario(
    ues: list[UEConfig],
    ctrls: dict[int, ChannelModController],
    phase_duration: int = 60,
    phase_fn: Callable[[list[UEConfig]], list[tuple]] = scenario_d_random,
    raw_path_loss: bool = False,
    scenario_label: str = "D",
    max_phases: Optional[int] = None,
    epoch: float = 0.0,
    guard: Optional[CrashGuard] = None,
    grid_align: bool = False,
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
        if grid_align and epoch and time.time() < epoch:
            log.info("等待相位原點（%.0f 秒後）開始，兩台主機同時起跑", epoch - time.time())
            time.sleep(epoch - time.time())
        while True:
            phase += 1
            if epoch:
                phase_index = int((time.time() - epoch) // phase_duration)
                configs = phase_fn(ues, phase_index)
            else:
                configs = phase_fn(ues)
            log.info("── 相位 %d ──", phase)
            apply_scenario_phase(ues, ctrls, configs, raw_path_loss=raw_path_loss)
            if guard:
                guard.check()   # 崩潰多半發生在改 UE 端通道的同一秒，套完相位立刻檢查

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
            # grid_align：相位邊界固定在 epoch + k×phase_duration（套用通道的耗時算在相位內），
            # phase_index 才會連號、不跳號，壅塞相位的時間比例才會等於設計值。
            deadline = (epoch + (phase_index + 1) * phase_duration
                        if (grid_align and epoch) else time.time() + phase_duration)
            while time.time() < deadline:
                time.sleep(10)
                now = time.time()
                if guard:
                    guard.check()
                for ue in ues:
                    # ── 容器級別失敗 ──────────────────────────────────────────
                    if ue._iperf_proc is not None and ue._iperf_proc.poll() is not None:
                        log.warning("iperf3 supervisor 退出 %s (rc=%d)，重啟 loop...",
                                    ue.container, ue._iperf_proc.returncode)
                        ue._iperf_proc = None
                        start_iperf_client(ue)
                        continue
                    # ── DL flow watchdog ──────────────────────────────────────
                    # UDP 不適用：UDP 沒有壅塞退讓，rx 長時間不增加代表排程器真的把這個
                    # UE 餓住（正是要量測到的現象），重啟 iperf3 不會讓它分到資源，
                    # 反而會覆寫 iperf3 log 檔（open "w"）、抹掉 measure_stage.py 正在讀的
                    # 取樣資料，人為壓低覆蓋率（2026-09-22 UDP 量測 PC2 單台被強制重啟 17 次）。
                    if ue._iperf_proc is None or ue.protocol == "udp":
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
                if guard:
                    guard.check()
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
        reset_ue_dl_degradation(ues)


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
# 主實驗場景 HS／HSH（熱點＋細胞邊緣的結構化隨機場景）與泛化測試場景 G（2026-10-01）
# =============================================================================
# 離線評估（/home/lindor/pf16_run_20260930/bh_model/rs_design.py）：通用隨機場景（G）的壅塞相位，PF 與上限的平均差距只有
# 約 0.2~1%（TCP），PF 已接近最佳；改進空間只出現在「下游熱點＋relay 帶細胞邊緣 UE」與「同節點重需求 UE＋邊緣 UE」。
# HS 的每個壅塞相位一定包含這兩種現象（relay 與 access 同時壅塞），其餘全部隨機；並含「不該介入」的變體。
# 相位狀態沿用 T 的 11 相位骨架（壅塞相位 2/4/5/7/9），量測分析可直接沿用。
#   熱點 branch（隨機 1 支）：一個 access 節點 2 個好通道 UE 各需求 18~24、另一個 2 個好通道 UE 各需求 10~16；
#     relay 直連 UE：80% 為細胞邊緣（L23/24、需求 4~8，該介入：讓 slot 給 backhaul）、20% 為好通道（不該介入）。
#   （2026-10-01 調參：離線上限壅塞相位平均 +10.5%、中位數 +12.4%；60%／16~22／8~14 時為 +5.3%。）
#   混合 branch（其餘 3 支隨機 1 支）：一個 access 節點＝重需求 UE（L18/22、16~22）＋邊緣 UE（L23/24、4~8）；
#     30% 為 N 類變體（重需求 UE 改需求 6~9，不該遮邊緣 UE）。另一個 access 節點與 relay UE 輕負載。
#   其餘 2 支 branch：背景輕負載（通道依 G 的分布、需求 0.3~1）。混合 branch 的輕節點與 relay UE 也是 0.3~1。
#   正常相位：G 的抽樣，總需求縮放到 HS_NORMAL_TOTAL。
# HSH（異質版）：熱點／混合 branch 依每個 branch 固定的熱度機率抽（HS_BRANCH_HOT_P，依結構性種子排列），其餘同 HS。
HS_L_TIERS: list[tuple[float, float]] = [(3.0, 0.10), (10.0, 0.25), (18.0, 0.25), (22.0, 0.20), (23.0, 0.10), (24.0, 0.10)]
HS_NORMAL_TOTAL: float = 60.0      # 正常相位總需求（donor 上限約 109 的 55%）
G_CONGESTED_TOTAL: float = 115.0   # G 壅塞相位總需求（donor 上限的約 105%）
HS_UE_CAP: float = 25.0
HS_BRANCH_HOT_P: list[float] = [0.55, 0.25, 0.12, 0.08]
HS_VERY_RANGE: tuple[float, float] = (18.0, 24.0)    # 熱點 branch 極重 access 節點的每 UE 需求（PB 實測配置 22）
HS_HEAVY_RANGE: tuple[float, float] = (10.0, 14.0)    # 熱點 branch 中等 access 節點的每 UE 需求（PB 實測配置 12）
HS_BG_RANGE: tuple[float, float] = (0.3, 1.0)        # 非熱點 branch 的每 UE 需求（PB 實測配置 1）
HS_P_RELAY_EDGE: float = 0.8       # 熱點 branch 的 relay UE 為細胞邊緣 L24（該介入）的機率；否則 L10（不該遮）
HS_RELAY_UE_RANGE: tuple[float, float] = (6.0, 10.0)  # 熱點 branch relay UE 的需求（PB 實測配置 8）
# 2026-10-03 HS 第四版：每個壅塞相位另在一個非熱點 branch 放一個「混合 access 節點」（場景 PA 平台實測：M 類遮壞 UE +12～14%、
# N 類遮了 −17～−25%），讓 access 節點也有依狀態而定的決策。M 類機率 HS_P_ACCESS_M，否則 N 類；哪個 UE 是好 UE 隨機。
HS_P_ACCESS_M: float = 0.6
HS_MIX_M_GOOD_RANGE: tuple[float, float] = (20.0, 24.0)   # M 類好 UE（L22）需求（PA 實測配置 22）
HS_MIX_N_GOOD_RANGE: tuple[float, float] = (7.0, 9.0)     # N 類好 UE（L22）需求（PA 實測配置 8）
HS_MIX_BAD_RANGE: tuple[float, float] = (5.0, 7.0)        # 壞 UE（L24）需求（PA 實測配置 6）
# HS 第五版（2026-10-04，HS5＝量測、HSX5＝訓練）：混合 access 節點的好 UE 改為好通道 L10（每個 slot 送得多，遮壞 UE 讓出的 slot 價值高）、
# 壞 UE 需求提高到 10～12（PF 下會佔走較多 slot），擴大 access 層的改善空間；量測放 2 個混合節點、訓練放 3 個。
HS5_MIX_GOOD_L: float = 10.0
HS5_MIX_M_GOOD_RANGE: tuple[float, float] = (20.0, 24.0)
HS5_MIX_N_GOOD_RANGE: tuple[float, float] = (6.0, 8.0)
HS5_MIX_BAD_RANGE: tuple[float, float] = (10.0, 12.0)
# HSB（HS 平衡版，2026-10-04）：結構同 HS 第四版（PB 熱點＋1 個混合 access 節點），但需求縮小，讓壅塞總需求（約 95）落在
# 平台容量（CPU 限制約 100～108 sim Mbps）以內——第四版約 115，超過容量，排程改善被平台上限壓縮（同 seed 規則增益第三版 +9、第四版 +5～7）。
HSB_RANGES: dict[str, tuple[float, float]] = {
    "very": (15.0, 19.0), "heavy": (8.0, 11.0), "relay_ue": (6.0, 10.0),
    "mix_m_good": (14.0, 17.0), "mix_n_good": (6.0, 8.0), "mix_bad": (5.0, 7.0),
}
# HSC（2026-10-04）：熱點 branch 完全同 HS 第三版（relay backhaul 瓶頸，規則 +13%），混合 access 節點保留但需求縮小
# （M 類好 UE 12～14＋壞 UE 5～7、N 類好 UE 5～6），只取代一個非熱點 branch 的背景負載，總需求只比第三版多約 15。
HSC_RANGES: dict[str, tuple[float, float]] = {"mix_m_good": (12.0, 14.0), "mix_n_good": (5.0, 6.0), "mix_bad": (5.0, 7.0)}
# HSD（2026-10-04）：第三版（PF 約 70，熱點 relay backhaul 是局部瓶頸）的熱點 branch 中，把「中等」access 節點改成混合節點
# （M 類 60%：好 UE L22 需求 12～14＋壞 UE L24 需求 5～7；N 類：好 UE 需求 4～6），其他 branch 維持背景輕負載、不加混合節點。
# 理由：第四版／HSB／HSC 的 PF 都在 81～83（平台總吞吐量天花板附近），排程沒有空間；access 決策要放在被卡住的熱點 branch 裡。
HSD_MIX = {"m_good": (12.0, 14.0), "n_good": (4.0, 6.0), "bad": (5.0, 7.0)}
# HSE（2026-10-04）：HSD（熱點 branch＝relay backhaul 瓶頸，含熱點內混合節點）＋另一個非熱點 branch 放一個小的混合 access 節點
# （瓶頸在 access 自己的無線端：M 類好 UE L22 需求 9～11＋壞 UE L24 4～6；N 類好 UE 3～4），熱點極重節點調低到 15～19。
# HSD 實測：只有 relay 介入 +23%，但熱點內的 access 遮罩有害（瓶頸在 backhaul）；access 的正向決策要放在 backhaul 沒卡住的分支。
# 2026-10-04 參數修正（HSE v2）：v1 平台實測 PF 下熱點外好 UE 已拿到需求的約 99%（seed 1041／20260930 逐相位），access 沒有缺口。
# 改為：熱點外混合節點回到 PA 規模（M 類好 UE 20～24、壞 UE 5～7、N 類好 UE 7～9）、拿掉熱點內混合節點（遮了有害），
# 熱點 branch 減量（極重 11～13、中等 7～9）把壅塞總需求維持在約 89。v1 的範圍保留在 HSE_V1_RANGES。
HSE_V1_RANGES: dict = {"hsd": True, "very": (15.0, 19.0), "mix_m_good": (9.0, 11.0), "mix_n_good": (3.0, 4.0), "mix_bad": (4.0, 6.0)}
# HSE v3（2026-10-04）：v2 把熱點 branch 減量後熱點下游 access 在 PF 下多已吃飽（relay 沒有空間），且 DU 速度 0.300、CPU 閒置 66～78%，
# 平台仍有餘裕 → 熱點加回第三版的量（極重 15～19、中等 10～14），熱點外混合節點維持 v2。v2 的範圍保留在 HSE_V2_RANGES。
HSE_V2_RANGES: dict = {"very": (11.0, 13.0), "heavy": (7.0, 9.0), "mix_m_good": (20.0, 24.0), "mix_n_good": (7.0, 9.0), "mix_bad": (5.0, 7.0)}
HSE_RANGES: dict = {"very": (15.0, 19.0), "heavy": (10.0, 14.0), "mix_m_good": (20.0, 24.0), "mix_n_good": (7.0, 9.0), "mix_bad": (5.0, 7.0)}
HS_MIX_KEY: int = 100                                     # _rng_for 的鍵（避開 UE 編號 1～24 與 0）
_HS_BRANCH_ORDER: list[int] = sorted(range(4), key=lambda b: _structural_rng(TH_HARDSHIP_SEED + 7, b).random())


def _hs_nodes_of_branch(b: int) -> tuple[int, int, int]:
    """branch b（0~3）＝ relay b+1、access 2b+5、2b+6。"""
    return b + 1, 2 * b + 5, 2 * b + 6


def _hs_ues_of_node(n: int) -> list[int]:
    return [2 * n - 9, 2 * n - 8] if n >= 5 else [15 + 2 * n, 16 + 2 * n]


def _hs_tier(rng: random.Random) -> float:
    return rng.choices([l for l, _ in HS_L_TIERS], [p for _, p in HS_L_TIERS])[0]


def _g_all(seed: int, phase_index: int, total: float) -> dict[int, tuple[float, float]]:
    """通用隨機：每個節點負載權重 Gamma(1)、每 UE 通道依 HS_L_TIERS、需求＝權重×U(0.5,1.5)，整體縮放到 total。"""
    prng = _rng_for(seed, 0, phase_index)
    w = {n: prng.gammavariate(1.0, 1.0) for n in range(1, 13)}
    raw: dict[int, tuple[float, float]] = {}
    for u in range(1, 25):
        r = _rng_for(seed, u, phase_index)
        n = (u - 1) // 2 + 5 if u <= 16 else (u - 17) // 2 + 1
        raw[u] = (_hs_tier(r), w[n] * r.uniform(0.5, 1.5))
    scale = total / sum(d for _, d in raw.values())
    return {u: (L, min(HS_UE_CAP, d * scale)) for u, (L, d) in raw.items()}


def _hs_all(seed: int, phase_index: int, hetero: bool, n_mix: int = 1, v5: bool = False,
            rng_over: Optional[dict] = None) -> tuple[dict[int, tuple[float, float]], str]:
    """
    HS／HSH 壅塞相位的全部 24 個 UE 配置與一行描述（給 log）。

    2026-10-02 改版：每個壅塞相位＝一個「PB 結構」熱點 branch（平台實測 PF 74 → 遮 relay 邊緣 UE 84，+14.5%；
    動態規則 RULE_KIND=dyn 83，+12%），其他三個 branch 輕負載。熱點 relay 的兩個直連 UE 以 HS_P_RELAY_EDGE 機率在細胞邊緣
    （L24，該讓 slot 給 backhaul），否則好通道（L10，不該遮）；下游一個 access 節點極重、一個中等，全部 L10。
    數值在 PB 實測配置（relay UE 8、下游 12／22）附近抽樣。原本的「混合 access 節點」結構平台實測沒有增益（好 UE 在 PF 下已被滿足），已移除。
    """
    prng = _rng_for(seed, 0, phase_index)
    branches = list(range(4))
    if hetero:
        p = {_HS_BRANCH_ORDER[i]: HS_BRANCH_HOT_P[i] for i in range(4)}
        hot = prng.choices(branches, [p[b] for b in branches])[0]
    else:
        hot = prng.choice(branches)
    relay_edge = prng.random() < HS_P_RELAY_EDGE
    cfg: dict[int, tuple[float, float]] = {}
    r = lambda u: _rng_for(seed, u, phase_index)
    rl, a1, a2 = _hs_nodes_of_branch(hot)
    very, heavy = (a1, a2) if prng.random() < 0.5 else (a2, a1)
    R = rng_over or {}
    for u in _hs_ues_of_node(very):
        g = r(u); cfg[u] = (10.0, g.uniform(*R.get("very", HS_VERY_RANGE)))
    hsd_desc = ""
    if R.get("hsd"):
        hr = _rng_for(seed, HS_MIX_KEY + 50, phase_index)
        hm = hr.random() < HS_P_ACCESS_M
        hg, hb = _hs_ues_of_node(heavy) if hr.random() < 0.5 else _hs_ues_of_node(heavy)[::-1]
        cfg[hg] = (22.0, hr.uniform(*(HSD_MIX["m_good"] if hm else HSD_MIX["n_good"])))
        cfg[hb] = (24.0, hr.uniform(*HSD_MIX["bad"]))
        hsd_desc = f"混合 access Node{heavy}＝{'M 類→該遮' if hm else 'N 類→不該遮'}（好 UE{hg}、壞 UE{hb}）"
    else:
        for u in _hs_ues_of_node(heavy):
            g = r(u); cfg[u] = (10.0, g.uniform(*R.get("heavy", HS_HEAVY_RANGE)))
    for u in _hs_ues_of_node(rl):
        g = r(u); cfg[u] = ((24.0 if relay_edge else 10.0), g.uniform(*R.get("relay_ue", HS_RELAY_UE_RANGE)))
    for b in branches:
        if b == hot:
            continue
        rb, b1, b2 = _hs_nodes_of_branch(b)
        for u in _hs_ues_of_node(b1) + _hs_ues_of_node(b2) + _hs_ues_of_node(rb):
            g = r(u); cfg[u] = (_hs_tier(g), g.uniform(*HS_BG_RANGE))
    # 混合 access 節點（第四版）：用獨立的亂數流，熱點與背景的抽樣與第三版逐值相同。
    # n_mix＞1（訓練用 HSX，2026-10-04）：每個非熱點 branch 各放一個混合節點（第一個與 HS 逐值相同，其餘用另外的鍵），
    # access 決策狀態約 ×3；量測場景 HS 仍為 n_mix=1。
    mrng = _rng_for(seed, HS_MIX_KEY, phase_index)
    others = [b for b in branches if b != hot]
    mb0 = mrng.choice(others)
    order = [mb0] + [b for b in others if b != mb0]
    mixdesc = []
    for i, mb in enumerate(order[:n_mix]):
        rr = mrng if i == 0 else _rng_for(seed, HS_MIX_KEY + i, phase_index)
        mnode = _hs_nodes_of_branch(mb)[1 + rr.randrange(2)]
        m_type = rr.random() < HS_P_ACCESS_M
        good_u, bad_u = _hs_ues_of_node(mnode) if rr.random() < 0.5 else _hs_ues_of_node(mnode)[::-1]
        if v5:
            cfg[good_u] = (HS5_MIX_GOOD_L, rr.uniform(*(HS5_MIX_M_GOOD_RANGE if m_type else HS5_MIX_N_GOOD_RANGE)))
            cfg[bad_u] = (24.0, rr.uniform(*HS5_MIX_BAD_RANGE))
        else:
            cfg[good_u] = (22.0, rr.uniform(*(R.get("mix_m_good", HS_MIX_M_GOOD_RANGE) if m_type else R.get("mix_n_good", HS_MIX_N_GOOD_RANGE))))
            cfg[bad_u] = (24.0, rr.uniform(*R.get("mix_bad", HS_MIX_BAD_RANGE)))
        mixdesc.append(f"混合 access Node{mnode}＝{'M 類→該遮' if m_type else 'N 類→不該遮'}（好 UE{good_u}、壞 UE{bad_u}）")
    if hsd_desc:
        mixdesc = [hsd_desc] + (mixdesc if n_mix > 0 else [])
    desc = (f"熱點 branch={hot + 1}（PB 結構；relay UE {'邊緣→該介入' if relay_edge else '好通道→不該介入'}）；" + "；".join(mixdesc))
    return {u: (L, min(HS_UE_CAP, d)) for u, (L, d) in cfg.items()}, desc


def scenario_hs(name: str, ues: list[UEConfig], seed: int, phase_index: int, protocol: str = "tcp") -> list[tuple[float, float, str]]:
    """HS／HSH／G：對 ues（任意主機子集，含 relay UE）回傳配置；每台主機都會印相位狀態（含 PC1）。"""
    congested = t_phase_congested(phase_index)
    if not congested:
        allc = _g_all(seed, phase_index, HS_NORMAL_TOTAL); desc = f"正常相位（通用隨機，總需求 {HS_NORMAL_TOTAL:.0f}）"
    elif name == "G":
        allc = _g_all(seed, phase_index, G_CONGESTED_TOTAL); desc = f"壅塞相位（通用隨機，總需求 {G_CONGESTED_TOTAL:.0f}）"
    else:
        allc, desc = _hs_all(seed, phase_index, hetero=(name == "HSH"),
                             n_mix={"HSX": 3, "HSX5": 3, "HS5": 2, "HSD": 0}.get(name, 1), v5=name in ("HS5", "HSX5"),
                             rng_over={"HSB": HSB_RANGES, "HSC": HSC_RANGES, "HSD": {"hsd": True}, "HSE": HSE_RANGES}.get(name))
    log.info("Scenario %s 相位狀態：%s（phase_index=%d，週期內第 %d/%d 個）%s", name, "壅塞" if congested else "正常",
             phase_index, phase_index % T_CYCLE_PHASES, T_CYCLE_PHASES, desc)
    return [(allc[u.global_id][0], allc[u.global_id][1], protocol) for u in ues]


# =============================================================================
# relay 直連 UE（UE17~24，2026-10-01）與場景配置的單一入口
# =============================================================================
# relay 直連 UE 的角色（需求、下行通道惡化）尚未設計：在 relay_ue_configs() 定義之前，所有場景對 relay UE 一律閒置
# （需求 0 → iperf3 不啟動），下行通道設為 RELAY_UE_DEFAULT_L，16 個 access UE 的行為與加入 relay UE 之前完全相同。
# 場景執行（run_dynamic_scenario）與量測分析（iab/analyze_stage.py）都經由 scenario_configs() 取每相位配置，
# 目標值只在這裡定義一次。

RELAY_UE_IDS: frozenset[int] = frozenset(range(17, 25))
RELAY_UE_DEFAULT_L: float = 10.0   # 場景損耗指標 L（下行 MCS≈28）；relay UE 閒置時的通道


def is_relay_ue(ue: UEConfig) -> bool:
    return ue.global_id in RELAY_UE_IDS


def relay_ue_configs(
    scenario: str, ues: list[UEConfig], phase_index: int, seed: Optional[int] = None, protocol: str = "tcp"
) -> list[tuple[float, float, str]]:
    """
    relay 直連 UE 每相位的 (L, 需求 sim Mbps, 協定)，順序同 ues。

    目前（2026-10-01）只有試驗場景 PB／PU 定義了 relay UE（RELAY_PILOT_CFGS）；其他場景一律閒置，下行通道 RELAY_UE_DEFAULT_L。
    之後要讓 relay UE 產生流量或惡化它的下行路徑時只改這裡：L 經 UE 端 chanmod 套用（set_ue_dl_degradation，
    relay UE 用的是 nrue.uicc.chanmod.conf，與 access UE 相同），需求 > 0 就會啟動 iperf3。
    """
    if scenario in RELAY_PILOT_CFGS:
        return scenario_relay_pilot(scenario, ues, phase_index, protocol)
    return [(RELAY_UE_DEFAULT_L, 0.0, protocol) for _ in ues]


# relay 直連 UE 平台試驗（2026-10-01，固定配置、每相位相同，同場景 P 的用法）：只壓 branch 1（relay Node1、access Node5/6），
# 其他 access UE 輕負載（L=10、需求 1），其他 relay UE 閒置。離線模型（/home/lindor/pf16_run_20260930/bh_model/relay_scan2.py）：
#   PB（backhaul 與 relay 自己 UE 的取捨）：relay 兩個壞通道 UE＋下游極重負載 → relay 層上限 TCP +12.4／UDP +13.5 sim Mbps
#   PU（relay 自己 UE 之間）：relay 一個 L18 重需求＋一個 L24 UE，下游重負載 → relay 層上限 +7.5（TCP／UDP）
RELAY_PILOT_CFGS: dict[str, dict[int, tuple[float, float]]] = {
    "PB": {1: (10.0, 12.0), 2: (10.0, 12.0), 3: (10.0, 22.0), 4: (10.0, 22.0), 17: (24.0, 8.0), 18: (24.0, 8.0)},
    "PU": {1: (10.0, 12.0), 2: (10.0, 12.0), 3: (10.0, 12.0), 4: (10.0, 12.0), 17: (18.0, 22.0), 18: (24.0, 4.0)},
    # PA（2026-10-03，access 層決策試驗）：Node5／Node9＝M 類（好 UE L22 需求 22＋壞 UE L24 需求 6，該遮壞 UE；同場景 P），
    # Node7／Node11＝N 類（好 UE L22 需求 8＋壞 UE L24 需求 6，好 UE 本來就吃得飽，遮了只會虧；同 TM 的 N22）；其他 access UE 輕負載、relay UE 閒置。
    "PA": {1: (22.0, 22.0), 2: (24.0, 6.0), 9: (22.0, 22.0), 10: (24.0, 6.0),
           5: (22.0, 8.0), 6: (24.0, 6.0), 13: (22.0, 8.0), 14: (24.0, 6.0)},
    # SW1／SW2（2026-10-03，Σlog 遮罩強度掃描）：branch 1＝PB 結構（relay UE17/18 L24，下游 Node5 2×需求 12、Node6 2×需求 22），
    # Node9＝混合 access 節點（好 UE9 L22、壞 UE10 L24 需求 6）。兩組只差需求：SW1 relay UE 4／好 UE9 22，SW2 relay UE 10／好 UE9 14。
    # 用固定遮罩檔位掃描，看 Σlog（relay 子樹 UE17,18,1~4；access 子樹 UE9,10）的最佳檔位是否隨需求改變。
    "SW1": {17: (24.0, 4.0), 18: (24.0, 4.0), 1: (10.0, 12.0), 2: (10.0, 12.0), 3: (10.0, 22.0), 4: (10.0, 22.0),
            9: (22.0, 22.0), 10: (24.0, 6.0)},
    "SW2": {17: (24.0, 10.0), 18: (24.0, 10.0), 1: (10.0, 12.0), 2: (10.0, 12.0), 3: (10.0, 22.0), 4: (10.0, 22.0),
            9: (22.0, 14.0), 10: (24.0, 6.0)},
    # SW3（2026-10-03，遮罩位置敏感度分析）：relay UE 需求 10（同 SW2）＋access 好 UE9 需求 22（同 SW1），兩層的遮罩效果都較明顯。
    "SW3": {17: (24.0, 10.0), 18: (24.0, 10.0), 1: (10.0, 12.0), 2: (10.0, 12.0), 3: (10.0, 22.0), 4: (10.0, 22.0),
            9: (22.0, 22.0), 10: (24.0, 6.0)},
}


def scenario_relay_pilot(name: str, ues: list[UEConfig], phase_index: int, protocol: str = "tcp") -> list[tuple[float, float, str]]:
    """relay 直連 UE 平台試驗 PB／PU：固定配置，每個相位相同（對 access UE 與 relay UE 都適用）。"""
    if any(not is_relay_ue(u) for u in ues):
        log.info("Scenario %s 相位狀態：壅塞（phase_index=%d，固定配置）", name, phase_index)
    cfg = RELAY_PILOT_CFGS[name]
    out = []
    for ue in ues:
        default = (RELAY_UE_DEFAULT_L, 0.0) if is_relay_ue(ue) else (10.0, 1.0)
        L, bw = cfg.get(ue.global_id, default)
        out.append((L, bw, protocol))
    return out


# access UE（UE1~16）的場景函數；簽名統一為 (ues, phase_index, seed, protocol)
_ACCESS_SCENARIO_FNS: dict[str, Callable[..., list[tuple]]] = {
    "T":   lambda u, pi, seed, p: scenario_t_tiered(u, pi, protocol=p),
    "TR":  lambda u, pi, seed, p: scenario_t_random(u, seed, pi, protocol=p),
    "TH":  lambda u, pi, seed, p: scenario_th_heterogeneous(u, seed, pi, protocol=p),
    "TM":  lambda u, pi, seed, p: scenario_tm_mixed(u, pi, protocol=p),
    "TMR": lambda u, pi, seed, p: scenario_tm_random(u, seed, pi, protocol=p),
    "TMH": lambda u, pi, seed, p: scenario_tmh_heterogeneous(u, seed, pi, protocol=p),
    "P":   lambda u, pi, seed, p: scenario_p_pilot(u, pi, protocol=p),
    "PB":  lambda u, pi, seed, p: scenario_relay_pilot("PB", u, pi, protocol=p),
    "PU":  lambda u, pi, seed, p: scenario_relay_pilot("PU", u, pi, protocol=p),
    "PA":  lambda u, pi, seed, p: scenario_relay_pilot("PA", u, pi, protocol=p),
    "SW1": lambda u, pi, seed, p: scenario_relay_pilot("SW1", u, pi, protocol=p),
    "SW2": lambda u, pi, seed, p: scenario_relay_pilot("SW2", u, pi, protocol=p),
    "SW3": lambda u, pi, seed, p: scenario_relay_pilot("SW3", u, pi, protocol=p),
    "R":   lambda u, pi, seed, p: scenario_r_realistic(u, seed, pi, protocol=p),
}


def _phase_congested(scenario: str, seed: Optional[int], phase_index: int) -> Optional[bool]:
    """場景在這個相位是否為壅塞相位（R 沒有狀態標籤，回傳 None）。"""
    if scenario in ("T", "TH", "TM", "TMH"):
        return t_phase_congested(phase_index)
    if scenario in ("TR", "TMR"):
        return t_random_phase_congested(seed, phase_index)
    if scenario in ("P", "PB", "PU", "PA", "SW1", "SW2", "SW3"):
        return True
    return None


def scenario_configs(
    scenario: str, ues: list[UEConfig], phase_index: int, seed: Optional[int] = None, protocol: Optional[str] = "tcp"
) -> list[tuple]:
    """
    場景在 phase_index 對 ues（任意主機的 UE 子集，可含 relay UE）的配置，順序同 ues。
    access UE 交給原場景函數、relay UE 交給 relay_ue_configs()。本機只有 relay UE（PC1）時，access 場景函數不會被
    呼叫、不會印出「相位狀態」log；這裡補印同樣格式的一行，量測分析才能對齊相位。
    protocol=None 只用於 Scenario R（TCP/UDP 混合）；relay UE 此時用 tcp。
    """
    if scenario in ("HS", "HSH", "G", "HSX", "HS5", "HSX5", "HSB", "HSC", "HSD", "HSE"):   # 完整場景：access UE 與 relay UE 一起抽樣（HSX＝訓練用，3 個混合 access 節點）
        return scenario_hs(scenario, ues, seed, phase_index, protocol or "tcp")
    access = [u for u in ues if not is_relay_ue(u)]
    relay = [u for u in ues if is_relay_ue(u)]
    acc = _ACCESS_SCENARIO_FNS[scenario](access, phase_index, seed, protocol) if access else []
    if relay and not access:
        cong = _phase_congested(scenario, seed, phase_index)
        if cong is not None:
            log.info("Scenario %s 相位狀態：%s（phase_index=%d，本機只有 relay 直連 UE）",
                     scenario, "壅塞" if cong else "正常", phase_index)
    rel = relay_ue_configs(scenario, relay, phase_index, seed, protocol or "tcp") if relay else []
    it_a, it_r = iter(acc), iter(rel)
    return [next(it_r) if is_relay_ue(u) else next(it_a) for u in ues]


# =============================================================================
# 入口
# =============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="IAB 流量+路徑損耗場景控制器（三主機 12-node/24-UE 版：UE17~24 為 relay 直連 UE）"
    )
    parser.add_argument(
        "--host", choices=["pc1", "pc2", "pc3"], default=None,
        help="只控制該主機負責的 UE/Node 子集（見 CLAUDE.md HOST_OF_NODE）。"
             "未指定時嘗試從 hostname 猜測，猜不出來則控制全部 24 個 UE（單機測試用）。",
    )
    parser.add_argument(
        "--scenario", choices=["A", "B", "C", "D", "R", "T", "TR", "TH", "P", "PB", "PU", "PA", "SW1", "SW2", "SW3", "TM", "TMR", "TMH", "HS", "HSH", "G", "HSX", "HS5", "HSX5", "HSB", "HSC", "HSD", "HSE"], default="R",
        help="場景選擇：A=CQI差異, B=流量不均, C=最差公平性, D=均勻隨機（已被R取代）, "
             "R=真實隨機（預設，area-uniform path_loss + 持久化 profile + 協定混合）, "
             "T=分層交叉（低/中/高流量 × 低/中/高路徑損耗 3x3，2026-09-18 新增，"
             "見 scenario_t_tiered() 說明）, "
             "TR=隨機化的兩狀態 T（訓練專用：同 T 的檔位與 45%% 壅塞比例，但相位排列與每 UE 組合由 --seed 隨機打散）",
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
    parser.add_argument(
        "--protocol", choices=["tcp", "udp"], default=None,
        help="全部 UE 統一用這個協定（適用 T/R/A/B/C）。未指定時各場景維持原本行為："
             "T/A/B/C=tcp、R=TCP/UDP 混合（P_UDP）。UDP 沒有壅塞視窗，量到的吞吐量"
             "直接反映排程器實際分配的容量；TCP 的達成吞吐量會被視窗/RTT 乘積卡住，"
             "見 scenario_t_tiered() 說明。UDP 模式下 DL frozen watchdog 會略過該 UE。",
    )
    parser.add_argument(
        "--no-dl-degrade", action="store_true",
        help="不對 UE 端下行通道加損耗（回到只惡化上行的舊行為；等同 SCENARIO_DL_DEGRADE=0）",
    )
    parser.add_argument(
        "--phase-origin", type=float, default=None,
        help="Scenario T/R：相位原點（Unix 秒）。兩台主機傳同一個值（例如 `date +%%s` + 10），相位 0 從此刻開始、"
             "每相位剛好 --phase-duration 秒、編號連號；未指定則沿用固定 epoch（相位可能跳號）。",
    )
    parser.add_argument(
        "--congested-only", action="store_true",
        help="驗證用（2026-10-03）：只跑 T 骨架的壅塞相位——第 k 個實際相位對應週期內第 k 個壅塞相位的 phase_index"
             "（2,4,5,7,9,13,...），log 印出的是對應後的 phase_index，analyze_stage.py 不需修改。正式量測不要用。",
    )
    parser.add_argument(
        "--on-crash", choices=["abort", "warn"], default=None,
        help="場景期間本機 UE/MT/DU 容器崩潰重啟時的處理：abort=清理後以 exit code 2 結束"
             "（本次量測無效）、warn=只記錄錯誤繼續跑。預設：有限相位/固定場景（量測）=abort，"
             "無限訓練=warn（訓練由 training_watchdog.sh 負責復原）。偵測到時也會寫 "
             "/tmp/scenario_invalid_<hostname>.txt。",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    global DL_DEGRADE_ENABLED
    if args.no_dl_degrade:
        DL_DEGRADE_ENABLED = False
    log.info("UE 端下行通道惡化：%s（場景損耗指標 0~%.0f → DEGRADE_PATH，終點 ploss=%.0f noise=%.0f）",
             "啟用" if DL_DEGRADE_ENABLED else "關閉", PATHLOSS_SAFE_MAX_DB, DEGRADE_PATH[-1][1], DEGRADE_PATH[-1][2])
    host = args.host or _my_hostname_role()
    if host:
        log.info("--host=%s：只控制該主機負責的 UE/Node 子集", host)
    else:
        log.warning("未指定 --host 且無法從 hostname 猜測，將控制全部 24 個 UE（單機測試用）")

    if args.calibrate:
        run_calibration(args.node)
        return

    ues = build_ue_list(host)
    ctrls = build_controllers(host)
    if args.scenario in ("A", "B", "C", "D") and any(is_relay_ue(u) for u in ues):
        # 舊固定／均勻隨機場景沒有 relay 直連 UE 的定義，只控制 access UE（relay UE 維持閒置、通道不動）
        ues = [u for u in ues if not is_relay_ue(u)]
        if not ues:
            log.info("場景 %s 不控制 relay 直連 UE，本機（--host=%s）沒有其他 UE，直接結束", args.scenario, host)
            return
    if not ues:
        log.error("此 --host=%s 沒有任何 UE，請確認 HOST_OF_NODE/NODE_CONFIG 設定", host)
        sys.exit(1)

    if not args.no_wait:
        wait_for_ue_interfaces(ues)

    # 有限相位（量測）一律 abort；2026-10-01 前這裡只列 T/TR/R，TH/TM/TMH/P 的量測崩潰時只會警告
    finite = args.scenario in ("A", "B", "C") or (args.scenario != "D" and args.num_phases is not None)
    on_crash = args.on_crash or ("abort" if finite else "warn")
    guard = CrashGuard(abort=(on_crash == "abort"))
    guard.arm()
    try:
        _run_selected(args, ues, ctrls, guard)
    except ScenarioCrash as exc:
        log.error("場景因 RAN 容器崩潰而中止（%s）：本次量測無效，需乾淨重啟後重跑", exc)
        sys.exit(2)


def _congested_phase_index(k: int) -> int:
    """第 k 個（從 0 起）壅塞相位在 T 骨架中的 phase_index（--congested-only 用）。"""
    cong = sorted(T_CONGESTED_PHASES)
    return (k // len(cong)) * T_CYCLE_PHASES + cong[k % len(cong)]


def _run_selected(args: argparse.Namespace, ues: list[UEConfig],
                  ctrls: dict[int, ChannelModController], guard: CrashGuard) -> None:
    if args.scenario == "D":
        run_dynamic_scenario(ues, ctrls, phase_duration=args.phase_duration, guard=guard)
        return
    if args.scenario in ("A", "B", "C"):
        run_fixed_scenario(args.scenario, ues, ctrls, duration=args.duration,
                           protocol=args.protocol or "tcp", guard=guard)
        return

    # 動態場景（T/TR/TH/TM/TMR/TMH/P/R）：每相位配置一律經 scenario_configs()（access UE＋relay 直連 UE）
    import secrets
    scn = args.scenario
    seed: Optional[int] = None
    if scn in ("TR", "TH", "TMR", "TMH", "R", "HS", "HSH", "G", "HSX", "HS5", "HSX5", "HSB", "HSC", "HSD", "HSE"):
        seed = args.seed if args.seed is not None else secrets.randbits(32)
    if scn == "R":
        proto: Optional[str] = args.protocol   # None＝TCP/UDP 混合（P_UDP）
        log.info("Scenario R seed=%d（PC1/PC2/PC3 三邊用同一個 --seed %d 才會場景一致）protocol=%s",
                 seed, seed, args.protocol or "mix")
        assign_ue_profiles(ues, seed)
    else:
        proto = args.protocol or "tcp"
        if seed is not None:
            log.info("Scenario %s seed=%d protocol=%s（各主機必須用同一個 --seed；建議同時給 --phase-origin）",
                     scn, seed, proto)
        elif scn == "TM":
            log.info("Scenario TM protocol=%s（混合通道壅塞候選基準）", proto)
        elif scn == "P":
            log.info("Scenario P protocol=%s（混合通道壅塞試驗，固定配置）", proto)
        else:
            log.info("Scenario %s protocol=%s", scn, proto)
    # 固定 epoch 1700000000（2023-11-14）只是絕對時間錨點：各主機不需通訊就落在同一個 phase_index
    run_dynamic_scenario(
        ues, ctrls,
        phase_duration=args.phase_duration,
        phase_fn=lambda u, phase_index: scenario_configs(
            scn, u, _congested_phase_index(phase_index) if args.congested_only else phase_index, seed, proto),
        raw_path_loss=True,
        scenario_label=scn,
        max_phases=args.num_phases,
        epoch=(args.phase_origin if args.phase_origin else 1700000000.0),
        guard=guard,
        grid_align=bool(args.phase_origin),
    )


if __name__ == "__main__":
    main()
