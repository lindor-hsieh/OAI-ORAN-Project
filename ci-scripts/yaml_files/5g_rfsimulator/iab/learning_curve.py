#!/usr/bin/env python3
"""learning_curve.py — Local DRL v2 學習曲線的一個取樣點（2026-10-01）。

在 PC1 執行（容器外）：把本檔與 PF 預訓練 checkpoint 放到 /tmp（inference 容器掛載 /tmp），在 inference-node1 容器內
讀 MongoDB 最近 --window-min 分鐘的 node{N}_experiences，對每個節點計算：
  n          經驗筆數
  cont       壅塞樣本比例（同訓練的 _contended_mask：決策當下最大佇列 ≥ CONTENDED_BUF_BYTES 且 ≥2 活躍 UE）
  r_cont     壅塞樣本平均 reward
  adv_pf     壅塞樣本的 r − V_PF(s)：V_PF 是凍結的 PF 預訓練 Critic（訓練開始前的備份），> 0 代表比 PF 好
  adv_pf%    adv_pf ÷ mean V_PF(s)
  interv     壅塞樣本中活躍 UE 選非全開檔位（tier ≠ 4）的比例
  steps      inference 容器 log 最後一次 [訓練][MLP] 的 step 與 entropy
結果附加到 --csv（每節點一列＋ALL 一列），並印出一行摘要＋節點表。

用法：python3 iab/learning_curve.py --pf-ckpt /home/lindor/drl_ckpt_pretrained_hs_20261001 --csv <輸出.csv>
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

INNER = r'''
import json, sys, datetime as dt
sys.path.insert(0, "/app")
import numpy as np, pymongo, torch
from drl_agent import DRLAgent, CONTENDED_BUF_BYTES
win, ckdir = float(sys.argv[1]), sys.argv[2]
db = pymongo.MongoClient("mongodb://localhost:27017")["iab_xapp"]
since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=win)
out = {}
for n in range(1, 13):
    docs = list(db[f"node{n}_experiences"].find({"timestamp": {"$gte": since}},
                {"_id": 0, "state_vec": 1, "mask_vec": 1, "reward": 1, "action_tiers": 1}))
    if not docs:
        out[n] = {"n": 0}; continue
    ag = DRLAgent(node_id=n, model_dir=ckdir); ag.load()
    S = torch.tensor(np.array([d["state_vec"] for d in docs], np.float32))
    M = torch.tensor(np.array([d["mask_vec"] for d in docs], bool))
    R = np.array([d["reward"] for d in docs], np.float32)
    with torch.no_grad():
        V = ag.critic(S, M).cpu().numpy().reshape(-1)
        C = ag._contended_mask(S, M).cpu().numpy()
    tiers = [np.array(d.get("action_tiers", []))[np.array(d["mask_vec"], bool)[:len(d.get("action_tiers", []))]]
             for d, c in zip(docs, C) if c]
    act = np.concatenate(tiers) if tiers else np.zeros(0)
    rc, vc = R[C], V[C]
    out[n] = {"n": len(docs), "cont": float(C.mean()),
              "r_cont": float(rc.mean()) if len(rc) else None,
              "adv_pf": float((rc - vc).mean()) if len(rc) else None,
              "v_pf": float(vc.mean()) if len(rc) else None,
              "n_cont": int(C.sum()),
              "interv": float((act != 4).mean()) if len(act) else None}
print("JSON" + json.dumps(out))
'''

STEP_RE = re.compile(r"\[訓練\]\[MLP\] step=(\d+) .*?entropy=([\d.]+)")


def last_step(node: int) -> tuple[int | None, float | None]:
    try:
        log = subprocess.run(["docker", "logs", "--tail", "400", f"inference-node{node}"],
                             capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return None, None
    lines = [l for l in (log.stdout + log.stderr).splitlines() if "[訓練][MLP]" in l]
    if not lines:
        return None, None
    m = STEP_RE.search(lines[-1])
    if not m:
        return None, None
    # Actor 跳過（批次壅塞樣本 < 8）時 log 的熵恆為 0，不代表策略塌縮 → 以 NaN 表示，表格顯示「跳過」
    return int(m.group(1)), (float("nan") if "Actor跳過" in lines[-1] else float(m.group(2)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pf-ckpt", required=True, help="PF 預訓練 checkpoint 目錄（model_nodeN.pt）")
    ap.add_argument("--csv", required=True)
    ap.add_argument("--window-min", type=float, default=20.0)
    ap.add_argument("--elapsed-min", type=float, default=None, help="訓練已進行幾分鐘（寫進 CSV）")
    args = ap.parse_args()

    ck = Path("/tmp/lc_pf_ckpt"); ck.mkdir(exist_ok=True)
    for f in Path(args.pf_ckpt).glob("model_node*.pt"):
        shutil.copy2(f, ck / f.name)
    Path("/tmp/lc_inner.py").write_text(INNER)
    r = subprocess.run(["docker", "exec", "-w", "/app", "inference-node1", "python3", "/tmp/lc_inner.py",
                        str(args.window_min), str(ck)], capture_output=True, text=True, timeout=600)
    line = next((l for l in r.stdout.splitlines() if l.startswith("JSON")), None)
    if line is None:
        print(f"[學習曲線] ✘ 取樣失敗：{(r.stderr or r.stdout)[-400:]}")
        return 1
    res = {int(k): v for k, v in json.loads(line[4:]).items()}

    now = datetime.now().strftime("%F %T")
    rows, tot_n, adv_w, v_w, nc_w, intv_w = [], 0, 0.0, 0.0, 0, 0.0
    for n in range(1, 13):
        d = res[n]; st, ent = last_step(n)
        d["step"], d["entropy"] = st, ent
        if d.get("n_cont"):
            adv_w += d["adv_pf"] * d["n_cont"]; v_w += d["v_pf"] * d["n_cont"]
            intv_w += (d["interv"] or 0.0) * d["n_cont"]; nc_w += d["n_cont"]
        tot_n += d.get("n", 0)
        rows.append((n, d))
    adv_all = adv_w / nc_w if nc_w else None
    pct_all = 100 * adv_w / v_w if v_w > 0 else None
    intv_all = intv_w / nc_w if nc_w else None

    new = not Path(args.csv).exists()
    with open(args.csv, "a") as f:
        if new:
            f.write("time,elapsed_min,node,n,cont,n_cont,r_cont,v_pf,adv_pf,adv_pf_pct,interv,step,entropy\n")
        for n, d in rows:
            pct = 100 * d["adv_pf"] / d["v_pf"] if d.get("v_pf") else None
            f.write(",".join(str(x) for x in [now, args.elapsed_min, n, d.get("n", 0), d.get("cont"), d.get("n_cont"),
                    d.get("r_cont"), d.get("v_pf"), d.get("adv_pf"), pct, d.get("interv"), d["step"], d["entropy"]]) + "\n")
        f.write(",".join(str(x) for x in [now, args.elapsed_min, "ALL", tot_n, None, nc_w, None, v_w / nc_w if nc_w else None,
                adv_all, pct_all, intv_all, None, None]) + "\n")

    fmt = lambda x, p="{:+.1f}": "—" if x is None else p.format(x)
    print(f"[學習曲線 {now}｜訓練 {fmt(args.elapsed_min, '{:.0f}')} 分] 壅塞樣本 {nc_w} 筆｜"
          f"相對 PF（凍結 Critic）{fmt(pct_all)}%｜介入比例 {fmt(None if intv_all is None else 100 * intv_all, '{:.0f}')}%")
    print("  節點 " + " ".join(f"N{n:<5d}" for n, _ in rows))
    print("  vsPF%" + " ".join(f"{fmt(100 * d['adv_pf'] / d['v_pf'] if d.get('v_pf') else None):>6s}" for _, d in rows))
    print("  介入%" + " ".join(f"{fmt(None if d.get('interv') is None else 100 * d['interv'], '{:.0f}'):>6s}" for _, d in rows))
    print("  step " + " ".join(f"{fmt(d['step'], '{:d}'):>6s}" for _, d in rows))
    ent = lambda e: "跳過" if e is not None and e != e else fmt(e, "{:.2f}")
    print("  熵   " + " ".join(f"{ent(d['entropy']):>6s}" for _, d in rows) + "（跳過＝本輪壅塞樣本不足、Actor 未更新）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
