# 港口数字集群对讲调度服务

呼叫控制与话权管理服务(只管信令和"谁能讲",不管语音流)。纯 Python 标准库,无第三方依赖。

## 结构

```
trunking/
  engine.py       单线程离散事件引擎(逻辑时钟,毫秒;同毫秒事件按提交顺序处理)
  models.py       终端 / 通话组 / 呼叫 / 话权队列 数据模型
  controller.py   核心:呼叫控制 + 话权管理 + 并组拆组 + 扫描收听计算
  scenario.py     验收剧本 DSL 与执行器
acceptance_scenarios.py  11 个验收剧本(需求 1~4 全覆盖)
run_acceptance.py        跑验收剧本
run_stress.py            随机压测 + 不变式校验
server.py                调度台服务(实时模拟 + HTTP API)
console.html             调度台页面
```

## 运行

```bash
python3 run_acceptance.py     # 验收剧本,逐步比对期望
python3 run_stress.py         # 随机压测(默认 20 种子 × 300 终端 × 12 组)
python3 run_stress.py --seed 4 --terminals 500 --steps 20000   # 复现/加量
python3 server.py --port 8080 # 调度台,打开 http://127.0.0.1:8080
```

## 需求对应

**1. 组呼** — `Controller.terminal_ptt_press`:按键即建立(`_setup_call`),无需被叫应答,
组内在线终端被拉入;呼叫进行中开机(`terminal_power`)或切组过来(`terminal_switch_group`)
的终端中途加入并立刻收到当前话权持有者;无人讲话超过 `hang_time_ms` 自动拆线
(`_arm_hang_timer` / `_on_hang_timeout`)。剧本 S1。

**2. 话权** — 一个呼叫同一时刻只有一个 `holder`。有人在讲时按组配置
`floor_mode=queue`(排队,先到先讲)或 `reject`(直接拒绝 `FLOOR_DENIED`)。
单次讲话超过 `max_talk_ms` 强制收回(`_on_talk_timeout`)。松键释放后话权给排队的
下一个,并向全组广播 `FLOOR_TAKEN`(现在谁在讲)。剧本 S2/S3/S4。

**3. 优先级与紧急呼叫** — 申请者优先级高于持有者即抢占(`_preempt`),被打断者收到
`FLOOR_PREEMPTED` 明确通知,排队模式下排到队首。紧急呼叫优先级最高
(`EMERGENCY_PRIORITY`),发起时先把该终端所在的其它呼叫拆掉
(`_detach_from_other_calls`),并把组里所有在线成员强拉进来(`_force_pull`)。剧本 S5/S6。

**4. 并组/拆组** — 调度员 `merge_groups`:各组进行中的呼叫合并成一路,话权保留给
优先级最高的在讲者,其余在讲者被通知打断并排队首;各组排队申请按请求时间合并,
先后不乱。`unmerge`:各回各组,话权留在持有者申请的组,排队申请各回各组且顺序不变。
终端属于多个组时按扫描优先级决定听哪个(`listening_group`),紧急呼叫最高。剧本 S9/S10。
调度员强拆:`dispatcher_force_release`,剧本 S11。

## 终端模拟

`Terminal.latency_ms` 模拟弱信号信令晚到(信令按到达顺序处理,先按不一定先讲,剧本 S8);
压测里终端有的按着不放(长 hold)、有的同一毫秒并发按键(burst)、随机开关机/切组/
紧急呼叫,调度员随机并组/拆组/强拆。

## 验收与红线

- 每个剧本在多个检查点比对:每个组的话权持有者、排队队列、每部终端在听哪个组、
  关键通知(被打断/被拒/强收)是否送达。
- 每个检查点和压测的每一个事件之后都跑 `check_invariants()`,红线包括:
  **任何一个组任何时刻不许有两个话权持有者**(每个组至多一路呼叫、每路呼叫至多一个
  持有者、同一终端不能同时持有两路话权)、队列无重复、持有者必须是参与者等。
- 压测规模:500 终端 × 20 组 × 50 种子 × 约 3 万事件,全部通过。

## 设计说明

- 整个状态机跑在单线程事件引擎上,真实部署时把 Engine 换成网络收发即可,核心逻辑不变;
  定时器(讲话限时、空闲拆线)用"代际号"取消,避免过期定时器误伤。
- 被抢占者排到队首是排队模式下的设计选择(被行政打断的人优先补讲),拒绝模式下只通知。
- 并组时若两路都有人在讲,优先级高的一路保留话权;平手取呼叫建立早的。
- 紧急呼叫在收听优先级上压过一切扫描配置。
