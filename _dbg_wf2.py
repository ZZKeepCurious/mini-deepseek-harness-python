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
parent = AgentLoop(Session("parent"), FakeLlmAdapter(final_text="x"), reg, ctx,
                   system_prompt="p")
tmp = tempfile.TemporaryDirectory()
persistence = JsonlPersistence(tmp.name)
manager = SubagentContinuationManager(parent, persistence)
install_ptc_runtime(ctx)
ctx.provide("subagents", manager)
engine = install_workflow_engine(ctx, {"provider": "spawn"})
print("engine.ctx is ctx:", engine.ctx is ctx)
print("engine name:", engine.name)
events = []
ctx.on("workflow/start", lambda info: events.append(info))
run = engine.start({"meta": {"name": "audit", "description": "d"},
                    "script": "return {'ok': True}", "parent": parent})
print("events after start:", events)
try:
    engine.emit_workflow_event("workflow/start", run._run_info())
    print("direct emit returned ok, events:", events)
except Exception as e:
    print("direct emit raised:", type(e), e)
engine.ctx.emit("workflow/start", {"via-engine-ctx": 1})
print("events after engine.ctx.emit:", events)
engine.emit_workflow_event("workflow/start", {"plain": 2})
print("events after plain emit_workflow_event:", events)
print("method:", engine.emit_workflow_event.__func__ if hasattr(engine.emit_workflow_event, '__func__') else engine.emit_workflow_event)
ctx.dispose()