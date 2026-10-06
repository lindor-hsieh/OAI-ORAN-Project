#!/usr/bin/env python3
"""Reversible single-DU 2/4 active UE probe. Never edit deployment files.

UE3/4 original containers are stopped and renamed (retained), and temporary
clones connect to Node5. Finally originals return to Node6. A journal permits
explicit --restore after interruption. Wall durations are internal only;
results and progress report simulated time at S=0.3.
"""
import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
EXT = "192.168.72.135"

def command(args, host=None, check=True, timeout=90):
    if host:
        args = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, shlex.join(args)]
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"{shlex.join(args)}: {result.stderr[-1500:]}")
    return result

def inspect(name, host=None):
    return json.loads(command(["docker", "inspect", name], host).stdout)[0]

def channel(name, ploss=None, noise=None):
    cmds = ["channelmod show current"] if ploss is None else [
        f"channelmod modify 0 ploss {ploss}", f"channelmod modify 0 noise_power_dB {noise}"]
    shell = "exec 3<>/dev/tcp/127.0.0.1/9301; sleep 0.3; "
    shell += " ".join("echo " + shlex.quote(c) + " >&3; sleep 0.5;" for c in cmds)
    shell += " timeout 1 cat <&3"
    return command(["docker", "exec", name, "bash", "-c", shell], "pc2", check=False).stdout

def wait_link(name, seconds=100):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        addr = command(["docker", "exec", name, "ip", "-j", "addr", "show", "oaitun_ue1"], "pc2", False)
        if addr.returncode == 0 and '"local"' in addr.stdout:
            command(["docker", "exec", "-u", "0", name, "ip", "route", "replace", "default",
                     "via", "12.1.1.1", "dev", "oaitun_ue1"], "pc2")
            ping = command(["docker", "exec", name, "ping", "-c", "1", "-W", "2", EXT], "pc2", False)
            if ping.returncode == 0:
                return
        time.sleep(3)
    raise RuntimeError(f"{name} tunnel/ping did not recover")

def rnti(name):
    logs = command(["docker", "logs", "--tail", "250", name], "pc2")
    matches = re.findall(r"UE 0 RNTI ([0-9a-fA-F]+) stats", logs.stdout + logs.stderr)
    if not matches:
        raise RuntimeError(f"Cannot establish actual C-RNTI for {name}")
    return int(matches[-1], 16)

def write_mode(root, kind="pf", bad=(), trial=None):
    temp = root / "mode.next"
    temp.write_text(json.dumps({"kind": kind, "bad_rntis": list(bad), "trial": trial}))
    temp.replace(root / "mode.json")

def log(root, msg):
    line = time.strftime("%F %T") + " " + msg
    print(line, flush=True)
    with (root / "progress.log").open("a") as f:
        f.write(line + "\n")

def save(root, state):
    (root / "journal.json").write_text(json.dumps(state, indent=2))

