#!/usr/bin/env python3
"""
drl_report.py — DRL vs PF Baseline 效能對比報告

在 PC1 執行：
  python3 iab/drl_report.py                     # 完整測試（~10 分鐘）
  python3 iab/drl_report.py --no-perf           # 只生成 DRL 訓練統計圖
  python3 iab/drl_report.py --iters 1           # 快速測試（每 UE 1 輪，~3 分鐘）
  python3 iab/drl_report.py --output ~/report/  # 指定輸出目錄

流程：
  1. 停止 UE 容器內的競爭 iperf3 流量（避免 port 衝突）
  2. 對 UE1~6 跑 ping + TCP DL/UL 效能測試（SSH → PC2）
  3. 從 MongoDB 撈 DRL 訓練統計（reward 曲線、各 node 狀態）
  4. 生成 PNG 圖表（reward 收斂 + 效能對比）
  5. 輸出 Markdown 對比報告

依賴：pip install matplotlib numpy pymongo
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── 可選依賴 ──────────────────────────────────────────────────────────────────

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    HAS_PLOT = True
except ImportError:
    HAS_PLOT = False
    print("[WARN] matplotlib/numpy 未安裝，跳過作圖  pip install matplotlib numpy",
          file=sys.stderr)

try:
    from pymongo import MongoClient
    HAS_MONGO = True
except ImportError:
    HAS_MONGO = False
    print("[WARN] pymongo 未安裝，跳過 DRL 統計  pip install pymongo", file=sys.stderr)

# ── PF Baseline（2026-04-20，iab_perf_test.sh 3 輪平均）─────────────────────

PF_PER_UE: dict[str, dict] = {
    "UE1": {"lat": 76.36, "tcp_dl": 6.81,  "tcp_ul": 9.21},
    "UE2": {"lat": 67.84, "tcp_dl": 10.66, "tcp_ul": 9.59},
    "UE3": {"lat": 83.25, "tcp_dl": 11.35, "tcp_ul": 9.34},
    "UE4": {"lat": 71.39, "tcp_dl": 6.81,  "tcp_ul": 9.90},
    "UE5": {"lat": 65.12, "tcp_dl": 9.44,  "tcp_ul": 8.95},
    "UE6": {"lat": 81.43, "tcp_dl": 6.99,  "tcp_ul": 9.67},
}
PF_AVG = {"lat": 74.23, "tcp_dl": 8.67, "tcp_ul": 9.44, "jfi": 0.924}

# ── 設定 ──────────────────────────────────────────────────────────────────────

PC2_USER    = "lindor"
PC2_IP      = "192.168.88.2"
SSH_BASE    = ["ssh", "-o", "StrictHostKeyChecking=no",
               "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
               f"{PC2_USER}@{PC2_IP}"]
UPF_DN_IP   = "192.168.72.135"   # ext-dn iperf3 server
MONGO_URI   = "mongodb://localhost:27017/"
UE_NAMES    = [f"rfsim5g-end-ue-{i}" for i in range(1, 7)]
IPERF_DURATION = 5               # 每次 iperf3 測試秒數


# ── 工具函式 ──────────────────────────────────────────────────────────────────

def ssh(cmd: str, timeout: int = 20) -> str:
    r = subprocess.run(SSH_BASE + [cmd], capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip()


def jfi(values: list[float]) -> float:
    """Jain's Fairness Index = (Σxᵢ)² / (n × Σxᵢ²)，值域 [1/n, 1]。"""
    v = [x for x in values if x > 0]
    if not v:
        return 0.0
    s  = sum(v)
    s2 = sum(x ** 2 for x in v)
    return s ** 2 / (len(v) * s2) if s2 > 1e-9 else 0.0


def parse_iperf_mbps(raw: str) -> Optional[float]:
    """從 iperf3 輸出解析 receiver 端的 Mbps。"""
    for line in raw.splitlines():
        if "receiver" not in line:
            continue
        parts = line.split()
        for i, p in enumerate(parts):
            if p in ("Mbps", "Gbps", "Kbps") and i > 0:
                try:
                    v = float(parts[i - 1])
                    if p == "Gbps": v *= 1000.0
                    if p == "Kbps": v /= 1000.0
                    return v
                except ValueError:
                    pass
    return None


