# Mini DeepSeek Harness (Python)

English | [中文](README.zh.md)

**Mini DeepSeek Harness** is a Python agent runtime. It re-implements the core contracts of [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (`dsh`, the open-source agent harness by [DeepSeek AI](https://deepseek.com)) layer by layer, and aims to be production-ready. It depends on `httpx` (DeepSeek SSE transport), `filelock` (credential cross-process writer locking), `watchdog` (file watch), and `pyyaml` (YAML config); the web transport layer is an optional extra installed with `pip install ".[web]"` (`fastapi` + `uvicorn`). Mature libraries are used directly; the standard library is kept only where no equivalent library fits.

The upstream system is built on one idea, **everything is a plugin**, powered by [Cordis](https://github.com/cordiverse/cordis), a dependency-injection and event-bus framework described in [_A Programming Paradigm for Spatiotemporal Composability_](https://github.com/cordiverse/paper). This repository rebuilds its core contracts in Python: the event-sourced session log, the plugin event bus, the turn/step agent loop, and the capability-seam triangle (Service Definition / Service Provider / Consumer). Anyone with `python3` can read, run, and modify the code.

> **A production-oriented re-implementation, not a byte-for-byte port.** It is not affiliated with DeepSeek AI. We favor the parts that matter in real deployment: the wire contract, the event-sourced session log, and reliability / security / interop semantics. Deviations from upstream are documented, never silently assumed.

> **Disclaimer**: a large part of this repository — including the analysis report and the handbook — was summarized, written, and re-implemented with the help of AI assistants. It may contain misunderstandings or inaccuracies about the upstream source code and documentation. The upstream repository itself is the only authoritative reference.

## Documentation

Two complementary documents (both in Chinese):

- **[Analysis report](docs/report/index.md)** — a deep dive into the upstream repository: five-layer architecture, the `ctx` service map, core techniques, and key processing flows, fully illustrated with Mermaid diagrams (landing page + six topic pages). Rendered on GitHub Pages via MkDocs: https://zzkeepcurious.github.io/mini-deepseek-harness-python/
- **[Step-by-step handbook](docs/chapters/)** — how the system grows from zero, one chapter at a time: concepts → minimal runnable code → invariants/tests → checkpoint exercises.

See [ROADMAP.md](ROADMAP.md) for where this project is heading.

## What's inside

| Capability | Upstream counterpart |
|---|---|
| Event-sourced session (envelope `{type,seq,time,data}`, 1-based turn/step, deep-freeze, `derive_messages`, interrupted repair) | `packages/core/session` |
| Durable storage (JSONL / SQLite, zstd concatenated-frame container with one event per line by default, `root/--<projectKey>--/<encoded-id>/session.v3.jsonl[.zstd]` layout, header + `SESSION_FORMAT_VERSION=3` bidirectional refusal, encoding/layout mismatches rejected outright, flush barrier, crash recovery, multi-generation read-side that migrates released v0/v1 artifacts in-place via the upstream chain) | `packages/session/session-persistence` + `session-format-*` |
| Plugin event bus (emit / waterfall / parallel / serial, scopes, dependency-driven activation, epoch reload via HMR service + `watch_user_patches`) | `vendor/cordis` + `vendor/hmr` + `core/scope` + `core/hmr` |
| Config schema engine (full schemastery port: 17 resolvers, meta clone, toString/toJSON/i18n/simplify, `~standard` protocol face) | `vendor/schemastery/src/index.ts` |
| Tool registry + execution pipeline (schema validation, pre/execute/post, timeout) | `packages/core/tools` |
| Agent loop (async-driven turn/step state machine, sync facade driven by a process-wide resident event loop, pre-step rejection, tool-feedback continuation) | `core/agent-loop` |
| LLM seam (async `stream(messages, tools, signal)` contract, fake adapter, official DeepSeek SSE adapter over `httpx` async streaming, four-level `reasoning_effort`) | `llm/llm` + `llm/llm-deepseek` |
| Image-capable DeepSeek requests (catalog capability resolution, `ImageRequestTarget` projection geometry, provider vision-token pricing, Files API upload/reuse with a durable index, inline-base64 fallback, bounded stale-id retry, normalized-image diagnostics; `image/offload` durable omission with `IMAGE_OFFLOAD_REQUIRED` recovery) | `llm/llm-deepseek` (`common/*`) + `attachment/attachment-local` + `compaction/compaction-image-offload` |
| Model request retry / backoff (normal/always policy, `agent/request-error`, `llm/retry` audit pair, fused-signal pre-dispatch check, event-driven multi-signal race cancellable wait, plugin teardown draining in-flight recoveries) | `llm/llm-retry` + `llm/llm/src/retry-policy.ts` |
| Token metering (incremental fold, usage anchor, 4 chars/token heuristic) | `llm/token-meter` |
| Context compaction (pre-step pressure + `CONTEXT_WINDOW_EXCEEDED` recovery, surface-replace checkpoint transaction, optional tool-result pruner stage) | `compaction/compaction-basic` + `compaction-tool-result-pruner` |
| Background jobs (`job_output`/`job_list`/`job_kill`, completion notices, per-owner cap; no `job/*` session events) | `packages/jobs` (jobs-local + tool-jobs) |
| Plan mode (log-only `plan/mode` state, plan:policy prompt-section injection, queued in-turn commit) | `packages/plan/plan-mode` |
| Plan review UI (`/plan` command, `exit_plan_mode` tool, user-questions channel, plan projection) | `packages/plan/plan-mode` |
| Command surface (`/`-command registry, `command/run` + `command/done` pairing) | `packages/interaction/commands` |
| Goals (`goal/change` event-sourced fold, `GoalService`, automatic goal-round continuation, `get_goal`/`create_goal`/`update_goal` tools, `/goal` command) | `packages/goal` (goal + goal-round-driver + tool-goal + command-goal) |
| System prompt sections (ordered section registration + rendering into each request) | `core/system-prompt` |
| Boot & composition (YAML/JSON overlays, `!!js` env interpolation, startup assertions) | `packages/boot` |
| Headless one-shot entry (`--profile headless "task"`: stdout final text, exit code by turn/end reason) | `packages/bundle/headless` + `apps/cli` |
| Web transport layer + browser frontend (`--profile web`; two-envelope RPC, `/api/remote.mux`, `$events`, follow/control, approval bridge, FastAPI carrier, session export, `webui/` frontend; see details below) | `packages/api/gateway` + `packages/api/session-controller` + `packages/api/remotes` + `host/frontend-static` + `host/webserver` |
| Launcher options (`--patch`, `--dump-config` / `--dump-default-config`, read-only composition dump) | `apps/cli/src/args.ts` |
| Session management CLI (`miniharness sessions` list/resume/delete/stats; mini teaching extension; `stats` renders sessionStats/tokenUsage projection + last-turn token accounting) | web surface (upstream) |
| Telemetry / usage stats (sessionStats + tokenUsage projections as real `projections.values` on `session.follow`/`session.control` and Remote snapshot/baseline; per-turn token accounting `derive_turn_token_usage` fail-closed on missing boundaries; opt-in `UsageStatsService` on `ctx.usageStats`) | `packages/session/session-stats` + `packages/llm/token-meter` |
| Session store service (`ctx.sessions`: create/prepare/enter/announce lifecycle, fork with 5 error codes, flush checkpoint, `session/created|disposed|event|flush` events) | `packages/core/session` (SessionStore) |
| Storage hub (`ctx.storage`: named backend registry + mountable storage forms, schema-validated KV domains with single-writer chain and post-persist `domain/changed` events, JSON medium as a single-unit document or per-record documents with atomic rewrite) | `packages/storage` (storage + storage-domain + storage-json) |
| Capability seams (sandbox / credentials / authorization / subagent; see details below) | capability seams docs |
| Continuable subagents (durable child sessions, async settlement, lifecycle events, control tools; see details below) | `packages/subagent` |
| Agent Teams (roster + mailbox + shared task DAG; see details below) | `packages/experimental/agent-team` + `tool-agent-team` |
| MCP client (stdio / streamable-http, reconnect, tool ownership registration; see details below) | `packages/mcp/mcp-client` + `mcp-resources` |
| Presets / agent intervention / trajectory / dynamic plugins / approval | `packages/preset` + `core/agent` + `interaction` |
| Preset system (shipped `system` root + multi-root first-root-wins roster, `project_preset`/`project_session_agent_preset` projections, `PresetLockedError` on already-started sessions, shipped presets read-only to authoring, `agent.cordis.yml` → mini Preset translation; `miniharness presets list/show/select/delete` as a teaching-extension CLI for the upstream web Remote surface) | `packages/preset` (agent-presets) |
| Protocol entries (ACP / JSON-RPC SDK / hooks bridge) | `acp` + `sdk` + `hooks` |
| Official Python SDK interop (upstream `DeepSeekHarness` drives mini worker via `_launch_args`; `tests/test_upstream_sdk_interop.py`, skips without pydantic/upstream sources) | `python/sdk` |
| Async event bus, parallel tool execution + barrier | `core/agent-loop` |
| CI (GitHub Actions, Python 3.10~3.13, integration-tagged real-API tests) | — |

### Capability details

**Web transport layer + browser frontend.** Started with `--profile web`. Two-envelope RPC (`client-request` / `server-response`), a WebApi unary session service, and the Remote stream wire (`open` / `cancel` / `item` / `end` / `error` frames over a single `/api/remote.mux` WebSocket). A `$events` registry forwards `api-session/*` and settles the `approval/request` waterfall through `$events/result`; `session.follow` and `session.control` carry the streams. The approval bridge connects the async `tools/ask` gate to the `$events` waterfall. The FastAPI carrier mirrors the gateway `stream-server.ts` / `handler.ts` status-code chain. Session-log export at `GET /api/session.export` zips the root session, subagent descendants, and referenced media, keeping the 200 / 400 / 404 / 501 / 500 status chain with a private error shell. The productized `webui/` frontend is a standalone React project at the repo top that depends only on the wire contract: session list and create, Trajectory (virtualized windowing, Overview collapsed view, full-text search), approval waterfall, and queue/jobs panel; its `vite build` output is served from `MINIHARNESS_WEBUI_DIST`. The `web/static/` vanilla SPA is kept as a teaching reference only and does not work against the current backend.

**Capability seams.**

- Sandbox: backend, policy service, and bash consumer executor, including `ctx.sandboxPolicy` resolution, `sandbox/mode` log override, and `ctx.shell` confined wrapping with three-way attribution.
- Credentials: four layers plus a record service (`read` / `describe` / `list` / `modify` / `delete_record`), with `<scope>/<id>` key grammar, a 30s cross-process writer lock, `modifyRecord` as the only write path, and the `ctx.credentials` Service plus `credentials/record-updated` event.
- Authorization: `install_authorization(ctx)` provides `registerFlow` / `list` / `describe` / `cancel` / `begin` and the `authorization/settled` event, with the error-code set DUPLICATE_FLOW / NO_FLOW / UNKNOWN_METHOD / ALREADY_IN_FLIGHT / NOT_COMMITTED / DECLINED. Credential commits are booked through `credentials/record-updated` and rechecked with `describe_record`.
- Subagent: ACP, SDK, and fork channels.

**Continuable subagents.** `start_continuable` / `send_message` (with an optional initial prompt), durable child sessions with cold resume, settlement delivery, and async event-driven A8 (submit-and-return, `watchSettlement`, steer batch merge, ownership bookkeeping waiting / settled). Lifecycle events `subagent/start` / `subagent/end` pair by runId, fold `epochStopReason` / `foldConsumedWork` at the end, and dispatch through the delegating parent's scope carrier. A named provider registry (`register_provider`, publishing `subagent/provider-removed` on dispose) supports DRAINING admission cutoff (`drain` / `drain_descendants` with `assert_admitting`, verbatim refusal wording), an interrupt authority matrix (user / ancestor authority, absent-target no-op), and nested delegation (exec.agent as the authorization subject, grandchild settlement notices to the direct parent). The model-side delegation tool `subagent` uses verbatim descriptions, a canonical value with `Tool.render`, and `run_in_background` routing. Control tools: `send_message` / `interrupt_agent` / `list_agents`.

**Agent Teams.** An implicit-root roster, a durable peer mailbox, and a shared task DAG. The four event types `team/member`(v2) / `team/task` / `team/message/queued` / `team/message/delivered` are all log-only, with the Team Lead session as the authoritative journal. Members are provisioned as `start_continuable` children. The task board uses CAS transitions for 8 actions, rejects cycles, and reports advisory write-scope overlap. The model side has 9 tools plus a `team:policy` prompt section. Spawn delivery runs both sync and async (inside the event loop). Wire and Remote endpoints are not carried; error semantics come from the `TeamError.code` closed set.

**MCP client.** `apply(ctx, config)` supports stdio and streamable-http transports, reconnect with exponential back-off and a budget, tool ownership registration `${server}.${name}#${hash}` with `sync_tools` swap-on-change, `tools/list_changed` resync, a server-instructions byte cap with `failOnStartupError`, image projection, and darkfrozen output render. Carrier note (SDK 2.2): the stdio transport does not surface child EOF or exit (`_drain_stdout` blocks indefinitely), so mini uses a bounded RPC race (5s, aborted on `generation.lost`) plus a lifeline ping watchdog (15s heartbeat, 3s timeout).

The upstream browser frontend (`packages/client`, React monorepo) is not reproduced verbatim: its wire surface matches upstream, so an upstream client pointed at the mini backend works. Two consumer fronts ship: the productized `webui/` (repo-top standalone React + TypeScript + Vite project, depends only on the wire contract; build & run: [`webui/README.md`](webui/README.md)), plus `web/static/` as a vanilla SPA teaching reference only (old SSE wire, does not work against the current alpha.1 backend).

## Getting started

Requirements: Python 3.10+. Required dependencies install with `pip install -e .`: `httpx` (DeepSeek SSE transport), `filelock` (credential cross-process writer locking), `watchdog` (HMR file watch), `pyyaml` (YAML config); the web transport layer needs `pip install ".[web]"`.

```sh
# run all tests
python -m unittest discover -s tests -t .

# end-to-end demo (fake model + tools + crash recovery, no API key needed)
python -m miniharness.demo

# multi-turn chat with the fake model
python examples/chat_demo.py

# plan mode + goal demo (/plan, exit_plan_mode review, /goal, goal-round continuation)
python examples/plan_goal_demo.py --approve

# one-shot task, like `dsh --profile headless "task"` (needs DEEPSEEK_API_KEY)
python -m miniharness.cli --profile headless "run the tests"

# start the web transport server (requires: pip install ".[web]")
python -m miniharness.cli --profile web

# read-only composition dump, like `dsh --dump-config`
python -m miniharness.cli --dump-config

# list / resume / delete persisted sessions
python -m miniharness.cli sessions

# agent presets: shipped system root + user presets (list/show/select/delete)
python -m miniharness.cli presets list
```

All CLI-written state lives under `MINIHARNESS_HOME` (default `~/.miniharness`): `--profile headless` sessions and `miniharness sessions` under `$MINIHARNESS_HOME/sessions`, user presets under `$MINIHARNESS_HOME/.agent-presets`. Point it elsewhere to relocate all durable state, e.g. `export MINIHARNESS_HOME=/data/miniharness`.

### Talk to the real DeepSeek API (optional)

```sh
export DEEPSEEK_API_KEY=sk-...            # PowerShell: set DEEPSEEK_API_KEY=sk-...
python examples/real_api_demo.py
```

### Install as a CLI

```sh
pip install -e .
miniharness            # equivalent to `python -m miniharness.cli` (same entry point)
```

## Project layout

```
mini-deepseek-harness-python/
├── miniharness/             # core package (mature OSS libraries first, family layout, see docs/architecture.md)
│   ├── core/                # upstream packages/core
│   │   ├── session/         # types / json / message / invariant / repair / surface / session
│   │   │   ├── persistence.py
│   │   │   ├── generation.py  # multi-generation read-side + migrate-on-open
│   │   │   ├── released/      # released v0/v1 codecs + v0→v1→v2→v3 migration chain
│   │   │   └── zstd_frames.py
│   │   ├── scope.py         # Context / PluginManager
│   │   ├── tools.py         # tool registry + execution pipeline
│   │   └── agent_loop/      # agent.py + tool_calls.py
│   ├── llm/                 # upstream packages/llm
│   │   ├── protocol.py      # StreamChunk / LlmAdapter / LlmFailure / BlockAssembler
│   │   ├── deepseek.py      # DeepSeek wire serialization + SSE adapter
│   │   ├── fake.py          # FakeLlmAdapter (no API key)
│   │   ├── retry_policy.py  # retry policy parsing (normal/always)
│   │   ├── retry.py         # agent/request-error recovery + backoff
│   │   ├── token_meter.py   # TokenMeter incremental fold + usage anchor
│   │   └── deepseek_files/  # DeepSeek Files API execution cluster (image requests)
│   ├── compaction/          # upstream packages/compaction
│   │   ├── engine.py        # pre-step pressure + request-error overflow recovery
│   │   ├── region.py        # selectCompactableRange + checkpoint transaction
│   │   ├── summarizer.py    # prefix-replay summarization + checkpoint framing
│   │   └── config.py        # spec parsing (threshold / retain / retries)
│   ├── boot/                # upstream packages/boot
│   │   ├── boot.py          # startup + patch overlays
│   │   ├── composition.py   # YAML config / !!js interpolation / dump rendering
│   │   └── dotenv.py        # .env parsing (parse_dotenv)
│   ├── cli/                 # apps/cli
│   │   ├── main.py          # launcher options (profile / patch / dump)
│   │   ├── headless.py      # one-shot task entry
│   │   ├── default_tools.py # default toolset for headless
│   │   └── session_cmds.py  # session list / resume / delete
│   ├── protocol/            # acp / sdk / hooks bridges
│   ├── seams/               # sandbox / credentials (incl. CredentialsService) / authorization / subagent seams (incl. windows-acl kernel executor)
│   ├── shell/               # ctx.shell bash executor family (local + sandboxed)
│   ├── goal/  plan/  jobs/  skills/  commands/  attachment/
│   ├── web/                 # apiproxy subset: envelope / api / streams / approvals / server / downloads / frontend / launcher
│   ├── web/static/          # vanilla SPA teaching reference (old SSE wire; product frontend = ../webui)
│   ├── preset/  extensions/  interaction/  client/
│   ├── demo.py              # end-to-end demo
│   └── example_plugins.py   # boot demo plugins
├── tests/                   # acceptance tests (unittest)
├── examples/                # chat & real-API demos
└── docs/
    ├── index.md            # handbook index (learning map)
    ├── architecture.md      # architecture + upstream mapping
    ├── chapters/            # 00-setup ~ 15-schemastery tutorials
    └── report/              # analysis report (MkDocs Markdown, Mermaid diagrams)
```

## Acknowledgements

- [DeepSeek AI](https://deepseek.com) and the [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) team, for the original system and for open-sourcing it.
- The [Cordis](https://github.com/cordiverse/cordis) project, for the plugin paradigm this project re-implements.

## License

[MIT](LICENSE)
