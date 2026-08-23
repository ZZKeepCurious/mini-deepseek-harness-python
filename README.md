# Mini DeepSeek Harness (Python)

English | [中文](README.zh.md)

**Mini DeepSeek Harness** is an educational, from-scratch re-implementation of [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (`dsh`) — the open-source agent harness developed by [DeepSeek AI](https://deepseek.com) — written in Python (**stdlib-first**, with `httpx` for the DeepSeek SSE transport, optional `pyyaml`, and an optional `[web]` extra: `fastapi` + `uvicorn` for the HTTP/SSE transport layer).

The upstream project builds its entire system on a philosophy where **everything is a plugin**, powered by [Cordis](https://github.com/cordiverse/cordis), a dependency-injection and event-bus framework whose design is described in [_A Programming Paradigm for Spatiotemporal Composability_](https://github.com/cordiverse/paper). We deeply admire this design. This repository is our homage: instead of only reading about it, we re-implement its core contracts — the event-sourced session log, the plugin event bus, the turn/step agent loop, and the capability-seam triangle (Service Definition / Service Provider / Consumer) — preferring mature open-source libraries over hand-rolling (the required third-party packages are `httpx` for the DeepSeek SSE transport, `filelock` for credential cross-process writer locking, and `watchdog` for the Cordis HMR file watch; `pyyaml` is optional for YAML config), so anyone with `python3` can read, run, and modify them.

> **This is a learning project, not a port.** It is not affiliated with DeepSeek AI. We do not aim for feature parity or a drop-in replacement; we aim to understand and teach the ideas.

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
| Durable storage (JSONL / SQLite, nested `root/<projectDir>/<encoded-id>/session.jsonl` layout, header + `SESSION_FORMAT_VERSION=0` fail-closed, flush barrier, crash recovery) | `packages/session/session-persistence` |
| Plugin event bus (emit / waterfall / parallel / serial, scopes, dependency-driven activation, epoch reload via HMR service + `watch_user_patches`) | `vendor/cordis` + `vendor/hmr` + `core/scope` + `core/hmr` |
| Config schema engine (full schemastery port: 17 resolvers, meta clone, toString/toJSON/i18n/simplify, `~standard` protocol face) | `vendor/schemastery/src/index.ts` |
| Tool registry + execution pipeline (schema validation, pre/execute/post, timeout) | `packages/core/tools` |
| Agent loop (async-driven turn/step state machine with sync facade, pre-step rejection, tool-feedback continuation) | `core/agent-loop` |
| LLM seam (async `stream(messages, tools, signal)` contract, fake adapter, official DeepSeek SSE adapter over `httpx` async streaming, four-level `reasoning_effort`) | `llm/llm` + `llm/llm-deepseek` |
| Model request retry / backoff (normal/always policy, `agent/request-error`, `llm/retry` audit pair, event-driven cancellable wait) | `llm/llm-retry` + `llm/llm/src/retry-policy.ts` |
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
| Web transport layer + browser SPA (`--profile web`: four-quadrant RPC envelope, WebApi unary session service, mux/host SSE event streams with splice-reprojected queue snapshots and per-frame rpcId, approval bridge (`approval/requested\|resolved` mux frames + `POST /api/respond` RpcReceipt), FastAPI carrier mirroring `handler.ts` status-code chain + `/api/respond` + frontend-static contract, vanilla SPA (session list/create, Trajectory fold, approval panel, command/config, queue/jobs panel)) | `packages/host/apiproxy` + `host/frontend-static` + `host/webserver` |
| Launcher options (`--patch`, `--dump-config` / `--dump-default-config`, read-only composition dump) | `apps/cli/src/args.ts` |
| Session management CLI (`miniharness sessions` list/resume/delete; mini teaching extension) | web surface (upstream) |
| Session store service (`ctx.sessions`: create/prepare/enter/announce lifecycle, fork with 5 error codes, flush checkpoint, `session/created|disposed|event|flush` events) | `packages/core/session` (SessionStore) |
| Capability seams (sandbox backends / credential layers / subagent ACP+SDK+fork channels) | capability seams docs |
| Continuable subagents (`start_continuable`/`send_message` (with initial prompt), durable child session + cold resume, settlement delivery, async event-driven A8 (submit-and-return + watchSettlement + steer batch merge + ownership bookkeeping waiting/settled), lifecycle events `subagent/start`/`subagent/end` (runId-paired + epochStopReason/foldConsumedWork outcome folding + scoped dispatch via the delegating parent's scope carrier), named provider registry (`register_provider` → `subagent/provider-removed` edge on dispose), DRAINING admission cutoff (`drain`/`drain_descendants` + `assert_admitting`, verbatim refusal wording), interrupt authority matrix (user/ancestor authority + absent-target no-op), nested delegation (exec.agent as authorization subject, grandchild settlement notices to the direct parent), model-side delegation tool `subagent` (verbatim descriptions, canonical value + `Tool.render`, `run_in_background` routing), `send_message`/`interrupt_agent`/`list_agents` control tools) | `packages/subagent` (subagent + subagent-in-process-driver + tool-subagent-control + tool-subagent-report) |
| Presets / agent intervention / trajectory / dynamic plugins / approval | `packages/preset` + `core/agent` + `interaction` |
| Protocol entries (ACP / JSON-RPC SDK / hooks bridge) | `acp` + `sdk` + `hooks` |
| Official Python SDK interop (upstream `DeepSeekHarness` drives mini worker via `launch_args_override`; `tests/test_upstream_sdk_interop.py`, skips without pydantic/upstream sources) | `python/sdk` |
| Async event bus, true parallel tools + barrier | `core/agent-loop` |
| CI (GitHub Actions, Python 3.10~3.13, integration-tagged real-API tests) | — |

Planned: the upstream browser frontend (`packages/client`, React monorepo) is not reproduced verbatim; its wire surface is fully aligned (an upstream client pointed at the mini backend works) and a vanilla SPA (no build step) ships as the consumer.

Status: **1239 tests passing** (`httpx` for the DeepSeek SSE transport, `filelock` for credential cross-process writer locking, `watchdog` for the Cordis HMR file watch, optional `pyyaml` for YAML config, optional `[web]` extra for the HTTP/SSE transport layer). coverage 87%.

## Getting started

Requirements: Python 3.10+ (stdlib-first; `httpx` for the DeepSeek SSE transport, optional `pyyaml` for YAML config files).

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
```

### Talk to the real DeepSeek API (optional)

```sh
export DEEPSEEK_API_KEY=sk-...            # PowerShell: set DEEPSEEK_API_KEY=sk-...
python examples/real_api_demo.py
```

### Install as a CLI

```sh
pip install -e .
miniharness
```

## Project layout

```
mini-deepseek-harness-python/
├── miniharness/             # core package (stdlib-first, family layout, see docs/architecture.md)
│   ├── core/                # upstream packages/core
│   │   ├── session/         # types / json / message / invariant / repair / surface / session
│   │   │   └── persistence.py
│   │   ├── scope.py         # Context / PluginManager
│   │   ├── tools.py         # tool registry + execution pipeline
│   │   └── agent_loop/      # agent.py + tool_calls.py
│   ├── llm/                 # upstream packages/llm
│   │   ├── protocol.py      # StreamChunk / LlmAdapter / LlmFailure / BlockAssembler
│   │   ├── deepseek.py      # DeepSeek wire serialization + SSE adapter
│   │   ├── fake.py          # FakeLlmAdapter (no API key)
│   │   ├── retry_policy.py  # retry policy parsing (normal/always)
│   │   ├── retry.py         # agent/request-error recovery + backoff
│   │   └── token_meter.py   # TokenMeter incremental fold + usage anchor
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
│   ├── seams/               # sandbox / credentials / subagent seams
│   ├── web/                 # apiproxy subset: envelope / api / streams / approvals / server / frontend / launcher
│   ├── web/static/          # vanilla SPA browser frontend (index.html / app.js / style.css)
│   ├── preset/  extensions/  interaction/  client/
│   ├── demo.py              # end-to-end demo
│   └── example_plugins.py   # boot demo plugins
├── tests/                   # acceptance tests (unittest)
├── examples/                # chat & real-API demos
└── docs/
    ├── index.md            # handbook index (learning map)
    ├── architecture.md      # architecture + upstream mapping
    ├── chapters/            # 00-setup ~ 12-handbook tutorials
    └── report/              # analysis report (MkDocs Markdown, Mermaid diagrams)
```

## Acknowledgements

- [DeepSeek AI](https://deepseek.com) and the [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) team, for the original system and for open-sourcing it.
- The [Cordis](https://github.com/cordiverse/cordis) project, for the plugin paradigm this project re-implements.

## License

[MIT](LICENSE)
