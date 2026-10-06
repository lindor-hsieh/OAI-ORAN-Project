#!/usr/bin/env python3
"""policy_diag.py — Local DRL v3 訓練期策略診斷（2026-10-03）。

讀 MongoDB 最近 --window-min 分鐘的 node{N}_experiences（訓練時 DRL 實際採取的動作），依「決策當下可觀測的狀態」把
「壞通道 UE」（同節點 MCS 比最好的低 ≥4 的活躍非 MT UE）分類，統計 DRL 給它的遮罩檔位：
  relay-該遮 ：relay 節點、MT 積壓（佇列 ≥100 KB）——backhaul 被餓，遮邊緣 UE 讓 slot（HS 熱點）
  relay-不遮 ：relay 節點、MT 沒積壓
  access-M   ：access 節點、通道最好的 UE 積壓 ≥100 KB（好 UE 吃不飽，遮壞 UE 有增益）
  access-N   ：access 節點、通道最好的 UE 沒積壓（好 UE 已滿足，遮了只會虧）
另統計 MT 被遮的比例（應趨近 0）。選單從容器內 drl_agent.MASK_TIERS 讀取，k＝該檔允許的 DL＋S slot 數（每 frame 共 16）；
依 k 分組：重遮 ≤3、中重 4～6、中 7～10、輕 11～15、不遮 16。
分類只用於診斷，不參與訓練。用法（PC1）：python3 iab/policy_diag.py [--window-min 20]
"""
import argparse, json, subprocess, sys

INNER = r'''
import sys, json, math, datetime as dt, pymongo
win = float(sys.argv[1])
db = pymongo.MongoClient("mongodb://localhost:27017")["iab_xapp"]
since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=win)
F = 5; L = math.log1p(2_000_000.0)
import sys as _sys; _sys.path.insert(0, "/app"); import drl_agent as _da
MASKS = list(_da.MASK_TIERS)   # 與容器內 DRL 實際使用的選單一致
DLS = set(range(0,8)) | set(range(10,18))   # DL slot 0～6、10～16＋S slot 7、17
NT = len(MASKS)
res = {}
mt_masked = [0, 0]
joint = [0, 0]   # relay-該遮且有 ≥2 個壞通道子節點時，全部同時被遮的比例（v3.1 要學的組合動作）
for n in range(1, 13):
    for d in db[f"node{n}_experiences"].find({"timestamp": {"$gte": since}, "behavior_logp": {"$ne": None}},
                                             {"_id": 0, "state_vec": 1, "mask_vec": 1, "action_tiers": 1}):
        s, m, a = d["state_vec"], d["mask_vec"], d.get("action_tiers", [])
        idx = [i for i in range(len(m)) if m[i]]
        if len(idx) < 2 or len(a) < len(m):
            continue
        mcs = {i: s[i*F+1]*28 for i in idx}; buf = {i: math.expm1(s[i*F+2]*L) for i in idx}; mt = {i: s[i*F+3] > 0.5 for i in idx}
        for i in idx:
            if mt[i]:
                mt_masked[0] += a[i] != NT - 1; mt_masked[1] += 1
        top = max(idx, key=lambda i: mcs[i])
        bads = [i for i in idx if not mt[i] and mcs[top] - mcs[i] >= 4]
        if n <= 4 and len(bads) >= 2 and any(mt[j] and buf[j] >= 1e5 for j in idx):
            joint[0] += all(a[i] != NT - 1 for i in bads); joint[1] += 1
        for i in idx:
            if mt[i] or mcs[top] - mcs[i] < 4:
                continue
            if n <= 4:
                cls = "relay-該遮" if any(mt[j] and buf[j] >= 1e5 for j in idx) else "relay-不遮"
            else:
                cls = "access-M" if buf[top] >= 1e5 else "access-N"
            k = sum(1 for t in range(20) if (MASKS[int(a[i])] >> (t % 16)) & 1 and t in DLS)   # 允許的 DL＋S slot 數（滿 16）
            g = 0 if k <= 3 else 1 if k <= 6 else 2 if k <= 10 else 3 if k <= 15 else 4
            res.setdefault(cls, [0]*5)[g] += 1
print("JSON" + json.dumps({"cls": res, "mt": mt_masked, "joint": joint}))
'''

ap = argparse.ArgumentParser(); ap.add_argument("--window-min", type=float, default=20.0); args = ap.parse_args()
open("/tmp/policy_diag_inner.py", "w").write(INNER)
r = subprocess.run(["docker", "exec", "-w", "/app", "inference-node1", "python3", "/tmp/policy_diag_inner.py",
                    str(args.window_min)], capture_output=True, text=True, timeout=300)
line = next((l for l in r.stdout.splitlines() if l.startswith("JSON")), None)
if line is None:
    print("[策略診斷] ✘ 失敗：", (r.stderr or r.stdout)[-300:]); sys.exit(1)
d = json.loads(line[4:])
print(f"[策略診斷｜最近 {args.window_min:.0f} 分鐘] 壞通道 UE 的遮罩分布（允許的 DL＋S slot 數 ≤3／4～6／7～10／11～15／16＝不遮）：")
for k in ("relay-該遮", "relay-不遮", "access-M", "access-N"):
    c = d["cls"].get(k, [0]*5); t = sum(c)
    if t:
        print(f"  {k:9s} n={t:5d}  " + " ".join(f"{100*x/t:4.0f}%" for x in c) + f"   有遮 {100*(t-c[4])/t:4.0f}%")
    else:
        print(f"  {k:9s} n=    0")
mt = d["mt"]; print(f"  MT 被遮比例 {100*mt[0]/max(mt[1],1):.1f}%（n={mt[1]}）")
j = d.get("joint", [0, 0]); print(f"  relay-該遮時兩個邊緣 UE 同時被遮 {100*j[0]/max(j[1],1):.1f}%（n={j[1]}）")
