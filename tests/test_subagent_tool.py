"""模型侧 subagent 委托工具验收（P1-1；对齐 packages/subagent/tool-subagent/src/index.ts）。

覆盖：
  * schema/描述逐字三形态（continuable / one-shot / disabled 实例整个省略参数）
  * resolveDelegationRun 路由：缺省随 backgroundMode；禁用实例强制后台逐字拒绝；
    continuable 显式前台等待首回合（jobs.spec.ts:1115 同款语义）
  * 前台内联泵收结果（render = 子输出文本）+ maxDepth + persona/toolFilter 透传
  * 后台 canonical ack：continuable → 持久 subagentId / one-shot → jobId；
    job 按 stopReason 结算；jobs 缺失逐字报错
  * continuable 常驻提示节注册（systemPrompt 服务；one-shot 不注册）
  * kill → interrupt 子代理 → 子回合 aborted → job killed
  * 模型选择（rs.1 增量，model-selection.ts + list-models.ts）：schema 参数三
    形态、逐字错误、策略路由拦截、preflight 时机、list_subagent_models 编目

运行：python -m unittest tests.test_subagent_tool -v
"""
import asyncio
import json
import re
import tempfile
import threading
import time
import unittest

from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.core.scope import Context
from miniharness.core.session import Session
from miniharness.core.session.persistence import JsonlPersistence
from miniharness.core.session_store import install_sessions
from miniharness.core.system_prompt import SYSTEM_PROMPT_SERVICE, SystemPromptService
from miniharness.core.tools import ToolExec, ToolRegistry
from miniharness.jobs import install_jobs
from miniharness.llm import FakeLlmAdapter, StreamChunk
from miniharness.llm.protocol import LlmFailure, StreamAborted
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


def _signalled(signal) -> bool:
    """双形状取消判读（镜像 jobs/registry._signal_aborted）。"""
    if signal is None:
        return False
    aborted = getattr(signal, "aborted", None)
    if callable(aborted):
        try:
            return bool(aborted())
        except Exception:
            return False
    ev = getattr(signal, "signal", signal)
    is_set = getattr(ev, "is_set", None)
    return bool(is_set()) if callable(is_set) else False


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

    async def stream(self, messages, tools, signal=None):
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


class _GatedChild(FakeLlmAdapter):
    """stream 阻塞在 gate 上（模拟长任务）；等待期间轮询取消信号，一经置位即中止。

    取消判读双保险：stream 收到的 signal 参数 + 测试注入的 is_cancelled 回调
    （扫 manager 激活物的 interrupted 标志，interrupt() 同步置位，确定性）。
    """

    def __init__(self):
        super().__init__()
        self.gate = threading.Event()
        self.reached = threading.Event()
        self.is_cancelled = lambda: False

    async def stream(self, messages, tools, signal=None):
        self.reached.set()
        while True:
            if _signalled(signal) or self.is_cancelled():
                raise StreamAborted("cancelled while gated")
            if self.gate.wait(0.05):
                break
        yield StreamChunk("block-start", index=0, blockType="text")
        yield StreamChunk("text-delta", index=0, text="子响应")
        yield StreamChunk("block-end", index=0, block={"type": "text", "text": "子响应"})
        yield StreamChunk("finish", reason={"kind": "stop"})


