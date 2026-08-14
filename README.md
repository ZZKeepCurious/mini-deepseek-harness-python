# Mini DeepSeek Harness (Python)

English | [中文](README.zh.md)

**Mini DeepSeek Harness** is an educational, from-scratch re-implementation of [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (`dsh`) — the open-source agent harness developed by [DeepSeek AI](https://deepseek.com) — written in **pure Python standard library**.

The upstream project builds its entire system on a philosophy where **everything is a plugin**, powered by [Cordis](https://github.com/cordiverse/cordis), a dependency-injection and event-bus framework whose design is described in [_A Programming Paradigm for Spatiotemporal Composability_](https://github.com/cordiverse/paper). We deeply admire this design. This repository is our homage: instead of only reading about it, we re-implement its core contracts — the event-sourced session log, the plugin event bus, the turn/step agent loop, and the capability-seam triangle (Service Definition / Service Provider / Consumer) — with **zero third-party dependencies**, so anyone with `python3` can read, run, and modify them.

> **This is a learning project, not a port.** It is not affiliated with DeepSeek AI. We do not aim for feature parity or a drop-in replacement; we aim to understand and teach the ideas.

## Documentation

Two complementary documents (both in Chinese):

- **[Analysis report](docs/report/DEEPSEEK-HARNESS-DEEP-LEARNING-GUIDE.html)** — a deep dive into the upstream repository: five-layer architecture, the `ctx` service map, core techniques, and key processing flows, fully illustrated with Mermaid diagrams.
- **[Step-by-step handbook](docs/chapters/)** — how the system grows from zero, one chapter at a time: concepts → minimal runnable code → invariants/tests → checkpoint exercises.

See [ROADMAP.md](ROADMAP.md) for where this project is heading.

## What's inside

| Capability | Status | Upstream counterpart |
|---|---|---|
| Event-sourced session (seq, deep-freeze, `derive_messages`, interrupted repair) | done | `packages/core/session` |
| Durable storage (JSONL / SQLite, flush barrier, fail-closed load, crash recovery) | done | `packages/session/session-persistence` |
| Plugin event bus (emit / waterfall / parallel / serial, scopes, dependency-driven activation) | done | `vendor/cordis` + `core/scope` |
| Tool registry + execution pipeline (schema validation, pre/execute/post, timeout) | done | `packages/core/tools` |
| Agent loop (turn/step state machine, pre-step rejection, tool-feedback continuation) | done | `core/agent-loop` |
| LLM seam (StreamChunk protocol, fake adapter, official DeepSeek SSE adapter) | done | `llm/llm` + `llm/llm-deepseek` |
| Boot & composition (`apply_patch` overlays, startup assertions) | done | `packages/boot` |
| Capability seams basics (sandbox / credentials / subagent) | partial | capability seams docs |
| Async event bus, true parallel tools + barrier | planned | `core/agent-loop` |
| CLI, YAML config, official SDK interop | planned | `apps/dsh`, `python/` |

Status: **62 unit tests passing** (stdlib only).

## Getting started

Requirements: Python 3.10+, no third-party packages.

```sh
# run all tests
python -m unittest discover -s tests -t .

# end-to-end demo (fake model + tools + crash recovery, no API key needed)
python -m miniharness.demo

# multi-turn chat with the fake model
python examples/chat_demo.py
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
├── miniharness/          # core package (stdlib only)
│   ├── session.py        # event-sourced session, projection, invariants
│   ├── bus.py            # Context registry / event bus / scopes / plugin activation
│   ├── tools.py          # tool registry + execution pipeline
│   ├── llm.py            # StreamChunk protocol + fake / DeepSeek adapters
│   ├── loop.py           # agent loop state machine
│   ├── persistence.py    # JSONL / SQLite backends + crash recovery
│   ├── boot.py           # startup + patch overlays
│   ├── seams.py          # sandbox / credentials / subagent seams
│   └── demo.py           # end-to-end demo
├── tests/                # 62 acceptance tests (unittest)
├── examples/             # chat & real-API demos
└── docs/
    ├── README.md         # handbook index (learning map)
    ├── chapters/         # 00-setup ~ 06-advanced-seams tutorials
    └── report/           # analysis report (HTML, Mermaid diagrams)
```

## Acknowledgements

- [DeepSeek AI](https://deepseek.com) and the [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) team, for the original system and for open-sourcing it.
- The [Cordis](https://github.com/cordiverse/cordis) project, for the plugin paradigm this project re-implements.

## License

[MIT](LICENSE)