def restore(root, state):
    errors = []
    write_mode(root)
    time.sleep(3)
    for u in range(1, 5):
        command(["docker", "exec", f"rfsim5g-end-ue-{u}", "pkill", "-f", "^iperf3 -c"], "pc2", check=False)
    # Only processes/containers created by this probe are stopped.
    if state.get("controller"):
        command(["docker", "stop", state["controller"]], check=False)
    for entry in reversed(state.get("moved", [])):
        try:
            name, old, archive = entry["name"], entry["old"], entry["archive"]
            orig = command(["docker", "inspect", old], "pc2", False)
            if orig.returncode == 0:
                current = command(["docker", "inspect", name], "pc2", False)
                if current.returncode == 0:
                    command(["docker", "stop", name], "pc2", False)
                    command(["docker", "rename", name, archive], "pc2")
                command(["docker", "rename", old, name], "pc2")
            if entry["running"]:
                command(["docker", "start", name], "pc2")
        except Exception as exc:
            errors.append(str(exc))
    if state.get("moved") and state.get("node6_routes"):
        # rfsim reconnect can stall after both clients disconnect. Restore both
        # original identities first, then restart only their original DU.
        command(["docker", "restart", "rfsim5g-iab-du-6"], "pc2")
        for route in state["node6_routes"]:
            if route.get("gateway"):
                args = ["docker", "exec", "-u", "0", "rfsim5g-iab-du-6", "ip", "route", "replace",
                        route["dst"], "via", route["gateway"]]
                # Docker may renumber interfaces after restart; let the kernel
                # resolve the interface from the saved gateway address.
                command(args, "pc2")
        for entry in state["moved"]:
            if entry["running"]:
                try:
                    wait_link(entry["name"])
                except Exception as exc:
                    errors.append(str(exc))
    for name, values in state.get("channels", {}).items():
        out = channel(name, *values)
        if "noise" not in out or "path loss" not in out:
            errors.append(f"channel restore not acknowledged: {name}")
    for name in state.get("inference_running", []):
        result = command(["docker", "start", name], check=False)
        if result.returncode:
            errors.append(result.stderr)
    # Check original IDs, commands and model bytes; keep all artifacts.
    for entry in state.get("moved", []):
        try:
            if inspect(entry["name"], "pc2")["Id"] != entry["id"]:
                errors.append(f"Original container identity mismatch: {entry['name']}")
        except Exception as exc:
            errors.append(str(exc))
    for name, digest in state.get("model_hashes", {}).items():
        result = command(["docker", "exec", name, "sha256sum", f"/app/models/model_node{name.split('node')[1]}.pt"], check=False)
        if result.returncode or result.stdout.split()[0] != digest:
            errors.append(f"Model checksum changed/unavailable: {name}")
    state["restored"] = not errors
    state["restore_errors"] = errors
    save(root, state)
    log(root, "RESTORED" if not errors else "RESTORE_ERRORS " + json.dumps(errors))
    if errors:
        raise RuntimeError("Restoration incomplete; consult journal.json")

def clone_ue(root, state, number):
    name = f"rfsim5g-end-ue-{number}"
    old = name + "_before_" + root.name
    archive = name + "_probe_" + root.name
    original = inspect(name, "pc2")
    entry = {"name": name, "old": old, "archive": archive,
             "id": original["Id"], "running": original["State"]["Running"]}
    state["moved"].append(entry)
    save(root, state)  # journal before the first mutation
    command(["docker", "stop", name], "pc2")
    command(["docker", "rename", name, old], "pc2")
    args = ["docker", "run", "-d", "--name", name, "--privileged",
            "--device", "/dev/net/tun:/dev/net/tun", "--restart", "no"]
    networks = list(original["NetworkSettings"]["Networks"])
    if len(networks) != 1:
        raise RuntimeError("Probe expects one existing internal UE network")
    args += ["--network", networks[0], "-e", "LD_LIBRARY_PATH=/usr/local/lib:/usr/local/lib/oai_libs"]
    for m in original["Mounts"]:
        args += ["-v", m["Source"] + ":" + m["Destination"] + (":ro" if not m["RW"] else "")]
    cmd = list(original["Config"]["Cmd"])
    cmd[cmd.index("--rfsimulator.serveraddr") + 1] = "192.168.74.20"
    cmd[cmd.index("--rfsimulator.serverport") + 1] = "4048"
    args += [original["Image"]] + cmd
    command(args, "pc2")
    wait_link(name)

