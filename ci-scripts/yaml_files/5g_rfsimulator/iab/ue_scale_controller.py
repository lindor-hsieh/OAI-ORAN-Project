#!/usr/bin/env python3
"""Temporary, model-free xApp responder for ue_scale_probe.py. No Mongo writes."""
import json
import signal
import sys
import time
from pathlib import Path

import zmq

root = Path(sys.argv[1])
ctx = zmq.Context()
poller = zmq.Poller()
sockets = {}
running = True

def stop(*_):
    global running
    running = False

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
for node in range(1, 13):
    sock = ctx.socket(zmq.REP)
    sock.setsockopt(zmq.LINGER, 0)
    sock.bind(f"ipc:///tmp/zmq_node{node}_inference.ipc")
    sockets[sock] = node
    poller.register(sock, zmq.POLLIN)

with (root / "control.jsonl").open("a", buffering=1) as log:
    while running:
        for sock in dict(poller.poll(100)):
            node = sockets[sock]
            try:
                request = sock.recv_json()
                try:
                    mode = json.loads((root / "mode.json").read_text())
                except (OSError, ValueError):
                    mode = {"kind": "pf", "bad_rntis": []}
                bad = set(mode.get("bad_rntis", []))
                allocations = []
                for ue in request.get("ues", []):
                    rnti = int(ue["rnti"])
                    limited = node == 5 and rnti in bad
                    allocations.append({"rnti": rnti,
                        "prb_abs": 32 if limited and mode["kind"] == "cap" else 106,
                        "slot_mask": 0x1111 if limited and mode["kind"] == "mask" else 0xffff})
                # Respond before disk I/O: the existing C xApp has a short timeout.
                sock.send_json({"allocations": allocations})
                if node == 5:
                    log.write(json.dumps({"time": time.time(), "mode": mode,
                        "request": request, "allocations": allocations}) + "\n")
            except Exception as exc:
                print(f"node={node}: {exc}", flush=True)
                raise
for sock in sockets:
    sock.close()
ctx.term()
