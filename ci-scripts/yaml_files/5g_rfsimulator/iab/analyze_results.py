#!/usr/bin/env python3
"""
analyze_results.py — 論文數據分析與 IEEE 圖表生成

輸入:  iab/results/<policy>/iperf_ue{1-6}.json
       iab/results/<policy>/ping_ue{1-6}.txt
       iab/results/<policy>/cpu.log

輸出:
  fig2_throughput.pdf  — 各策略 UDP 吞吐量比較 (Fig. 2)
  fig3_latency_cdf.pdf — 各策略延遲 CDF (Fig. 3)
  paper_variables.txt  — 論文代入值 (X, L, P 變數)
"""

import json
import os
import re
import sys
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pathlib import Path

# ─── Paths ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
RESULTS_DIR = SCRIPT_DIR / "results"

# ─── Policy order & display labels ────────────────────────────────────────
POLICIES = ["fixed50", "fixed30", "fixed10", "adaptive", "pf"]
LABELS   = ["50/50", "30/70", "10/90", "Adaptive", "PF"]

# IEEE-style plot settings (black-and-white printable)
LINE_STYLES = ["-", "--", "-.", ":", (0, (3, 1, 1, 1))]
MARKERS     = ["o", "s", "^", "D", "x"]
COLORS      = ["#000000", "#333333", "#666666", "#999999", "#BBBBBB"]
MARKER_EVERY = 20   # thin out markers on CDF curves

# ─── iperf3 parser ─────────────────────────────────────────────────────────

def parse_iperf_json(path: Path) -> float:
    """Return receiver throughput in Mbps from an iperf3 -J output file."""
    try:
        with open(path) as f:
            data = json.load(f)
        # iperf3 UDP: receiver bits_per_second is in end.sum (server side)
        # Try end.sum_received first, fallback to end.sum
        end = data.get("end", {})
        for key in ("sum_received", "sum"):
            s = end.get(key)
            if s and s.get("bits_per_second", 0) > 0:
                return s["bits_per_second"] / 1e6
        # Fallback: last interval receiver
        intervals = data.get("intervals", [])
        if intervals:
            last = intervals[-1].get("sum", {})
            return last.get("bits_per_second", 0) / 1e6
    except Exception as e:
        print(f"  [warn] iperf parse error {path}: {e}")
    return 0.0


def total_throughput_mbps(policy: str) -> float:
    """Sum receiver throughput across 6 UEs for a policy (Mbps)."""
    total = 0.0
    for i in range(1, 7):
        p = RESULTS_DIR / policy / f"iperf_ue{i}.json"
        if p.exists():
            mbps = parse_iperf_json(p)
            total += mbps
        else:
            print(f"  [warn] missing {p}")
    return total

# ─── ping parser ──────────────────────────────────────────────────────────

def parse_ping_rtts(path: Path) -> list:
    """Return list of RTT samples (ms) from a ping output file."""
    rtts = []
    try:
        text = path.read_text()
        # Match lines like: 64 bytes from ...: icmp_seq=1 ttl=64 time=12.3 ms
        for m in re.finditer(r"time=([\d.]+)\s*ms", text):
            rtts.append(float(m.group(1)))
    except Exception as e:
        print(f"  [warn] ping parse error {path}: {e}")
    return rtts


def all_rtts(policy: str) -> list:
    """Aggregate all UE RTTs for a policy."""
    rtts = []
    for i in range(1, 7):
        p = RESULTS_DIR / policy / f"ping_ue{i}.txt"
        if p.exists():
            rtts.extend(parse_ping_rtts(p))
        else:
            print(f"  [warn] missing {p}")
    return rtts

# ─── CPU parser ───────────────────────────────────────────────────────────

