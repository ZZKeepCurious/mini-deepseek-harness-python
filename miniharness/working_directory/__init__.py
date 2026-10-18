"""working-directory —— 会话工作目录（ctx.workingDirectory）。

对应 dsh 真实源码：
  * packages/session/working-directory/src/index.ts（WorkingDirectoryService）
  * packages/session/tool-working-directory/src/index.ts（working_directory 工具）

职责（上游模块 docstring 同构）：
  * 唯一持有部署缺省 `defaultDirectory`（无初始项目的会话回退），以及每个
    会话的有效目录与其模型可见的变更。
  * 会话目录以**会话日志为存储**：运行时切换 = 追加恰一条
    `working-directory/change` 事件（写路径 commit），并注入一条
    `source.kind == 'working-directory'` 的 transition notice（模型可见 ⟺
    已记录：目录变更本身也是日志事件）。不可变的会话 header（mini
    `session.meta['cwd']`）始终保留原始项目目录，冷恢复即状态。
  * `workingDirectory` 投影（stateVersion 1）：init = `header.cwd ?? None`，
    apply 命中 `working-directory/change` 取 `event.data.cwd`。`ctx.workingDirectory.get(session)`
    即投影状态 ?? 部署缺省，零文件系统 I/O。
  * `working-directory:current` 上下文节：order = CONTEXT_ORDERS.WORKING_DIRECTORY
    (100)，required=true，interpolate=false——目录名里的 `{{...}}` 字面呈现；
    运行时上下文抑制下仍保留（required）。
  * 装配期 prepend 监听器：有 agent 时先视情况恢复目录（ensure），再强制把该
    必需贡献写回装配结果（缺失则 unshift、存在则覆盖 text 与 interpolate），
    使任何装配变换都无法吞掉当前目录（上游同款）。
  * 容器化并发：`serialize` 按会话串行化 ensure/set；服务 dispose 时中止
    生命周期信号并排干在途操作（对齐 AbortSignal + operations 排干）。
  * `working_directory` 工具：读 / 改调用会话的目录（cd 缺省读、给出则切换）。

载体差异（登记 verified-diffs §3）：
  * 上游 `AbortSignal.any([lifecycle, signal])` → mini `_Control` 合并判取消
    （判定 `.is_set()` / `.aborted`；取消原因取信号的 `.reason`/`.abort_reason`，
    无则用 AbortError 文案）。
  * 上游 zod `stateSchema`/`viewSchema` → mini 运行期可调用校验（无声明合并）。
  * 无 per-agent fiber 断言差异：mini 用 `agent.ctx.fiber.assert_active()`。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from weakref import WeakKeyDictionary

from ..core.scope import Context, Service
from ..core.session import create_message, text_block
from ..core.working_cwd import working_directory_of
from ..session_projection import ProjectionDefinition

__all__ = [
    "CONTEXT_NAME",
    "EVENT_TYPE",
    "PROJECTION_KEY",
    "SOURCE_KIND",
    "WorkingDirectoryService",
    "effective_working_directory",
    "install_working_directory",
    "register_working_directory_tool",
]

CONTEXT_NAME = "working-directory:current"
EVENT_TYPE = "working-directory/change"
PROJECTION_KEY = "workingDirectory"
SOURCE_KIND = "working-directory"

CONTEXT_ORDER_NAME = "WORKING_DIRECTORY"

_ABORTED_TEXT = "This operation was aborted"
_DISPOSED_TEXT = "working-directory service disposed"


def _directory_schema(value):
    """投影状态/wire 视图校验（上游 zod `string().min(1).refine(isAbsolute).nullable()`）。"""
    if value is None:
        return None
    if not isinstance(value, str) or value == "" or not os.path.isabs(value):
        raise ValueError(f"invalid working directory value: {value!r}")
    return value


def _signal_aborted(signal) -> bool:
    """判一个取消信号是否已置位（对齐 AbortSignal.aborted；兼容 mini 多种信号）。"""
    if signal is None:
        return False
    if hasattr(signal, "is_set") and callable(signal.is_set):
        return bool(signal.is_set())
    aborted = getattr(signal, "aborted", None)
    return bool(aborted) if aborted is not None else False


def _signal_reason(signal) -> BaseException | None:
    """取取消原因（无 → None）；上游 `controller.abort(reason)` 的 reason。"""
    if signal is None:
        return None
    for attr in ("reason", "abort_reason"):
        value = getattr(signal, attr, None)
        if isinstance(value, BaseException):
            return value
    return None


async def _await_maybe(value):
    if inspect.isawaitable(value):
        return await value
    return value


async def _drain_operations(pending: list) -> None:
    """等待在途目录操作结算（对齐上游 `Promise.allSettled`，吞掉取消异常）。"""
    await asyncio.gather(*pending, return_exceptions=True)


class _Lifecycle:
    """服务生命周期取消信号（对齐上游 `new AbortController()`）。"""

    def __init__(self) -> None:
        self._error: BaseException | None = None

    @property
    def aborted(self) -> bool:
        return self._error is not None

    def abort(self, error: BaseException) -> None:
        if self._error is None:
            self._error = error

    def error(self) -> BaseException:
        return self._error or RuntimeError(_DISPOSED_TEXT)


class _Control:
    """合并调用方信号与服务生命周期的取消闸（对齐 `AbortSignal.any`）。"""

    def __init__(self, lifecycle: _Lifecycle, signal=None):
        self._lifecycle = lifecycle
        self._signal = signal

    def is_set(self) -> bool:
        return self._lifecycle.aborted or _signal_aborted(self._signal)

    def throw_if_aborted(self) -> None:
        if self._lifecycle.aborted:
            raise self._lifecycle.error()
        if _signal_aborted(self._signal):
            raise _signal_reason(self._signal) or RuntimeError(_ABORTED_TEXT)


def _assert_agent_active(agent) -> None:
    """被拆解的 Agent 不得再发布目录变更（上游 `agent.ctx.fiber.assertActive()`）。"""
    ctx = getattr(agent, "ctx", None)
    fiber = getattr(ctx, "fiber", None)
    if fiber is not None:
        fiber.assert_active()


def effective_working_directory(agent_or_session, ctx: Context | None = None) -> str | None:
    """读取一个 agent/session 当前有效工作目录（同步、零 I/O）。

    消费者面：`ctx.workingDirectory.get(session)`（相对路径基准、shell 起始目录
    等）。无 workingDirectory 服务时回退会话 header（`session.meta['cwd']`）。
    见 `miniharness.core.working_cwd.working_directory_of`（本函数是其别名）。
    """
    return working_directory_of(agent_or_session, ctx)


class WorkingDirectoryService(Service):
    """ctx.workingDirectory（上游 WorkingDirectoryService）。

    Config：{defaultDirectory?: 绝对执行世界回退目录；缺省取启动目录}。
    依赖 ctx 上已提供的 fs / sessionProjections / systemPrompt（上游 `inject`）。
    """

    provide = "workingDirectory"

    def __init__(self, ctx: Context, config: dict | None = None):
        config = dict(config or {})
        directory = config.get("defaultDirectory") or os.getcwd()
        if not os.path.isabs(directory):
            raise ValueError("working-directory: defaultDirectory must be absolute")
        self.default_directory: str = directory
        self._pending: WeakKeyDictionary = WeakKeyDictionary()
        self._operations: set = set()
        self._lifecycle = _Lifecycle()
        super().__init__(ctx, "workingDirectory")
        self._install()

    # ---------- 装配 ----------

    def _install(self) -> None:
        ctx = self.ctx
        fs = ctx.get("fs")
        projections = ctx.get("sessionProjections")
        prompt = ctx.get("systemPrompt")
        if fs is None or projections is None or prompt is None:
            raise ValueError(
                "working-directory requires fs, sessionProjections, and systemPrompt services")
        projections.register(ProjectionDefinition(
            PROJECTION_KEY,
            init=lambda header, inherited: (header or {}).get("cwd") or None,
            apply=lambda state, event: (
                event["data"]["cwd"] if event.get("type") == EVENT_TYPE else state),
            state_version=1,
            view=lambda state: state,
            state_schema=_directory_schema,
            view_schema=_directory_schema,
        ))
        prompt.context(
            name=CONTEXT_NAME,
            order=prompt.get_context_order(CONTEXT_ORDER_NAME),
            required=True,
            interpolate=False,
            text=lambda context: (
                "" if (context or {}).get("agent") is None
                else self._context_text(context["agent"].session)),
        )
        ctx.on("system-prompt/assemble", self._on_assemble, prepend=True)
        ctx.effect(lambda: lambda: self._dispose(), "workingDirectory lifecycle")

    async def _on_assemble(self, cur, next_):
        context = (cur or {}).get("context") or {}
        agent = context.get("agent")
        if agent is None:
            return await _await_maybe(next_())
        await self.ensure(agent, context.get("signal"))
        payload = await _await_maybe(next_())
        if self._lifecycle.aborted:
            raise self._lifecycle.error()
        if not isinstance(payload, dict):
            return payload
        assembly = payload.get("assembly")
        if not isinstance(assembly, dict):
            return payload
        text = self._context_text(agent.session)
        contexts = assembly.get("contexts")
        if not isinstance(contexts, list):
            contexts = []
        located = next((entry for entry in contexts
                        if isinstance(entry, dict) and entry.get("name") == CONTEXT_NAME), None)
        if located is None:
            assembly["contexts"] = [
                {"name": CONTEXT_NAME, "text": text, "interpolate": False}, *contexts]
        else:
            located["text"] = text
            located["interpolate"] = False
        return payload

    def _dispose(self) -> None:
        """拆解：取消生命周期并排干在途操作（上游 effect 的 async disposer 语义）。

        上游 `ctx.effect(() => async () => { abort; await allSettled(operations) })`
        在事件循环内 await 结算。mini 的 fiber 拆解可能落在无运行循环的同步路径
        （如嵌宿同步 `ctx.dispose()`），故此处同步取消生命周期；仅在确有运行循环
        时以任务排干在途操作——避免无循环时的 async disposer fail-loud，且不改
        "取消先于结算"的次序。
        """
        self._lifecycle.abort(RuntimeError(_DISPOSED_TEXT))
        pending = [op for op in self._operations if not op.done()]
        if not pending:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(_drain_operations(pending))

    # ---------- 读面 ----------

    def get(self, session) -> str:
        """读取已提交目录（零文件系统 I/O）：投影状态 ?? 部署缺省。"""
        projections = self.ctx.get("sessionProjections")
        state = projections.state_of(session, PROJECTION_KEY) if projections is not None else None
        return state if state is not None else self.default_directory

    def _context_text(self, session) -> str:
        return f"Current working directory: {json.dumps(self.get(session))}."

    # ---------- 写面 ----------

    async def ensure(self, agent, signal=None) -> str:
        """校验当前目录；消失时恢复原始项目（提交早于兑现）。"""
        return await self._serialize(agent, signal, self._ensure_action)

    async def _ensure_action(self, agent, control: _Control) -> str:
        session = agent.session
        current = self.get(session)
        fs = self._require_fs()
        target = await fs.resolve(current, {"signal": control})
        info = await fs.stat(target, control)
        control.throw_if_aborted()
        _assert_agent_active(agent)
        if info is not None and getattr(info, "type", None) == "directory":
            projections = self.ctx.get("sessionProjections")
            if projections.state_of(session, PROJECTION_KEY) is None:
                session.append(EVENT_TYPE, {"cwd": current})
            return current
        original = (session.meta or {}).get("cwd") or self.default_directory
        restored = await self._directory(original, original, control)
        self._commit(agent, restored, True, control)
        return restored

    async def set(self, agent, path: str, signal=None) -> str:
        """切换一个会话的目录（不改既有进程与权限）；提交早于兑现。"""
        return await self._serialize(
            agent, signal, lambda a, control: self._set_action(a, path, control))

    async def _set_action(self, agent, path: str, control: _Control) -> str:
        if len(path) == 0:
            raise ValueError("working-directory: cd must not be empty")
        directory = await self._directory(path, self.get(agent.session), control)
        self._commit(agent, directory, False, control)
        return directory

    async def _directory(self, path: str, cwd: str, control: _Control) -> str:
        fs = self._require_fs()
        target = await fs.resolve(path, {"cwd": cwd, "signal": control})
        info = await fs.stat(target, control)
        control.throw_if_aborted()
        if info is None or getattr(info, "type", None) != "directory":
            raise ValueError(
                f"working-directory: directory does not exist: {json.dumps(path)}")
        return fs.process_path(target)

    def _commit(self, agent, directory: str, recovered: bool, control: _Control) -> None:
        control.throw_if_aborted()
        _assert_agent_active(agent)
        session = agent.session
        previous = self.get(session)
        if previous == directory:
            return
        session.append(EVENT_TYPE, {"cwd": directory})
        if recovered:
            text = (f"The working directory {json.dumps(previous)} is unavailable. "
                    f"The working directory is now {json.dumps(directory)}.")
        else:
            text = (f"The working directory changed from {json.dumps(previous)} "
                    f"to {json.dumps(directory)}.")
        notice = create_message("user", [text_block(text)], {"kind": SOURCE_KIND})
        try:
            agent.inject(notice)
        except Exception as error:  # noqa: BLE001 - notice 失败仅告警，状态已提交
            logger = getattr(self.ctx, "logger", None)
            if logger is not None and callable(getattr(logger, "warn", None)):
                logger.warn(
                    f"working-directory: committed {json.dumps(directory)}, "
                    f"but could not queue its notice: {error}")

    async def _serialize(self, agent, signal, action):
        session = agent.session
        control = _Control(self._lifecycle, signal)
        loop = asyncio.get_running_loop()
        gate = loop.create_future()
        settled = loop.create_future()
        previous = self._pending.get(session)
        self._pending[session] = gate
        self._operations.add(settled)
        try:
            if previous is not None:
                await asyncio.shield(previous)
            control.throw_if_aborted()
            _assert_agent_active(agent)
            return await action(agent, control)
        finally:
            if not gate.done():
                gate.set_result(None)
            self._operations.discard(settled)
            if not settled.done():
                settled.set_result(None)

    def _require_fs(self):
        fs = self.ctx.get("fs")
        if fs is None:
            raise ValueError("working-directory requires an fs service")
        return fs


def install_working_directory(ctx: Context, config: dict | None = None) -> WorkingDirectoryService:
    """装配 `ctx.workingDirectory`（幂等：已存在则复用现有实例）。"""
    existing = ctx.get("workingDirectory")
    if existing is not None:
        return existing
    return WorkingDirectoryService(ctx, config or {})


def register_working_directory_tool(ctx: Context, registry=None):
    """注册 `working_directory` 工具（读 / cd）；返回注销 disposer。

    上游对照：packages/session/tool-working-directory/src/index.ts（defineTool）。
    """
    from ..core.tools import Tool

    service = ctx.get("workingDirectory")
    if service is None:
        raise ValueError("register_working_directory_tool requires ctx.workingDirectory")
    registry = registry if registry is not None else ctx.get("tools")
    if registry is None:
        raise ValueError("register_working_directory_tool requires a tools registry")

    async def execute(args, exec_):
        agent = getattr(exec_, "agent", None)
        if agent is None:
            raise ValueError("working_directory requires an agent Session")
        cd = args.get("cd") if isinstance(args, dict) else None
        signal = getattr(exec_, "signal", None)
        if cd is None:
            cwd = await service.ensure(agent, signal)
        else:
            cwd = await service.set(agent, cd, signal)
        return {"cwd": cwd}

    def render(_args, value):
        return [text_block(value["cwd"])]

    tool = Tool(
        name="working_directory",
        description=(
            "Read the current working directory, or change it with cd. Relative paths "
            "use the current directory. Existing shells and running processes keep their "
            "own directories."),
        parameters={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "cd": {"type": "string",
                       "description": "Existing directory to enter. Omit to read the "
                                      "current directory."},
            },
        },
        output={
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"cwd": {"type": "string"}},
            },
        },
        execute=execute,
        render=render,
    )
    return registry.register(tool)