class TestSchemaAndRouting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.persistence = JsonlPersistence(self.tmp.name)
        self.parent, self.ctx, self.reg = _parent_loop()

    def _tool(self, **config):
        mgr = SubagentContinuationManager(self.parent, self.persistence)
        return install_subagent_delegation_tool(self.ctx, self.reg, mgr, config or None)

    def test_continuable_description_and_param_verbatim(self):
        tool = self._tool(background_mode="continuable")
        self.assertEqual(tool.description,
                         delegation_tool._DESCRIPTION + delegation_tool._SUFFIX_CONTINUABLE)
        prop = tool.parameters["properties"]["run_in_background"]
        self.assertEqual(prop["description"], delegation_tool._PARAM_DESC_CONTINUABLE)
        self.assertEqual(tool.parameters["required"], ["description", "prompt"])

    def test_one_shot_default_description_verbatim(self):
        tool = self._tool()
        self.assertEqual(tool.description,
                         delegation_tool._DESCRIPTION + delegation_tool._SUFFIX_ONE_SHOT)
        self.assertEqual(tool.parameters["properties"]["run_in_background"]["description"],
                         delegation_tool._PARAM_DESC_ONE_SHOT)

    def test_disabled_omits_run_in_background(self):
        # schema 整个省略该参数（index.ts:229），描述取 disabled 后缀
        tool = self._tool(enable_run_in_background=False)
        self.assertEqual(tool.description,
                         delegation_tool._DESCRIPTION + delegation_tool._SUFFIX_DISABLED)
        self.assertNotIn("run_in_background", tool.parameters["properties"])

    def test_unknown_provider_rejected(self):
        with self.assertRaises(SubagentError) as cm:
            self._tool(provider="acp")
        self.assertEqual(cm.exception.code, "UNAVAILABLE")

    def test_forced_background_rejected_verbatim(self):
        # 禁用实例强制后台：schema 省略还需执行期强制（index.ts:257）
        tool = self._tool(enable_run_in_background=False)
        with self.assertRaises(RuntimeError) as cm:
            _execute(tool, {"description": "研", "prompt": "x",
                            "run_in_background": True}, self.parent)
        self.assertEqual(str(cm.exception),
                         "run_in_background is disabled for this tool instance "
                         "(enableRunInBackground: false)")

    def test_requires_calling_agent(self):
        tool = self._tool()
        with self.assertRaises(RuntimeError) as cm:
            _execute(tool, {"description": "研", "prompt": "x"}, None)
        self.assertEqual(str(cm.exception),
                         "subagent tool requires a calling agent (exec.agent was undefined)")


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
            delegation_tool._DESCRIPTION + delegation_tool._SUFFIX_ONE_SHOT
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
        self.assertEqual(value["kind"], "foreground")
        descriptor = fold_subagent_descriptor(
            self.persistence.inspect(value["runId"])["events"])
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


class TestForegroundDelegation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.persistence = JsonlPersistence(self.tmp.name)

    def test_foreground_returns_child_output_inline(self):
        # one-shot 缺省前台；ctx 不装 jobs 服务 → 前台不依赖 jobs
        parent, _, mgr = self._scripted({"description": "研", "prompt": "查资料"})
        parent.run("委派任务")
        cid = mgr.list_children()[0]["id"]
        events = self.persistence.inspect(cid)["events"]
        types = [e["type"] for e in events]
        self.assertIn("subagent/descriptor", types)
        self.assertEqual(events[-1]["type"], "turn/end")
        self.assertEqual(events[-1]["data"]["reason"]["kind"], "completed")
        # render = 输出文本块拼接（前台 canonical 的 runId/output 不进模型面）
        self.assertIn("子响应", _all_event_text(parent.session.events))
        # 结算通知照常投递：父 running → next-step 边界消费
        notices = _notices(parent.session)
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["data"]["source"]["senderSessionId"], cid)
        self.assertEqual(parent.last_response(), "父响应")

    def test_continuable_explicit_false_waits_foreground(self):
        # jobs.spec.ts:1115 同款语义：continuable 模式显式 run_in_background:false
        # → 等待首回合结果；同样不触碰 jobs 服务
        parent, _, mgr = self._scripted(
            {"description": "研", "prompt": "查资料", "run_in_background": False},
            config={"background_mode": "continuable"})
        parent.run("委派任务")
        cid = mgr.list_children()[0]["id"]
        events = self.persistence.inspect(cid)["events"]
        self.assertEqual(events[-1]["data"]["reason"]["kind"], "completed")
        self.assertIn("子响应", _all_event_text(parent.session.events))
        self.assertEqual(mgr.state_of(cid)["kind"], "idle")

    def test_max_depth_propagates(self):
        parent, ctx, reg = _parent_loop()
        mgr = _manager(parent, self.persistence, max_depth=0)
        tool = install_subagent_delegation_tool(ctx, reg, mgr)
        with self.assertRaises(SubagentError) as cm:
            _execute(tool, {"description": "研", "prompt": "x"}, parent)
        self.assertEqual(cm.exception.code, "MAX_DEPTH_EXCEEDED")

    def test_persona_and_tool_filter_passthrough(self):
        parent, ctx, reg = _parent_loop()
        mgr = _manager(parent, self.persistence)
        tool = install_subagent_delegation_tool(
            ctx, reg, mgr,
            {"persona": "你是子代理研究员", "tool_filter": ["report"]})
        value = _execute(tool, {"description": "研", "prompt": "x"}, parent)
        self.assertEqual(value["kind"], "foreground")
        descriptor = fold_subagent_descriptor(
            self.persistence.inspect(value["runId"])["events"])
        self.assertEqual(descriptor["label"], "研")
        self.assertEqual(descriptor["persona"], "你是子代理研究员")
        self.assertEqual(descriptor["toolFilter"], {"allow": ["report"]})

    def _scripted(self, args, config=None, **manager_kwargs):
        parent, ctx, reg = _parent_loop(adapter=_DelegatingParent(args))
        mgr = _manager(parent, self.persistence, **manager_kwargs)
        install_subagent_delegation_tool(ctx, reg, mgr, config)
        return parent, ctx, mgr


