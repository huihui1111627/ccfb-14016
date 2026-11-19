"""验收剧本:每个时刻每个组的话权在谁手里、谁在排队、每部终端在听哪个组,
全部写成期望逐步比对。覆盖需求 1~4 的全部条款。"""

from trunking.scenario import Scenario


def s1_late_join_and_auto_teardown():
    """S1 组呼免应答建立 / 中途加入 / 无人讲话自动拆线"""
    sc = Scenario("S1 组呼建立·中途加入·自动拆线")
    sc.group("G1", floor_mode="queue", max_talk_ms=3000, hang_time_ms=1000)
    sc.terminal("T1", priority=1, groups={"G1": 1})
    sc.terminal("T2", priority=1, groups={"G1": 1})
    sc.terminal("T3", priority=1, groups={"G1": 1}, powered=False)  # 关机中
    sc.ptt(0, "T1")                       # 按键即建立, 无需应答
    sc.expect_call(50, "G1", holder="T1", queue=[],
                   participants=["T1", "T2"])   # T3 关机不在
    sc.power(100, "T3", True)             # 呼叫进行中开机 -> 中途加入
    sc.expect_call(150, "G1", holder="T1", participants=["T1", "T2", "T3"])
    sc.expect_notified(150, "T3", "CALL_JOINED", holder="T1")  # 进来就知道谁在讲
    sc.release(200, "T1")                 # 松键, 无人再讲 -> 启动挂机定时
    sc.expect_call(300, "G1", holder=None, queue=[])
    sc.expect_no_call(1300, "G1")         # 静默超过 hang_time=1000ms 自动拆
    sc.expect_notified(1300, "T2", "CALL_ENDED", reason="HANG_TIMEOUT")
    return sc


def s2_floor_queue():
    """S2 排队模式:先到先讲, 松键给下一个, 全组收到现在谁在讲"""
    sc = Scenario("S2 话权排队")
    sc.group("G1", floor_mode="queue", max_talk_ms=3000, hang_time_ms=1000)
    for tid in ("T1", "T2", "T3"):
        sc.terminal(tid, priority=1, groups={"G1": 1})
    sc.ptt(0, "T1")
    sc.ptt(10, "T2")
    sc.ptt(20, "T3")
    sc.expect_call(30, "G1", holder="T1", queue=["T2", "T3"])
    sc.expect_notified(30, "T2", "FLOOR_QUEUED", pos=1)
    sc.expect_notified(30, "T3", "FLOOR_QUEUED", pos=2)
    sc.release(40, "T1")
    sc.expect_call(50, "G1", holder="T2", queue=["T3"])
    sc.expect_notified(50, "T1", "FLOOR_TAKEN", holder="T2")   # 全组知道换谁讲了
    sc.expect_notified(50, "T3", "FLOOR_TAKEN", holder="T2")
    sc.release(60, "T2")
    sc.expect_call(70, "G1", holder="T3", queue=[])
    sc.release(80, "T3")
    sc.expect_call(90, "G1", holder=None, queue=[])
    return sc


def s3_reject_mode():
    """S3 拒绝模式(按组配置):有人在讲时直接拒绝, 不排队"""
    sc = Scenario("S3 拒绝模式")
    sc.group("G2", floor_mode="reject", max_talk_ms=3000, hang_time_ms=1000)
    sc.terminal("T1", priority=1, groups={"G2": 1})
    sc.terminal("T2", priority=1, groups={"G2": 1})
    sc.ptt(0, "T1")
    sc.ptt(50, "T2")
    sc.expect_call(100, "G2", holder="T1", queue=[])
    sc.expect_notified(100, "T2", "FLOOR_DENIED", holder="T1")
    return sc


def s4_max_talk_timeout():
    """S4 讲话最长时限, 到点强制收回并交给排队的下一个"""
    sc = Scenario("S4 讲话超时强收")
    sc.group("G1", floor_mode="queue", max_talk_ms=2000, hang_time_ms=1000)
    sc.terminal("T1", priority=1, groups={"G1": 1})
    sc.terminal("T2", priority=1, groups={"G1": 1})
    sc.ptt(0, "T1")                        # T1 按着不放
    sc.ptt(10, "T2")                       # T2 排队
    sc.expect_call(1000, "G1", holder="T1", queue=["T2"])
    sc.expect_call(2100, "G1", holder="T2", queue=[])  # 2000ms 到点强收, T2 补讲
    sc.expect_notified(2100, "T1", "FLOOR_REVOKED", reason="MAX_TALK")
    return sc


