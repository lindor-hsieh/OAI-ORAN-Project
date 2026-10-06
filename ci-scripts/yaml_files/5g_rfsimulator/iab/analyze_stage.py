#!/usr/bin/env python3
"""
analyze_stage.py — 兩狀態 Scenario T 的 Stage 量測分析（Stage 1~5 共用，比較指標見 CLAUDE.md 第 3 節「判讀指標」）

用法：python3 iab/analyze_stage.py <run_stage_measure.sh 的輸出目錄> <tag>
輸入：<dir>/<tag>_scn_{pc1,pc2,pc3}.log（有檔案的主機才讀；pc1 只有 relay 直連 UE17~24）（場景 log，含「Scenario T 相位狀態」或「Scenario TH 相位狀態」；TH 與 T 相位/壅塞結構相同，但每 UE 的流量指派不同，目標值由對應的場景函數算出，TH 另從 log 讀 seed）與 <dir>/<tag>_{pc1,pc2,pc3}.csv（measure_stage.py 輸出）
輸出：逐 UE 吞吐量（壅塞/正常相位分開）、壅塞驗證（各相位過載 UE 比例、壅塞時間佔比）、
      整段 JFI、壅塞相位的需求滿足率（達成/目標）與其 JFI、壅塞/正常相位 RTT。數字為模擬時間（吞吐量 ÷S、RTT ×S；S 讀本機速度檔，分析舊資料時用環境變數 ANALYZE_S 指定當時的 S）。
      目標（需求）為 0 的 UE-相位（例如尚未定義角色、閒置的 relay 直連 UE）不納入統計。
"""
import sys,re,csv,time,statistics as st,collections
sys.path.insert(0,str(__import__('pathlib').Path(__file__).resolve().parent.parent/'scenarios'))
import logging; logging.disable(logging.CRITICAL)   # 呼叫場景函數算目標時不要印場景 log
import os, traffic_scenario as T
SPD=float(os.environ.get("ANALYZE_S") or T.sim_speed()); SD=sys.argv[1]; tag=sys.argv[2]; day=time.strftime("%Y-%m-%d")
ts=lambda s: time.mktime(time.strptime(day+" "+s,"%Y-%m-%d %H:%M:%S"))
def jfi(x): x=[v for v in x]; return (sum(x)**2/(len(x)*sum(v*v for v in x))) if x and sum(v*v for v in x)>0 else 0
rec=[]   # (ue, phase_idx, state, offered, achieved_sim or None, rtt_wall)
phase_info={}   # (host)->list of (t0,t1,pi,state)
HOSTS=[h for h in ("pc1","pc2","pc3") if os.path.exists(f"{SD}/{tag}_scn_{h}.log") and os.path.exists(f"{SD}/{tag}_{h}.csv")]
assert HOSTS, f"{SD} 裡找不到 {tag}_scn_<host>.log／{tag}_<host>.csv"
for h in HOSTS:
    ues=T.build_ue_list(h)
    st_lines=[]; label=None; seed=None
    for l in open(f"{SD}/{tag}_scn_{h}.log"):
        m=re.match(r"\[(\d\d:\d\d:\d\d)\] INFO Scenario (TMH|TM|TH|HSE|HSD|HSC|HSB|HSX5|HS5|HSH|HS|G|PB|PU|PA|SW1|SW2|SW3|T|P) 相位狀態：(壅塞|正常)（phase_index=(\d+)",l)
        if m: label=m.group(2); st_lines.append((ts(m.group(1)),m.group(3),int(m.group(4))))
        ms=re.search(r"Scenario (?:TMH|TH|HSE|HSD|HSC|HSB|HSX5|HS5|HSH|HS|G) seed=(\d+)",l)
        if ms: seed=int(ms.group(1))
    # 每 UE 的目標（offered）一律經 traffic_scenario.scenario_configs() 計算（場景執行用的同一個入口，含 relay 直連 UE），
    # 不在這裡重寫規則（2026-09-30 以前這裡寫死 T 的輪替公式，TH 目標全錯）。TH／TMH 的 seed 從場景 log 讀。
    if label in ("TH","TMH","HS","HSH","G","HS5","HSX5","HSB","HSC","HSD","HSE"): assert seed is not None, f"{h}：{label} 場景 log 裡找不到 seed"
    assign=lambda pi,ues=ues,label=label,seed=seed: T.scenario_configs(label or "T",ues,pi,seed,"tcp")
    rows=list(csv.DictReader(open(f"{SD}/{tag}_{h}.csv")))
    by_ue=collections.defaultdict(list)
    for r in rows: by_ue[r["ue_container"]].append((ts(r["timestamp"].split()[1]),r["achieved_mbps"],r["rtt_ms"]))
    for k,(t0,state,pi) in enumerate(st_lines):
        t1=st_lines[k+1][0] if k+1<len(st_lines) else t0+110
        phase_info.setdefault(h,[]).append((t0,t1,pi,state))
        cfgs=assign(pi)   # [(ploss, offered_sim_mbps, proto), ...]，順序同 ues
        for i,ue in enumerate(ues):
            pn,off=cfgs[i][0],cfgs[i][1]; tn=off
            xs=[(a,b) for (t,a,b) in by_ue[ue.container] if t0+30<=t<t1]
            tp=[float(a)/SPD for a,b in xs if a!=""]; rt=[float(b) for a,b in xs if b!=""]
            if off<=0: continue   # 目標為 0（閒置）不納入
            if len(tp)>=3: rec.append((ue.container,pi,state,off,st.mean(tp),(st.mean(rt) if rt else None),pn,tn))
