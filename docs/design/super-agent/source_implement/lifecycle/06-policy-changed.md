# 6. Policy 变更

<p class="lead">Settings 保存新 Policy 版本后，Engine 收到 `policyChanged(from, to, effects)`：通知 open Ticket、对进行中的 Eval reassess，并可能改变 wake/revalidate 行为。这是「运行时改规则」与「已冻结 Mandate」之间的接缝。</p>

## 在主线中的位置

可在任意时刻发生：Eval 等待主 Session decide 时（与第 2 章交叉）、Ticket 运行中、或 Ticket 已 ended 等人消息。不改变 Trigger 摄入规则本身，但改变 **gate、proactive 关闭、requester 是否仍允许** 等（`policy.ts` 的 `policyEffects`）。

## 域内步骤

1. **Settings**（`app/settings.ts`）持久化新版本并 diff 出 `policyEffects`（规则变更、proactive 关闭、requester 不再允许等）。
2. **`Engine.policyChanged`**（Settings Pick）在事务内：
   - **`Tickets.policyChanged`** — 通知受影响的 open Ticket（可能 end、drop 订阅或标记需人处理）。
   - **`Evals.policyChanged`** — 对 active Eval reassess 或 unavailable。
3. 后续 **Pass pump** 可能 `Tickets.wake` 或新 Eval（取决于 revalidate 与 Trigger）。

Mandate 在 accept 时冻结 `policyVersion`；**revalidate(mandate)** 决定 ended Ticket 能否因新消息 wake，而不是 retroactive 改 Mandate 字段。

## 与 assessment 的交叉

若 Policy 在 candidate 已展示但未 decide 时变更，Eval 可能收到 reassess request 或直接被 unavailable，避免用旧规则 accept。旁路 `core-engine.spec.ts` 有 `policyChanged during assessment` 类用例。

## 涉及模块

| 路径 | 职责 |
|------|------|
| `policy.ts` | gate、revalidate、policyEffects |
| `app/settings.ts` | 保存、调 policyChanged |
| `core/tickets.ts` | policyChanged、wake |
| `core/evals.ts` | policyChanged |

## 写者与不变量

- Settings 与 history：**Settings** 模块写 KV/表；Engine 只响应 `policyChanged` 命令。
- Ticket/Eval 状态：**Engine**。

## 边界

- 仅 volatile 部署字段变更可能不经过 Engine（Deployment 热更新）；**Policy 内容**变更必走本流程。
- proactive 关闭可能 end 一批 Ticket，不等 worker ready。

## 测试

- 旁路：`policy.spec.ts`；`core-engine.spec.ts` policyChanged。
- Python：`test_flows.test_flow_policy_changed`（receive + pump → set_version v2 → policy_changed → pump → assessor requests ≥ 2）。

Assessor / Sessions 如何把 Policy 变更展示到主 Session 见 [reference-semantics.md](../reference-semantics.md) Assessor 一节；管理面保存路径见 [侧链](../sidechain.md)。