def parse_ping_ms(raw: str) -> Optional[float]:
    """從 ping 輸出解析平均 RTT (ms)。"""
    m = re.search(r"rtt .* = [\d.]+/([\d.]+)/", raw)
    return float(m.group(1)) if m else None


# ── 效能測試 ──────────────────────────────────────────────────────────────────

def stop_competing_iperf(ue: str) -> None:
    """殺掉 UE 容器內正在跑的 iperf3 client 與 while-loop，避免 port 佔用。"""
    ssh(f"docker exec {ue} sh -c "
        f"'pkill -9 -f iperf3 2>/dev/null; pkill -9 -f \"while true\" 2>/dev/null; true'",
        timeout=8)


def test_ue(container: str, n_iters: int) -> dict:
    """
    對單一 UE 容器跑 ping + TCP DL/UL，回傳各指標的多輪平均值。
    若容器未啟動或無 IP，回傳空 dict。
    """
    ue_ip = ssh(
        f"docker exec {container} "
        f"ip -f inet addr show oaitun_ue1 2>/dev/null "
        f"| grep -oP '(?<=inet\\s)\\d+(\\.\\d+){{3}}'",
        timeout=8,
    )
    if not ue_ip:
        return {}

    lats, tcp_dls, tcp_uls = [], [], []

    for i in range(n_iters):
        print(f"    [{i+1}/{n_iters}]", end="  ", flush=True)

        # Ping（2 包）
        raw = ssh(f"docker exec {container} ping -c 2 -W 2 {UPF_DN_IP} 2>/dev/null",
                  timeout=12)
        v = parse_ping_ms(raw)
        if v:
            lats.append(v)
            print(f"lat={v:.1f}ms", end="  ", flush=True)

        # TCP DL（-R reverse：server → UE）
        raw = ssh(
            f"timeout {IPERF_DURATION + 5} docker exec {container} "
            f"iperf3 -c {UPF_DN_IP} -t {IPERF_DURATION} -R 2>/dev/null",
            timeout=IPERF_DURATION + 12,
        )
        v = parse_iperf_mbps(raw)
        if v:
            tcp_dls.append(v)
            print(f"dl={v:.2f}Mbps", end="  ", flush=True)

        # TCP UL（UE → server）
        raw = ssh(
            f"timeout {IPERF_DURATION + 5} docker exec {container} "
            f"iperf3 -c {UPF_DN_IP} -t {IPERF_DURATION} 2>/dev/null",
            timeout=IPERF_DURATION + 12,
        )
        v = parse_iperf_mbps(raw)
        if v:
            tcp_uls.append(v)
            print(f"ul={v:.2f}Mbps", end="  ", flush=True)

        print()

    def avg(lst: list) -> Optional[float]:
        return sum(lst) / len(lst) if lst else None

    return {
        "ip":     ue_ip,
        "lat":    avg(lats),
        "tcp_dl": avg(tcp_dls),
        "tcp_ul": avg(tcp_uls),
    }


def run_perf_tests(n_iters: int) -> dict[str, dict]:
    est = n_iters * len(UE_NAMES) * (IPERF_DURATION * 2 + 15) // 60
    print(f"\n[Phase 1] 效能測試（每 UE {n_iters} 輪，預估 {est} 分鐘）")
    print("          先停止 UE 內的競爭流量...")

    for ue in UE_NAMES:
        try:
            stop_competing_iperf(ue)
        except Exception:
            pass

    import time; time.sleep(1)

    results: dict[str, dict] = {}
    for idx, container in enumerate(UE_NAMES, 1):
        label = f"UE{idx}"
        print(f"\n  {label} ({container})")
        try:
            r = test_ue(container, n_iters)
            if r:
                results[label] = r
            else:
                print("    (容器未啟動或無 IP，跳過)")
        except subprocess.TimeoutExpired:
            print("    (SSH timeout，跳過)")
        except Exception as e:
            print(f"    (錯誤: {e})")

    return results


# ── MongoDB DRL 統計 ───────────────────────────────────────────────────────────