class TestBackgroundDelegation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.persistence = JsonlPersistence(self.tmp.name)

    def _setup(self, args, config=None):
        parent, ctx, reg = _parent_loop(adapter=_DelegatingParent(args))
        install_jobs(ctx)
        mgr = _manager(parent, self.persistence)
        install_subagent_delegation_tool(ctx, reg, mgr, config)
        return parent, ctx, mgr

    def _job_snapshots(self, ctx, caller):
        return ctx.get("jobs").list(caller=caller)

    def test_continuable_default_ack_and_job_completes(self):
        parent, ctx, mgr = self._setup({"description": "研", "prompt": "查资料"},
                                       {"background_mode": "continuable"})
        parent.run("委派任务")
        cid = mgr.list_children()[0]["id"]
        # ack render：started subagent <持久 child id>
        self.assertIn(f"started subagent {cid}",
                      _all_event_text(parent.session.events))
        # job 按 owner 栅栏可见，终态 completed
        _wait_until(lambda: bool(self._job_snapshots(ctx, parent))
                    and self._job_snapshots(ctx, parent)[0]["status"] == "completed")
        snap = self._job_snapshots(ctx, parent)[0]
        self.assertEqual(snap["kind"], "subagent")
        self.assertEqual(snap["label"], "研")
        self.assertEqual(snap["ownerSession"], parent.id)
        # 子首回合完成并落盘（worker 先结算子再 settle box）
        events = self.persistence.inspect(cid)["events"]
        self.assertEqual(events[-1]["type"], "turn/end")
        self.assertEqual(events[-1]["data"]["reason"]["kind"], "completed")
        # 结算通知最终送达父代理
        _wait_until(lambda: bool(_notices(parent.session)))
        notice = _notices(parent.session)[0]
        text = "".join(b["text"] for b in notice["data"]["content"] if b["type"] == "text")
        self.assertIn(f"Background subagent {cid} finished and will do no further work", text)

    def test_one_shot_background_returns_job_id(self):
        parent, ctx, mgr = self._setup(
            {"description": "研", "prompt": "查资料", "run_in_background": True})
        parent.run("委派任务")
        # ack render：started background subagent job <jobId>（id 可预测 <kind>-N）
        self.assertIn("started background subagent job subagent-1",
                      _all_event_text(parent.session.events))
        _wait_until(lambda: bool(self._job_snapshots(ctx, parent))
                    and self._job_snapshots(ctx, parent)[0]["status"] == "completed")
        snap = self._job_snapshots(ctx, parent)[0]
        self.assertEqual(snap["id"], "subagent-1")
        self.assertEqual(snap["kind"], "subagent")

    def test_failed_background_job_detail_carries_diagnostic(self):
        # run-settlement failureDetail：失败 outcome 的 detail 附 "; diagnostic: ..."
        class BoomChild(FakeLlmAdapter):
            async def stream(self, messages, tools, signal=None):
                raise LlmFailure("RATE_LIMIT", "429 Too Many Requests")
                yield  # noqa: unreachable —— 使本函数成为异步生成器（对齐适配器协议）

        parent, ctx, reg = _parent_loop(adapter=_DelegatingParent(
            {"description": "炸", "prompt": "去失败", "run_in_background": True}))
        install_jobs(ctx)
        mgr = SubagentContinuationManager(
            parent, self.persistence, adapter_factory=lambda p, m, e=None: BoomChild())
        install_subagent_delegation_tool(ctx, reg, mgr)
        parent.run("委派任务")
        _wait_until(lambda: any(s["status"] == "failed"
                                for s in ctx.get("jobs").list(caller=parent)))
        snap = next(s for s in ctx.get("jobs").list(caller=parent)
                    if s["status"] == "failed")
        self.assertEqual(
            snap["detail"],
            "error; diagnostic: RATE_LIMIT: 429 Too Many Requests")

    def test_job_outcome_diagnostic_wording(self):
        # 纯函数面（run-settlement 对齐）：completed 携带 output 文本；
        # 无诊断 aborted → killed；有诊断与其余 reason → failed 原始词拼接诊断
        self.assertEqual(delegation_tool._job_outcome("completed", None, "终文本"),
                         {"status": "completed", "output": "终文本"})
        self.assertEqual(delegation_tool._job_outcome("aborted", None),
                         {"status": "killed"})
        self.assertEqual(delegation_tool._job_outcome("aborted", "unexpected signal"),
                         {"status": "failed",
                          "detail": "aborted; diagnostic: unexpected signal"})
        self.assertEqual(delegation_tool._job_outcome("error", None),
                         {"status": "failed", "detail": "error"})
        self.assertEqual(
            delegation_tool._job_outcome("error", "RATE_LIMIT: 429"),
            {"status": "failed",
             "detail": "error; diagnostic: RATE_LIMIT: 429"})
        self.assertEqual(
            delegation_tool._job_outcome("max-tokens", "上下文超限"),
            {"status": "failed",
             "detail":
                 "max-tokens; diagnostic: 上下文超限"})

    def test_missing_jobs_service_verbatim_error(self):
        # ctx 无 jobs 服务 → 逐字报错（index.ts 同款补救指引）
        parent, ctx, reg = _parent_loop()
        mgr = _manager(parent, self.persistence)
        tool = install_subagent_delegation_tool(ctx, reg, mgr)
        with self.assertRaises(RuntimeError) as cm:
            _execute(tool, {"description": "研", "prompt": "x",
                            "run_in_background": True}, parent)
        self.assertEqual(
            str(cm.exception),
            "background jobs unavailable: load @deepseek-ai/dsh-jobs "
            "and @deepseek-ai/dsh-tool-jobs")

    def test_system_prompt_section_registered_for_continuable(self):
        parent, ctx, reg = _parent_loop()
        svc = SystemPromptService(ctx)
        ctx.provide(SYSTEM_PROMPT_SERVICE, svc)
        mgr = _manager(parent, self.persistence)
        install_subagent_delegation_tool(ctx, reg, mgr,
                                         {"background_mode": "continuable"})
        rendered = {s["name"]: s["text"] for s in svc.render({})}
        self.assertIn("tool:subagent", rendered)
        self.assertIn("Use subagent in the background by default.",
                      rendered["tool:subagent"])

    def test_no_section_for_one_shot(self):
        parent, ctx, reg = _parent_loop()
        svc = SystemPromptService(ctx)
        ctx.provide(SYSTEM_PROMPT_SERVICE, svc)
        mgr = _manager(parent, self.persistence)
        install_subagent_delegation_tool(ctx, reg, mgr)
        names = [s["name"] for s in svc.render({})]
        self.assertNotIn("tool:subagent", names)


