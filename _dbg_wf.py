import tempfile

from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.core.scope import Context
from miniharness.core.session import Session
from miniharness.core.session.persistence import JsonlPersistence
from miniharness.core.session_store import install_sessions
from miniharness.core.tools import ToolRegistry
from miniharness.llm import FakeLlmAdapter
from miniharness.ptc_runtime import install_ptc_runtime
from miniharness.seams.subagent import SubagentContinuationManager
from miniharness.workflow_ptc import install_workflow_engine

ctx = Context()
install_sessions(ctx)
reg = ToolRegistry(ctx)
parent = AgentLoop(Session("parent"), FakeLlmAdapter(final_text="父响应"), reg, ctx,
                   system_prompt="你是父代理。")
tmp = tempfile.TemporaryDirectory()
persistence = JsonlPersistence(tmp.name)
manager = SubagentContinuationManager(parent, persistence)
install_ptc_runtime(ctx)
ctx.provide("subagents", manager)
engine = install_workflow_engine(ctx, {"provider": "spawn"})
print("engine:", engine)
print("subagents on ctx:", ctx.get("subagents") is not None)
print("ptcRuntime on ctx:", ctx.get("ptcRuntime") is not None)
events = []
ctx.on("workflow/start", lambda info: events.append(info))
run = engine.start({"meta": {"name": "audit", "description": "d"},
                    "script": "return {'ok': True}", "parent": parent})
print("run:", run)
print("events:", events)
print("run id:", run.id)
print("run meta:", run.meta)
ctx.dispose()