def fetch_drl_stats() -> dict:
    """撈各 Node 的 reward 序列與彙總統計。"""
    if not HAS_MONGO:
        return {}
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        db     = client["iab_xapp"]
        stats  = {}
        for n in range(1, 6):
            docs = list(
                db[f"node{n}_experiences"]
                .find({"reward": {"$exists": True}}, {"reward": 1, "_id": 0})
                .sort("_id", 1)
            )
            rewards = [d["reward"] for d in docs
                       if isinstance(d.get("reward"), (int, float))]
            if not rewards:
                continue
            recent = rewards[-200:]
            arr    = rewards  # keep full list for plots
            stats[f"node{n}"] = {
                "total":      len(rewards),
                "rewards":    arr,
                "recent_avg": sum(recent) / len(recent),
                "recent_std": float(np.std(recent)) if HAS_PLOT else 0.0,
                "peak":       max(rewards),
                "min":        min(rewards),
            }
        return stats
    except Exception as e:
        print(f"[WARN] MongoDB 連線失敗: {e}", file=sys.stderr)
        return {}


# ── 作圖 ──────────────────────────────────────────────────────────────────────

def plot_reward_convergence(drl_stats: dict, out: Path) -> bool:
    if not HAS_PLOT or not drl_stats:
        return False

    nodes = sorted(drl_stats.keys())   # node1 … node5
    n     = len(nodes)
    fig, axes = plt.subplots(1, n, figsize=(4 * n, 4), sharey=True)
    if n == 1:
        axes = [axes]
    fig.suptitle("DRL Reward Convergence (Moving Average, window=100)", fontsize=13)

    colors = ["steelblue", "darkorange", "seagreen", "crimson", "mediumpurple"]
    for ax, key, color in zip(axes, nodes, colors):
        rewards = drl_stats[key]["rewards"]
        window  = min(100, max(1, len(rewards) // 20))
        kernel  = np.ones(window) / window
        smooth  = np.convolve(rewards, kernel, mode="valid")
        x       = np.arange(len(smooth))

        ax.plot(x, smooth, linewidth=1.8, color=color)
        ax.fill_between(x,
                        np.clip(smooth - 0.08, -0.3, 1.0),
                        np.clip(smooth + 0.08, -0.3, 1.0),
                        alpha=0.15, color=color)
        ax.axhline(0, color="red", linestyle="--", linewidth=0.8, alpha=0.6)
        ax.axhline(drl_stats[key]["recent_avg"], color=color,
                   linestyle=":", linewidth=1.0, alpha=0.7)

        total = drl_stats[key]["total"]
        ravg  = drl_stats[key]["recent_avg"]
        ax.set_title(f"{key.capitalize()}\nn={total:,}  recent={ravg:.3f}", fontsize=10)
        ax.set_xlabel("Training Steps")
        ax.set_ylim(-0.35, 1.05)
        ax.grid(True, alpha=0.3)

    axes[0].set_ylabel("Reward (MA)")
    plt.tight_layout()
    plt.savefig(out, dpi=150, bbox_inches="tight")
    plt.close()
    return True


def plot_performance_comparison(perf: dict[str, dict], out: Path) -> bool:
    if not HAS_PLOT:
        return False

    ue_keys = ["UE1", "UE2", "UE3", "UE4", "UE5", "UE6"]

    # 計算 DRL 平均
    drl_dls  = [perf[k]["tcp_dl"] for k in ue_keys if k in perf and perf[k].get("tcp_dl")]
    drl_lats = [perf[k]["lat"]    for k in ue_keys if k in perf and perf[k].get("lat")]
    drl_avg_dl  = sum(drl_dls)  / len(drl_dls)  if drl_dls  else 0.0
    drl_avg_lat = sum(drl_lats) / len(drl_lats) if drl_lats else 0.0
    drl_jfi_val = jfi(drl_dls)

    labels    = ue_keys + ["Avg"]
    pf_dl     = [PF_PER_UE[k]["tcp_dl"] for k in ue_keys] + [PF_AVG["tcp_dl"]]
    drl_dl    = [perf.get(k, {}).get("tcp_dl") or 0.0 for k in ue_keys] + [drl_avg_dl]
    pf_lat    = [PF_PER_UE[k]["lat"]    for k in ue_keys] + [PF_AVG["lat"]]
    drl_lat   = [perf.get(k, {}).get("lat")    or 0.0 for k in ue_keys] + [drl_avg_lat]

    x  = np.arange(len(labels))
    w  = 0.35
    pf_color  = "#4878cf"
    drl_color = "#6acc65"

    fig, axes = plt.subplots(1, 3, figsize=(18, 5))
    fig.suptitle("DRL vs PF Baseline — Performance Comparison", fontsize=14, fontweight="bold")

    # ── TCP DL ──────────────────────────────────────────────────────
    ax = axes[0]
    b1 = ax.bar(x - w/2, pf_dl,  w, label="PF Baseline", color=pf_color,  alpha=0.85)
    b2 = ax.bar(x + w/2, drl_dl, w, label="DRL",          color=drl_color, alpha=0.85)
    ax.set_title("TCP Downlink Throughput (Mbps)", fontsize=12)
    ax.set_xticks(x); ax.set_xticklabels(labels)
    ax.set_ylabel("Mbps"); ax.legend(); ax.grid(axis="y", alpha=0.3)
    for bar in b1:
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.1,
                f"{bar.get_height():.1f}", ha="center", va="bottom", fontsize=8)
    for bar in b2:
        if bar.get_height() > 0:
            ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.1,
                    f"{bar.get_height():.1f}", ha="center", va="bottom", fontsize=8)

    # ── Latency ─────────────────────────────────────────────────────
    ax = axes[1]
    b3 = ax.bar(x - w/2, pf_lat,  w, label="PF Baseline", color=pf_color,  alpha=0.85)
    b4 = ax.bar(x + w/2, drl_lat, w, label="DRL",          color=drl_color, alpha=0.85)
    ax.set_title("Round-Trip Latency (ms)", fontsize=12)
    ax.set_xticks(x); ax.set_xticklabels(labels)
    ax.set_ylabel("ms"); ax.legend(); ax.grid(axis="y", alpha=0.3)
    for bar in b3:
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.3,
                f"{bar.get_height():.0f}", ha="center", va="bottom", fontsize=8)
    for bar in b4:
        if bar.get_height() > 0:
            ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.3,
                    f"{bar.get_height():.0f}", ha="center", va="bottom", fontsize=8)

    # ── JFI ─────────────────────────────────────────────────────────
    ax = axes[2]
    jfi_labels = ["PF Baseline", "DRL"]
    jfi_vals   = [PF_AVG["jfi"], drl_jfi_val]
    jfi_colors = [pf_color, drl_color]
    bars = ax.bar(jfi_labels, jfi_vals, color=jfi_colors, alpha=0.85, width=0.4)
    ax.axhline(PF_AVG["jfi"], color=pf_color, linestyle="--", linewidth=1.2, alpha=0.7,
               label=f"PF JFI={PF_AVG['jfi']:.3f}")
    ax.set_title("Jain's Fairness Index (TCP-DL)", fontsize=12)
    ax.set_ylim(0, 1.05); ax.set_ylabel("JFI"); ax.legend(); ax.grid(axis="y", alpha=0.3)
    for bar, val in zip(bars, jfi_vals):
        ax.text(bar.get_x() + bar.get_width()/2, val + 0.01,
                f"{val:.3f}", ha="center", va="bottom", fontsize=11, fontweight="bold")

    plt.tight_layout()
    plt.savefig(out, dpi=150, bbox_inches="tight")
    plt.close()
    return True


