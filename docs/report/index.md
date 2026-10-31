# DeepSeek Harness 解读报告

<p class="lead">六个主题子页解读上游 dsh：全景、内核、流程、产品面、复现路线与附录。下文 Mermaid 图与<b>四维对照表</b>（上游包 ↔ mini 模块 ↔ 手册章节 ↔ 报告页面）便于在文档间跳转。</p>

<div class="pill-row">
  <span class="tag t-blue">everything is a plugin</span>
  <span class="tag t-blue">event-sourcing</span>
  <span class="tag t-blue">capability seams</span>
  <span class="tag t-green">TypeScript strict</span>
  <span class="tag t-green">Cordis vendored</span>
  <span class="tag t-amber">Python SDK 存在</span>
</div>

## 报告结构

```mermaid
flowchart LR
  subgraph REP["报告 · 读什么"]
    O1["01 全景与分层架构"]
    O2["02 系统架构与内核"]
    O3["03 关键处理流程"]
    O4["04 产品面全解读"]
    O5["05 路线图与 Python 复现"]
    O6["06 附录与 HOWTO"]
  end
  subgraph MD["手册 · 怎么做"]
    M1["00-05 基础章&lt;br/&gt;(环境/会话/总线/工具/Loop/持久化)"]
    M2["06-15 进阶章&lt;br/&gt;(扩展口/入口/组合/干预/轨迹/动态/异步/Cordis/scope/schema)"]
  end
  subgraph SRC["源码（唯一权威）"]
    S1["deepseek-harness/ 上游仓库"]
    S2["mini-deepseek-harness-python/ 复现"]
  end
  SRC --> REP --> MD
  SRC --> MD
```

<p class="fig-cap">图 A：三层阅读结构——源码是唯一权威，报告回答"是什么/为什么"，手册回答"怎么亲手做出来"。基础章沿主线把核心约定立起来；进阶章每章独立，可按需选读。</p>

| 子页 | 内容 |
|---|---|
| [01 项目全景与分层架构](01-overview.md) | 仓库构成、五层架构（应用/组合/能力/框架/外部 SDK） |
| [02 系统架构与内核](02-architecture.md) | 核心包脊柱、ctx 服务地图、事件体系、外围接入面；技术核心六节（Cordis 模型/事件溯源/扩展口/类型/作用域/门禁） |
| [03 关键处理流程](03-flows.md) | Turn/Step、工具管线、持久化、LLM 流式、启动组合五条时序 |
| [04 产品面全解读](04-product-surface.md) | **十大议题**：模式设计、外部入口、Trajectory、干预面、审批、自我修改、resume、plan/goal、压缩与后台、skills |
| [05 路线图与 Python 复现](05-roadmap.md) | 学习路线、概念映射表、迷你复现清单、实操资源索引 |
| [06 附录与 HOWTO](06-appendix.md) | Python SDK、添加插件、HOWTO、参考表、结语 |

## 四维对照表

每一行是一个解读主题；mini 模块列给出对应实现位置，手册/报告列给出深入阅读的入口。

