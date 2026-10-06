#!/usr/bin/env python3
"""relay 直連 UE 承載試驗驅動（2026-10-01）。

每台主機各跑一份（--host pc1/pc2/pc3），只操作本機的 UE：設定下行通道（場景損耗指標 L）、啟動 iperf3 -R、
跑 --duration 秒後停止，輸出每個 UE 去掉前 --warmup 秒後的平均吞吐量（模擬時間 Mbps＝牆鐘 ÷ S）與本機 CPU 閒置率。
UE17~24 是 relay 直連 UE（容器在 PC1，iperf port 5217~5224）。

--cfg：JSON，{global_id: [L, 需求 sim Mbps, "tcp"|"udp"]}；不在表內的本機 UE 不產生流量。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scenarios"))
import traffic_scenario as T  # noqa: E402

RELAY_UES = {u: (u - 15) // 2 for u in range(17, 25)}       # UE → relay node（17,18→1 … 23,24→4）
T.UE_IPERF_PORTS.update({f"rfsim5g-end-ue-{u}": 5200 + u for u in RELAY_UES})
LINE_RE = re.compile(r"\]\s+([\d.]+)-([\d.]+)\s+sec\s+[\d.]+\s+\w?Bytes\s+([\d.]+)\s+([KMG]?)bits/sec")
SCALE = {"": 1e-6, "K": 1e-3, "M": 1.0, "G": 1e3}


def local_ues(host: str) -> list[T.UEConfig]:
    if host == "pc1":
        return [T.UEConfig(container=f"rfsim5g-end-ue-{u}", ue_id=0, node_id=n, global_id=u) for u, n in RELAY_UES.items()]
    return T.build_ue_list(host)


def cpu_sampler(stop: threading.Event, out: list[float], every: float = 5.0) -> None:
    def snap() -> tuple[int, int]:
        v = list(map(int, open("/proc/stat").readline().split()[1:]))
        return v[3] + v[4], sum(v)
    prev = snap()
    while not stop.wait(every):
        cur = snap()
        idle, tot = cur[0] - prev[0], cur[1] - prev[1]
        if tot > 0:
            out.append(100.0 * idle / tot)
        prev = cur


def parse_log(container: str, warmup: float) -> tuple[float, int]:
    p = Path(f"/tmp/iperf_client_{container}.log")
    if not p.exists():
        return 0.0, 0
    vals = []
    for line in p.read_text(errors="ignore").splitlines():
        if "sender" in line or "receiver" in line:
            continue
        m = LINE_RE.search(line)
        if m and float(m.group(1)) >= warmup:
            vals.append(float(m.group(3)) * SCALE[m.group(4)])
    return (sum(vals) / len(vals) if vals else 0.0), len(vals)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True, choices=["pc1", "pc2", "pc3"])
    ap.add_argument("--cfg", required=True)
    ap.add_argument("--duration", type=int, default=120)
    ap.add_argument("--warmup", type=float, default=20.0)
    ap.add_argument("--start-at", type=float, default=0.0, help="epoch 秒；三台同時開始用")
    ap.add_argument("--settle", type=float, default=15.0, help="設定通道後等待秒數")
    args = ap.parse_args()

    cfg = {int(k): v for k, v in json.loads(args.cfg).items()}
    ues = [u for u in local_ues(args.host) if u.global_id in cfg]
    S = T.sim_speed()
    for ue in ues:
        L, dem, proto = cfg[ue.global_id]
        ue.bandwidth_mbps, ue.protocol = float(dem), proto
        T.set_ue_dl_degradation(ue, float(L))
    time.sleep(args.settle)
    if args.start_at > time.time():
        time.sleep(args.start_at - time.time())

    stop = threading.Event(); idle: list[float] = []
    th = threading.Thread(target=cpu_sampler, args=(stop, idle), daemon=True); th.start()
    for ue in ues:
        T.start_iperf_client(ue)
    time.sleep(args.duration)
    stop.set(); th.join()
    for ue in ues:
        T.stop_iperf_client(ue)

    res = {}
    for ue in ues:
        mbps, n = parse_log(ue.container, args.warmup)
        res[ue.global_id] = {"L": cfg[ue.global_id][0], "demand": cfg[ue.global_id][1], "proto": cfg[ue.global_id][2],
                             "sim_mbps": round(mbps / S, 3), "samples": n}
    out = {"host": args.host, "S": S, "cpu_idle_mean": round(sum(idle) / len(idle), 1) if idle else None,
           "cpu_idle_min": round(min(idle), 1) if idle else None, "ues": res}
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
