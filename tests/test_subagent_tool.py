"""模型侧 subagent 委托工具验收（P0-4；对齐 packages/subagent/tool-subagent/src/index.ts）。

覆盖 dsh-v0.2.1-rc.2 起的激活契约：
  * schema/描述：cwd + description + prompt（必填 description/prompt）；不再有
    run_in_background / background_mode；描述后缀取 prepareContinuable 分支
  * canonical 输出恒为 {kind:'activation', subagentId}；render `started subagent {id}`
  * 配置逐字拒绝 persona/toolFilter（改由 Agent Preset 承担）
  * cwd 进子会话 header；maxDepth 由 manager 强制；委派深度上下文注册
  * 模型选择（tool-subagent model-selection.ts + list-models.ts）：schema 参数、
    逐字错误、策略路由拦截、preflight 时机、list_subagent_models 编目
  * 常驻提示节逐字注册

运行：python -m unittest tests.test_subagent_tool -v
"""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.core.scope import Context
from miniharness.core.session import Session
from miniharness.core.session.persistence import JsonlPersistence
from miniharness.core.session_store import install_sessions
from miniharness.core.system_prompt import SYSTEM_PROMPT_SERVICE, SystemPromptService
from miniharness.core.tools import Tool, ToolExec, ToolRegistry
from miniharness.llm import FakeLlmAdapter, StreamChunk
from miniharness.preset.presets import PresetRoster
from miniharness.preset.registry import AgentPresetRegistry, PresetDefinition
from miniharness.seams.subagent import (
    SubagentContinuationManager,
    SubagentError,
    fold_subagent_descriptor,
    install_subagent_delegation_tool,
)
from miniharness.seams.subagent import tool as delegation_tool


def _parent_loop(session_id="parent", adapter=None):
    ctx = Context()
    install_sessions(ctx)
    reg = ToolRegistry(ctx)
    loop = AgentLoop(Session(session_id), adapter or FakeLlmAdapter(final_text="父响应"),
                     reg, ctx, system_prompt="你是父代理。")
    return loop, ctx, reg


def _manager(parent, persistence, **kwargs):
    kwargs.setdefault("adapter_factory",
                      lambda p, m, e=None: FakeLlmAdapter(final_text="子响应"))
    return SubagentContinuationManager(parent, persistence, **kwargs)


def _execute(tool, args, agent=None):
    return asyncio.run(tool.execute(dict(args), ToolExec(agent=agent)))