| 上游包 / 文件（唯一权威） | mini 模块 | 手册章节 | 报告页面 |
|---|---|---|---|
| `packages/core/session` | `core/session/` | 01 | 02 §3-4 |
| `packages/context + vendor/cordis(core)` | `core/scope.py` | 02 | 02 §4.1 |
| `packages/core/tools`（含 `when` / `resolve_activation`） | `core/tools.py` | 03 | 03 §5.2 |
| `packages/llm/pi-ai`（`llm-pi-ai/request-headers`） | `llm/pi_ai.py` | 04 §4.9 | — |
| `packages/ptc-runtime/*` + `packages/workflow/workflow-ptc`（sandboxMode / confine） | `ptc_runtime/` + `workflow_ptc/` + `ptc/run_code.py` | — | [architecture.md](../architecture.md) 映射表 |
| `packages/workflow/{workflow,tool-workflow}` | `workflow/` + `tool_workflow/` | — | — |
| `packages/boot/plugin-manager` + `packages/host/plugin-inventory` | `boot/plugin_manager.py` + `web/api.py` Remote | 08 | 04 议题 2 |
| `packages/boot/plugin-market` | `boot/plugin_market.py` + `cli/plugins/plugin_market.py` | — | [interface-wire.md](../interface-wire.md) §4.15 |
| `packages/experimental/action-cards`（opt-in） | `experimental/action_cards/` + `cli/plugins/action_cards.py` | — | — |
| `packages/schedule/schedule` + `schedule/tool-schedule` | `schedule/` + `web/api.py` `schedule/*` | 07 §7.5 | 04 议题 8 |
| `packages/llm/llm + llm-deepseek` | `llm/` | 04 | 02 §3 + 03 §5.4 |
| `packages/core/agent-loop` | `core/agent_loop/agent.py` | 04 / 06 | 03 §5.1 |
| `packages/session/session-persistence` | `core/session/persistence.py` | 05 | 03 §5.3 |
| `packages/extensions/cordis-host-runner + packages/boot` | `boot/boot.py` | 05 | 03 §5.5 |
| `vendor/loader + vendor/include` | `loader/`（EntryTree + Entry + Group + Loader + Include + patch） | 08 §8.3.1 | 03 §5.5 |
| `packages/bundle/headless + apps/cli/src` | `cli/headless.py + cli/main.py` | 07 | 04 议题 2 |
| `apps/cli/config/agent-presets/*` | `preset/presets.py` | 08 | 04 议题 1 |
| `packages/core/agent（runtime-types）` | `core/agent_loop/agent.py（干预面）` | 09 | 04 议题 4 |
| `packages/interaction/user-approval` | `interaction/approval.py` | 09 | 04 议题 5 |
| `packages/client/ui-trajectory` | `client/trajectory.py` | 10 | 04 议题 3 |
| `packages/extensions（tool-cordis 等）` | `extensions/dynamic.py` | 11 | 04 议题 6 |
| `packages/sdk/protocol（transport + types）` | `protocol/sdk.py` | 07 §7.6 | 04 议题 2 |
| `packages/acp/acp` | `protocol/acp.py` | 07 §7.7 | 04 议题 2 |
| `packages/hooks（hook-protocol + hooks-claude-code）` | `protocol/hooks.py` | 07 §7.8 | 04 议题 2 |
| `core/agent-loop 并行编排 + core/context 并发模型` | `core/agent_loop/tool_calls.py + core/scope.py` | 12 | 03 §5.1 |
| `packages/sandbox/sandbox + sandbox-local + sandbox-windows-acl + native/landlock-run` | `seams/sandbox_local.py + seams/landlock_run.py + seams/sandbox_windows_acl/` | 06 §6.9 | — |
| `packages/sandbox/sandbox-policy` | `seams/sandbox_policy.py` | 06 §6.9 | — |
| `packages/shell/shell + bash-local + bash-sandbox` | `shell/`（bash_local + bash_sandbox + helpers） | 06 §6.9 | — |
| `packages/credentials/credentials-local` | `seams/credentials_local.py` | 06 §6.9 | — |
| `packages/subprocess/subprocess（环境清洗）` | `seams/subprocess_env.py` | — | — |
| `packages/subagent/*（subagent + 三通道 provider + in-process-driver + control/report 工具）` | `seams/subagent/`（descriptor + providers + worker + continuation + tool） | 06 §6.9 | — |
| `packages/llm/llm-retry + llm/llm（retry-policy）+ core/agent（agent/request-error）` | `llm/retry.py + llm/retry_policy.py（+ core/agent_loop/agent.py 接线）` | 04 §4.9 | — |
| `packages/boot/app-boot（loadOverlayPatches / loadEnv / config-dump）+ apps/cli/src/args.ts` | `boot/composition.py + cli/main.py` | 05 + 07 | 03 §5.5 |
| `web 表面会话管理（上游无 CLI）` | `cli/session_cmds.py` | 07 | 04 议题 2 |
| （工程化） | `.github/workflows/ci.yml + tests/test_real_api.py` | 00 | — |
| `packages/client/connection + packages/api/gateway + packages/api/session-controller + packages/api/remotes + host/frontend-static + host/webserver` | `web/`（envelope + stream_protocol + mux + events + streams + api + approvals + server + frontend + launcher） | 07 §7.5 | 04 议题 2 |
| （wire 约定，非上游包） | `docs/interface-wire.md`（与 `WebApi.ROUTES` 同步） | 07 §7.5.2 | — |
| （模块映射，非上游包） | `docs/architecture.md` | 08 + 各章 | 05 |
| `packages/session-query/session-log-export（导出域）+ api/session-controller（端点约定面）` | `web/downloads.py` | 07 §7.5 | — |
| `packages/bundle/web-app` + `packages/client` | `webui/`（产品化 React+TS+Vite 独立工程，只依赖 wire 约定）+ `web/static/`（vanilla SPA 教学参照，旧 SSE wire 不对新后端工作） | 07 §7.5.4 | 04 议题 2 |
| `packages/skill/（skill + skill-filesystem + tool-skill + skill-badge）` | `skills/`（registry + filesystem + tool_skill） | 13 | 04 议题 10 |
| `vendor/schemastery/src/index.ts` | `core/schema.py` | 15 | — |
| `packages/core/scope（dsh-scope 协议原语）` | `core/dsh_scope.py` | 14 | 02 §4.5 |
| `vendor/hmr + packages/boot/app-boot（watchUserPatches）` | `core/hmr.py + boot/boot.py（watch_user_patches）` | 02 | 02 §4.1 |
| `packages/core/session（SessionStore 服务层）` | `core/session_store.py` | — | 04 议题 7 |
| `packages/attachment/（attachment + attachment-local）` | `attachment/`（types + error + image + store + file_store——verbatim 文件附件对应上游 alpha.1 新增面） | 07 §7.7 | — |
| `packages/token-meter` | `llm/token_meter.py` | — | 04 议题 9 |
| `packages/compaction/（compaction-basic + compaction-tool-result-pruner）` | `compaction/`（config + region + summarizer + engine + tool_result_pruner） | — | 04 议题 9 |
| `packages/jobs/（jobs-local + tool-jobs）` | `jobs/`（types + registry + tools） | — | 04 议题 9 |
| `packages/plan/plan-mode` | `plan/`（config + mode + review + projection） | — | 04 议题 8 |
| `packages/goal/（goal + goal-round-driver + tool-goal + command-goal）` | `goal/`（domain + service + prompt + driver + tools + commands） | — | 04 议题 8 |
| `packages/interaction/commands` | `commands/`（CommandRegistry + command/run|done 配对） | — | 04 议题 8 |

## 与教程手册的关系

<div class="grid" markdown>

<div class="card" markdown>

**报告**讲上游与 mini 的设计：是什么、为什么、产品面与流程；按主题阅读，图在 MkDocs 中渲染。

</div>

<div class="card" markdown>

**手册**带你在 Python 里逐步实现，章节与 `miniharness/` 代码对应；章节目录见 [docs/index.md](../index.md)。

</div>

</div>

手册入口：站点首页 [docs/index.md](../index.md)。章节 08-11 与报告 04 页议题一一对应（08 组合层↔议题 1，09 干预面↔议题 4，10 轨迹↔议题 3，11 动态插件↔议题 6）。

---

<p class="foot-note">配套教程手册：docs/chapters/（从 0 到 1 实现核心系统）· 图表由 Mermaid.js 渲染。</p>