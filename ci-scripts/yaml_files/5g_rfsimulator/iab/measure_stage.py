#!/usr/bin/env python3
"""
measure_stage.py — 五階段路線圖共用的量測取樣腳本（見 CLAUDE.md 第 3 節）

跟 traffic_scenario.py（負責產生動態流量 + 路徑損耗環境）同時在同一台主機
上執行，職責分離：traffic_scenario.py 不做任何量測記錄，這支腳本只讀不寫
（讀 iperf3 client 的即時 log、對每個 UE 做獨立 ping），把「該主機負責的
UE，在多 UE 同時競爭流量下實際達成的吞吐量與延遲」定期取樣寫進 CSV。

每個 stage（PF baseline / avg FL / cluster FL / 自訂 FL / 自訂FL+改進版DRL）
都用同一支腳本、同一套取樣方式量測，才能公平比較。

用法（三台主機各自跑一份，需搭配同時在跑的 traffic_scenario.py）：
  python3 iab/measure_stage.py --host pc1 --duration 180 --interval 5 \
      --out /tmp/stage_samples_pc1.csv
"""

from __future__ import annotations

import argparse
import csv
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scenarios"))
from traffic_scenario import EXT_DN_IP, build_ue_list  # noqa: E402

# iperf3 -i 1 的即時進度行範例：
#   "[  5]   4.00-5.00   sec  3.75 MBytes  31.5 Mbits/sec"
# 結尾摘要行含 "sender"/"receiver"，故意排除，只抓「進行中」的即時速率。
_RATE_RE = re.compile(r"([\d.]+)\s+(Mbits|Kbits|Gbits)/sec")


def _latest_rate_mbps(log_path: Path) -> float | None:
    """從 iperf3 client log 尾端抓最近一筆「進行中」速率樣本（Mbps）。"""
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 4096))
            tail = f.read().decode("utf-8", errors="ignore")
    except FileNotFoundError:
        return None

    for line in reversed(tail.splitlines()):
        if "sender" in line or "receiver" in line:
            continue  # 排除結尾累計摘要，只要即時進行中的樣本
        m = _RATE_RE.search(line)
        if m:
            val, unit = float(m.group(1)), m.group(2)
            if unit == "Kbits":
                val /= 1000.0
            elif unit == "Gbits":
                val *= 1000.0
            return val
    return None


def _ping_rtt_ms(container: str) -> float | None:
    try:
        result = subprocess.run(
            ["docker", "exec", container, "ping", "-c", "1", "-W", "1", EXT_DN_IP],
            capture_output=True, text=True, timeout=3,
        )
    except Exception:
        return None
    m = re.search(r"time=([\d.]+)\s*ms", result.stdout)
    return float(m.group(1)) if m else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=["pc1", "pc2", "pc3"], required=True)
    parser.add_argument("--duration", type=int, default=180, help="取樣總秒數")
    parser.add_argument("--interval", type=int, default=5, help="每幾秒取樣一次")
    parser.add_argument("--out", required=True, help="輸出 CSV 路徑")
    args = parser.parse_args()

    ues = build_ue_list(args.host)
    if not ues:
        print(f"[measure_stage] --host={args.host} 沒有任何 UE，請確認 HOST_OF_NODE", file=sys.stderr)
        sys.exit(1)

    print(f"[measure_stage] host={args.host} 取樣 {len(ues)} 個 UE："
          f"{[u.container for u in ues]}，共 {args.duration}s，每 {args.interval}s 一次")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "host", "ue_container", "achieved_mbps", "rtt_ms"])

        # ping 呼叫（docker exec + ping）在 UE 數量多、host 較忙時序列執行會讓
        # 單一週期耗時遠超過 --interval（PC2 曾經觀測到樣本數只有預期的 2~4 成）。
        # 改成平行送出全部 UE 的 ping，週期耗時壓到接近「最慢那個 ping」而不是總和。
        with ThreadPoolExecutor(max_workers=max(len(ues), 1)) as pool:
            deadline = time.time() + args.duration
            while time.time() < deadline:
                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                mbps_list = [_latest_rate_mbps(Path(f"/tmp/iperf_client_{ue.container}.log")) for ue in ues]
                rtt_list = list(pool.map(lambda ue: _ping_rtt_ms(ue.container), ues))
                for ue, mbps, rtt in zip(ues, mbps_list, rtt_list):
                    writer.writerow([ts, args.host, ue.container,
                                      f"{mbps:.2f}" if mbps is not None else "",
                                      f"{rtt:.2f}" if rtt is not None else ""])
                f.flush()
                time.sleep(args.interval)

    print(f"[measure_stage] 完成，結果寫入 {out_path}")


if __name__ == "__main__":
    main()