def s5_priority_preempt():
    """S5 高优先级打断低优先级, 被打断者收到明确通知并排到队首"""
    sc = Scenario("S5 优先级打断")
    sc.group("G1", floor_mode="queue", max_talk_ms=5000, hang_time_ms=1000)
    sc.terminal("T1", priority=1, groups={"G1": 1})   # 门机司机
    sc.terminal("T2", priority=5, groups={"G1": 1})   # 调度员
    sc.ptt(0, "T1")
    sc.ptt(100, "T2")                                 # 高优先级打断
    sc.expect_call(150, "G1", holder="T2", queue=["T1"])
    sc.expect_notified(150, "T1", "FLOOR_PREEMPTED", by="T2")  # 明确通知
    sc.release(200, "T2")
    sc.expect_call(250, "G1", holder="T1", queue=[])  # 被打断的补讲
    return sc


def s6_emergency_call():
    """S6 紧急呼叫:拆掉自己所在的其它呼叫, 强拉全组, 抢占话权"""
    sc = Scenario("S6 紧急呼叫")
    sc.group("G1", floor_mode="queue", max_talk_ms=5000, hang_time_ms=1000)
    sc.group("G2", floor_mode="queue", max_talk_ms=5000, hang_time_ms=1000)
    sc.terminal("T1", priority=1, groups={"G1": 1})
    sc.terminal("T2", priority=1, groups={"G1": 1})
    sc.terminal("T3", priority=1, groups={"G1": 1}, powered=False)  # 关机的拉不进来
    sc.terminal("T4", priority=1, groups={"G1": 1, "G2": 2}, active="G2")
    sc.terminal("T5", priority=1, groups={"G2": 1})
    sc.ptt(0, "T1")                        # G1 呼叫, T1 在讲
    sc.ptt(10, "T4")                       # T4 在 G2 按键, G2 呼叫, T4 在讲
    sc.expect_call(50, "G2", holder="T4")
    sc.switch(60, "T4", "G1")              # T4 旋钮切到 G1(仍持有 G2 话权)
    sc.emergency(100, "T4")                # T4 在 G1 发起紧急呼叫
    # 自己在 G2 的话权先被拆掉
    sc.expect_call(150, "G2", holder=None, queue=[])
    sc.expect_notified(150, "T4", "FLOOR_REVOKED", reason="EMERGENCY_EXIT")
    # G1 变成紧急呼叫, T4 抢占话权, 在讲的 T1 被明确通知
    sc.expect_call(150, "G1", holder="T4", emergency=True, queue=["T1"])
    sc.expect_notified(150, "T1", "FLOOR_PREEMPTED", by="T4")
    # 组里所有人(在线的)都被强拉进来
    sc.expect_call(150, "G1", participants=["T1", "T2", "T4"])
    return sc


def s7_same_millisecond():
    """S7 同一毫秒一起按:按信令到达顺序处理, 先到者拿话权, 其余排队"""
    sc = Scenario("S7 同毫秒并发按键")
    sc.group("G1", floor_mode="queue", max_talk_ms=3000, hang_time_ms=1000)
    for tid in ("T1", "T2", "T3"):
        sc.terminal(tid, priority=1, groups={"G1": 1})
    sc.ptt(0, "T1")   # 三条信令同一毫秒到达, 按提交顺序 T1->T2->T3
    sc.ptt(0, "T2")
    sc.ptt(0, "T3")
    sc.expect_call(10, "G1", holder="T1", queue=["T2", "T3"])
    return sc


def s8_bad_signal_late_arrival():
    """S8 信号不好按键晚到:谁先按不算数, 信令先到的拿话权"""
    sc = Scenario("S8 弱信号晚到")
    sc.group("G1", floor_mode="queue", max_talk_ms=3000, hang_time_ms=1000)
    sc.terminal("T1", priority=1, groups={"G1": 1}, latency_ms=80)  # 信号差
    sc.terminal("T2", priority=1, groups={"G1": 1})
    sc.ptt(0, "T1")   # T1 先按, 但消息 t=80 才到
    sc.ptt(0, "T2")   # T2 后按, 消息 t=0 即到
    sc.expect_call(10, "G1", holder="T2", queue=[])
    sc.expect_call(100, "G1", holder="T2", queue=["T1"])  # T1 晚到只能排队
    return sc