class TestKillCancelsChildJob(unittest.TestCase):
    """kill 语义：cancel 钩子 → interrupt 子代理 → 子回合 aborted → job killed。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.persistence = JsonlPersistence(self.tmp.name)
        self.parent, self.ctx, self.reg = _parent_loop()
        install_jobs(self.ctx)
        self.child_adapter = _GatedChild()
        self.mgr = SubagentContinuationManager(
            self.parent, self.persistence,
            adapter_factory=lambda p, m, e=None: self.child_adapter)
        self.child_adapter.is_cancelled = lambda: any(
            a.get("interrupted") for a in self.mgr._activations.values())
        install_subagent_delegation_tool(self.ctx, self.reg, self.mgr,
                                         {"background_mode": "continuable"})

    def test_kill_interrupts_child_and_job_killed(self):
        ack = _execute(self.tool(), {"description": "长任务", "prompt": "慢慢做"},
                       self.parent)
        cid = ack["subagentId"]
        self.assertEqual(ack["kind"], "continuable")
        jobs = self.ctx.get("jobs")
        # 等子代理进入 LLM 流（turn 已开）再 kill，避免与启动竞速
        _wait_until(self.child_adapter.reached.is_set)
        job_id = jobs.list(caller=self.parent)[0]["id"]
        self.assertEqual(jobs.kill(job_id, caller=self.parent), "requested")
        self.child_adapter.gate.set()   # 解锁兜底（信号轮询应已先行退出）
        _wait_until(lambda: jobs.get(job_id, caller=self.parent)["status"] == "killed")
        events = self.persistence.inspect(cid)["events"]
        self.assertEqual(events[-1]["type"], "turn/end")
        self.assertEqual(events[-1]["data"]["reason"]["kind"], "aborted")
        _wait_until(lambda: bool(_notices(self.parent.session)))
        notice = _notices(self.parent.session)[0]
        text = "".join(b["text"] for b in notice["data"]["content"] if b["type"] == "text")
        self.assertIn(f"Background subagent {cid} was stopped before it finished.", text)

    def tool(self):
        return next(t for t in [self.reg.resolve("subagent")] if t is not None)


if __name__ == "__main__":
    unittest.main()