def _wait_until(predicate, timeout=5.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("condition not met within timeout")
        time.sleep(interval)


def _all_event_text(events) -> str:
    return "\n".join(str(e.get("data")) for e in events)


def _notices(parent_session, kind="subagent-settled"):
    return [e for e in parent_session.events
            if e["type"] == "user/message"
            and e["data"].get("source", {}).get("kind") == kind]


class _DelegatingParent(FakeLlmAdapter):
    """父脚本：首次调用发 subagent 工具调用（固定参数），之后固定文本。"""

    def __init__(self, tool_args):
        super().__init__()
        self.calls = 0
        self.tool_args = dict(tool_args)

    async def stream(self, messages, tools, signal=None, session_id=None, purpose=None):
        self.calls += 1
        if self.calls == 1:
            arguments = json.dumps(self.tool_args, ensure_ascii=False)
            yield StreamChunk("block-start", index=0, blockType="tool-call")
            yield StreamChunk("tool-call-delta", index=0, id="call_0",
                              name="subagent", argumentsDelta=arguments)
            yield StreamChunk("block-end", index=0, block={
                "type": "tool-call", "id": "call_0", "name": "subagent",
                "arguments": arguments,
            })
            yield StreamChunk("finish", reason={"kind": "tool-calls"})
        else:
            yield StreamChunk("block-start", index=0, blockType="text")
            yield StreamChunk("text-delta", index=0, text="父响应")
            yield StreamChunk("block-end", index=0, block={"type": "text", "text": "父响应"})
            yield StreamChunk("finish", reason={"kind": "stop"})


class TestSchemaAndContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.persistence = JsonlPersistence(self.tmp.name)
        self.parent, self.ctx, self.reg = _parent_loop()

    def _tool(self, **config):
        mgr = SubagentContinuationManager(self.parent, self.persistence)
        return install_subagent_delegation_tool(self.ctx, self.reg, mgr, config or None)

    def test_managed_description_and_params_verbatim(self):
        tool = self._tool()
        self.assertEqual(tool.description,
                         delegation_tool._DESCRIPTION + delegation_tool._SUFFIX_MANAGED)
        props = tool.parameters["properties"]
        self.assertEqual(props["cwd"]["description"], delegation_tool._PARAM_DESC_CWD)
        self.assertEqual(props["description"]["type"], "string")
        self.assertEqual(props["prompt"]["type"], "string")
        self.assertNotIn("run_in_background", props)
        self.assertNotIn("background_mode", props)
        self.assertEqual(tool.parameters["required"], ["description", "prompt"])

    def test_persona_config_rejected_verbatim(self):
        with self.assertRaises(SubagentError) as cm:
            self._tool(persona="你是子代理")
        self.assertEqual(
            str(cm.exception),
            "tool-subagent: persona is no longer supported; configure persona and tool plugins "
            "in an Agent Preset and select it with agentPreset (spawn only)")

    def test_tool_filter_config_rejected_verbatim(self):
        with self.assertRaises(SubagentError) as cm:
            self._tool(tool_filter=["report"])
        self.assertEqual(
            str(cm.exception),
            "tool-subagent: toolFilter is no longer supported; configure persona and tool plugins "
            "in an Agent Preset and select it with agentPreset (spawn only)")

    def test_unknown_provider_rejected(self):
        with self.assertRaises(SubagentError) as cm:
            self._tool(provider="acp")
        self.assertEqual(cm.exception.code, "UNAVAILABLE")

    def test_requires_calling_agent(self):
        tool = self._tool()
        with self.assertRaises(RuntimeError) as cm:
            _execute(tool, {"description": "研", "prompt": "x"}, None)
        self.assertEqual(str(cm.exception),
                         "subagent tool requires a calling agent (exec.agent was undefined)")

    def test_activation_output_and_render(self):
        tool = self._tool()
        value = _execute(tool, {"description": "研", "prompt": "查资料"}, self.parent)
        self.assertEqual(value["kind"], "activation")
        cid = value["subagentId"]
        rendered = tool.render({"description": "研", "prompt": "查资料"}, value)
        self.assertEqual(rendered, [{"type": "text", "text": f"started subagent {cid}"}])

    def test_cwd_param_sets_child_header(self):
        tool = self._tool()
        value = _execute(tool, {"description": "研", "prompt": "x", "cwd": self.tmp.name},
                         self.parent)
        meta = self.persistence.inspect(value["subagentId"])["meta"]
        self.assertEqual(meta["cwd"], self.tmp.name)

    def test_max_depth_propagates(self):
        parent, ctx, reg = _parent_loop()
        mgr = _manager(parent, self.persistence, max_depth=0)
        tool = install_subagent_delegation_tool(ctx, reg, mgr)
        with self.assertRaises(SubagentError) as cm:
            _execute(tool, {"description": "研", "prompt": "x"}, parent)
        self.assertEqual(cm.exception.code, "MAX_DEPTH_EXCEEDED")


class TestModelSelection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.persistence = JsonlPersistence(self.tmp.name)
        self.parent, self.ctx, self.reg = _parent_loop()

    def _tool(self, **config):
        mgr = SubagentContinuationManager(self.parent, self.persistence)
        return install_subagent_delegation_tool(self.ctx, self.reg, mgr, config or None)

    def test_model_selection_schema_verbatim(self):
        # enabled：provider/model/reasoning_effort 三参数 + choiceDescription 后缀
        # （index.ts:376-412；route 无默认值走 inherit-parent 分支）
        tool = self._tool(model_selection=True,
                          model_routes=[{"provider": "fake", "model": "chat-a"}])
        self.assertEqual(
            tool.description,
            delegation_tool._DESCRIPTION + delegation_tool._SUFFIX_MANAGED
            + delegation_tool._CHOICE_DESCRIPTION)
        props = tool.parameters["properties"]
        self.assertEqual(props["provider"]["type"], "string")
        self.assertEqual(props["model"]["type"], "string")
        self.assertEqual(props["reasoning_effort"]["type"], "string")
        self.assertEqual(props["provider"]["description"],
                         delegation_tool._PARAM_DESC_PROVIDER)
        self.assertEqual(props["model"]["description"],
                         delegation_tool._PARAM_DESC_MODEL)
        self.assertEqual(props["reasoning_effort"]["description"],
                         delegation_tool._PARAM_DESC_REASONING_EFFORT)

    def test_model_selection_disabled_rejects_verbatim(self):
        tool = self._tool()
        with self.assertRaises(RuntimeError) as cm:
            _execute(tool, {"description": "研", "prompt": "x",
                            "provider": "fake"}, self.parent)
        self.assertEqual(str(cm.exception),
                         "child model selection is disabled for this tool instance")

    def test_model_selection_requires_routes_and_validates(self):
        with self.assertRaises(RuntimeError) as cm:
            self._tool(model_selection=True, model_routes=[])
        self.assertEqual(
            str(cm.exception),
            "enabled subagent model selection requires at least one allowed model")
        merger = delegation_tool
        self.assertEqual(merger._validate_model_routes(
            [{"provider": "p", "model": "m"}]),
            [{"provider": "p", "model": "m"}])
        with self.assertRaises(RuntimeError) as cm:
            merger._validate_model_routes("nope")
        self.assertEqual(str(cm.exception),
                         "subagent model selection requires an array of routes")
        for bad in ([{"provider": "p"}], [{"model": "m"}],
                    [{"provider": "", "model": "m"}], [{"provider": "p", "model": ""}],
                    [{"provider": 7, "model": "m"}]):
            with self.assertRaises(RuntimeError) as cm:
                merger._validate_model_routes(bad)
            self.assertEqual(
                str(cm.exception),
                "subagent model selection requires non-empty provider and model ids")
        with self.assertRaises(RuntimeError) as cm:
            merger._validate_model_routes(
                [{"provider": "p", "model": "m"}, {"provider": "p", "model": "m"}])
        self.assertEqual(str(cm.exception),
                         'subagent model selection repeats route "p/m"')

    def test_model_selection_route_not_allowed_verbatim(self):
        tool = self._tool(model_selection=True,
                          model_routes=[{"provider": "fake", "model": "chat-a"}])
        with self.assertRaises(RuntimeError) as cm:
            _execute(tool, {"description": "研", "prompt": "x",
                            "provider": "fake", "model": "nope"}, self.parent)
        self.assertEqual(
            str(cm.exception),
            'child LLM route "fake/nope" is not allowed for this Session')

    def test_model_selection_preflight_unresolvable_route(self):
        # 策略允许但 provider 未注册 → preflight 在子创建前抛 UNAVAILABLE
        tool = self._tool(model_selection=True,
                          model_routes=[{"provider": "nope", "model": "x"}])
        with self.assertRaises(SubagentError) as cm:
            _execute(tool, {"description": "研", "prompt": "x",
                            "provider": "nope", "model": "x"}, self.parent)
        self.assertEqual(cm.exception.code, "UNAVAILABLE")

    def test_model_selection_threads_route_to_descriptor(self):
        # 选择结果进 child descriptor（agentProvider/agentModel/agentReasoningEffort）
        tool = self._tool(model_selection=True,
                          model_routes=[{"provider": "fake", "model": "chat-a"}])
        value = _execute(tool, {"description": "研", "prompt": "x",
                                "provider": "fake", "model": "chat-a",
                                "reasoning_effort": "low"}, self.parent)
        self.assertEqual(value["kind"], "activation")
        descriptor = fold_subagent_descriptor(
            self.persistence.inspect(value["subagentId"])["events"])
        self.assertEqual(descriptor["agentProvider"], "fake")
        self.assertEqual(descriptor["agentModel"], "chat-a")
        self.assertEqual(descriptor["agentReasoningEffort"], "low")

    def test_requested_agent_options_merge_semantics(self):
        # 换路由未带 effort → 丢弃配置里路由属地的 effort（所选模型用默认档）
        requested = delegation_tool._requested_agent_options(
            self.parent,
            {"provider": "fake", "model": "chat-a", "reasoningEffort": "high"},
            {"provider": "fake", "model": "chat-b"}, enabled=True)
        self.assertEqual(requested, {"provider": "fake", "model": "chat-b"})
        # 纯 effort 覆盖保留配置路由
        kept = delegation_tool._requested_agent_options(
            self.parent,
            {"provider": "fake", "model": "chat-a", "reasoningEffort": "high"},
            {"reasoning_effort": "low"}, enabled=True)
        self.assertEqual(kept, {"provider": "fake", "model": "chat-a",
                                "reasoningEffort": "low"})
        # 空串按非空校验拒绝
        with self.assertRaises(RuntimeError) as cm:
            delegation_tool._requested_agent_options(
                self.parent, None, {"provider": ""}, enabled=True)
        self.assertEqual(str(cm.exception), "child LLM `provider` must be non-empty")
        with self.assertRaises(RuntimeError) as cm:
            delegation_tool._requested_agent_options(
                self.parent, None, {"provider": "fake"}, enabled=True)
        self.assertEqual(
            str(cm.exception),
            "child LLM `provider` and `model` must be supplied together")

    def test_list_subagent_models_policy_catalog(self):
        # 策略 routes 即 mini 编目（无上游 llm catalog 的教学简化）：
        # 无参列出 registered providers，provider 列出模型，精确路由查 efforts
        self._tool(
            model_selection=True,
            model_routes=[{"provider": "fake", "model": "chat-a"},
                          {"provider": "fake", "model": "chat-b"}],
        )
        listing = self.reg.resolve("list_subagent_models")
        self.assertIsNotNone(listing)
        out = _execute(listing, {}, self.parent)
        self.assertEqual(out, "fake — fake")
        out = _execute(listing, {"provider": "fake"}, self.parent)
        self.assertIn("fake/chat-a — chat-a", out)
        self.assertIn("fake/chat-b — chat-b", out)
        out = _execute(listing, {"provider": "fake", "model": "chat-a"}, self.parent)
        self.assertEqual(
            out, "fake/chat-a — chat-a\nReasoning efforts:\n(no advertised reasoning efforts)")
        with self.assertRaises(RuntimeError) as cm:
            _execute(listing, {"model": "chat-a"}, self.parent)
        self.assertEqual(str(cm.exception), "`model` requires `provider`")
        with self.assertRaises(RuntimeError) as cm:
            _execute(listing, {"provider": "other"}, self.parent)
        self.assertEqual(
            str(cm.exception),
            'LLM provider "other" is not allowed for this Session')
        with self.assertRaises(RuntimeError) as cm:
            _execute(listing, {"provider": "fake", "model": "other"}, self.parent)
        self.assertEqual(
            str(cm.exception),
            'child LLM route "fake/other" is not allowed for this Session')


class TestActivationDelegation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.persistence = JsonlPersistence(self.tmp.name)

    def _scripted(self, args, config=None, **manager_kwargs):
        parent, ctx, reg = _parent_loop(adapter=_DelegatingParent(args))
        mgr = _manager(parent, self.persistence, **manager_kwargs)
        install_subagent_delegation_tool(ctx, reg, mgr, config)
        return parent, ctx, mgr

    def test_activation_returns_child_id_and_settles_notice(self):
        # 每次委托 = 独立激活：工具回子 id（render），子回合跑完并投递结算通知；
        # 不再依赖 jobs 服务，也无前台/后台之分
        parent, _, mgr = self._scripted({"description": "研", "prompt": "查资料"})
        parent.run("委派任务")
        cid = mgr.list_children()[0]["id"]
        self.assertIn(f"started subagent {cid}", _all_event_text(parent.session.events))
        events = self.persistence.inspect(cid)["events"]
        self.assertIn("subagent/descriptor", [e["type"] for e in events])
        self.assertEqual(events[-1]["type"], "turn/end")
        self.assertEqual(events[-1]["data"]["reason"]["kind"], "completed")
        notices = _notices(parent.session)
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["data"]["source"]["senderSessionId"], cid)
        self.assertEqual(parent.last_response(), "父响应")

    def test_system_prompt_section_registered_verbatim(self):
        parent, ctx, reg = _parent_loop()
        svc = SystemPromptService(ctx)
        ctx.provide(SYSTEM_PROMPT_SERVICE, svc)
        mgr = _manager(parent, self.persistence)
        install_subagent_delegation_tool(ctx, reg, mgr)
        rendered = {s["name"]: s["text"] for s in svc.render({})}
        self.assertIn("tool:subagent", rendered)
        self.assertEqual(
            rendered["tool:subagent"],
            "Start independent delegations with `subagent` together in one assistant message and "
            "continue useful work while they run.")

    def test_delegation_depth_context_registered(self):
        # max_depth 解析非 None → 注册 tool:subagent:depth 上下文
        parent, ctx, reg = _parent_loop()
        svc = SystemPromptService(ctx)
        ctx.provide(SYSTEM_PROMPT_SERVICE, svc)
        mgr = _manager(parent, self.persistence)
        install_subagent_delegation_tool(ctx, reg, mgr)
        self.assertIn("tool:subagent:depth", [c["name"] for c in svc._contexts])