def s9_merge_and_unmerge():
    """S9 并组:两路通话合并, 排队按时间合序;拆开后各回各组, 先后不乱"""
    sc = Scenario("S9 并组与拆组")
    sc.group("G1", floor_mode="queue", max_talk_ms=5000, hang_time_ms=1000)
    sc.group("G2", floor_mode="queue", max_talk_ms=5000, hang_time_ms=1000)
    sc.terminal("T1", priority=1, groups={"G1": 1})
    sc.terminal("T2", priority=1, groups={"G1": 1})
    sc.terminal("T3", priority=5, groups={"G2": 1})
    sc.terminal("T4", priority=1, groups={"G2": 1})
    sc.ptt(0, "T1")                        # G1: T1 讲
    sc.ptt(10, "T2")                       # G1: T2 排队(请求时刻 10)
    sc.ptt(20, "T3")                       # G2: T3 讲(优先级 5)
    sc.ptt(30, "T4")                       # G2: T4 排队(请求时刻 30)
    sc.merge(100, "m", ["G1", "G2"])
    # 两路都有人讲:优先级高的 T3 保留话权, T1 被通知打断并排队首;
    # 排队按请求时间合并: T2(10) 在 T4(30) 前
    sc.expect_call(150, "G1", holder="T3", queue=["T1", "T2", "T4"])
    sc.expect_call(150, "G2", holder="T3", queue=["T1", "T2", "T4"])
    sc.expect_notified(150, "T1", "FLOOR_PREEMPTED", by="MERGE")
    sc.unmerge(200, "m")
    # 各回各组:话权留在 T3 的 G2;T1/T2 回 G1 且顺序不变, T1 补讲
    sc.expect_call(250, "G1", holder="T1", queue=["T2"])
    sc.expect_call(250, "G2", holder="T3", queue=["T4"])
    sc.release(300, "T3")
    sc.expect_call(350, "G2", holder="T4", queue=[])
    return sc


def s10_scan_priority():
    """S10 终端属于多个组时按扫描优先级决定听哪个;紧急呼叫最高"""
    sc = Scenario("S10 扫描优先级")
    sc.group("G1", floor_mode="queue", max_talk_ms=5000, hang_time_ms=1000)
    sc.group("G2", floor_mode="queue", max_talk_ms=5000, hang_time_ms=1000)
    sc.terminal("T1", priority=1, groups={"G1": 1, "G2": 5})  # G2 扫描优先级高
    sc.terminal("T2", priority=1, groups={"G1": 1})
    sc.terminal("T3", priority=1, groups={"G2": 1})
    sc.terminal("T4", priority=1, groups={"G1": 1})
    sc.ptt(0, "T2")                        # G1 呼叫
    sc.ptt(10, "T3")                       # G2 呼叫
    sc.expect_listening(50, "T1", "G2")    # 两边都有呼叫, 听扫描优先级高的
    sc.emergency(100, "T4")                # G1 紧急呼叫
    sc.expect_listening(150, "T1", "G1")   # 紧急呼叫压过扫描优先级
    return sc


def s11_dispatcher_force_release():
    """S11 调度台强拆:收回当前话权, 交给排队的下一个"""
    sc = Scenario("S11 调度台强拆")
    sc.group("G1", floor_mode="queue", max_talk_ms=5000, hang_time_ms=1000)
    sc.terminal("T1", priority=1, groups={"G1": 1})
    sc.terminal("T2", priority=1, groups={"G1": 1})
    sc.ptt(0, "T1")
    sc.ptt(10, "T2")
    sc.force_release(100, "G1")
    sc.expect_call(150, "G1", holder="T2", queue=[])
    sc.expect_notified(150, "T1", "FLOOR_REVOKED", reason="DISPATCHER")
    return sc


def all_scenarios():
    return [
        s1_late_join_and_auto_teardown(),
        s2_floor_queue(),
        s3_reject_mode(),
        s4_max_talk_timeout(),
        s5_priority_preempt(),
        s6_emergency_call(),
        s7_same_millisecond(),
        s8_bad_signal_late_arrival(),
        s9_merge_and_unmerge(),
        s10_scan_priority(),
        s11_dispatcher_force_release(),
    ]
