#!/usr/bin/env python3
"""policy_prob.py — 直接讀每個節點目前的 Actor（兩段式），在最近經驗的各類狀態下算「策略機率」（2026-10-04，v3.2 學習判定用）。
類別同 policy_diag.py。relay：P(兩個壞通道子節點都被遮)＝P(強度≠全開)×p_apply_i×p_apply_j；access：P(只遮壞 UE)＝P(強度≠全開)×p_apply_壞×(1−p_apply_好)。
另算 P(MT 被遮)、P(好 UE 被遮)。每個節點在自己的容器內執行（模型在各自的 volume）。用法（PC1）：python3 iab/policy_prob.py [--window-min 30]"""
import argparse, json, subprocess, sys
INNER = r'''
import sys, json, math, datetime as dt
sys.path.insert(0, "/app")
import numpy as np, pymongo, torch, drl_agent as da
n, win = int(sys.argv[1]), float(sys.argv[2])
db = pymongo.MongoClient("mongodb://localhost:27017")["iab_xapp"]
ag = da.DRLAgent(node_id=n, model_dir="/app/models"); loaded = ag.load()
since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=win)
docs = [d for d in db[f"node{n}_experiences"].find({"timestamp": {"$gte": since}}, {"_id": 0, "state_vec": 1, "mask_vec": 1})
        if len(d.get("state_vec", [])) == da.STATE_DIM]
F = da.UE_FEAT_DIM; L = math.log1p(da.MAX_BUF_INFO)
out = {"loaded": loaded, "steps": ag._train_steps, "cls": {}, "mt": [], "good": []}
if docs and getattr(da, "ACTION_SPACE", "") == "dqn":
    # DQN：在每個狀態算 argmax Q（貪婪動作），統計「正確動作被選中」的比例（ε 探索另計）
    for d in docs:
        s, m = d["state_vec"], d["mask_vec"]; idx = [j for j in range(len(m)) if m[j]]
        if len(idx) < 2: continue
        mcs = {j: s[j*F+1]*28 for j in idx}; buf = {j: math.expm1(s[j*F+2]*L) for j in idx}; mt = {j: s[j*F+3] > 0.5 for j in idx}
        top = max(idx, key=lambda j: mcs[j]); bad = [j for j in idx if not mt[j] and mcs[top]-mcs[j] >= 4]
        if not bad: continue
        cands, q = ag.dqn_q_values(s, m); sub = set(cands[int(q.argmax())][0])
        out["mt"].append(0.0)
        if n <= 4:
            if len(bad) < 2: continue
            cls = "relay-該遮" if any(mt[j] and buf[j] >= 1e5 for j in idx) else "relay-不遮"
            p = float(set(bad[:2]) <= sub)
        else:
            cls = "access-M" if buf[top] >= 1e5 else "access-N"
            p = float(bad[0] in sub and top not in sub)
            out["good"].append(float(top in sub))
        out["cls"].setdefault(cls, []).append(p)
elif docs:
    S = torch.tensor(np.array([d["state_vec"] for d in docs], np.float32)); M = torch.tensor(np.array([d["mask_vec"] for d in docs], bool))
    with torch.no_grad():
        tl, al = ag.actor(S, M); pa = torch.sigmoid(al)
        p_on = torch.ones(len(docs)) if getattr(da, "FACTORED_MODE", "node_on") == "apply_first" else 1 - torch.softmax(tl, -1)[:, da.PF_TIER]
    for i, d in enumerate(docs):
        s, m = d["state_vec"], d["mask_vec"]; idx = [j for j in range(len(m)) if m[j]]
        if len(idx) < 2: continue
        mcs = {j: s[j*F+1]*28 for j in idx}; buf = {j: math.expm1(s[j*F+2]*L) for j in idx}; mt = {j: s[j*F+3] > 0.5 for j in idx}
        for j in idx:
            if mt[j]: out["mt"].append(float(p_on[i]*pa[i, j]))
        top = max(idx, key=lambda j: mcs[j]); bad = [j for j in idx if not mt[j] and mcs[top]-mcs[j] >= 4]
        if not bad: continue
        if n <= 4:
            if len(bad) < 2: continue
            cls = "relay-該遮" if any(mt[j] and buf[j] >= 1e5 for j in idx) else "relay-不遮"
            p = float(p_on[i] * pa[i, bad[0]] * pa[i, bad[1]])
        else:
            cls = "access-M" if buf[top] >= 1e5 else "access-N"
            p = float(p_on[i] * pa[i, bad[0]] * (1 - pa[i, top]))
            out["good"].append(float(p_on[i] * pa[i, top]))
        out["cls"].setdefault(cls, []).append(p)
print("JSON" + json.dumps({"loaded": out["loaded"], "steps": out["steps"],
      "cls": {k: [float(np.mean(v)), len(v)] for k, v in out["cls"].items()},
      "mt": [float(np.mean(out["mt"])) if out["mt"] else None, len(out["mt"])],
      "good": [float(np.mean(out["good"])) if out["good"] else None, len(out["good"])]}))
'''
ap = argparse.ArgumentParser(); ap.add_argument("--window-min", type=float, default=30.0); args = ap.parse_args()
open("/tmp/policy_prob_inner.py", "w").write(INNER)
agg = {}; mt = []; good = []; steps = []
for n in range(1, 13):
    r = subprocess.run(["docker", "exec", "-w", "/app", f"inference-node{n}", "python3", "/tmp/policy_prob_inner.py", str(n), str(args.window_min)],
                       capture_output=True, text=True, timeout=300)
    line = next((l for l in r.stdout.splitlines() if l.startswith("JSON")), None)
    if not line: continue
    d = json.loads(line[4:]); steps.append(d["steps"])
    for k, (v, c) in d["cls"].items(): agg.setdefault(k, []).append((v, c))
    if d["mt"][0] is not None: mt.append(d["mt"])
    if d["good"][0] is not None: good.append(d["good"])
wm = lambda xs: sum(v * c for v, c in xs) / max(sum(c for _, c in xs), 1)
print(f"[策略機率｜最近 {args.window_min:.0f} 分鐘狀態、目前模型（步數 {min(steps) if steps else '—'}～{max(steps) if steps else '—'}）]")
print("  relay 兩個壞子節點都遮：該遮 {:.1%}（n={}）｜不該遮 {:.1%}（n={}）".format(wm(agg.get("relay-該遮", [])), sum(c for _, c in agg.get("relay-該遮", [])), wm(agg.get("relay-不遮", [])), sum(c for _, c in agg.get("relay-不遮", []))))
print("  access 只遮壞 UE：M 類 {:.1%}（n={}）｜N 類 {:.1%}（n={}）".format(wm(agg.get("access-M", [])), sum(c for _, c in agg.get("access-M", [])), wm(agg.get("access-N", [])), sum(c for _, c in agg.get("access-N", []))))
print("  MT 被遮 {:.1%}｜access 好 UE 被遮 {:.1%}（DQN＝貪婪動作選中的比例；node_on 初始 relay 10%、access 10%、好 UE 20%；apply_first 約 9%、21%、30%）".format(wm(mt), wm(good)))
