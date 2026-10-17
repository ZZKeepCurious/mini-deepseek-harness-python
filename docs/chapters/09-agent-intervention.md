# 第 9 章：Agent 干预面——唤醒、转向、注入与取消

> 对应 dsh 真实源码：`packages/core/agent`（Agent 接口，`src/runtime-types.ts:164-243`）+ `packages/core/agent-loop`（pre-step 瀑布与 turn 闭合，`src/agent.ts:265-283`）。
> 前置：第 4 章。本章产出：`miniharness/core/agent_loop/agent.py` 干预面、`miniharness/interaction/approval.py`；测试 `tests/test_loop_intervention.py`、`tests/test_approval.py`。

## 9.1 这一章要做什么

第 4 章只实现了"喂一条消息"（followup）。宿主（web UI、hooks 桥、ACP 客户端、编排程序）还需要在一个 run 进行中干预它：唤醒、转向、注入背景、取消、等待静默。上游把这组操作做成了七个方法的 `Agent` 接口，本章讲 mini 的 `AgentLoop` 如何实现这些语义。

### 上游接口全景

`Agent` 接口（`runtime-types.ts:164-243`）是宿主侧唯一把手。七个方法按"唤醒 vs 不唤醒"分组：

| 方法 | 唤醒？ | 语义 | 日志体现 |
|---|---|---|---|
| `followup(messages)` | 是（下一 turn） | 消息入 inbox，agent idle 时开 turn | inbox 事件 + turn/start |
| `steer(steer)` | 是（下一 step） | idle 时同步开 turn；running 时下个 step 边界消费，可改向 | inbox 事件 + 下个 step |
| `inject(messages)` | 否 | running 中入 inbox，不触发新 turn | inbox 事件 |
| `send(msg, target, wakeup)` | 可配置 | 统一原语：wakeup 决定 followup 还是 inject | inbox 事件 |
| `cancel(cause, opts)` | — | 清 inbox（除非 keepInbox）+ abort 活跃活动；idle no-op | inbox/spliced + turn/end aborted |
| `whenIdle()` | — | 整机 quiescence：无活跃 driver 且无 maintenance 才 resolve | — |
| `runMaintenance(task)` | — | true idle 下执行非回合维护（如 compactNow） | 无会话事件 |

> 防混淆：这里有两套「唤醒」词汇。`Agent.send(…, wakeup)` 的 wakeup 是**通用投递布尔**（上游至今如此）；子代理 report 的投递词汇才是从 `'wakeup'` 改名而来的 `'quiet' | 'next-step'`（`reportDelivery`，见第 04 章 subagent 一节）；后者不再使用 `'wakeup'` 值，前者没有。

关键语义细节：

1. **所有输入统一进一个 inbox，inbox 内部分两列**：`followup` 进 `next-turn`，`steer`/`inject` 进 `next-step`。这就是为什么 `inject` 的"背景信息"会在下个 step 边界被一并消费，而不是等下一次 `followup`。（`core/agent_loop/inbox.py`，每次增删都落 `agent/inbox/spliced`。）
2. **取消是边界生效的**：`cancel` 清空排队 + 置协作式取消标记，活跃活动在各自的执行点退出；未派发的 tool call 补 `TOOL_ABORTED_BEFORE_DISPATCH`（错误码 `ABORTED_BEFORE_DISPATCH`）错误结果对，turn 以 `{kind:'aborted'}` 闭合。
3. **被拒绝的 claimed 消息"既不丢弃也不重发"**（pre-step reject 的语义，见第 04 章）。
4. **生命周期三态**：对外只暴露 `idle | running`（内部另有 maintenance）；`running` 是"驱动级排干区间"而非 turn 是否开着；quiescence = 无活跃 driver 且无 maintenance。
5. **干预通道不止七个方法**：`agent/pre-step` 瀑布（决策 `{kind:'reject'}|{kind:'enter',messages}`，`agent.ts:265-283`，reject → `turn/end {kind:'blocked'}`）、`agent/request` 瀑布（请求派生前改配置）、`tools/pre-execute` 瀑布（审批，见报告 04 页议题 5）。

## 9.2 mini 复现：干预面实现

mini 的 loop 是"单一 async 驱动 + 同步门面"：`_pump_async` 是唯一主循环，`followup`/`steer` 在 driver 已起时只入队并线程安全唤醒，没有 driver 时经同步门面直接驱动一轮。干预语义落在三处：双列 inbox、协作式取消标记、驻留标记。

