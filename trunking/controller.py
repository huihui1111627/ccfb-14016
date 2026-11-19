"""呼叫控制与话权管理核心。

职责:
- 组呼的建立 / 中途加入 / 空闲自动拆除
- 话权授予、排队、拒绝、限时强收、优先级抢占、紧急呼叫
- 调度员并组 / 拆组 / 强拆
- 终端扫描收听组的计算

所有状态变更都发生在 Engine 的事件里(单线程),因此不需要锁;
真实部署时把 Engine 换成网络收发即可,状态机不变。
"""

from __future__ import annotations

import itertools
from collections import defaultdict

from .engine import Engine
from .models import EMERGENCY_PRIORITY, Call, FloorMode, GroupConfig, QueueEntry, Terminal


class Controller:
    def __init__(self, engine=None):
        self.engine = engine or Engine()
        self.groups = {}          # gid -> GroupConfig
        self.terminals = {}       # tid -> Terminal
        self.calls = {}           # cid -> Call
        self.group_call = {}      # gid -> Call (每个组至多一个进行中的呼叫)
        self.merges = {}         # mid -> set(gid)  调度员并组配置
        self.gid_to_merge = {}    # gid -> mid
        self._cid = itertools.count(1)
        self._mid = itertools.count(1)
        self._seq = itertools.count()
        # 下发给终端的信令通知: tid -> [(ts, 类型, 数据)]
        self.notifications = defaultdict(list)
        self.log = []             # 全局信令日志, 便于排查和验收

    # ---------------------------------------------------------------- 基础配置

    def add_group(self, cfg: GroupConfig):
        self.groups[cfg.gid] = cfg

    def add_terminal(self, t: Terminal):
        self.terminals[t.tid] = t

    # ---------------------------------------------------------------- 通知

    def notify(self, tid, ntype, **data):
        rec = (self.engine.now, ntype, data)
        self.notifications[tid].append(rec)
        self.log.append((self.engine.now, tid, ntype, data))

    def _broadcast_queue(self, call):
        q = [e.tid for e in call.queue]
        for tid in call.participants:
            self.notify(tid, "QUEUE_UPDATE", cid=call.cid, queue=list(q))

    # ---------------------------------------------------------------- 呼叫生命周期

    def _join(self, call, tid):
        """把终端拉进呼叫;中途加入的终端立刻被告知当前谁在讲。"""
        if tid in call.participants:
            return
        call.participants.add(tid)
        self.notify(tid, "CALL_JOINED", cid=call.cid, gids=sorted(call.gids),
                    holder=call.holder, emergency=call.emergency)

    def _setup_call(self, gid, emergency=False):
        """组呼不需要被叫应答,有人按键即建立,组内在线终端被拉进来。"""
        gids = {gid}
        mid = self.gid_to_merge.get(gid)
        if mid is not None and mid in self.merges:
            gids = set(self.merges[mid])  # 并组期间, 任一组成员按键建立的是合并大组呼叫
        else:
            mid = None
        call = Call(cid=next(self._cid), gids=gids, created_ts=self.engine.now,
                    emergency=emergency, merge_id=mid)
        self.calls[call.cid] = call
        for g in gids:
            self.group_call[g] = call
        for t in self.terminals.values():
            if t.powered and any(g in t.groups for g in gids):
                self._join(call, t.tid)
        return call

    def _teardown(self, call, reason):
        for tid in sorted(call.participants):
            self.notify(tid, "CALL_ENDED", cid=call.cid, reason=reason)
        for g in call.gids:
            self.group_call.pop(g, None)
        self.calls.pop(call.cid, None)
        # 注意: 并组配置(merges)不随呼叫拆除, 由调度员显式拆开

    def _arm_hang_timer(self, call):
        """没人讲话时启动挂机定时, 超时自动拆线。"""
        call.hang_gen += 1
        gen = call.hang_gen
        hang = min(self.groups[g].hang_time_ms for g in call.gids)
        self.engine.call_later(hang, self._on_hang_timeout, call.cid, gen)

    def _on_hang_timeout(self, cid, gen):
        call = self.calls.get(cid)
        if call is None or gen != call.hang_gen or call.holder is not None:
            return
        self._teardown(call, "HANG_TIMEOUT")

    def _on_talk_timeout(self, cid, gen):
        call = self.calls.get(cid)
        if call is None or gen != call.talk_gen or call.holder is None:
            return
        self._revoke(call, "MAX_TALK")
        self._next_floor(call)

    # ---------------------------------------------------------------- 话权控制

    def _request_floor(self, call, tid, gid, prio):
        if call.holder == tid:
            return  # 重复按键, 忽略
        # 拿到话权(直接授予或抢占)前, 先把自己旧的排队申请撤掉
        if any(e.tid == tid for e in call.queue):
            call.queue = [e for e in call.queue if e.tid != tid]
            self._broadcast_queue(call)
        if call.holder is None:
            self._grant(call, tid, gid, prio)
            return
        if prio > call.holder_priority:
            self._preempt(call, tid, gid, prio)
            return
        cfg = self.groups[gid]
        if cfg.floor_mode == FloorMode.QUEUE:
            if not any(e.tid == tid for e in call.queue):
                call.queue.append(QueueEntry(tid, gid, self.engine.now,
                                             next(self._seq), prio))
                self.notify(tid, "FLOOR_QUEUED", cid=call.cid, pos=len(call.queue))
                self._broadcast_queue(call)
        else:
            self.notify(tid, "FLOOR_DENIED", cid=call.cid, holder=call.holder)

    def _grant(self, call, tid, gid, prio):
        call.holder = tid
        call.holder_gid = gid
        call.holder_priority = prio
        call.hang_gen += 1   # 有人在讲, 取消挂机定时
        call.talk_gen += 1
        gen = call.talk_gen
        max_talk = min(self.groups[g].max_talk_ms for g in call.gids)
        self.engine.call_later(max_talk, self._on_talk_timeout, call.cid, gen)
        self.notify(tid, "FLOOR_GRANTED", cid=call.cid)
        # 通知全组现在是谁在讲
        for other in call.participants:
            if other != tid:
                self.notify(other, "FLOOR_TAKEN", cid=call.cid, holder=tid)

    def _revoke(self, call, reason):
        """收回当前话权(松键/超时/抢占/强拆/掉线)。只收回,不转手。"""
        old = call.holder
        if old is None:
            return
        call.holder = None
        call.holder_priority = 0
        call.talk_gen += 1
        self.notify(old, "FLOOR_REVOKED", cid=call.cid, reason=reason)

    def _preempt(self, call, tid, gid, prio):
        """高优先级打断正在讲的低优先级。被打断者收到明确通知,
        排队模式的组里被排到队首(松键/收回后第一个补讲)。"""
        old, old_gid, old_prio = call.holder, call.holder_gid, call.holder_priority
        call.holder = None
        call.holder_priority = 0
        call.talk_gen += 1
        self.notify(old, "FLOOR_PREEMPTED", cid=call.cid, by=tid)
        if self.groups[old_gid].floor_mode == FloorMode.QUEUE:
            call.queue.insert(0, QueueEntry(old, old_gid, self.engine.now,
                                            next(self._seq), old_prio))
        self._grant(call, tid, gid, prio)
        self._broadcast_queue(call)

    def _next_floor(self, call):
        """话权空出:给排队的下一个;没人排则启动挂机定时。"""
        while call.queue:
            e = call.queue.pop(0)
            if e.tid in call.participants and self.terminals[e.tid].powered:
                self._grant(call, e.tid, e.gid, e.priority)
                self._broadcast_queue(call)
                return
        self._broadcast_queue(call)
        self._arm_hang_timer(call)

    # ---------------------------------------------------------------- 终端信令入口

    def terminal_ptt_press(self, tid, emergency=False):
        t = self.terminals.get(tid)
        if t is None or not t.powered or not t.active_group:
            return
        gid = t.active_group
        if emergency:
            # 紧急呼叫:先把自己当前所在的其它呼叫拆掉
            self._detach_from_other_calls(tid, keep_gid=gid)
        else:
            # 终端不能在两个呼叫里同时讲话/等话权:
            # 换组按键视为先松开原来的, 其它呼叫里的排队申请一并取消
            self._release_if_holder_elsewhere(tid, gid)
            self._cancel_queue_elsewhere(tid, gid)
        call = self.group_call.get(gid)
        if call is None:
            call = self._setup_call(gid, emergency=emergency)
        elif emergency and not call.emergency:
            call.emergency = True
            self._force_pull(call)  # 组里所有人不管在干什么都被强拉进来
        if tid not in call.participants:
            self._join(call, tid)
        prio = EMERGENCY_PRIORITY if emergency else t.priority
        self._request_floor(call, tid, gid, prio)

    def terminal_ptt_release(self, tid):
        """松键:释放话权给排队的下一个;还没轮到的话权申请一并取消。"""
        for call in list(self.calls.values()):
            if any(e.tid == tid for e in call.queue):
                call.queue = [e for e in call.queue if e.tid != tid]
                self._broadcast_queue(call)
            if call.holder == tid:
                self._revoke(call, "RELEASE")
                self._next_floor(call)

    def terminal_power(self, tid, on):
        t = self.terminals.get(tid)
        if t is None or t.powered == on:
            return
        t.powered = on
        if on:
            # 开机时呼叫已在进行 -> 中途加入
            for gid in t.groups:
                call = self.group_call.get(gid)
                if call is not None:
                    self._join(call, tid)
        else:
            for call in list(self.calls.values()):
                if tid not in call.participants:
                    continue
                call.participants.discard(tid)
                if any(e.tid == tid for e in call.queue):
                    call.queue = [e for e in call.queue if e.tid != tid]
                    self._broadcast_queue(call)
                if call.holder == tid:
                    self._revoke(call, "POWER_OFF")
                    self._next_floor(call)
                if not call.participants:
                    self._teardown(call, "EMPTY")

    def terminal_switch_group(self, tid, gid):
        """切组:呼叫已在进行则中途加入。原所在各组的收听关系保留(扫描)。"""
        t = self.terminals.get(tid)
        if t is None or gid not in t.groups:
            return
        t.active_group = gid
        if t.powered:
            call = self.group_call.get(gid)
            if call is not None:
                self._join(call, tid)

    def _release_if_holder_elsewhere(self, tid, gid):
        for call in list(self.calls.values()):
            if gid in call.gids:
                continue
            if call.holder == tid:
                self._revoke(call, "SWITCH_GROUP")
                self._next_floor(call)

    def _cancel_queue_elsewhere(self, tid, keep_gid):
        for call in list(self.calls.values()):
            if keep_gid in call.gids:
                continue
            if any(e.tid == tid for e in call.queue):
                call.queue = [e for e in call.queue if e.tid != tid]
                self._broadcast_queue(call)

    def _detach_from_other_calls(self, tid, keep_gid):
        """紧急呼叫发起前,把终端从其它呼叫中退出(话权释放、排队取消)。"""
        for call in list(self.calls.values()):
            if keep_gid in call.gids:
                continue
            if tid in call.participants:
                call.participants.discard(tid)
                self.notify(tid, "CALL_LEFT", cid=call.cid, reason="EMERGENCY")
            if any(e.tid == tid for e in call.queue):
                call.queue = [e for e in call.queue if e.tid != tid]
                self._broadcast_queue(call)
            if call.holder == tid:
                self._revoke(call, "EMERGENCY_EXIT")
                self._next_floor(call)
            if not call.participants:
                self._teardown(call, "EMPTY")

    def _force_pull(self, call):
        """紧急呼叫:组里所有在线成员不管在干什么都被强拉进来。"""
        for t in self.terminals.values():
            if not t.powered:
                continue
            if any(g in t.groups for g in call.gids) and t.tid not in call.participants:
                call.participants.add(t.tid)
                self.notify(t.tid, "EMERGENCY_FORCED", cid=call.cid, holder=call.holder)

    # ---------------------------------------------------------------- 调度台操作

    def dispatcher_force_release(self, gid):
        """强拆:收回该组(或其所在并组)当前话权,交给排队的下一个。"""
        call = self.group_call.get(gid)
        if call is not None and call.holder is not None:
            self._revoke(call, "DISPATCHER")
            self._next_floor(call)

    def merge_groups(self, gids):
        """并组:几个组合成一个大组一起讲。

        各组正在进行的呼叫合并成一路:话权持有者优先级最高的那路保留话权,
        其余在讲的被明确通知打断并排到队首;各组的排队申请按请求时间先后
        合并,顺序不乱。
        """
        all_gids = set()
        for g in gids:
            all_gids.add(g)
            old_mid = self.gid_to_merge.get(g)
            if old_mid is not None and old_mid in self.merges:
                all_gids |= self.merges.pop(old_mid)
        mid = "M%d" % next(self._mid)
        self.merges[mid] = all_gids
        for g in all_gids:
            self.gid_to_merge[g] = mid

        existing = []
        for g in all_gids:
            c = self.group_call.get(g)
            if c is not None and c not in existing:
                existing.append(c)
        if not existing:
            return mid

        # 主呼叫:话权持有者优先级最高;平手取建立早的
        base = max(existing, key=lambda c: (c.holder_priority if c.holder else -1,
                                            -c.created_ts))
        base.merge_id = mid
        base.gids = set(all_gids)
        preempted = []
        for c in existing:
            if c is base:
                continue
            if c.holder is not None:
                preempted.append((c.holder, c.holder_gid, c.holder_priority))
                self.notify(c.holder, "FLOOR_PREEMPTED", cid=base.cid, by="MERGE")
                c.holder = None
                c.holder_priority = 0
                c.talk_gen += 1
            # 排队申请按请求时间先后合并; 同一终端可能在两路都排过队, 去重留最早
            merged_q = sorted(base.queue + c.queue, key=lambda e: (e.ts, e.seq))
            seen = set()
            base.queue = [e for e in merged_q if not (e.tid in seen or seen.add(e.tid))]
            base.participants |= c.participants
            base.emergency = base.emergency or c.emergency
            self.calls.pop(c.cid, None)
        # 被并组打断的排在队首(排队模式的组)
        for tid, gid, prio in preempted:
            if self.groups[gid].floor_mode == FloorMode.QUEUE:
                base.queue.insert(0, QueueEntry(tid, gid, self.engine.now,
                                                next(self._seq), prio))
        for g in all_gids:
            self.group_call[g] = base
        if base.holder is None:
            if base.queue:
                self._next_floor(base)
            else:
                self._arm_hang_timer(base)
        for tid in sorted(base.participants):
            self.notify(tid, "GROUPS_MERGED", mid=mid, gids=sorted(all_gids),
                        holder=base.holder)
        return mid

    def unmerge(self, mid):
        """拆组:各回各组。话权留在持有者申请的组;排队申请各回各组且顺序不乱。"""
        gids = self.merges.pop(mid, None)
        if gids is None:
            return
        for g in gids:
            self.gid_to_merge.pop(g, None)
        call = next((c for c in self.calls.values() if c.merge_id == mid), None)
        if call is None:
            return
        call.merge_id = None
        new_calls = {}
        for g in sorted(gids):
            nc = Call(cid=next(self._cid), gids={g}, created_ts=self.engine.now,
                      emergency=call.emergency)
            self.calls[nc.cid] = nc
            self.group_call[g] = nc
            new_calls[g] = nc
        for tid in call.participants:
            t = self.terminals[tid]
            for g in gids:
                if g in t.groups:
                    new_calls[g].participants.add(tid)
        for e in call.queue:  # 顺序保持
            if e.gid in new_calls:
                new_calls[e.gid].queue.append(e)
        if call.holder is not None and call.holder_gid in new_calls:
            hc = new_calls[call.holder_gid]
            hc.holder, hc.holder_gid, hc.holder_priority = (
                call.holder, call.holder_gid, call.holder_priority)
            hc.talk_gen += 1
            gen = hc.talk_gen
            max_talk = min(self.groups[g].max_talk_ms for g in hc.gids)
            self.engine.call_later(max_talk, self._on_talk_timeout, hc.cid, gen)
        self.calls.pop(call.cid, None)
        for g, nc in new_calls.items():
            if not nc.participants:
                self.calls.pop(nc.cid, None)
                self.group_call.pop(g, None)
                continue
            if nc.holder is None:
                if nc.queue:
                    self._next_floor(nc)
                else:
                    self._arm_hang_timer(nc)
            for tid in sorted(nc.participants):
                self.notify(tid, "GROUPS_UNMERGED", mid=mid, gid=g, holder=nc.holder)

    # ---------------------------------------------------------------- 收听组计算

    def listening_group(self, tid):
        """终端此刻在听哪个组。

        正在讲的听自己的呼叫;否则紧急呼叫优先,再按组的扫描优先级,
        平手优先当前旋钮所在组。
        """
        t = self.terminals.get(tid)
        if t is None or not t.powered:
            return None
        best, best_key = None, None
        for call in self.calls.values():
            if tid not in call.participants:
                continue
            for g in call.gids:
                if g not in t.groups:
                    continue
                key = (1 if call.emergency else 0,
                       t.groups[g],
                       1 if g == t.active_group else 0,
                       g)
                if best_key is None or key > best_key:
                    best, best_key = g, key
        return best

    # ---------------------------------------------------------------- 快照与不变式

    def snapshot(self):
        """调度台界面用的全量状态。"""
        groups = []
        for gid in sorted(self.groups):
            cfg = self.groups[gid]
            call = self.group_call.get(gid)
            groups.append({
                "gid": gid,
                "name": cfg.name,
                "floor_mode": cfg.floor_mode.value,
                "call": None if call is None else {
                    "cid": call.cid,
                    "gids": sorted(call.gids),
                    "holder": call.holder,
                    "queue": [e.tid for e in call.queue],
                    "participants": sorted(call.participants),
                    "emergency": call.emergency,
                    "merge_id": call.merge_id,
                },
            })
        terminals = []
        for tid in sorted(self.terminals):
            t = self.terminals[tid]
            terminals.append({
                "tid": tid,
                "name": t.name,
                "priority": t.priority,
                "powered": t.powered,
                "active_group": t.active_group,
                "listening": self.listening_group(tid),
            })
        return {
            "now": self.engine.now,
            "groups": groups,
            "terminals": terminals,
            "merges": {mid: sorted(gs) for mid, gs in self.merges.items()},
        }

    def check_invariants(self):
        """压测/验收用:任何时刻都必须成立的条件。"""
        errs = []
        gid_owner = {}
        holders = []
        for call in self.calls.values():
            if call.holder is not None:
                holders.append((call.cid, call.holder))
                if call.holder not in call.participants:
                    errs.append("呼叫%d 话权持有者 %s 不在参与者中" % (call.cid, call.holder))
            q = [e.tid for e in call.queue]
            if len(set(q)) != len(q):
                errs.append("呼叫%d 排队队列有重复: %s" % (call.cid, q))
            if call.holder is not None and call.holder in q:
                errs.append("呼叫%d 持有者 %s 同时在排队" % (call.cid, call.holder))
            for g in call.gids:
                if g in gid_owner:
                    errs.append("组 %s 同时属于呼叫%d和呼叫%d"
                                % (g, gid_owner[g], call.cid))
                gid_owner[g] = call.cid
            for e in call.queue:
                if e.gid not in call.gids:
                    errs.append("呼叫%d 队列项 %s 的组 %s 不在呼叫中"
                                % (call.cid, e.tid, e.gid))
        # 核心红线:任何一个组任何时刻不许有两个话权持有者
        tids = [h for _, h in holders]
        if len(set(tids)) != len(tids):
            errs.append("同一终端持有多个呼叫的话权: %s" % holders)
        for gid, call in self.group_call.items():
            if gid not in call.gids:
                errs.append("group_call[%s] 指向的呼叫%d不包含该组" % (gid, call.cid))
            if self.calls.get(call.cid) is not call:
                errs.append("group_call[%s] 指向已失效的呼叫%d" % (gid, call.cid))
        return errs
