"""验收剧本 DSL 与执行器。

剧本写法:
    sc = Scenario("S2 话权排队")
    sc.group("G1", floor_mode="queue", max_talk_ms=3000, hang_time_ms=1000)
    sc.terminal("T1", priority=1, groups={"G1": 1})
    sc.terminal("T2", priority=1, groups={"G1": 1}, latency_ms=80)  # 信号不好
    sc.ptt(0, "T1")            # t=0 按下
    sc.ptt(10, "T2")           # t=10 按下(消息 t=90 才到)
    sc.release(100, "T1")      # t=100 松键
    sc.expect_call(200, "G1", holder="T2", queue=[])
    sc.expect_listening(200, "T1", "G1")

终端信令按 (剧本时刻 + 终端 latency_ms) 到达控制器;检查点在指定时刻
(该时刻及之前到达的事件全部处理完后)逐一比对期望状态。
"""

from __future__ import annotations

from .controller import Controller
from .engine import Engine
from .models import GroupConfig, Terminal

ANY = object()  # expect_call 里表示"该项不关心"


class Scenario:
    def __init__(self, name):
        self.name = name
        self._groups = []
        self._terminals = []
        self._events = []      # (ts, tid或None, fn)
        self._checks = []      # (ts, 描述, fn)
        self._merge_tags = {}

    # ---------------- 配置 ----------------

    def group(self, gid, floor_mode="queue", max_talk_ms=30_000, hang_time_ms=5_000,
              name=None):
        self._groups.append(GroupConfig(gid=gid, name=name or gid,
                                        floor_mode=floor_mode,
                                        max_talk_ms=max_talk_ms,
                                        hang_time_ms=hang_time_ms))
        return self

    def terminal(self, tid, priority=1, groups=None, active=None, powered=True,
                 latency_ms=0):
        self._terminals.append(Terminal(tid=tid, priority=priority,
                                        groups=groups or {}, active_group=active,
                                        powered=powered, latency_ms=latency_ms))
        return self

    # ---------------- 事件 ----------------

    def _add(self, ts, tid, fn):
        self._events.append((ts, tid, fn))
        return self

    def ptt(self, ts, tid):
        return self._add(ts, tid, lambda ctl: ctl.terminal_ptt_press(tid))

    def emergency(self, ts, tid):
        return self._add(ts, tid, lambda ctl: ctl.terminal_ptt_press(tid, emergency=True))

    def release(self, ts, tid):
        return self._add(ts, tid, lambda ctl: ctl.terminal_ptt_release(tid))

    def power(self, ts, tid, on):
        return self._add(ts, tid, lambda ctl: ctl.terminal_power(tid, on))

    def switch(self, ts, tid, gid):
        return self._add(ts, tid, lambda ctl: ctl.terminal_switch_group(tid, gid))

    def force_release(self, ts, gid):
        return self._add(ts, None, lambda ctl: ctl.dispatcher_force_release(gid))

    def merge(self, ts, tag, gids):
        def fn(ctl):
            self._merge_tags[tag] = ctl.merge_groups(gids)
        return self._add(ts, None, fn)

    def unmerge(self, ts, tag):
        def fn(ctl):
            ctl.unmerge(self._merge_tags[tag])
        return self._add(ts, None, fn)

    # ---------------- 期望 ----------------

    def expect_call(self, ts, gid, holder=ANY, queue=ANY, emergency=ANY,
                    participants=ANY):
        def check(ctl):
            call = ctl.group_call.get(gid)
            if call is None:
                return ["%s 期望有进行中的呼叫, 实际没有" % gid]
            errs = []
            if holder is not ANY and call.holder != holder:
                errs.append("%s 话权期望 %s, 实际 %s" % (gid, holder, call.holder))
            if queue is not ANY:
                actual = [e.tid for e in call.queue]
                if actual != list(queue):
                    errs.append("%s 排队期望 %s, 实际 %s" % (gid, list(queue), actual))
            if emergency is not ANY and call.emergency != emergency:
                errs.append("%s 紧急标记期望 %s, 实际 %s" % (gid, emergency, call.emergency))
            if participants is not ANY and call.participants != set(participants):
                errs.append("%s 参与者期望 %s, 实际 %s"
                            % (gid, sorted(participants), sorted(call.participants)))
            return errs
        self._checks.append((ts, "expect_call(%s)" % gid, check))
        return self

    def expect_no_call(self, ts, gid):
        def check(ctl):
            if ctl.group_call.get(gid) is not None:
                return ["%s 期望呼叫已拆除, 实际仍在" % gid]
            return []
        self._checks.append((ts, "expect_no_call(%s)" % gid, check))
        return self

    def expect_listening(self, ts, tid, gid):
        def check(ctl):
            actual = ctl.listening_group(tid)
            if actual != gid:
                return ["%s 期望在听 %s, 实际在听 %s" % (tid, gid, actual)]
            return []
        self._checks.append((ts, "expect_listening(%s)" % tid, check))
        return self

    def expect_notified(self, ts, tid, ntype, **match):
        """到 ts 为止, 终端应收到过某类通知(可附带字段匹配)。"""
        def check(ctl):
            for t, n, data in ctl.notifications.get(tid, []):
                if t > ts or n != ntype:
                    continue
                if all(data.get(k) == v for k, v in match.items()):
                    return []
            return ["%s 到 t=%d 未收到通知 %s %s" % (tid, ts, ntype, match or "")]
        self._checks.append((ts, "expect_notified(%s,%s)" % (tid, ntype), check))
        return self


def run_scenario(sc):
    """执行剧本, 返回 (controller, 失败列表)。每个检查点同时校验系统不变式。"""
    eng = Engine()
    ctl = Controller(eng)
    for g in sc._groups:
        ctl.add_group(g)
    for t in sc._terminals:
        ctl.add_terminal(t)
    latency = {t.tid: t.latency_ms for t in sc._terminals}
    for ts, tid, fn in sc._events:
        eng.call_at(ts + latency.get(tid, 0), fn, ctl)
    failures = []
    for ts, desc, check in sorted(sc._checks, key=lambda c: c[0]):
        eng.run_until(ts)
        for e in check(ctl):
            failures.append("[t=%d] %s: %s" % (ts, desc, e))
        for e in ctl.check_invariants():
            failures.append("[t=%d] 不变式违反: %s" % (ts, e))
    eng.run_all()
    for e in ctl.check_invariants():
        failures.append("[结束] 不变式违反: %s" % e)
    return ctl, failures