```python
def steer(self, content, source="user"):
    """下一 step 唤醒：idle 时开 turn；running 时入 next-step，
    当前 step 跑完后由主循环在边界消费。"""
    message = content if isinstance(content, dict) else create_message(
        "user", [text_block(content)], _injected_source(source))
    self.inbox.append("next-step", message)
    self._parked = False                       # 唤醒即恢复被驻留的队列
    if self._driver is not None and not self._driver.done():
        self._request_wake()                   # 事件驱动模式：只入队 + 唤醒
    else:
        self._pump_sync_facade()               # 无 driver：直接同步跑完

def inject(self, content, source="plugin"):
    """非唤醒注入：只入 next-step，不开 turn。
    后续任一 followup/steer 唤醒主循环时在下一个 step 边界一并消费。"""
    message = content if isinstance(content, dict) else create_message(
        "user", [text_block(content)], _injected_source(source))
    self.inbox.append("next-step", message)

def cancel(self, cause=None, keep_inbox=False):
    """取消：清 inbox（除非 keep_inbox）+ 中止活跃回合。"""
    if not self._turn_open and not self.inbox:
        return
    if keep_inbox and self.inbox:
        self._parked = True                    # 保留队列但不自动续跑，等下次唤醒
    else:
        self.inbox.clear()
        self._parked = False
    if self._turn_open:
        self._cancelled = True
        self._turn_end = {"kind": "aborted",
                          "reason": aborted_cancel_cause(cause) or {"kind": "user"}}
        if self._step_signal is not None:
            self._step_signal.signal.set()
        self._set_cancel_event()

def when_idle(self):
    """quiescence：无活跃回合即 idle。"""
    return self.status == "idle" and not self._turn_open

def run_maintenance(self, task):
    """维护任务：仅 true idle 下执行，不落 turn 日志；
    执行期间 status='maintenance'（when_idle 返回 False）。"""
    if not self.when_idle():
        raise RuntimeError("run_maintenance 要求 true idle（无活跃回合）")
    self.status = "maintenance"
    try:
        return task()
    finally:
        self.status = "idle"
```

三个标记与主循环的关系：

- **`_cancelled`**：协作式取消标记，`__init__` 初始为 `False`。`cancel` 把它置真，主循环 `_pump_async` 在每轮边界检查：

  ```python
  while (self.inbox or self._continue) and not self._cancelled and not self._parked:
      ...
  ```

  所以取消不打断正在执行的工具，而是等当前 step 跑完、回到循环条件时不再开下一个 step。`finally` 里的 `_close_turn` 落 `turn/end {kind:'aborted'}`，随后复位 `_cancelled`（下次 `followup` 不受影响）。工具回调内调用 `cancel` 也走同一条路。
- **`_parked`**：`cancel(keep_inbox=True)` 的驻留标记。队列保留，但驱动不再自动续跑，直到下一次 `followup`/`steer` 把它清掉并唤醒。
- **`_turn_end`**：本轮结束原因。`cancel` 先写入 `{kind:'aborted'}`，`_close_turn` 优先采用它，避免后续 step 的结束原因覆盖取消。

## 9.3 边界生效的验证方式

最直接的测试是在工具回调里调用 `cancel`，模拟"宿主在 step 执行中途叫停"：

```python
def bash_exec(args, exec_ctx):
    loop.cancel("user")     # step 内叫停
    return "done"

loop = AgentLoop(session, adapter, registry, ctx)
loop.run("跑命令")
# 断言：turn/end reason == {kind:'aborted', reason:{kind:'user'}}，且只有一个 step/start
```

取消在边界生效：工具跑完回到循环条件，`_cancelled` 为真 → 不再开下一个 step → `finally` 落 `turn/end`。这正是上游"活跃活动 abort + 回合以 aborted 闭合"的协作式对应物：不杀驱动任务，由执行点自己退出。

## 9.4 硬性规定

1. `steer` 从 idle 调用开 turn；`inject` 从 idle 调用**不开** turn（status 保持 idle、无 turn/start）。
2. `inject` 后接 `followup`，两条消息按边界顺序进日志：同一列内保持 FIFO，`next-step` 在下一个 step 边界先被认领。
3. `cancel`：idle 且 inbox 空 → 完全 no-op（零事件）；`keep_inbox=True` 且队列非空 → 置 `_parked` 保留、不自动续跑；step 内调用 → turn 以 `{kind:'aborted'}` 闭合且不再继续 step。
4. `run_maintenance`：仅 true idle；running 中调用抛 `RuntimeError`；执行不产生任何会话事件。
5. `when_idle`：maintenance 期间返回 False。

