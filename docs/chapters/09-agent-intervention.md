# 09 Agent 干预面：唤醒、转向、注入与取消

> 本章回答一个问题：宿主（web UI、hooks 桥、ACP 客户端、编排程序）怎么干预一个运行中的 agent？第 04 章只实现了"喂一条消息"（followup）；上游把干预做成了七个方法的完整接口面。我们逐一对齐语义，在 mini 的 `AgentLoop` 上补齐 `steer` / `inject` / `cancel` / `when_idle` / `run_maintenance`。
>
> 对应 dsh 真实源码：`packages/core/agent`（Agent 接口，`src/runtime-types.ts:64-144`）+ `packages/core/agent-loop`（pre-step 瀑布与 turn 闭合，`src/agent.ts:225-269`）。mini 复现于 `miniharness/loop.py` 干预面一节。

## 9.1 上游接口全景

`Agent` 接口（`runtime-types.ts:64-144`）是宿主侧唯一把手。七个方法按"唤醒 vs 不唤醒"分组：

| 方法 | 唤醒？ | 语义 | 日志体现 |
|---|---|---|---|
| `followup(messages)` | 是（下一 turn） | 消息入 inbox，agent idle 时开 turn | inbox 事件 + turn/start |
| `steer(steer)` | 是（下一 step） | idle 时同步开 turn；running 时下个 step 边界消费，可改向 | inbox 事件 + 下个 step |
| `inject(messages)` | 否 | running 中入 inbox，不触发新 turn | inbox 事件 |
| `send(msg, target, wakeup)` | 可配置 | 统一原语：wakeup 决定 followup 还是 inject | inbox 事件 |
| `cancel(cause, opts)` | — | 清 inbox（除非 keepInbox）+ abort 活跃活动；idle no-op | inbox/spliced + turn/end aborted |
| `whenIdle()` | — | 整机 quiescence：无活跃 driver 且无 maintenance 才 resolve | — |
| `runMaintenance(task)` | — | true idle 下执行非回合维护（如 compactNow） | 无会话事件 |

关键语义细节：

1. **inbox 是唯一队列**：所有输入统一进 inbox，消息成为普通 FIFO turn——这就是为什么 `inject` 的"背景信息"会在下一次 `followup` 时一并进入日志（顺序保持）；
2. **取消是边界生效的**：`cancel` 清空排队 + 标记中止，活跃 step 跑完后不再继续；未派发的 tool call 补 `ABORTED_BEFORE_DISPATCH` 错误结果对，turn 以 `{kind:'aborted'}` 闭合；
3. **被拒绝的 claimed 消息"既不丢弃也不重发"**（pre-step reject 的语义，见第 04 章）；
4. **生命周期三态**：对外只暴露 `idle | running`（内部另有 maintenance）；`running` 是"驱动级排干区间"而非 turn 是否开着；quiescence = 无活跃 driver 且无 maintenance；
5. **干预通道不止七个方法**：`agent/pre-step` 瀑布（决策 `{kind:'reject'}|{kind:'enter',messages}`，`agent.ts:225-243`，reject → `turn/end {kind:'blocked'}`）、`agent/request` 瀑布（请求派生前改配置）、`tools/pre-execute` 瀑布（审批，见报告 04 页议题 5）。

## 9.2 mini 复现：同步模型下的干预面

mini 是同步 loop（一个线程里跑完整个回合），上游的"running 时入队、下个边界消费"在这里等价于：**循环条件在每个 step 之后检查**。所以语义对齐点是"边界生效"，不是"并发中断"。

```python
def steer(self, content, source="user"):
    """下一 step 唤醒：idle 时立即开 turn；running 时入 inbox，
    当前 step 跑完后边界消费（同步模型下循环条件在每 step 后检查）。"""
    message = create_message(
        "user", [text_block(content)],
        {"kind": "user"} if source == "user" else {"kind": "plugin", "plugin": source},
    )
    self.inbox.append(message)
    self._pump()

def inject(self, content, source="plugin"):
    """非唤醒注入：只入 inbox，不开 turn。
    后续任一 followup/steer 触发 pump 时按 FIFO 一并消费。"""
    self.inbox.append(create_message(
        "user", [text_block(content)],
        {"kind": "plugin", "plugin": source},
    ))

def cancel(self, cause=None, keep_inbox=False):
    """取消：清 inbox（除非 keep_inbox）+ 中止活跃回合。
    同步模型无法中断正在执行的 step，取消在 step 边界生效：
    当前 step 跑完后不再继续（工具回调内调用也可），
    turn 以 {kind:'aborted'} 闭合；无活跃回合且 inbox 空 → no-op。"""
    if not self._turn_open and not self.inbox:
        return
    if not keep_inbox:
        self.inbox.clear()
    if self._turn_open:
        self._cancelled = True
        self._turn_end = {"kind": "aborted"}

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

`_pump` 的循环条件加入取消标记：

```python
while (self.inbox or self._continue) and not self._cancelled:
    ...
