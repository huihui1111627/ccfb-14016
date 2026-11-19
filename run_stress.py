#!/usr/bin/env python3
"""随机压测:几百部终端乱序按键、长按不放、同毫秒并发、弱信号晚到、
紧急呼叫、调度员并组/拆组/强拆,每处理一个事件就校验一次系统不变式。

红线:任何一个组任何时刻都不许有两个话权持有者。

用法:
    python3 run_stress.py                      # 默认 20 个种子
    python3 run_stress.py --seed 42            # 复现单一种子
    python3 run_stress.py --terminals 500 --steps 20000
"""

import argparse
import random
import sys

from trunking import Controller, Engine, GroupConfig, Terminal


def build(rng, n_groups, n_terminals):
    eng = Engine()
    ctl = Controller(eng)
    for i in range(n_groups):
        ctl.add_group(GroupConfig(
            gid="G%d" % i,
            floor_mode=rng.choice(["queue", "reject"]),
            max_talk_ms=rng.choice([1500, 3000, 8000]),
            hang_time_ms=rng.choice([400, 1500, 4000]),
        ))
    gids = list(ctl.groups)
    for i in range(n_terminals):
        k = rng.randint(1, min(3, n_groups))
        groups = {g: rng.randint(1, 10) for g in rng.sample(gids, k)}
        ctl.add_terminal(Terminal(
            tid="T%d" % i,
            priority=rng.randint(1, 5),
            groups=groups,
            powered=rng.random() > 0.1,
            latency_ms=rng.choice([0, 0, 0, 20, 80, 200]),  # 有的信号不好
        ))
    return eng, ctl


def run_one(seed, n_terminals, n_groups, steps):
    rng = random.Random(seed)
    eng, ctl = build(rng, n_groups, n_terminals)
    tids = sorted(ctl.terminals)
    gids = sorted(ctl.groups)
    live_merges = []

    def op_release(tid):
        ctl.terminal_ptt_release(tid)

    def op_press(tid):
        ctl.terminal_ptt_press(tid)
        # 大部分人讲一会儿就松, 少数人按着不放
        hold = rng.choice([rng.randint(50, 1500), rng.randint(50, 1500),
                           rng.randint(5000, 25000)])
        eng.call_at(eng.now + hold, op_release, tid)

    def op_burst(gid):
        # 同一毫秒一起按:挑当前旋钮在同一组的一撮终端同时按下
        same = [t for t in tids if ctl.terminals[t].active_group == gid]
        rng.shuffle(same)
        for tid in same[:rng.randint(2, 6)]:
            op_press(tid)

    def op_emergency(tid):
        ctl.terminal_ptt_press(tid, emergency=True)
        eng.call_at(eng.now + rng.randint(500, 5000), op_release, tid)

    def op_power(tid):
        t = ctl.terminals[tid]
        ctl.terminal_power(tid, not t.powered)

    def op_switch(tid):
        t = ctl.terminals[tid]
        others = [g for g in t.groups if g != t.active_group]
        if others:
            ctl.terminal_switch_group(tid, rng.choice(others))

    def op_merge():
        gs = rng.sample(gids, rng.randint(2, min(4, len(gids))))
        live_merges.append(ctl.merge_groups(gs))

    def op_unmerge():
        if live_merges:
            ctl.unmerge(live_merges.pop(rng.randrange(len(live_merges))))

    def op_force_release():
        ctl.dispatcher_force_release(rng.choice(gids))

    t = 0
    for _ in range(steps):
        t += rng.randint(1, 30)
        r = rng.random()
        if r < 0.42:
            eng.call_at(t, op_press, rng.choice(tids))
        elif r < 0.48:
            eng.call_at(t, op_burst, rng.choice(gids))
        elif r < 0.52:
            eng.call_at(t, op_emergency, rng.choice(tids))
        elif r < 0.60:
            eng.call_at(t, op_power, rng.choice(tids))
        elif r < 0.68:
            eng.call_at(t, op_switch, rng.choice(tids))
        elif r < 0.72:
            eng.call_at(t, op_merge)
        elif r < 0.78:
            eng.call_at(t, op_unmerge)
        elif r < 0.84:
            eng.call_at(t, op_force_release)
        else:
            eng.call_at(t, op_release, rng.choice(tids))

    n_events = 0
    while eng.step():
        n_events += 1
        errs = ctl.check_invariants()
        if errs:
            print("✗ 不变式违反 seed=%d t=%d" % (seed, eng.now))
            for e in errs:
                print("    %s" % e)
            return False
    print("✓ seed=%-6d 终端=%d 组=%d 事件=%d" % (seed, n_terminals, n_groups, n_events))
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=None, help="只跑指定种子")
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--terminals", type=int, default=300)
    ap.add_argument("--groups", type=int, default=12)
    ap.add_argument("--steps", type=int, default=8000)
    args = ap.parse_args()
    seeds = [args.seed] if args.seed is not None else range(args.seeds)
    ok = all(run_one(s, args.terminals, args.groups, args.steps) for s in seeds)
    print("-" * 50)
    print("全部通过" if ok else "存在失败")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
