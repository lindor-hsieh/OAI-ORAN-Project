#!/usr/bin/env python3
"""
run_traffic.py — 論文 PRB 實驗固定流量序列（PC2 執行）

設計原則：
  - 所有 5 種 PRB 策略跑「完全相同」的流量序列，確保比較公平
  - 序列涵蓋 balanced / BH-heavy / AC-heavy / mixed / overload 等場景
  - 讓 Adaptive xApp 有機會展示動態調整效果，固定策略在某些 epoch 必然劣化
  - 使用 iperf3 UDP downlink (-R)，sum_received 在 client 端直接可讀

拓撲提醒：
  UE1,2 → IAB Node 3 → IAB Node 1 (relay) → Donor
  UE3,4 → IAB Node 4 → IAB Node 1 (relay) → Donor
  UE5,6 → IAB Node 5 → IAB Node 2 (relay) → Donor

流量序列（6 epochs × 30s，共 195s）：
  Epoch 1  balanced  : 所有 UE 20M            → 基準對照
  Epoch 2  BH-heavy  : UE1-4=50M, UE5-6=5M   → Node1 relay 瓶頸
  Epoch 3  AC-heavy  : UE1-4=5M,  UE5-6=50M  → Node2 relay 壓力
  Epoch 4  mixed     : 各 UE 不同負載          → 真實感流量
  Epoch 5  overload  : 所有 UE 35M            → 全面壓力測試
  Epoch 6  recovery  : 所有 UE 20M            → 恢復基準

用法（由 run_experiment_pc2.sh 呼叫）：
  python3 run_traffic.py <policy> <results_dir>
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

# ─── 設定 ─────────────────────────────────────────────────────────────────────
SERVER_IP   = "192.168.72.135"
PORT_BASE   = 5201          # UE i → 5200+i
EPOCH_SECS  = 30            # 每個 epoch 持續秒數
EPOCH_GAP   = 5             # epoch 間等待 iperf3 server 重啟
PING_INT    = 0.2           # ping 間隔 (s)

UE_CONTAINERS = [
    "rfsim5g-end-ue-1",
    "rfsim5g-end-ue-2",
    "rfsim5g-end-ue-3",
    "rfsim5g-end-ue-4",
    "rfsim5g-end-ue-5",
    "rfsim5g-end-ue-6",
]

# 固定流量序列：所有 policy 完全相同
# 系統容量約 30 Mbps / 6 UE = 5 Mbps/UE，流量設計在 50%~150% 容量範圍內
# 格式：(epoch_name, [bw_ue1, bw_ue2, bw_ue3, bw_ue4, bw_ue5, bw_ue6])
#
# 拓撲提醒：
#   UE1,2 → Node3 → Node1 relay → Donor
#   UE3,4 → Node4 → Node1 relay → Donor
#   UE5,6 → Node5 → Node2 relay → Donor
EPOCHS = [
    # 均衡：各 UE 5M，總 30M ≈ 系統容量 → 基準對照
    ("balanced",   ["5M",  "5M",  "5M",  "5M",  "5M",  "5M"]),
    # BH-heavy：Node1 relay 承載 UE1-4 各 10M = 40M，Node2 relay 只有 2M×2 = 4M
    ("bh_heavy",   ["10M", "10M", "10M", "10M",  "2M",  "2M"]),
    # AC-heavy：反轉，Node2 relay 承載重，Node1 relay 輕
    ("ac_heavy",   [ "2M",  "2M",  "2M",  "2M", "10M", "10M"]),
    # Mixed：同 relay 內兩 UE 負載差異大，測試公平性
    ("mixed",      [ "8M",  "2M",  "8M",  "2M",  "6M",  "3M"]),
    # Overload：全員 8M，總 48M ≈ 1.6× 容量 → 測試壓力下的策略差異
    ("overload",   [ "8M",  "8M",  "8M",  "8M",  "8M",  "8M"]),
    # Recovery：回到均衡，驗證策略能否恢復
    ("recovery",   ["5M",  "5M",  "5M",  "5M",  "5M",  "5M"]),
]

TOTAL_SECS = EPOCH_SECS * len(EPOCHS) + EPOCH_GAP * (len(EPOCHS) - 1)
PING_COUNT  = int(TOTAL_SECS / PING_INT)

# ─── Helpers ──────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[PC2 {ts}] {msg}", flush=True)


def docker_exec(container: str, cmd: list[str], **kwargs) -> subprocess.Popen:
    return subprocess.Popen(
        ["docker", "exec", container] + cmd,
        **kwargs
    )


def ensure_routing() -> None:
    """確保 UE 有 192.168.72.128/26 的路由（透過 oaitun_ue1）。"""
    for ue in UE_CONTAINERS:
        subprocess.run(
            ["docker", "exec", "-u", "0", ue,
             "ip", "route", "add", "192.168.72.128/26",
             "dev", "oaitun_ue1"],
            capture_output=True
        )


def check_ue_attached() -> int:
    count = 0
    for ue in UE_CONTAINERS:
        r = subprocess.run(
            ["docker", "exec", ue,
             "ip", "-4", "addr", "show", "oaitun_ue1"],
            capture_output=True, text=True
        )
        if "inet " in r.stdout:
            count += 1
        else:
            log(f"  WARNING: {ue} oaitun_ue1 not found")
    log(f"  {count}/{len(UE_CONTAINERS)} UEs attached")
    return count

# ─── Ping（背景，跑全程） ─────────────────────────────────────────────────────

def start_ping_all(results_dir: Path) -> list[subprocess.Popen]:
    procs = []
    for i, ue in enumerate(UE_CONTAINERS, 1):
        out = open(results_dir / f"ping_ue{i}.txt", "w")
        p = docker_exec(
            ue,
            ["ping", "-I", "oaitun_ue1",
             "-c", str(PING_COUNT),
             "-i", str(PING_INT),
             "-W", "2",
             SERVER_IP],
            stdout=out, stderr=out
        )
        procs.append(p)
    log(f"Ping started: {PING_COUNT} probes/UE  ({TOTAL_SECS}s)")
    return procs

# ─── iperf3（每個 epoch 並發送 6 UE） ────────────────────────────────────────

def run_epoch(epoch_idx: int, name: str, bitrates: list[str],
              results_dir: Path) -> None:
    log(f"  Epoch {epoch_idx} [{name}] {EPOCH_SECS}s  "
        f"UE1-4={bitrates[0]}/{bitrates[2]}  UE5-6={bitrates[4]}/{bitrates[5]}")

    procs = []
    out_files = []
    for i, (ue, bw) in enumerate(zip(UE_CONTAINERS, bitrates)):
        port = PORT_BASE + i
        out_path = results_dir / f"iperf_ue{i+1}_ep{epoch_idx}.json"
        out_f = open(out_path, "w")
        out_files.append(out_f)
        p = docker_exec(
            ue,
            ["iperf3",
             "-c", SERVER_IP, "-p", str(port),
             "-u", "-b", bw,
             "-t", str(EPOCH_SECS),
             "-R",   # downlink: server → UE；sum_received 在 client 端直接有效
             "-J"],
            stdout=out_f, stderr=subprocess.DEVNULL
        )
        procs.append(p)

    for p in procs:
        p.wait()
    for f in out_files:
        f.close()

    log(f"  Epoch {epoch_idx} done")


# ─── CPU 監控（背景） ─────────────────────────────────────────────────────────

ACCESS_DUS = ["rfsim5g-iab-du-3", "rfsim5g-iab-du-4", "rfsim5g-iab-du-5"]
_cpu_stop = threading.Event()

def _cpu_worker(outfile: Path) -> None:
    with open(outfile, "a") as f:
        while not _cpu_stop.is_set():
            ts = time.strftime("%H:%M:%S")
            f.write(f"=== {ts} ===\n")
            r = subprocess.run(
                ["docker", "stats", "--no-stream", "--format",
                 "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"]
                + ACCESS_DUS,
                capture_output=True, text=True
            )
            f.write(r.stdout)
            f.flush()
            _cpu_stop.wait(5)


def start_cpu_monitor(results_dir: Path) -> threading.Thread:
    t = threading.Thread(
        target=_cpu_worker,
        args=(results_dir / "cpu_access.log",),
        daemon=True
    )
    t.start()
    return t

# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <policy> <results_dir>")
        sys.exit(1)

    policy      = sys.argv[1]
    results_dir = Path(sys.argv[2])
    results_dir.mkdir(parents=True, exist_ok=True)

    log(f"Policy={policy}  Results={results_dir}")
    log(f"Sequence: {len(EPOCHS)} epochs × {EPOCH_SECS}s = {TOTAL_SECS}s total")

    ensure_routing()
    check_ue_attached()

    # 啟動 CPU 監控
    cpu_thread = start_cpu_monitor(results_dir)

    # 啟動 ping（背景，跑全程）
    ping_procs = start_ping_all(results_dir)

    # 依序跑所有 epoch
    for idx, (name, bitrates) in enumerate(EPOCHS, 1):
        run_epoch(idx, name, bitrates, results_dir)
        if idx < len(EPOCHS):
            log(f"  Gap {EPOCH_GAP}s (iperf3 server restart)...")
            time.sleep(EPOCH_GAP)

    # 等待 ping 結束
    log("Waiting for ping to finish...")
    for p in ping_procs:
        p.wait()

    _cpu_stop.set()
    cpu_thread.join(timeout=5)

    log(f"Done. Results in {results_dir}/")
    for f in sorted(results_dir.iterdir()):
        size = f.stat().st_size
        print(f"  {f.name:35s}  {size:>8,} bytes")


if __name__ == "__main__":
    main()