def run_trial(root, count, kind, repeat, duration):
    trial = f"n{count}_{kind}_r{repeat}"
    bad = [rnti(f"rfsim5g-end-ue-{u}") for u in (2, 4)[:count // 2]]
    write_mode(root, kind, bad, trial)
    time.sleep(10)
    cfg = {"1": [22, 22 if count == 2 else 11, "tcp"],
           "2": [24, 6 if count == 2 else 3, "tcp"]}
    if count == 4:
        cfg.update({"3": [22, 11, "tcp"], "4": [24, 3, "tcp"]})
    logs = command(["docker", "logs", "--tail", "150", "rfsim5g-iab-du-5"], "pc2")
    (root / (trial + "_du_before.log")).write_text(logs.stdout + logs.stderr)
    log(root, f"START {trial}: {duration * .3:g} 秒模擬時間，總需求 28 模擬 Mbps")
    result = command(["python3", str(BASE / "iab/relay_ue_pilot.py"), "--host", "pc2",
                      "--cfg", json.dumps(cfg), "--duration", str(duration), "--warmup", "30", "--settle", "3"],
                     "pc2", timeout=duration + 100)
    (root / (trial + ".stdout")).write_text(result.stdout)
    (root / (trial + ".stderr")).write_text(result.stderr)
    data = json.loads(result.stdout.strip().splitlines()[-1])
    if any(x["samples"] < duration - 40 for x in data["ues"].values()):
        raise RuntimeError(f"Insufficient iperf samples: {trial}")
    data.update(trial=trial, active_ues=count, mode=kind, repeat=repeat,
                total_sim_mbps=sum(x["sim_mbps"] for x in data["ues"].values()), bad_rntis=bad)
    current_bad = [rnti(f"rfsim5g-end-ue-{u}") for u in (2, 4)[:count // 2]]
    if current_bad != bad:
        raise RuntimeError(f"C-RNTI changed during {trial}; do not interpret as a valid control comparison")
    controls = []
    for line in (root / "control.jsonl").read_text().splitlines():
        record = json.loads(line)
        if record["mode"].get("trial") == trial:
            controls.append(record)
    if len(controls) < duration // 2:
        raise RuntimeError(f"MAC report coverage insufficient: {trial}: {len(controls)}")
    if any(not set(bad).issubset({int(u['rnti']) for u in c['request'].get('ues', [])}) for c in controls):
        raise RuntimeError(f"Target RNTI missing from MAC reports during {trial}")
    data["mac_reports"] = len(controls)
    data["control_evidence"] = "issued allocations and subsequent MAC statistics; no independent mask readback"
    logs = command(["docker", "logs", "--tail", "800", "rfsim5g-iab-du-5"], "pc2")
    (root / (trial + "_du_after.log")).write_text(logs.stdout + logs.stderr)
    frames = re.findall(r"(\d+\.\d+) .*Frame\.Slot (\d+)\.0", logs.stdout + logs.stderr)
    speeds = []
    for (ta, fa), (tb, fb) in zip(frames, frames[1:]):
        dt = float(tb) - float(ta)
        if dt > 0:
            speeds.append(((int(fb) - int(fa)) % 1024) * .01 / dt)
    data["du_speed_median"] = sorted(speeds)[len(speeds)//2] if speeds else None
    if not speeds or data["du_speed_median"] < .29:
        raise RuntimeError(f"DU speed check failed: {trial}: {data['du_speed_median']}")
    with (root / "results.jsonl").open("a") as f:
        f.write(json.dumps(data) + "\n")
    log(root, f"DONE {trial}: {data['total_sim_mbps']:.3f} 模擬 Mbps")
    write_mode(root)
    time.sleep(8)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--restore", action="store_true")
    ap.add_argument("--duration", type=int, default=120, help="internal wall-clock duration")
    args = ap.parse_args()
    root = Path(args.out).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if args.restore:
        restore(root, json.loads((root / "journal.json").read_text()))
        return
    if (root / "journal.json").exists():
        raise RuntimeError("Use a fresh output directory; existing artifacts are retained")
    # Refuse to take over a live training or measurement run.
    for host in (None, "pc2", "pc3"):
        ps = command(["ps", "-eo", "comm,args"], host).stdout
        if any(line.startswith(("python", "bash")) and any(s in line for s in
               ("traffic_scenario.py --", "training_watchdog.sh", "measure_stage.py --")) for line in ps.splitlines()):
            raise RuntimeError(f"Active experiment on {host or 'pc1'}")
    # Docker Up/healthcheck does not establish that the xApp sees E2 nodes.
    probe = command(["docker", "logs", "--tail", "20", "xapp-node5"])
    text = probe.stdout + probe.stderr
    if "等待 Node 5" in text:
        raise RuntimeError("Node5 xApp has not connected to its target E2 node; repair connection before topology changes")
    state = {"moved": [], "channels": {}, "inference_running": [], "model_hashes": {}, "restored": False}
    state["node6_routes"] = json.loads(command(["docker", "exec", "rfsim5g-iab-du-6", "ip", "-j", "route"], "pc2").stdout)
    for n in range(1, 13):
        name = f"inference-node{n}"
        info = inspect(name)
        (root / (name + "_before.json")).write_text(json.dumps(info, indent=2))
        if info["State"]["Running"]:
            state["inference_running"].append(name)
            digest = command(["docker", "exec", name, "sha256sum", f"/app/models/model_node{n}.pt"])
            state["model_hashes"][name] = digest.stdout.split()[0]
    for u in range(1, 5):
        name = f"rfsim5g-end-ue-{u}"
        text = channel(name)
        (root / (name + "_channel_before.txt")).write_text(text)
        match = re.search(r"path loss:\s*([-\d.]+)\s+noise:\s*([-\d.]+)", text)
        if not match:
            raise RuntimeError(f"Cannot snapshot channel: {name}")
        state["channels"][name] = [float(match[1]), float(match[2])]
    save(root, state)
    def interrupted(*_):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    try:
        clone_ue(root, state, 3)
        clone_ue(root, state, 4)
        for u in range(1, 5):
            wait_link(f"rfsim5g-end-ue-{u}")
            sys.path.insert(0, str(BASE / "scenarios"))
            import traffic_scenario as T
            p, noise = T.degrade_settings(22 if u % 2 else 24)
            if "noise" not in channel(f"rfsim5g-end-ue-{u}", p, noise):
                raise RuntimeError("Channel configuration failed")
        image = inspect("inference-node5")["Image"]
        write_mode(root)
        command(["docker", "stop"] + state["inference_running"])
        state["controller"] = "ue-scale-controller-" + root.name
        save(root, state)
        command(["docker", "run", "-d", "--name", state["controller"], "--network", "host",
                 "--cpuset-cpus", "12-15", "-v", "/tmp:/tmp", "-v", str(BASE / "iab") + ":/probe:ro",
                 image, "python3", "/probe/ue_scale_controller.py", str(root)])
        time.sleep(10)
        # Reverse mode order on repeat 2 to reduce order effects.
        for count in (2, 4):
            for repeat, modes in ((1, ("pf", "mask", "cap")), (2, ("cap", "mask", "pf"))):
                for kind in modes:
                    run_trial(root, count, kind, repeat, args.duration)
        rows = [json.loads(line) for line in (root / "results.jsonl").read_text().splitlines()]
        lines = ["# 單 DU 活躍 UE 數量驗證", "",
                 "Node5 四個 UE 保持附著，只改變活躍流量數量；UE3/4 暫由 Node6 移入。",
                 "固定總需求 28 模擬 Mbps，good/bad 需求合計 22/6，L=22/24。",
                 "其他分支無測試流量；此為單 DU 機制實驗，不是完整 HSE 測試。",
                 "PRB 控制是每 UE 上限 32/106，並非直接 PRB 分配。",
                 "每組 36 秒模擬時間，略過前 9 秒；第二次反轉模式順序。",
                 "控制記錄不等同 MAC mask 實際讀回；不得宣稱精確生效比例。", "",
                 "| 活躍 UE | 模式 | 第一次 | 第二次 | 平均模擬 Mbps | 相對 PF |", "|---|---|---|---|---|---|"]
        for count in (2, 4):
            pf = sum(r['total_sim_mbps'] for r in rows if r['active_ues'] == count and r['mode'] == 'pf') / 2
            for kind in ('pf', 'mask', 'cap'):
                pair = [r['total_sim_mbps'] for r in rows if r['active_ues'] == count and r['mode'] == kind]
                mean = sum(pair) / len(pair)
                lines.append(f"| {count} | {kind} | {pair[0]:.3f} | {pair[1]:.3f} | {mean:.3f} | {100*(mean/pf-1):+.1f}% |")
        (root / 'report.md').write_text('\n'.join(lines) + '\n')
        state["completed"] = True
        save(root, state)
    finally:
        restore(root, state)

if __name__ == "__main__":
    main()