# ---- per UE overall
print(f"### {tag}：逐 UE（模擬 Mbps = 牆鐘÷{SPD}；RTT sim = 牆鐘×{SPD}；主機 {HOSTS}）")
print("UE    全部達成  目標均值  壅塞相位達成/目標   正常相位達成/目標   RTT(sim,壅塞/正常)")
allue={}
for u in sorted({int(r[0].split("-")[-1]) for r in rec}):   # 有資料的 UE（UE1~16 access、UE17~24 relay 直連）
    c=f"rfsim5g-end-ue-{u}"; R=[r for r in rec if r[0]==c]
    def part(s):
        v=[r for r in R if r[2]==s]
        return (st.mean(r[4] for r in v),st.mean(r[3] for r in v),len(v),[r[5] for r in v if r[5] is not None]) if v else (float('nan'),float('nan'),0,[])
    cg=part("壅塞"); nm=part("正常")
    allue[u]=st.mean(r[4] for r in R)
    rc=st.mean(cg[3])*SPD if cg[3] else float('nan'); rn=st.mean(nm[3])*SPD if nm[3] else float('nan')
    print(f"UE{u:<3}{allue[u]:8.2f}{st.mean(r[3] for r in R):9.2f}   {cg[0]:6.2f}/{cg[1]:<5.2f}(n={cg[2]})   {nm[0]:6.2f}/{nm[1]:<5.2f}(n={nm[2]})   {rc:5.1f}/{rn:5.1f}")
# ---- congestion measured
phases=sorted({(r[1],r[2]) for r in rec})
print("\n### 壅塞驗證：各相位 過載UE比例(目標>=3 且 達成/目標<0.85) 與 送達總量（兩台主機合併，以 phase_index 對齊）")
tot_ph=collections.Counter(); over_ph={}
for pi,state in phases:
    R=[r for r in rec if r[1]==pi]
    over=sum(1 for r in R if r[3]>=3 and r[4]/r[3]<0.85)/len(R); deliv=sum(r[4] for r in R); off=sum(r[3] for r in R)
    over_ph[pi]=(state,over,deliv,off,len(R))
    print(f"phase_index={pi:>7} 狀態={state} UE數={len(R):2d} 過載UE比例={over*100:5.1f}%  總送達={deliv:6.1f}  總目標={off:6.1f}")
cg=[v for v in over_ph.values() if v[0]=="壅塞"]; nm=[v for v in over_ph.values() if v[0]=="正常"]
n=len(over_ph)
print(f"\n壅塞相位 {len(cg)}/{n} = {100*len(cg)/n:.0f}%（按相位個數；設計 5/11=45%）")
if cg: print(f"壅塞相位平均：過載UE {100*st.mean(v[1] for v in cg):.0f}%  總送達 {st.mean(v[2] for v in cg):.0f}  總目標 {st.mean(v[3] for v in cg):.0f}")
if nm: print(f"正常相位平均：過載UE {100*st.mean(v[1] for v in nm):.0f}%  總送達 {st.mean(v[2] for v in nm):.0f}  總目標 {st.mean(v[3] for v in nm):.0f}")
allr=len(rec); ov=sum(1 for r in rec if r[3]>=3 and r[4]/r[3]<0.85)
print(f"全部 UE-相位中過載比例 = {100*ov/allr:.1f}%（{ov}/{allr}）")
# ---- summary
rt_all=[r[5] for r in rec if r[5] is not None]
print(f"\n### 摘要：JFI(逐UE全程平均)={jfi(list(allue.values())):.4f}  平均吞吐量 sim={st.mean(allue.values()):.2f} Mbps  平均RTT sim={st.mean(rt_all)*SPD:.1f} ms")
for s in ("壅塞","正常"):
    per=collections.defaultdict(list)
    for r in rec:
        if r[2]==s: per[r[0]].append(r[4])
    if per:
        m=[st.mean(v) for v in per.values()]; rr=[r[5] for r in rec if r[2]==s and r[5] is not None]
        print(f"  {s}相位：JFI={jfi(m):.4f} 平均吞吐量 sim={st.mean(m):.2f} 平均RTT sim={st.mean(rr)*SPD:.1f} ms")
# ---- 需求滿足率的 JFI（達成/目標，逐相位、UE 間）：壅塞時才反映排程的公平性
print("\n### 需求滿足率 = 達成/目標（上限 1）；JFI 為 UE 間、逐相位計算後取平均")
for s in ("壅塞","正常"):
    js=[];means=[]
    for pi,state in phases:
        if state!=s: continue
        R=[min(1.0,r[4]/r[3]) for r in rec if r[1]==pi]
        js.append(jfi(R)); means.append(st.mean(R))
    if js: print(f"  {s}相位：平均滿足率={st.mean(means):.3f}  滿足率JFI={st.mean(js):.4f}（{len(js)} 個相位）")
sat=collections.defaultdict(list)
for r in rec:
    if r[2]=="壅塞": sat[r[0]].append(min(1.0,r[4]/r[3]))
print("  壅塞相位各 UE 平均滿足率：", "  ".join(f"UE{int(k.split('-')[-1])}={st.mean(v):.2f}" for k,v in sorted(sat.items(),key=lambda kv:int(kv[0].split('-')[-1]))))
