# 侧链：部署、Glue 与管理面

<p class="lead">一条 Trigger 从进入到 Outbox 回复的主线不经过 Cordis 插件细节、HTTP 管理 API 或 React 面板。需要改部署、改工具参数翻译或读视图 SQL 时，再打开本篇。</p>

## 部署与 Policy

`deployment.ts` 描述机器级 `stateDir`、账户与 worker 槽位。Runtime 构造前对 `stateDir` 加文件锁，防止双进程同时迁移或写 SQLite。`DeploymentConfig` 里部分字段可热更新，账户变更则触发插件 `restart`（`index.ts`）。

`policy.ts` 提供无状态的 `Policy` 快照：`gate` 决定 Eval 候选是否 admissible；`revalidate` 在 Policy 变更后判断已结束 Ticket 能否被 `wake`；`admitsInferred` 控制主 Session 能否 inferred accept。Settings 保存新版本后走 [第 6 章 Policy 变更](lifecycle/06-policy-changed.md) 进 Engine。

`setup.ts` 与 `app/setup.ts` 收集「未配置完成」类问题；Passes 在 `configured()` 为 false 时不 pump，避免半成品环境狂刷 Eval。

Python：`superagent.infra.policy.SimplePolicy` 仅覆盖测试所需 gate；无 Cordis restart 语义。

## Glue：HTTP、工具、设置

管理 HTTP（`app/http.ts`、`app/api.ts`）与 Store 只读层（`store-read.ts`、各 `app/views/*`）为面板提供线程、Ticket、时间线与设置。**写**域状态必须调用 Engine，禁止 handler 直接改 SQLite 行。

| 面 | 模块 | 调用的 Engine 面 |
|----|------|------------------|
| 管理 HTTP | `http.ts`, `api.ts` | 读 Store；部分 People / MainSession 命令 |
| 主 Session 工具 | `app/tools/main.ts` | MainSession：`decide`、`attach`、`reply` 等 |
| Worker 工具 | `app/tools/executor.ts` | Executor：`report`、`handback` 等 |
| Settings | `app/settings.ts` | Settings：`policyChanged` |
| Prompts | `app/prompts.ts` | 版本化 prompt，不经 Engine 写 Ticket |

工具层把 JSON 参数译成 Engine 方法；`UserError`（`core/errors.ts`）映射为 HTTP 4xx 或工具拒绝文案。Prompts、cards、links 等多为展示与 IM 文案辅助，不参与 Trigger 摄入事务。

Python 包未实现管理 HTTP；本地可对 `Runtime.engine` 直接调用。若 [adopted.md](../adopted.md) 将来登记管理面原则，再评估是否接 mini `web/`。

## 管理 UI 边界

`client/*` 为 SPA；`app/views/*` 把 Store 读模型投影为概览、Ticket 执行面板、Source 配置、设置与记录时间线。视图不持有域写权限——按钮背后仍是 HTTP → Engine 或 Settings。

走读不逐组件解读 React；行为以 `panel-*.spec.tsx`、`views.spec.ts`、`timeline.spec.ts` 与 [proto-005 面板](../proto-005-panel.md) 为参照。与 Engine 相关的用户操作（stop、retry、reevaluate、面板 reply）最终都落在 Engine 的 People 或 MainSession Pick 上。

## 与主轴的衔接点

- Source 插件由 Runtime 注册，Trigger 仍经 [第 1 章 Trigger 摄入](lifecycle/01-trigger-receive.md) 进域。
- accept 后 launch/delivery 由 Effects 驱动 DshExecutor（[第 3 章 Assignment](lifecycle/03-assignment-lifecycle.md)）。
- 主 Session 上的 `decide(accept)` 与 worker 上的 `report` 是 Glue 最常触发的两条写路径。
