# Mini DeepSeek Harness (Python)

English | [中文](README.zh.md)

**Mini DeepSeek Harness** is an educational, from-scratch re-implementation of [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (`dsh`) — the open-source agent harness developed by [DeepSeek AI](https://deepseek.com) — written in **pure Python standard library**.

The upstream project builds its entire system on a philosophy where **everything is a plugin**, powered by [Cordis](https://github.com/cordiverse/cordis), a dependency-injection and event-bus framework whose design is described in [_A Programming Paradigm for Spatiotemporal Composability_](https://github.com/cordiverse/paper). We deeply admire this design. This repository is our homage: instead of only reading about it, we re-implement its core contracts — the event-sourced session log, the plugin event bus, the turn/step agent loop, and the capability-seam triangle (Service Definition / Service Provider / Consumer) — with **zero third-party dependencies**, so anyone with `python3` can read, run, and modify them.

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
| Durable storage (JSONL / SQLite, header + `SESSION_FORMAT_VERSION=0` fail-closed, flush barrier, crash recovery) | `packages/session/session-persistence` |
| Plugin event bus (emit / waterfall / parallel / serial, scopes, dependency-driven activation) | `vendor/cordis` + `core/scope` |
| Tool registry + execution pipeline (schema validation, pre/execute/post, timeout) | `packages/core/tools` |
| Agent loop (turn/step state machine, pre-step rejection, tool-feedback continuation) | `core/agent-loop` |
| LLM seam (StreamChunk protocol, fake adapter, official DeepSeek SSE adapter) | `llm/llm` + `llm/llm-deepseek` |
| Model request retry / backoff (normal/always policy, `agent/request-error`, `llm/retry` audit pair) | `llm/llm-retry` + `llm/llm/src/retry-policy.ts` |
| Boot & composition (YAML/JSON overlays, `!!js` env interpolation, startup assertions) | `packages/boot` |
| Headless one-shot entry (`--profile headless "task"`: stdout final text, exit code by turn/end reason) | `packages/bundle/headless` + `apps/cli` |
| Launcher options (`--patch`, `--dump-config` / `--dump-default-config`, read-only composition dump) | `apps/cli/src/args.ts` |
| Session management CLI (`miniharness sessions` list/resume/delete; mini teaching extension) | web surface (upstream) |
| Capability seams (sandbox backends / credential layers / subagent ACP+SDK+fork channels) | capability seams docs |
| Presets / agent intervention / trajectory / dynamic plugins / approval | `packages/preset` + `core/agent` + `interaction` |
| Protocol entries (ACP / JSON-RPC SDK / hooks bridge) | `acp` + `sdk` + `hooks` |
| Async event bus, true parallel tools + barrier | `core/agent-loop` |
| CI (GitHub Actions, Python 3.10~3.13, integration-tagged real-API tests) | — |

Planned: official Python SDK (`python/sdk`) interop tests.

Status: **413 unit tests passing** (stdlib only; optional `pyyaml` for YAML config).

## Getting started

Requirements: Python 3.10+, standard library only (optional `pyyaml` for YAML config files).

```sh
# run all tests
python -m unittest discover -s tests -t .

# end-to-end demo (fake model + tools + crash recovery, no API key needed)
python -m miniharness.demo

# multi-turn chat with the fake model
python examples/chat_demo.py

# one-shot task, like `dsh --profile headless "task"` (needs DEEPSEEK_API_KEY)
python -m miniharness.cli --profile headless "run the tests"

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
├── miniharness/             # core package (stdlib only, family layout, see docs/architecture.md)
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
│   │   └── retry.py         # agent/request-error recovery + backoff
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
