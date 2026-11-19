#!/usr/bin/env python3
"""调度台服务:真实时间跑模拟引擎 + HTTP API + 调度台页面。

    python3 server.py --port 8080
    打开 http://127.0.0.1:8080

页面按组实时显示谁在讲、谁在排队、每部终端在听哪个组;
调度员可以强拆、并组、拆组。后台模拟几百部终端随机发话。
"""

import argparse
import json
import os
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from trunking import Controller, Engine, GroupConfig, Terminal

GROUPS = [
    ("G1", "门机组", "queue", 20000, 8000),
    ("G2", "理货组", "queue", 20000, 8000),
    ("G3", "调度组", "queue", 30000, 10000),
    ("G4", "流机组", "queue", 15000, 6000),
    ("G5", "卡口组", "reject", 15000, 6000),
    ("G6", "安全监督", "queue", 30000, 10000),
    ("G7", "拖轮联络", "queue", 20000, 8000),
    ("G8", "泊位计划", "reject", 20000, 8000),
]

ROLES = ["门机司机", "理货员", "调度员", "流机司机", "卡口员", "安全员"]


class Sim:
    """真实时间驱动的模拟:引擎逻辑时钟按墙钟推进。"""

    def __init__(self, n_terminals=300):
        self.lock = threading.Lock()
        self.eng = Engine()
        self.ctl = Controller(self.eng)
        self.rng = random.Random(20261120)
        self.running = False
        for gid, name, mode, max_talk, hang in GROUPS:
            self.ctl.add_group(GroupConfig(gid=gid, name=name, floor_mode=mode,
                                           max_talk_ms=max_talk, hang_time_ms=hang))
        gids = [g[0] for g in GROUPS]
        for i in range(n_terminals):
            role = self.rng.choice(ROLES)
            k = 1 if self.rng.random() < 0.7 else 2
            groups = {g: self.rng.randint(1, 10) for g in self.rng.sample(gids, k)}
            self.ctl.add_terminal(Terminal(
                tid="T%03d" % i,
                name="%s-%02d" % (role, i % 50),
                priority=self.rng.choice([1, 1, 1, 2, 2, 3, 5]),
                groups=groups,
                powered=self.rng.random() > 0.05,
                latency_ms=self.rng.choice([0, 0, 0, 30, 120]),
            ))

    # ---------------- 模拟终端行为 ----------------

    def start(self):
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()
        with self.lock:
            self.eng.call_later(300, self._traffic_tick)

    def _loop(self):
        last = time.monotonic()
        while self.running:
            time.sleep(0.02)
            now = time.monotonic()
            elapsed = int((now - last) * 1000)
            last = now
            with self.lock:
                self.eng.run_until(self.eng.now + max(elapsed, 1))

    def _traffic_tick(self):
        rng = self.rng
        ctl = self.ctl
        tids = list(ctl.terminals)
        r = rng.random()
        if r < 0.60:
            tid = rng.choice(tids)
            t = ctl.terminals[tid]
            if t.powered:
                ctl.terminal_ptt_press(tid)
                hold = rng.choice([rng.randint(800, 4000), rng.randint(5000, 15000)])
                self.eng.call_later(hold + t.latency_ms, ctl.terminal_ptt_release, tid)
        elif r < 0.66:
            tid = rng.choice(tids)
            if ctl.terminals[tid].powered:
                ctl.terminal_ptt_press(tid, emergency=True)
                self.eng.call_later(rng.randint(1500, 5000),
                                    ctl.terminal_ptt_release, tid)
        elif r < 0.72:
            tid = rng.choice(tids)
            t = ctl.terminals[tid]
            ctl.terminal_power(tid, not t.powered)
        elif r < 0.80:
            tid = rng.choice(tids)
            t = ctl.terminals[tid]
            others = [g for g in t.groups if g != t.active_group]
            if others:
                ctl.terminal_switch_group(tid, rng.choice(others))
        self.eng.call_later(rng.randint(120, 500), self._traffic_tick)

    # ---------------- 调度台操作 ----------------

    def action(self, payload):
        with self.lock:
            op = payload.get("op")
            if op == "force_release":
                self.ctl.dispatcher_force_release(payload["gid"])
                return {"ok": True}
            if op == "merge":
                gids = payload.get("gids") or []
                if len(gids) < 2:
                    return {"ok": False, "error": "至少选择两个组"}
                mid = self.ctl.merge_groups(gids)
                return {"ok": True, "mid": mid}
            if op == "unmerge":
                self.ctl.unmerge(payload["mid"])
                return {"ok": True}
            return {"ok": False, "error": "未知操作"}

    def snapshot(self):
        with self.lock:
            return self.ctl.snapshot()


def make_handler(sim, page_path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/" or self.path.startswith("/index"):
                with open(page_path, "rb") as f:
                    self._send(200, f.read(), "text/html")
            elif self.path.startswith("/api/state"):
                self._send(200, json.dumps(sim.snapshot()))
            else:
                self._send(404, "{}")

        def do_POST(self):
            if self.path.startswith("/api/action"):
                n = int(self.headers.get("Content-Length", 0))
                try:
                    payload = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    payload = {}
                self._send(200, json.dumps(sim.action(payload)))
            else:
                self._send(404, "{}")

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--terminals", type=int, default=300)
    args = ap.parse_args()
    sim = Sim(n_terminals=args.terminals)
    sim.start()
    page = os.path.join(os.path.dirname(os.path.abspath(__file__)), "console.html")
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(sim, page))
    print("调度台: http://127.0.0.1:%d  (模拟终端 %d 部)" % (args.port, args.terminals))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        sim.running = False


if __name__ == "__main__":
    main()