def parse_cpu(policy: str) -> float:
    """Return mean CPU% across IAB containers during traffic from cpu.log."""
    path = RESULTS_DIR / policy / "cpu.log"
    if not path.exists():
        return 0.0
    try:
        text = path.read_text()
        # Find the "during_traffic" section
        m = re.search(r"=== during_traffic ===(.*?)(?:===|$)", text, re.DOTALL)
        section = m.group(1) if m else text
        # Match lines like: container_name   5.23%   ...
        vals = [float(x) for x in re.findall(r"(\d+\.\d+)%", section)]
        return float(np.mean(vals)) if vals else 0.0
    except Exception as e:
        print(f"  [warn] cpu parse error: {e}")
        return 0.0

# ─── Data collection ──────────────────────────────────────────────────────

def collect_all():
    tput   = {}   # policy → total Mbps
    rtts   = {}   # policy → [rtt_ms, ...]
    mean_l = {}   # policy → mean RTT
    p95    = {}   # policy → 95th-pct RTT
    cpu    = {}   # policy → mean CPU%

    for pol in POLICIES:
        d = RESULTS_DIR / pol
        if not d.exists():
            print(f"[warn] No results directory for policy '{pol}' — skipping")
            tput[pol]   = 0.0
            rtts[pol]   = []
            mean_l[pol] = 0.0
            p95[pol]    = 0.0
            cpu[pol]    = 0.0
            continue

        tp = total_throughput_mbps(pol)
        r  = all_rtts(pol)
        tput[pol]   = tp
        rtts[pol]   = r
        mean_l[pol] = float(np.mean(r)) if r else 0.0
        p95[pol]    = float(np.percentile(r, 95)) if r else 0.0
        cpu[pol]    = parse_cpu(pol)

        print(f"  {pol:10s}  tput={tp:6.2f} Mbps  "
              f"mean_lat={mean_l[pol]:5.1f} ms  "
              f"p95_lat={p95[pol]:5.1f} ms  "
              f"cpu={cpu[pol]:.1f}%  "
              f"(n_rtts={len(r)})")

    return tput, rtts, mean_l, p95, cpu

# ─── Figure 2: Throughput comparison ──────────────────────────────────────

