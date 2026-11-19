"""单线程离散事件引擎。

整个调度核心跑在逻辑时钟上(毫秒),所有信令、定时器都是事件,
按 (时间, 序号) 严格排序处理 —— 同一毫秒到达的消息按提交先后处理,
这样验收剧本和随机压测都是完全确定、可复现的。
"""

from __future__ import annotations

import heapq
import itertools


class Engine:
    def __init__(self):
        self.now = 0  # 逻辑时钟, 毫秒
        self._heap = []  # (触发时刻, 序号, 函数, 参数)
        self._seq = itertools.count()

    def call_later(self, delay_ms, fn, *args):
        self.call_at(self.now + max(0, int(delay_ms)), fn, *args)

    def call_at(self, ts, fn, *args):
        heapq.heappush(self._heap, (int(ts), next(self._seq), fn, args))

    def pending(self):
        return len(self._heap)

    def step(self):
        """处理最早的一个事件, 没有事件返回 False。"""
        if not self._heap:
            return False
        t, _, fn, args = heapq.heappop(self._heap)
        self.now = max(self.now, t)
        fn(*args)
        return True

    def run_until(self, ts):
        """把时钟推进到 ts, 处理途中所有事件(含恰好 ts 时刻的)。"""
        while self._heap and self._heap[0][0] <= ts:
            self.step()
        self.now = max(self.now, ts)

    def run_all(self):
        while self.step():
            pass