# ── Markdown 報告 ─────────────────────────────────────────────────────────────

def build_markdown(
    perf:           dict[str, dict],
    drl_stats:      dict,
    has_conv_plot:  bool,
    has_perf_plot:  bool,
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    L: list[str] = []

    L += [
        "# DRL vs PF Baseline 效能對比報告",
        "",
        f"> 生成時間：{now}  |  工具：drl_report.py",
        "",
    ]

    # ── Section 1: DRL 訓練狀態 ───────────────────────────────────
    if drl_stats:
        L += [
            "## 1. DRL 訓練狀態",
            "",
            "| Node | 總經驗數 | 近 200 筆平均 Reward | 標準差 | 峰值 |",
            "|------|---------|---------------------|--------|------|",
        ]
        for key in sorted(drl_stats.keys()):
            s = drl_stats[key]
            converged = "✓ 收斂" if s["recent_avg"] > 0.5 else "⚠ 訓練中"
            L.append(
                f"| {key.capitalize()} | {s['total']:,} | "
                f"{s['recent_avg']:.3f} | "
                f"±{s['recent_std']:.3f} | "
                f"{s['peak']:.3f} {converged} |"
            )
        L.append("")
        if has_conv_plot:
            L += ["![Reward Convergence](reward_convergence.png)", ""]
    else:
        L += ["## 1. DRL 訓練狀態", "", "> MongoDB 無資料或不可用。", ""]

    # ── Section 2: 效能對比 ───────────────────────────────────────
    if perf:
        ue_keys = ["UE1", "UE2", "UE3", "UE4", "UE5", "UE6"]

        drl_dls  = [perf[k]["tcp_dl"] for k in ue_keys if k in perf and perf[k].get("tcp_dl")]
        drl_lats = [perf[k]["lat"]    for k in ue_keys if k in perf and perf[k].get("lat")]
        drl_uls  = [perf[k]["tcp_ul"] for k in ue_keys if k in perf and perf[k].get("tcp_ul")]

        drl_avg_dl  = sum(drl_dls)  / len(drl_dls)  if drl_dls  else 0.0
        drl_avg_lat = sum(drl_lats) / len(drl_lats) if drl_lats else 0.0
        drl_avg_ul  = sum(drl_uls)  / len(drl_uls)  if drl_uls  else 0.0
        drl_jfi_val = jfi(drl_dls)

        def delta_str(drl_v: float, pf_v: float, higher_is_better: bool = True) -> str:
            d = drl_v - pf_v
            pct = d / pf_v * 100 if pf_v else 0
            better = (d >= 0) == higher_is_better
            arrow  = "↑" if d >= 0 else "↓"
            mark   = "✓" if better else "✗"
            return f"{mark} {arrow}{abs(d):.2f} ({arrow}{abs(pct):.1f}%)"

        # TCP-DL 表
        L += [
            "## 2. TCP Downlink 吞吐量 (Mbps)",
            "",
            "| UE | PF Baseline | DRL | 變化 |",
            "|----|------------|-----|------|",
        ]
        for k in ue_keys:
            pf_v  = PF_PER_UE[k]["tcp_dl"]
            drl_v = perf.get(k, {}).get("tcp_dl") or 0.0
            cell  = f"{drl_v:.2f}" if drl_v else "—"
            d_str = delta_str(drl_v, pf_v) if drl_v else "—"
            L.append(f"| {k} | {pf_v:.2f} | {cell} | {d_str} |")

        d_str = delta_str(drl_avg_dl, PF_AVG["tcp_dl"])
        L += [
            f"| **平均** | **{PF_AVG['tcp_dl']:.2f}** | **{drl_avg_dl:.2f}** | **{d_str}** |",
            "",
        ]

        # Latency 表
        L += [
            "## 3. 往返延遲 (ms)",
            "",
            "| UE | PF Baseline | DRL | 變化 |",
            "|----|------------|-----|------|",
        ]
        for k in ue_keys:
            pf_v  = PF_PER_UE[k]["lat"]
            drl_v = perf.get(k, {}).get("lat") or 0.0
            cell  = f"{drl_v:.1f}" if drl_v else "—"
            d_str = delta_str(drl_v, pf_v, higher_is_better=False) if drl_v else "—"
            L.append(f"| {k} | {pf_v:.1f} | {cell} | {d_str} |")

        d_str = delta_str(drl_avg_lat, PF_AVG["lat"], higher_is_better=False)
        L += [
            f"| **平均** | **{PF_AVG['lat']:.2f}** | **{drl_avg_lat:.2f}** | **{d_str}** |",
            "",
        ]

        # JFI + TCP-UL
        jfi_better = "✓ 優於 PF" if drl_jfi_val >= PF_AVG["jfi"] else "✗ 低於 PF"
        ul_better  = "✓" if drl_avg_ul >= PF_AVG["tcp_ul"] else "✗"
        L += [
            "## 4. 公平性與上行",
            "",
            "| 指標 | PF Baseline | DRL | 評價 |",
            "|------|------------|-----|------|",
            f"| Jain's Fairness Index (TCP-DL) | {PF_AVG['jfi']:.3f} | {drl_jfi_val:.3f} | {jfi_better} |",
            f"| Avg TCP-UL (Mbps) | {PF_AVG['tcp_ul']:.2f} | {drl_avg_ul:.2f} | {ul_better} |",
            "",
        ]

        if has_perf_plot:
            L += ["![Performance Comparison](performance_comparison.png)", ""]

        # 結論
        wins  = []
        loses = []
        if drl_avg_dl > PF_AVG["tcp_dl"]:
            pct = (drl_avg_dl - PF_AVG["tcp_dl"]) / PF_AVG["tcp_dl"] * 100
            wins.append(f"TCP-DL 吞吐量提升 **{pct:.1f}%**（{PF_AVG['tcp_dl']:.2f} → {drl_avg_dl:.2f} Mbps）")
        else:
            pct = (PF_AVG["tcp_dl"] - drl_avg_dl) / PF_AVG["tcp_dl"] * 100
            loses.append(f"TCP-DL 吞吐量下降 {pct:.1f}%（{PF_AVG['tcp_dl']:.2f} → {drl_avg_dl:.2f} Mbps）")

        if drl_jfi_val >= PF_AVG["jfi"]:
            wins.append(f"公平性提升 JFI {PF_AVG['jfi']:.3f} → **{drl_jfi_val:.3f}**")
        else:
            loses.append(f"公平性下降 JFI {PF_AVG['jfi']:.3f} → {drl_jfi_val:.3f}")

        if drl_avg_lat < PF_AVG["lat"]:
            pct = (PF_AVG["lat"] - drl_avg_lat) / PF_AVG["lat"] * 100
            wins.append(f"延遲降低 **{pct:.1f}%**（{PF_AVG['lat']:.2f} → {drl_avg_lat:.2f} ms）")
        else:
            pct = (drl_avg_lat - PF_AVG["lat"]) / PF_AVG["lat"] * 100
            loses.append(f"延遲上升 {pct:.1f}%（{PF_AVG['lat']:.2f} → {drl_avg_lat:.2f} ms）")

        L += ["## 5. 結論", ""]
        if wins:
            L.append("**DRL 優於 PF Baseline：**")
            L.extend(f"- ✓ {w}" for w in wins)
        if loses:
            L.append("\n**尚未超越 PF Baseline：**")
            L.extend(f"- ✗ {lo}" for lo in loses)
        L.append("")

    else:
        L += ["## 2. 效能對比", "", "> 未執行效能測試（使用 --no-perf 跳過）。", ""]

    L += [
        "---",
        "*由 drl_report.py 自動生成*",
    ]
    return "\n".join(L)


# ── 主程式 ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="DRL vs PF 對比報告")
    parser.add_argument("--no-perf", action="store_true",
                        help="跳過效能測試，只生成 DRL 訓練統計")
    parser.add_argument("--iters", type=int, default=3,
                        help="每 UE 測試輪數（預設 3）")
    parser.add_argument("--output", default=str(Path.home() / "drl_report"),
                        help="輸出目錄（預設 ~/drl_report）")
    args = parser.parse_args()

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path   = out_dir / "report.md"
    conv_path = out_dir / "reward_convergence.png"
    perf_path = out_dir / "performance_comparison.png"

    print(f"輸出目錄：{out_dir}")

    # Phase 1: 效能測試
    perf_results: dict[str, dict] = {}
    if not args.no_perf:
        perf_results = run_perf_tests(args.iters)
    else:
        print("\n[Phase 1] 跳過效能測試（--no-perf）")

    # Phase 2: DRL 統計
    print("\n[Phase 2] 查詢 MongoDB DRL 訓練統計...")
    drl_stats = fetch_drl_stats()
    if drl_stats:
        for key in sorted(drl_stats.keys()):
            s = drl_stats[key]
            print(f"  {key}: {s['total']:,} 筆  "
                  f"recent_avg={s['recent_avg']:.3f} ±{s['recent_std']:.3f}  "
                  f"peak={s['peak']:.3f}")
    else:
        print("  (無資料或 MongoDB 不可用)")

    # Phase 3: 作圖
    print("\n[Phase 3] 生成圖表...")
    has_conv = plot_reward_convergence(drl_stats, conv_path)
    has_perf = plot_performance_comparison(perf_results, perf_path) if perf_results else False
    if has_conv: print(f"  ✓ {conv_path}")
    if has_perf: print(f"  ✓ {perf_path}")
    if not HAS_PLOT:
        print("  (跳過：pip install matplotlib numpy)")

    # Phase 4: Markdown
    print("\n[Phase 4] 生成 Markdown 報告...")
    md = build_markdown(perf_results, drl_stats, has_conv, has_perf)
    md_path.write_text(md, encoding="utf-8")
    print(f"  ✓ {md_path}")

    # 快速預覽
    print("\n" + "═" * 60)
    preview_lines = md.splitlines()
    for line in preview_lines[:50]:
        print(line)
    if len(preview_lines) > 50:
        print(f"... (共 {len(preview_lines)} 行，完整報告見 {md_path})")
    print("═" * 60)


if __name__ == "__main__":
    main()
