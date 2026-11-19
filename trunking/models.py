"""数据模型:终端、通话组、呼叫、话权队列。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# 紧急呼叫优先级, 高于任何用户优先级, 发起即抢占
EMERGENCY_PRIORITY = 1_000_000


class FloorMode(str, Enum):
    QUEUE = "queue"    # 有人在讲时排队
    REJECT = "reject"  # 有人在讲时直接拒绝


@dataclass
class GroupConfig:
    gid: str
    name: str = ""
    floor_mode: FloorMode = FloorMode.QUEUE
    max_talk_ms: int = 30_000   # 单次讲话最长时限, 到点强制收回话权
    hang_time_ms: int = 5_000   # 无人讲话超过该时长, 呼叫自动拆除

    def __post_init__(self):
        self.floor_mode = FloorMode(self.floor_mode)
        if not self.name:
            self.name = self.gid


@dataclass
class Terminal:
    tid: str
    name: str = ""
    priority: int = 1                          # 用户优先级, 越大越高
    groups: dict = field(default_factory=dict)  # gid -> 扫描优先级(越大越优先听)
    active_group: str = None                   # 当前旋钮所在组(按键发话的目标组)
    powered: bool = True
    latency_ms: int = 0                        # 模拟信号不好: 该终端的信令晚到

    def __post_init__(self):
        if not self.name:
            self.name = self.tid
        if self.active_group is None and self.groups:
            self.active_group = next(iter(self.groups))


@dataclass
class QueueEntry:
    tid: str
    gid: str      # 通过哪个组申请的(并组拆开时各回各组)
    ts: int       # 控制器收到请求的时刻
    seq: int      # 同一时刻请求的先后序号
    priority: int


@dataclass
class Call:
    """一次组呼。并组时一个 Call 覆盖多个组(gids)。"""

    cid: int
    gids: set
    created_ts: int
    participants: set = field(default_factory=set)
    holder: str = None          # 当前话权持有者 tid, 同一时刻全呼叫唯一
    holder_gid: str = None      # 持有者是通过哪个组拿到的(拆组时留在该组)
    holder_priority: int = 0
    queue: list = field(default_factory=list)  # list[QueueEntry], 先到先讲
    emergency: bool = False
    merge_id: str = None
    hang_gen: int = 0           # 挂机定时器代际, 用于取消过期定时器
    talk_gen: int = 0           # 话权定时器代际, 用于取消过期定时器