```

`_close_turn` 里复位 `_cancelled`（下次 followup 不受上次取消影响）。

## 9.3 边界生效的验证方式

同步模型下最有趣的测试是**在工具回调里调用 `cancel`**——这模拟了"宿主在 step 执行中途叫停"：

```python
def bash_exec(args, exec_ctx):
    loop.cancel("用户叫停")     # step 内叫停
    return "done"

loop = AgentLoop(session, adapter, registry, ctx)
loop.run("跑命令")
# 断言：turn/end reason == {kind:'aborted'}，且只有一个 step/start
```

取消在 step 边界生效：工具跑完回到循环条件，`_cancelled` 为真 → 不再开下一个 step → finally 落 `turn/end {kind:'aborted'}`。这正是上游"活跃活动 abort + 回合以 aborted 闭合"的同步对应物。

## 9.4 硬性规定（被测试钉住）

1. `steer` 从 idle 调用开 turn；`inject` 从 idle 调用**不开** turn（status 保持 idle、无 turn/start）。
2. `inject` 后接 `followup`，两条消息按 FIFO 顺序进日志（inbox 是唯一队列）。
3. `cancel`：idle 且 inbox 空 → 完全 no-op（零事件）；清 inbox 但 `keep_inbox=True` 保留；step 内调用 → turn 以 `{kind:'aborted'}` 闭合且不再继续 step。
4. `run_maintenance`：仅 true idle；running 中调用抛 `RuntimeError`；执行不产生任何会话事件。
5. `when_idle`：maintenance 期间返回 False。

验证：`python -m unittest tests.test_loop_intervention -v`（11 个用例）。

## 9.5 审批：干预通道之一（`miniharness/approval.py`）

> 对应 dsh 真实源码：`packages/interaction/user-approval`。报告 04 页议题 5 已做全解读；这里落实可运行的最小版。

上游把审批做成了独立的能力 seam（`ApprovalService`），不是塞进 loop 的 if——这保持了"插件，不是 loop 改动"的约束。三个关键点：

1. **两档策略先行**：`ApprovalPolicy = 'ask' | 'never'`。`'ask'`（默认）委托给组合的 answerers，没有 answerer 就 **fail-closed 为 `'unavailable'`**（审批不能默认放行）；`'never'` 在派发**之前**确定性拒绝（`'rejected'`，不提示任何人）——CI / 无人值守的严格立场。策略是 durable 会话状态：`approval/policy` 事件可重放，**最后一条胜出**（纯 fold，resume 无需追赶机制）。
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

## 9.6 硬性规定（审批，被 18 个测试钉住）

1. `request` 在 open turn 外调用 → 抛错且零 approval 事件落日志。
2. `'never'` 拒绝时不咨询任何 answerer（注册顺序无关的确定性）。
3. 无 answerer / answerer 抛错 / 非词汇表返回值 → `'unavailable'`（fail closed，审批不默认放行）。
4. `allowed-once` 只授予被请求的动作；两次独立 ask 各走一对审计。
5. `set_approval_policy` 无效值在日志变更前抛 `TypeError`；`approval/policy` 不产生模型消息。
6. 审计对不携带 `surfaceOp`（log-only）。

验证：`python -m unittest tests.test_approval -v`（18 个用例，含 waterfall 短路、abort 前置、重放即状态）。

## 9.7 检查点

- [ ] 说出七个干预方法的唤醒分组，并解释"inbox 是唯一队列"为什么让 inject 与 followup 保持 FIFO；
- [ ] 解释"取消是边界生效的"在同步模型里如何成立（循环条件 + finally 落 turn/end）；
- [ ] 手动写一个"工具回调里 cancel"的用例，观察 turn/end reason；
- [ ] 说出 `'never'` 为什么必须由服务自身在派发前决定（而不是监听器形状的拦截器）；
- [ ] 解释审计对为什么必须 turn-enclosed（崩溃尾部语义）；
- [ ] 说出 mini 相对上游的简化（同步无并发、无 ABORTED_BEFORE_DISPATCH 补对、无 scopeTarget 按 agent 过滤）与语义对齐（aborted 闭合、边界生效、fail-closed 审批）。

> 下一章：轨迹投影引擎——把事件日志折叠成人类可读的回合台账（Trajectory）。