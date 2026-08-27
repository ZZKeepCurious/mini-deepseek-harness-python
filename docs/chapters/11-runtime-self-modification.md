# 11 运行时自我修改：动态插件的进程内存生命周期

> 本章回答一个问题：agent 能"给自己加插件"吗？上游的答案克制而有边界——能，但**临时插件只存在于进程内存**，明确不写配置文件、重启即失。我们复现这套生命周期语义本身（define → run → stop → undefine + 检查族），模型侧与审批往返不做。
>
> 对应 dsh 真实源码：`packages/extensions/`（`tool-cordis` 七工具 + `cordis-host-runner` 宿主）。mini 复现于 `miniharness/extensions/dynamic.py`。

## 11.1 上游的边界设计

自我修改在上游不是"agent 任意改配置"：`tool-cordis` 的说明文档 *What it does* 一节里就写了（已核实，`tool-cordis/README.md:19`）：

> 临时插件只保存在进程内存中，明确不写入 cordis.yml，也不会在重启后恢复。

这是刻意的安全设计：模型可以在这个进程里定义/运行/停止插件（改"运行时"），但不允许改"启动配置"（改"持久状态"需要人工操作文件）。配套还有几层约束：

1. **工具分两族**：检查族（`inspect_list` / `inspect_query` / `inspect_self`）+ 修改族（`define` / `run` / `stop` / `undefine`）；
2. **kind 限定**：`define` 只支持 `kind new`（mint 新的 dyn 包）；
3. **宿主耦合**：browser half 带审批往返，agent 不能单方面获得工具能力（审批侧）。web 的审批（第 09 章）管的是"模型要运行什么"，这里的宿主管线管"模型要定义什么"。

## 11.2 生命周期（对应 `runner.spec.ts:150-176` 的完整流程）

上游测试里的标准路径（已核实）：

1. `define`（kind: new）→ mint `dyn-1` / `pkg-1`，**只登记，不生效**；
2. `run` → 立即 `status: 'running'`、mint `run-1`，插件 `apply` 执行、`ctx.provide` 生效；
3. `invoke` → 调用插件提供的服务，返回 `42`；
4. `stop` / `undefine` → 逆序回收。

mini 复现的对应 API：

```python
registry = DynamicPluginRegistry(host_ctx)
pkg_id = registry.define("new", "演示插件", provides=["dynDoubler"],
                         apply=lambda ctx: ctx.provide("dynDoubler", lambda x: x * 2))
run = registry.run(pkg_id)                    # {"runId": "run-1", "status": "running", ...}
registry.invoke(run["runId"], "dynDoubler", 21)  # 42
registry.stop(run["runId"])
registry.undefine(pkg_id)
```

## 11.3 硬性规定（被测试钉住）

1. **define 只登记**：run 之前服务不可用（invoke 抛 KeyError）。
2. **运行中可重复 run / 可 undefine**：`run` = 若已运行先 `_retract` 旧 run 再激活新 run（**replace 语义，非拒绝**，对齐上游 `cordis-host-runner/index.ts:842`——旧 run 的服务立即消失，test `test_run_twice_replaces_old_run` 钉住）；`undefine` 对运行中包**自动 retract 后删除**并返回 `{ok: True, wasRunning}`（对齐上游 `index.ts:215-218`，test `test_undefine_auto_retracts_running` 钉住）；只有缺失包才拒绝——`run` 对未定义包抛 `KeyError`，`undefine` 返回 `{ok: false, reason: 'plugin-missing'}`（不抛错）。
3. **进程级冲突 fail loud**：插件声明的 provides 若祖先链上已存在（如 host 的 `session-persistence`），run 直接拒绝，host 服务不被覆盖。
4. **只存进程内存**：新 registry = "重启"，定义与运行全部不恢复（`inspect_self` 快照为空）。
5. **kind 限定**：非 `new` 的 define 抛 ValueError。
6. **stop 逆序回收**：dispose 隔离 scope（副作用逆序回滚），服务消失。

检查族（`inspect_self` / `query` / `list`）只读注册表快照，与修改族严格分离——与上游 inspect_* 工具族的边界一致。

## 11.4 简化标注

- 上游经模型调用（tool-cordis 是被工具调用的入口），mini 直接操作注册表对象；
- 上游带审批往返（browser half），mini 不做（载体简化，语义不造假）；
- 上游 `run` 会同步配置钩子/挂载包，mini 以 `apply(ctx)` 钩子 + 隔离 scope 表达等价效果。

## 11.5 检查点

- [ ] 说出"进程内存 + 不写配置文件"这个边界为什么重要（运行时 vs 持久状态分离）；
- [ ] 手写一次完整生命周期并观察各阶段 `inspect_self` 快照变化；
- [ ] 构造一个进程级冲突场景，验证 fail loud 且 host 服务不被覆盖；
- [ ] 说出 mini 相对上游的简化（模型侧入口、审批往返）。

> 第 08-11 章至此构成"组合层"闭环：预设（静态装配）→ 干预（运行时控制）→ 轨迹（事后投影）→ 动态插件（运行中自修改），审批（09 章 §9.5）管住干预通道。外部入口（07 章 §7.6-7.8）已复现；下一步进入第 12 章：异步化与并行工具执行。