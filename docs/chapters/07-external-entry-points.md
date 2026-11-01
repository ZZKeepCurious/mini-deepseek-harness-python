# 第 7 章：外部入口——两个产品表面与三个协议入口

> 对应 dsh 真实源码：`apps/cli` + `packages/boot/app-boot` + `packages/bundle/{headless,web-app,acp-app,sdk-app,sdk-minimal}` + `packages/{acp,sdk,hooks}`。
> 前置：第 4~6 章。本章产出：`miniharness/cli/headless.py`、`miniharness/web/`、`miniharness/protocol/{sdk,acp,hooks}.py`。

## 在 system-map 中的位置

本章对应 [system-map Fig S1](../system-map.md#fig-s1) 的 **Surface_07**（体裁：**解读**）。不要求从零敲内核；对照已实现代码与上游 profile 语义。总览图：[Fig S5](../system-map.md#fig-s5) · Wire：[wire-tour.md](../guides/wire-tour.md) · 走查：[T3](../guides/traces/web-unary-session.md)、[T4](../guides/traces/web-follow-approval.md)。

## 7.0 导读

- **读法**：上游「入口 = 组合树差异」→ mini `cli/main.py` + `boot/` + `web/` 映射 → 用 web 测试与 trace 验证。
- **篇幅**：§7.5 wire 细节以 [interface-wire.md](../interface-wire.md) 为字段单源；本章拆为多文件，下表跳转各节。

| 节 | 主题 | mini 主路径 |
|---|---|---|
| [§7.2](07-headless.md#sec-7-2) | headless 一次性任务 | `cli/headless.py`、`cli/main.py` |
| [§7.3](07-web-surface.md#sec-7-3) | web 表面概览 | `web/launcher.py`、`webui/` |
| [§7.4](07-protocol-overview.md#sec-7-4) | SDK / ACP / hooks 入口 | `protocol/` |
| [§7.5](07-web-transport.md#sec-7-5) | Gateway、mux、streams、审批桥 | `web/*.py` |
| [§7.6–§7.8](07-sdk.md#sec-7-6) | 三协议最小子集 | `protocol/{sdk,acp,hooks}.py`（[ACP](07-acp.md)、[hooks](07-hooks.md)） |
| [§7.9](07-external-summary.md#sec-7-9) | 入口对照总表 | — |
| 走查 | unary / 审批 / SDK | [T3](../guides/traces/web-unary-session.md)、[T4](../guides/traces/web-follow-approval.md)、[T5](../guides/traces/sdk-stdio.md) |

## 7.1 这一章要做什么：一切入口都是 profile

前六章讲内核：会话、总线、工具、loop、持久化、扩展口。这一章讲外壳——所有能"启动一个 dsh 进程"的路径，以及它们各自把什么约定暴露给外部。

dsh 没有"web 模式"和"headless 模式"两个程序。它只有一个启动器 `dsh`（`apps/cli/src/args.ts`），启动器只解析自己的标志，然后把剩余参数原样交给"被启动的 profile"。profile 是 `$DSH_HOME/profiles/<name>` 下的一个目录，里面有一个 `package.json`（声明有序的 bundle 层列表）和一个用户自己的 `cordis.patch.yml`。**每种入口 = 一份不同的插件组合树**，入口之间的差异全在组合层，内核一行不改。

启动器自己拥有的东西只有四类（`apps/cli/src/args.ts`）：

1. `--profile <name>`：启动哪个 profile（必填）。
2. `web`：`--profile web` 的硬编码别名。
3. `plugin --profile <name> <pnpm args>`：把 pnpm 转发进 profile 目录装插件。
4. `--dump-config` / `--dump-default-config`：打印组合树并退出，不启动。

被启动的应用自己解析自己的标志（`--host`、`--port`、任务文本、`--help`），所以 `dsh --profile web --help` 打印的是 web 应用的帮助，不是启动器的。这个"启动器只做薄壳"的分层是理解一切入口的前提。

出厂自带的 profile 模板有五个（`packages/boot/app-boot/src/profile.ts`）：

| profile | bundle 层 | 形态 |
|---|---|---|
| `web` | `dsh-base` + `dsh-web-app` | 浏览器表面，有 Host/HTTP/浏览器插件 |
| `headless` | `dsh-base` + `dsh-headless` | 一次性任务，无任何 Host 层 |
| `acp` | `dsh-base` + `dsh-acp-app` | ACP stdio 服务器 |
| `sdk` | `dsh-base` + `dsh-sdk-app` | SDK JSON-RPC 服务器 |
| `sdk-minimal` | `dsh-sdk-minimal` | 最小 SDK 组合（不叠 base） |

前四个都叠在 `dsh-base` 上；`sdk-minimal` 只带自己的 bundle。`dsh-base` 是共享的内核底座（persona、工具模式、PTC——programmatic tool calls，程序化工具调用——worker 等）。其它名字的 profile 首次使用不会自动初始化，必须先经 `dsh plugin` 路径显式创建（`initProfile`），否则 fail loud。

### mini 启动器（`miniharness/cli/main.py`）

`miniharness/cli/main.py` 复现了启动器的选项语义（同 `apps/cli/src/args.ts`）：

- `--profile headless "task"`：一次性任务（[§7.2](07-headless.md#sec-7-2) 全部语义）；`--profile web`：启动 FastAPI 服务表层（[§7.5](07-web-transport.md#sec-7-5)，需 fastapi/uvicorn 的 `[web]` extra）；未知 profile fail loud。
- 无任何参数（无 `--profile`/`--config`/`--patch`）时回退运行 `demo_main`（无 key 端到端演示，main.py）。
- `--patch <path>`（可重复）：YAML/JSON overlay 补丁，参与组合层叠与 dump。
- `--dump-config`：只读打印最终组合（boot-free，不启动任何应用）；`--dump-default-config`：只打印内置默认组合。两者互斥（`program.error` 同语义）；dump 不接受任务参数；`--dump-default-config` 不接受 `--patch`/`--config`。输出带行级 `# == <label>` 来源注释、`!!js` 表达式原样未求值、skipped patch warn 不失败、单文档可再加载（同 `renderConfigDump`）。
- 教学扩展（上游没有）：`--config <path>` 指定组合文件（上游用 profile 目录）；`miniharness sessions` 子命令（会话列表 / 恢复 / 删除 —— 上游会话管理在 web 表层，见 `miniharness/cli/session_cmds.py`）。
- headless 路径的内置默认组合为空（`cli/headless.py` 有简化标注）；组合层与 headless 运行时解耦：带 `--config/--patch` 跑任务时先 boot 验证（fail loud），headless 运行时仍为内置 adapter。