验证：`python -m unittest tests.test_loop_intervention -v`。

## 9.5 审批：干预通道之一（`miniharness/interaction/approval.py`）

> 对应 dsh 真实源码：`packages/interaction/user-approval`。报告 04 页议题 5 有完整解读；这里给出最小可运行版。

上游把审批做成了独立的能力 seam（`ApprovalService`），不是塞进 loop 的 if——这保持了"插件，不是 loop 改动"的约束。三个关键点：

1. **两档策略**：`ApprovalPolicy = 'ask' | 'never'`。`'ask'`（默认）委托给组合的 answerers，没有 answerer 就 **fail-closed 为 `'unavailable'`**（审批不能默认放行）；`'never'` 在派发**之前**确定性拒绝（`'rejected'`，不提示任何人）——CI / 无人值守的严格立场。策略是 durable 会话状态：`approval/policy` 事件可重放，**最后一条胜出**（纯 fold，resume 无需追赶逻辑）。
2. **审计对**：每次 ask 追加 `approval/asked`（id, toolName, callId?, reason?）+ `approval/decided`（id, outcome）一对，**log-only 非 surface**（模型看不到审计本身，只看到工具结果）。`'allowed-once'` 是唯一授权，且只作用于被请求的那一次动作（无跨调用豁免）。
3. **open turn 前置**：`approval.request()` 必须在未闭合的 turn 内调用——审计对必须 turn-enclosed，否则抛错且不追加任何东西（两次 turn 之间的裸事件在重载时与崩溃尾部无法区分，会被静默丢弃）。

mini 实现：

```python
service = ApprovalService(ctx)                       # 默认 ask；可传 policy="never"
set_approval_policy(session, "never")                # 写会话策略覆盖（可重放）
outcome = service.request(session, "bash",           # 返回 closed outcome
                          call_id="call_0", reason="需要执行")
# 审计对自动落日志：approval/asked + approval/decided（同 id）
```

decide 顺序与上游一致（`src/index.ts:304-344`）：

1. signal 已 aborted → `'cancelled'`；
2. 策略 `'never'` → `'rejected'`（服务自己的 request 路径决定，保证注册顺序无关的确定性）；
3. 否则 `ctx.waterfall('approval/request', req)` 派发 answerer 链——无监听器 / 抛错 / 返回非词汇表值一律归一化为 `'unavailable'`（fail closed）。

## 9.6 硬性规定（审批）

1. `request` 在 open turn 外调用 → 抛错且零 approval 事件落日志。
2. `'never'` 拒绝时不咨询任何 answerer（注册顺序无关的确定性）。
3. 无 answerer / answerer 抛错 / 非词汇表返回值 → `'unavailable'`（fail closed，审批不默认放行）。
4. `allowed-once` 只授予被请求的动作；两次独立 ask 各走一对审计。
5. `set_approval_policy` 无效值在日志变更前抛 `TypeError`；`approval/policy` 不产生模型消息。
6. 审计对不携带 `surfaceOp`（log-only）。

验证：`python -m unittest tests.test_approval -v`（含 waterfall 短路、abort 前置、重放即状态）。

## 9.7 检查点

1. 说出七个干预方法的唤醒分组，并解释双列 inbox（`next-turn` / `next-step`）为什么让 `followup` 与 `inject` 落在不同的消费边界。
2. 解释"取消是边界生效的"在协作式取消里如何成立（`_cancelled` + 主循环条件 + `finally` 落 `turn/end`）。
3. 手动写一个"工具回调里 cancel"的用例，观察 `turn/end` 的 reason。
4. 说出 `'never'` 为什么必须由服务自身在派发前决定（而不是监听器形状的拦截器）。
5. 解释审计对为什么必须 turn-enclosed（崩溃尾部语义）。
6. 说出 mini 的实现范围：双队列 inbox、`_parked` 驻留、未派发 tool call 的 `TOOL_ABORTED_BEFORE_DISPATCH` 合成错误、`scope_target` 按 agent 过滤派发、agent 实例注册表（`core/agents.py` + `AgentLoop.publish()`）；与上游的差别在载体（调度器实现、无 reserved attempt），语义一致（aborted 闭合、边界生效、fail-closed 审批）。

> 下一章：轨迹投影引擎——把事件日志折叠成人类可读的回合台账（Trajectory）。