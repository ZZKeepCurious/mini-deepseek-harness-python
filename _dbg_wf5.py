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
# simple body: inspect args and limits via a probe
script = (
    "probe = await workflowHost.begin()\n"
    "print('LIMITS', probe.get('limits'))\n"
    "return {'ok': True}\n"
)
run = engine.start({"meta": {"name": "c", "description": "d"},
                    "script": script, "parent": parent})
result = run.result()
print("result:", result)
ctx.dispose()