class _RecordingAdapter(FakeLlmAdapter):
    """子适配器：记录收到的工具名后走普通假模型回合（P0-4a 组合断言用）。"""

    def __init__(self, sink: dict, **kwargs):
        super().__init__(**kwargs)
        self._sink = sink

    async def stream(self, messages, tools, signal=None, session_id=None, purpose=None):
        self._sink["tools"] = [t["name"] if isinstance(t, dict) else getattr(t, "name", t)
                               for t in tools]
        self._sink["messages"] = messages
        async for chunk in super().stream(messages, tools, signal=signal,
                                          session_id=session_id, purpose=purpose):
            yield chunk


def _write_preset(root: Path, preset_id: str, manifest: dict) -> None:
    directory = root / preset_id
    directory.mkdir(parents=True)
    (directory / "preset.json").write_text(json.dumps(manifest), encoding="utf-8")


class TestPresetSelection(unittest.TestCase):
    """P0-4a：subagent `agent_preset` 选择 + 子组合挂载（对齐 preset-selection.ts）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.persistence = JsonlPersistence(self.tmp.name)
        self.parent, self.ctx, self.reg = _parent_loop()
        root = Path(self.tmp.name) / "presets"
        _write_preset(root, "writer", {
            "id": "writer", "name": "Writer", "order": 1, "tools": ["bash"],
            "persona": {"system_prompt": "You write."},
        })
        _write_preset(root, "reviewer", {
            "id": "reviewer", "name": "Reviewer", "order": 2, "tools": [],
            "persona": {"system_prompt": "You review.", "suffix": "End."},
        })
        self.roster = PresetRoster(root)
        self.registry = AgentPresetRegistry(self.ctx, roster=self.roster)
        self.ctx.provide("agentPresets", self.registry)
        self.reg.register(Tool(name="bash", description="b",
                               execute=lambda args, e: "ok"))
        self.reg.register(Tool(name="ls", description="l",
                               execute=lambda args, e: "ok"))

    def _tool(self, **config):
        mgr = SubagentContinuationManager(self.parent, self.persistence)
        return install_subagent_delegation_tool(self.ctx, self.reg, mgr, config or None)

    def _fresh(self):
        parent, ctx, reg = _parent_loop()
        ctx.provide("agentPresets", AgentPresetRegistry(ctx, roster=self.roster))
        reg.register(Tool(name="bash", description="b", execute=lambda a, e: "ok"))
        reg.register(Tool(name="ls", description="l", execute=lambda a, e: "ok"))
        mgr = SubagentContinuationManager(parent, self.persistence)
        return ctx, reg, mgr

    def test_registry_resolves_live_composition(self):
        comp = self.registry.resolve_composition("writer")
        self.assertEqual(comp["id"], "writer")
        self.assertEqual(comp["tools"], ["bash"])
        self.assertEqual(comp["persona"].system_prompt, "You write.")
        self.assertIsNone(self.registry.resolve_composition("nope"))

    def test_param_exposed_only_with_allowlist(self):
        ctx, reg, mgr = self._fresh()
        tool = install_subagent_delegation_tool(ctx, reg, mgr, {"agent_preset": "writer"})
        self.assertNotIn("agent_preset", tool.parameters["properties"])
        ctx, reg, mgr = self._fresh()
        tool = install_subagent_delegation_tool(
            ctx, reg, mgr,
            {"allowed_agent_presets": ["writer", "reviewer"], "agent_preset": "writer"})
        prop = tool.parameters["properties"]["agent_preset"]
        self.assertEqual(prop["enum"], ["writer", "reviewer"])
        self.assertIn("Available presets: writer, reviewer", prop["description"])

    def test_config_validation(self):
        for config in (
            {"allowed_agent_presets": []},
            {"allowed_agent_presets": ["writer", "writer"]},
            {"allowed_agent_presets": [""]},
            {"agent_preset": "writer", "allowed_agent_presets": ["reviewer"]},
            {"agent_preset": ""},
        ):
            with self.assertRaises(RuntimeError):
                self._tool(**config)

    def test_configured_requires_agent_presets_service(self):
        bare = Context()
        install_sessions(bare)
        reg = ToolRegistry(bare)
        parent = AgentLoop(Session("p"), FakeLlmAdapter(), reg, bare)
        mgr = SubagentContinuationManager(parent, self.persistence)
        with self.assertRaises(RuntimeError) as caught:
            install_subagent_delegation_tool(bare, reg, mgr,
                                             {"allowed_agent_presets": ["writer"]})
        self.assertIn("requires the agent-presets service", str(caught.exception))

    def test_resolve_default_allowlist_and_disabled(self):
        selection = delegation_tool._SubagentPresetSelection(
            {"agent_preset": "writer", "allowed_agent_presets": ["writer", "reviewer"]})
        self.assertEqual(selection.resolve({}), "writer")
        self.assertEqual(selection.resolve({"agent_preset": "reviewer"}), "reviewer")
        with self.assertRaises(RuntimeError):
            selection.resolve({"agent_preset": "nope"})
        disabled = delegation_tool._SubagentPresetSelection({})
        self.assertIsNone(disabled.resolve({}))
        with self.assertRaises(RuntimeError):
            disabled.resolve({"agent_preset": "writer"})

    def test_delegation_mounts_preset_tools_and_persona(self):
        sink: dict = {}
        adapter = _RecordingAdapter(sink, final_text="子响应")
        parent, ctx, reg = _parent_loop(
            adapter=_DelegatingParent({"description": "研", "prompt": "查资料",
                                       "agent_preset": "writer"}))
        registry = AgentPresetRegistry(ctx, roster=self.roster)
        ctx.provide("agentPresets", registry)
        reg.register(Tool(name="bash", description="b", execute=lambda a, e: "ok"))
        reg.register(Tool(name="ls", description="l", execute=lambda a, e: "ok"))
        mgr = SubagentContinuationManager(parent, self.persistence,
                                          adapter_factory=lambda p, m, e=None: adapter)
        install_subagent_delegation_tool(ctx, reg, mgr,
                                         {"allowed_agent_presets": ["writer"]})
        parent.run("委派任务")
        cid = mgr.list_children()[0]["id"]
        # 独立组合：子工具 = preset 工具 + report；父的 ls 不继承
        self.assertIn("bash", sink["tools"])
        self.assertIn("report", sink["tools"])
        self.assertNotIn("ls", sink["tools"])
        meta = self.persistence.inspect(cid)["meta"]
        self.assertEqual(meta["agentPreset"], "writer")
        events = self.persistence.inspect(cid)["events"]
        selected = [e for e in events if e["type"] == "agent-preset/selected"]
        self.assertEqual([e["data"]["agentPreset"] for e in selected], ["writer"])
        self.assertIn("You write.", str(sink["messages"]))

    def test_delegation_unknown_preset_refused(self):
        parent, ctx, reg = _parent_loop(
            adapter=_DelegatingParent({"description": "研", "prompt": "x",
                                       "agent_preset": "writer"}))
        registry = AgentPresetRegistry(ctx, roster=PresetRoster(Path(self.tmp.name) / "empty"))
        ctx.provide("agentPresets", registry)
        mgr = SubagentContinuationManager(parent, self.persistence)
        install_subagent_delegation_tool(ctx, reg, mgr,
                                         {"allowed_agent_presets": ["writer"]})
        parent.run("委派任务")
        self.assertIn('unknown agent preset "writer"', str(parent.session.events))


if __name__ == "__main__":
    unittest.main()