def plot_throughput(tput: dict, outpath: Path):
    fig, ax = plt.subplots(figsize=(5, 3.5))

    vals = [tput[p] for p in POLICIES]
    x    = np.arange(len(POLICIES))
    w    = 0.55
    bars = ax.bar(x, vals, width=w, color="white", edgecolor="black",
                  linewidth=1.2,
                  hatch=["", "//", "\\\\", "xx", ".."])

    ax.set_xticks(x)
    ax.set_xticklabels(LABELS, fontsize=9)
    ax.set_ylabel("Aggregate UDP Throughput (Mbps)", fontsize=9)
    ax.set_xlabel("PRB Allocation Policy", fontsize=9)
    ax.set_title("Fig. 2 — Throughput Comparison", fontsize=9, pad=4)
    ax.tick_params(labelsize=8)
    ax.set_ylim(0, max(vals) * 1.2 if vals else 1)

    # Annotate bar values
    for bar, v in zip(bars, vals):
        if v > 0:
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.3,
                    f"{v:.1f}", ha="center", va="bottom", fontsize=7)

    ax.yaxis.grid(True, linestyle="--", linewidth=0.5, alpha=0.7)
    ax.set_axisbelow(True)
    plt.tight_layout()
    fig.savefig(outpath, format="pdf", bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved {outpath}")


# ─── Figure 3: Latency CDF ─────────────────────────────────────────────────

def plot_latency_cdf(rtts: dict, outpath: Path):
    fig, ax = plt.subplots(figsize=(5, 3.5))

    for i, pol in enumerate(POLICIES):
        r = rtts[pol]
        if not r:
            continue
        sorted_r = np.sort(r)
        cdf      = np.arange(1, len(sorted_r) + 1) / len(sorted_r)
        # Thin markers to avoid clutter
        midx = slice(None, None, max(1, len(sorted_r) // MARKER_EVERY))
        ax.plot(sorted_r, cdf,
                label=LABELS[i],
                linestyle=LINE_STYLES[i],
                color=COLORS[i],
                linewidth=1.4,
                marker=MARKERS[i],
                markevery=max(1, len(sorted_r) // MARKER_EVERY),
                markersize=4)

    ax.set_xlabel("End-to-End Latency (ms)", fontsize=9)
    ax.set_ylabel("CDF", fontsize=9)
    ax.set_title("Fig. 3 — Latency CDF", fontsize=9, pad=4)
    ax.set_ylim(0, 1.05)
    ax.tick_params(labelsize=8)
    ax.legend(fontsize=7, loc="lower right", framealpha=0.9)
    ax.yaxis.grid(True, linestyle="--", linewidth=0.5, alpha=0.7)
    ax.xaxis.grid(True, linestyle="--", linewidth=0.5, alpha=0.7)
    ax.set_axisbelow(True)
    plt.tight_layout()
    fig.savefig(outpath, format="pdf", bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved {outpath}")


# ─── Paper variables summary ───────────────────────────────────────────────

def write_paper_vars(tput, mean_l, p95, cpu, outpath: Path):
    """Write all paper variables (X, L, P) to a plain-text file."""
    pol_map = {
        "fixed50":  ("50/50",  "50/50"),
        "fixed30":  ("30/70",  "30/70"),
        "fixed10":  ("10/90",  "10/90"),
        "adaptive": ("dyn",    "dyn"),
        "pf":       ("PF",     "PF"),
    }
    lines = ["=" * 60,
             "Paper Variables — Fill into LaTeX template",
             "=" * 60, ""]

    lines.append("--- Throughput (Section VI.B) ---")
    for pol in POLICIES:
        sym = pol_map[pol][0]
        lines.append(f"  X_{{{sym}}} = {tput[pol]:.2f} Mbps")
    lines.append("")

    lines.append("--- Mean Latency L (Section VI.C) ---")
    for pol in POLICIES:
        sym = pol_map[pol][0]
        lines.append(f"  L_{{{sym}}} = {mean_l[pol]:.2f} ms")
    lines.append("")

    lines.append("--- 95th-pct Latency P (Section VI.C) ---")
    for pol in POLICIES:
        sym = pol_map[pol][0]
        lines.append(f"  P_{{{sym}}} = {p95[pol]:.2f} ms")
    lines.append("")

    lines.append("--- CPU Utilisation (Section VI.D) ---")
    for pol in POLICIES:
        lines.append(f"  {pol:10s}: {cpu[pol]:.1f}%")
    if "adaptive" in cpu and "fixed50" in cpu and cpu["fixed50"] > 0:
        delta = cpu["adaptive"] - cpu["fixed50"]
        lines.append(f"  Adaptive overhead vs fixed50: {delta:+.1f}%")

    lines.append("")
    lines.append("=" * 60)

    text = "\n".join(lines)
    outpath.write_text(text)
    print(text)
    print(f"  Saved {outpath}")


# ─── Main ──────────────────────────────────────────────────────────────────

def main():
    matplotlib.rcParams.update({
        "font.family":      "serif",
        "font.serif":       ["Times New Roman", "DejaVu Serif"],
        "font.size":        9,
        "axes.linewidth":   0.8,
        "pdf.fonttype":     42,     # embed TrueType fonts in PDF
        "ps.fonttype":      42,
    })

    if not RESULTS_DIR.exists():
        print(f"Results directory not found: {RESULTS_DIR}")
        print("Run run_experiment.sh first.")
        sys.exit(1)

    print(f"Reading results from {RESULTS_DIR}/\n")
    tput, rtts, mean_l, p95, cpu = collect_all()
    print()

    out = RESULTS_DIR
    plot_throughput(tput,  out / "fig2_throughput.pdf")
    plot_latency_cdf(rtts, out / "fig3_latency_cdf.pdf")
    write_paper_vars(tput, mean_l, p95, cpu, out / "paper_variables.txt")

    print("\nDone.")


if __name__ == "__main__":
    main()
