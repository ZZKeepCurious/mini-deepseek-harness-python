"""web 远程方法面（GatewayStreams）：`$events` 装配 + session/terminal/workspace 流。

对齐上游 `packages/api/gateway/src/` + `packages/api/session-controller/src/`：
本类把 session-controller 进程侧能力折叠成一个可被 `web/mux.py` 按 endpoint
打开/取消的路由表（Remote method exports 的 stream 子集）：

  * `session/follow`  —— 单个会话跟随流（history.follow）：开流即一个 `snapshot`
    帧（header/cursor/records/hasMore/projections）后逐条 `event` 帧。
  * `session/control` —— 宿主级 live control：首个 `baseline` 帧（projections）
    后按变更给 `projection` 替换帧。
  * `terminal/retain` —— 窗口持有流：一帧 `retained` 后保持到取消或身份关闭。
  * `terminal/follow` —— 终端恢复流：一个 `snapshot` 帧后跟有序 `output`/`state`。
  * `workspace/follow` —— 工作区投影流：一个 `baseline` 帧后跟有序
    `upsert`/`remove`/`order`/`archived`/`pinned` 增量。
  * `workspaceFiles/changes` —— 文件观察流：`{kind:'ready'}` 后跟 `{kind:'change'}`。
  * `$events`         —— 远程事件流（`web/events.py` RemoteEventRegistry），承载
    api-session/* 转发源 + 审批瀑布 + 用户提问瀑布（`web/approvals.py` /
    `web/questions.py` bridge）。

进程侧数据来自 WebApi（`api._agents` 常驻 AgentLoop、`api.store` 的 Session、
`ctx` 的 jobs 注册表）。跨进程耦合面只有本类发布给 mux 的 wire 契约（endpoint
名 + 帧形状）；同包内部直接引用 WebApi（对齐上游同进程组装）。

mini 简化 / 已核对（须同步 verified-diffs §3.4)：follow 的 records 用会话日志
事件流 `Session.events` 投影（wire 形状对齐上游 `{type:'event', event}` 包装、
cursor = 最后已提交 seq；projections values 由 `telemetry/projection_values` 产出
真实 `sessionStats` + `tokenUsage` 视图，未建持久投影缓存——固定单位现场折叠等价，
见 verified-diffs §2.30），快照走 `WebApi._paginate`（消息对齐 + maxMessages/
turnWindow 截断，对齐 history.follow）；`_attach` 冷会话自动 resume 后取日志尾部
快照，再以 `_poll_new_events`（短轮询 + 空闲 sleep，一次捞出全部新事件，seq 严格
递增）实时补 event 帧。**已核实：alpha.1 wire 无 since 字段**（客户端连回 =
重开流重新投递完整 snapshot/baseline，README 明言单向通知重连不重放）——mini
同款，重连健壮性由「重开全量 + 客户端按 seq 去重」（webui TrajectoryBuffer）
保证，无游标也无需再造。control baseline 对齐上游 control.ts（全部 live 会话
每会话一条 projections 块，空也放；替换帧只来自 `sessionProjections.onChanged`，
queues/jobs 已从 rc.1 control wire 移除）。terminal/workspace 三条流把控制器进程侧
对象经 `TERMINAL_FOLLOW_POLL` 短轮询桥接成 async 生成器（`TerminalFollow`/
`TerminalFollower`/`WorkspaceFollow` 的非阻塞 pop），帧形状与顺序同上游。心跳 Ping 由
launcher 的 transport 级 ping 闭合（`web/launcher.py` uvicorn_options，不在此层）。
"""
from __future__ import annotations

import asyncio
from typing import Any

from .api import DEFAULT_MAX_MESSAGES, _Reject
from .args import (
    BoundaryReject,
    boundary_error_message,
    canonical_endpoint,
    validate_args,
)
from .events import RemoteEventRegistry
from .stream_protocol import REMOTE_EVENT_STREAM_ENDPOINT
from .uplink import UplinkInbox, UplinkItems
from ..telemetry import projection_values

__all__ = ["GatewayStreams", "RemoteStreamError", "StreamInvocation"]

FOLLOW_POLL_INTERVAL = 0.05
#: terminal/follow 帧轮询间隔（秒）：同步 follower 非阻塞 pop 的桥接粒度。
TERMINAL_FOLLOW_POLL = 0.01


class RemoteStreamError(RuntimeError):
    """某 endpoint 打开/运转失败（上游 RemoteStreamError family 的 mini 折叠）。

    @param code - RPC 码（stream-server 折进 error 帧 details）。
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class StreamInvocation:
    """一次 Remote 流调用在方法侧看到的上下文（上游 `ctx.invocation` 的 mini 面）。

    上游每个 Remote 方法都经 `this.ctx.invocation` 读本次调用的 request /
    service / peer / signal / uplink（`GatewayInvocation`，index.ts:1287-1335）：
    `uplink()` 每次调用取一次（第二次抛错），下行结束时 `close()` 释放——没人取
    用的上行被立即释放，取用过的关闭并丢弃未读项。

    @param endpoint - canonical endpoint（codec 失败进 details）。
    @param uplink - 该逻辑流的有界上行 inbox（`web/uplink.py`）。
    @param signal - 取消句柄（mux 关闭 / 客户端 cancel）。
    @param codec - 该方法声明的上行 codec；`None` 表示未声明，走无损 JSON 校验
        （上游 `prepared.descriptor.uplink?.codec ?? SRC_JSON_CODEC`）。
    """

    def __init__(self, endpoint: str, uplink: UplinkInbox | None, signal,
                 codec=None):
        self.endpoint = endpoint
        self.signal = signal
        self._uplink = uplink
        self._codec = codec
        self._items: UplinkItems | None = None
        self._taken = False

    def uplink(self) -> UplinkItems:
        """取本调用的上行项迭代器（逐项过 codec）；每次调用只能取一次。"""
        if self._taken:
            raise RuntimeError(
                f"typert gateway: {self.endpoint}: invocation.uplink() is "
                "available once per call")
        self._taken = True
        self._items = UplinkItems(self._uplink, self.endpoint, self._codec)
        return self._items

    def close(self) -> None:
        """下行结束：释放上行（上游 `invocation.close()`）。"""
        if self._items is not None:
            self._items.close()
            return
        self._taken = True
        release_uplink(self._uplink)


def _as_plain(value: Any) -> Any:
    from ..core.session.json import thaw
    return thaw(value)


def release_uplink(uplink: Any) -> None:
    """释放一条上行 inbox：无人再读，之后的客户端帧直接丢弃（上游 releaseUplink）。"""
    if uplink is not None:
        uplink.release()


async def _with_uplink(stream: Any, invocation: StreamInvocation):
    """代理一条流，结束时释放它的上行（上游 `invocation.close()` 的释放点）。"""
    try:
        async for value in stream:
            yield value
    finally:
        invocation.close()


class GatewayStreams:
    """WebApi 之上组装好的 Remote 方法面（$events + session/follow/control）。

    WebApi 构造时创建一次（`api.gateway`）；`web/mux.py` 的 WS open 按 endpoint
    分发到 `open_stream`；`web/server.py` 的 `$events/result` unary 经
    `receive_result` 消费；`dispose()` 级联清理转发源与审批桥。
    """

    def __init__(self, api: Any):
        self.api = api
        self.ctx = api.ctx
        self.events = RemoteEventRegistry(home=api.cwd)
        self.events.setup_source(api)
        from .approvals import RemoteApprovalBridge
        self.approvals = RemoteApprovalBridge(self)
        from .questions import RemoteQuestionBridge
        self.questions = RemoteQuestionBridge(self)
        self._control_queues: dict[asyncio.Queue, None] = {}
        self._attached = False
        self._disposers: list[Any] = []
        #: endpoint → 声明的上行 codec；出厂为空（rc.1 全部 Remote 方法 `In = never`），
        #: 声明点见 `uplink_codecs`。
        self._uplink_codecs: dict[str, Any] = {}

    # ---------- mux 分发入口 ----------

    def stream_kinds(self) -> dict[str, str]:
        """endpoint → 打开/消费实现（上游 RemoteMethod exports 的 stream 子集）。"""
        return {
            REMOTE_EVENT_STREAM_ENDPOINT: "$events",
            "session/follow": "follow",
            "session/control": "control",
            "terminal/retain": "terminal_retain",
            "terminal/follow": "terminal_follow",
            "workspace/follow": "workspace_follow",
            "workspaceFiles/changes": "workspace_changes",
            "job/list": "job_list",
            "job/follow": "job_follow",
        }

    def uplink_codecs(self) -> dict[str, Any]:
        """endpoint → 该方法声明的上行 codec（缺省即不声明，走无损 JSON 校验）。

        上游这份声明来自 typert 生成的 `InvocationDescriptor.uplink.codec`
        （types.ts:355-357）；rc.1 出厂的 Remote 方法全是 `In = never`
        （session/follow、control、terminal/follow、workspace/follow、
        workspaceFiles/changes、$events），故本表出厂为空。
        """
        return self._uplink_codecs

    def open_stream(self, endpoint: str, payload: Any, uplink: Any = None, signal=None):
        """按 endpoint 打开一个流：返回 async 生成器（帧 value 序列）。

        @param payload - open 帧的 payload（`{args: ...}`，各 endpoint 自校验）。
        @param uplink - 该逻辑流的有界上行 inbox（`web/uplink.py`）。`$events` 是
            网关自有流、无人读上行，open 时立即释放（上游 openWireStream）；其余端点
            的 uplink 活到该流结束（上游 `invocation.close()` 释放），期间客户端
            item 帧受字节上限约束、违例中止本流。
        @param signal - 可选取消句柄（mux 关闭/客户端 cancel 时终止）。
        @raises EventSourceFailure / RemoteStreamError。
        """
        endpoint = canonical_endpoint(endpoint)
        kind = self.stream_kinds().get(endpoint)
        if kind is None:
            # 未知 endpoint：上游 gateway 报 invocation-unavailable（index.ts:660
            # 「no active Remote method exports this endpoint」折算码）
            raise RemoteStreamError(
                "gateway/invocation-unavailable",
                f"typert gateway: {endpoint}: no active Remote method exports this endpoint")
        if kind == "$events":
            release_uplink(uplink)
            return self.events.open(payload, signal=signal)
        if (not isinstance(payload, dict) or set(payload) != {"args"}
                or not isinstance(payload["args"], dict)):
            raise RemoteStreamError(
                "gateway/arguments-invalid",
                f"typert gateway: {endpoint}: requires exactly an args object")
        try:
            validate_args(endpoint, payload["args"])
        except BoundaryReject as error:
            raise RemoteStreamError(
                error.code, boundary_error_message(endpoint, error.message)) from error
        invocation = StreamInvocation(
            endpoint, uplink, signal, self._uplink_codecs.get(endpoint))
        if kind == "follow":
            return _with_uplink(self._follow(payload["args"], invocation), invocation)
        if kind == "terminal_retain":
            return _with_uplink(
                self._terminal_retain(payload["args"], invocation), invocation)
        if kind == "terminal_follow":
            return _with_uplink(
                self._terminal_follow(payload["args"], invocation), invocation)
        if kind == "workspace_follow":
            return _with_uplink(
                self._workspace_follow(payload["args"], invocation), invocation)
        if kind == "workspace_changes":
            return _with_uplink(
                self._workspace_changes(payload["args"], invocation), invocation)
        if kind == "job_list":
            return _with_uplink(self._job_list(payload["args"], invocation), invocation)
        if kind == "job_follow":
            return _with_uplink(
                self._job_follow(payload["args"], invocation), invocation)
        return _with_uplink(self._control(invocation), invocation)

    # ---------- session/follow（历史跟随流） ----------

    async def _follow(self, args: dict, invocation: StreamInvocation):
        address = args.get("address")
        if (address.get("kind") != "session"
                or not isinstance(address.get("sessionId"), str)
                or not address["sessionId"]):
            raise RemoteStreamError("gateway/arguments-invalid",
                                    "session/follow requires a session address")
        max_messages = args.get("maxMessages")
        turn_window = args.get("turnWindow")
        # 上游 history.follow 先 validateHistoryWindow（maxMessages/turnWindow 语义门）
        # 再取源；业务码在此折成流 error（同 `_FollowSubscription`）。
        try:
            self.api._validate_history_window(max_messages, turn_window)
        except _Reject as error:
            raise RemoteStreamError(error.code, error.message) from error
        session_id = address["sessionId"]
        session = self.api.store.get(session_id)
        if session is None:
            raise RemoteStreamError("session/not-found",
                                    f'session "{session_id}" not found')
        if self.api._agents.get(session_id) is None:
            self.api._attach(session)
        events = list(session.events)
        # cursor = 最后一条已提交事件 seq（上游 0 基 seq 的 inclusive cursor，
        # history.ts `sourceLog.at(-1)?.seq ?? -1`；空日志 -1）
        cursor = events[-1]["seq"] if events else -1
        # 上游 follow 快照 paginate(events, undefined, maxMessages ?? DEFAULT, cursor,
        # turnWindow)：消息对齐 + turnWindow 截断（history.ts follow）。
        page, has_more = self.api._paginate(
            events, None, max_messages or DEFAULT_MAX_MESSAGES, cursor, turn_window)
        records = [self._record(e) for e in page]
        yield {"type": "snapshot", "header": self.api._wire_header(session),
               "cursor": cursor, "records": records, "hasMore": has_more,
               "projections": {"asOfSeq": cursor,
                               "values": projection_values(
                                   session, self.ctx.get("usageStats"),
                                   self.ctx.get("sessionProjections"))}}
        subscribed = cursor + 1
        while True:
            events = await _poll_new_events(
                self.api, session_id, subscribed, invocation.signal)
            if events is None:
                return
            for event in events:
                subscribed = event["seq"] + 1
                yield {"type": "event", "event": event}

    @staticmethod
    def _record(event: dict) -> dict:
        """SessionEventEntry 包装（上游 history.ts entryFor：`{type:'event', event}`）。"""
        return {"type": "event", "event": _as_plain(event)}

    # ---------- terminal/retain（窗口持有流） ----------

    async def _terminal_retain(self, args: dict, invocation: StreamInvocation):
        """`terminal/retain`：一条物理 Remote 流为一个终端身份持窗（不激活 Agent）。

        对齐上游 Remote stream `retain(sessionId, id)`（index.ts:191-205 → retention.ts
        `retain`）：先给一帧 `{type:'retained'}` 确认，随后保持到本流被取消或该身份
        被关闭（`terminal/close` / owner 拆解）——进程退出本身不结束持窗。
        载体简化（已登记）：上游 holders 集合只用于抑制「无人值守空闲回收」调度，
        mini 无该调度器，故持有不改变清理时机。
        """
        controller = self.api.ctx.get("terminalController")
        if controller is None:
            raise RemoteStreamError(
                "gateway/invocation-unavailable",
                "typert gateway: terminal/retain: terminal namespace is not mounted")
        try:
            terminal = controller.retain(args.get("sessionId"), args.get("id"))
        except Exception as error:  # noqa: BLE001 - 折流 error 帧（不关 WS）
            raise RemoteStreamError(getattr(error, "code", None) or "gateway/internal",
                                    str(error)) from error
        signal = invocation.signal
        yield {"type": "retained"}
        while not terminal.closed:
            if signal is not None and getattr(signal, "cancelled", lambda: False)():
                return
            await asyncio.sleep(TERMINAL_FOLLOW_POLL)

    # ---------- terminal/follow（浏览器终端恢复流） ----------

    async def _terminal_follow(self, args: dict, invocation: StreamInvocation):
        """`terminal/follow`：一个完整有界屏幕（snapshot）后跟有序 output/state 帧。

        对齐上游 Remote stream `follow(agent, id, attachmentId)`：附加即独占输入，
        分离不杀进程。mini 以 follower 的非阻塞 pop + 短轮询桥接同步载体
        （进程内输出回调在 provider reader 线程，见 terminal_controller/terminal.py）。
        取消（mux task.cancel）经 finally 分离。
        """
        controller = self.api.ctx.get("terminalController")
        if controller is None:
            raise RemoteStreamError(
                "gateway/invocation-unavailable",
                "typert gateway: terminal/follow: terminal namespace is not mounted")
        try:
            agent = self.api.resolve_terminal_agent(args.get("agentId"))
            follow = controller.follow(agent, args.get("id"), args.get("attachmentId"))
        except Exception as error:  # noqa: BLE001 - 折流 error 帧（不关 WS）
            raise RemoteStreamError(getattr(error, "code", None) or "gateway/internal",
                                    str(error)) from error
        try:
            yield follow.baseline
            while True:
                frame = follow.follower.pop()
                if frame is not None:
                    yield frame
                    continue
                if follow.follower.failure is not None:
                    raise RemoteStreamError("gateway/internal", str(follow.follower.failure))
                if follow.follower.finished or follow.follower.closed:
                    return
                await asyncio.sleep(TERMINAL_FOLLOW_POLL)
        finally:
            follow.detach()

    # ---------- workspace/follow（工作区投影流） ----------

    async def _workspace_follow(self, args: dict, invocation: StreamInvocation):
        """`workspace/follow`：一条完整 baseline 后跟有序 upsert/remove/order/archived。

        对齐上游 WorkspaceController.follow：重连开新代次并重发 baseline。mini 以
        controller 的 WorkspaceFollow（ctx `workspace/changed` 订阅 + 非阻塞 pop）
        经短轮询桥接同步载体。
        """
        controller = self.api.ctx.get("workspaceController")
        if controller is None:
            raise RemoteStreamError(
                "gateway/invocation-unavailable",
                "typert gateway: workspace/follow: workspace namespace is not mounted")
        follow = controller.follow()
        try:
            yield follow.baseline
            while True:
                frame = follow.pop()
                if frame is not None:
                    yield frame
                    continue
                await asyncio.sleep(TERMINAL_FOLLOW_POLL)
        finally:
            follow.close()

    # ---------- workspaceFiles/changes（文件观察流） ----------

    async def _workspace_changes(self, args: dict, invocation: StreamInvocation):
        """`workspaceFiles/changes`：目标级 OS watch 就绪后 `{kind:'ready'}`，
        随后命中目标的失效重 stat 产出 `change` 帧（`path` 必填）。"""
        controller = self.api.ctx.get("workspaceFiles")
        if controller is None:
            raise RemoteStreamError(
                "gateway/invocation-unavailable",
                "typert gateway: workspaceFiles/changes: workspaceFiles namespace is not mounted")
        try:
            scope = self.api.workspace_file_scope(args)
            changes = controller.changes(scope, args.get("path"))
        except Exception as error:  # noqa: BLE001 - 折流 error 帧（不关 WS）
            raise RemoteStreamError(getattr(error, "code", None) or "gateway/internal",
                                    str(error)) from error
        try:
            yield changes.ready
            while True:
                frame = changes.pop()
                if frame is not None:
                    yield frame
                    continue
                await asyncio.sleep(TERMINAL_FOLLOW_POLL)
        finally:
            changes.close()

    # ---------- job 域（job-controller 的流方法面） ----------

    #: job 流帧合并窗口（上游 job-controller observeFlushMs 默认 100ms）。
    JOB_FLUSH_MS = 0.1
    #: job output 帧软字节预算（上游 observeMaxFrameBytes 默认 64 KiB）。
    JOB_MAX_FRAME_BYTES = 64 * 1024
    #: job 流轮询间隔（秒）：同步事件总线 + abort 信号的桥接粒度。
    JOB_POLL = 0.02

    def _jobs_registry(self):
        registry = self.api.ctx.get("jobs")
        if registry is None:
            raise RemoteStreamError(
                "gateway/invocation-unavailable",
                "typert gateway: job namespace is not mounted in this deployment")
        return registry

    @staticmethod
    def _utf8_bytes(text: str) -> int:
        return len(text.encode("utf-8"))

    @staticmethod
    def _is_aborted(signal) -> bool:
        if signal is None:
            return False
        return bool(getattr(signal, "cancelled", lambda: False)()
                    or getattr(signal, "aborted", False))

    async def _wait_or_poll(self, waiter: asyncio.Event, signal) -> None:
        """等待事件置位或取消；返回后由调用方复核真实条件。"""
        while not waiter.is_set():
            if self._is_aborted(signal):
                return
            await asyncio.sleep(self.JOB_POLL)

    async def _job_list(self, args: dict, invocation: StreamInvocation):
        """`job/list`：一次会话可见作业集的整集替换帧流（对齐 rows.ts）。

        首帧即刻（`{type:'rows', jobs:[...]}`），此后每次触及可见作业的
        lifecycle 提交（registered/progress/stopping/settled/removed）合并
        flushMs 后发整集替换；纯 output 追加不刷新 roster。
        """
        registry = self._jobs_registry()
        session_id = args.get("sessionId")
        waiter: asyncio.Event = asyncio.Event()
        changed = {"owner": session_id, "output": False}

        def on_event(event: dict) -> None:
            if event.get("type") == "output":
                return
            owner = (event.get("job") or {}).get("owner")
            if owner is None or owner == session_id:
                waiter.set()

        unsubscribe = registry.events_for(self.ctx).subscribe(
            {"owners": "all"}, on_event)
        signal = invocation.signal
        try:
            yield {"type": "rows", "jobs": registry.list(session_id)}
            while True:
                await self._wait_or_poll(waiter, signal)
                if self._is_aborted(signal):
                    return
                waiter.clear()
                # 让突发提交合成一帧（上游 sleep(flushMs, signal)）。
                await asyncio.sleep(self.JOB_FLUSH_MS)
                if self._is_aborted(signal):
                    return
                yield {"type": "rows", "jobs": registry.list(session_id)}
        finally:
            unsubscribe()

    async def _job_follow(self, args: dict, invocation: StreamInvocation):
        """`job/follow`：锚帧 opened → 合并 output → 终态 status（对齐 observe.ts）。

        from 缺省 = 最老保留字节；removed 中途宣布 → 以移除投影发终态 status 并关流。
        """
        registry = self._jobs_registry()
        job_id = args.get("jobId")
        session_id = args.get("sessionId")
        requested_from = args.get("from")
        if requested_from is not None and (
                isinstance(requested_from, bool) or not isinstance(requested_from, int)
                or requested_from < 0):
            raise RemoteStreamError(
                "gateway/arguments-invalid",
                f"invalid observe offset: expected a non-negative safe integer, "
                f"got {requested_from!r}")
        try:
            job = registry.get(job_id, session_id)
        except Exception as error:  # noqa: BLE001 - 未知/外会话折 not-found
            raise RemoteStreamError(
                "job/not-found", str(error)) from error
        waiter: asyncio.Event = asyncio.Event()
        removed: list[dict] = []

        def on_event(event: dict) -> None:
            changed_id = event.get("id") if event.get("type") == "output" \
                else (event.get("job") or {}).get("id")
            if changed_id != job_id:
                return
            if event.get("type") == "removed":
                removed.append(event.get("job"))
            waiter.set()

        unsubscribe = registry.events_for(self.ctx).subscribe(
            {"owners": "all"}, on_event)
        signal = invocation.signal
        try:
            cursor = job["output"]["earliest"] if requested_from is None else requested_from
            yield {"type": "opened", "job": job, "from": cursor}
            while not self._is_aborted(signal):
                if removed:
                    yield {"type": "status", "job": removed[0]}
                    return
                read = registry.read_at(job_id, cursor, session_id)
                if read.get("chunks") or read.get("lossy"):
                    async for frame in self._job_output_frames(
                            read.get("chunks") or [], read.get("next", cursor),
                            read.get("lossy", False)):
                        yield frame
                cursor = read.get("next", cursor)
                job = registry.get(job_id, session_id)
                if job.get("status") not in ("running", "stopping") \
                        and cursor >= (job.get("output") or {}).get("total", 0):
                    yield {"type": "status", "job": job}
                    return
                await self._wait_or_poll(waiter, signal)
                if not waiter.is_set():
                    continue
                waiter.clear()
                await asyncio.sleep(self.JOB_FLUSH_MS)
        finally:
            unsubscribe()

    async def _job_output_frames(self, chunks: list, next_offset: int,
                                 lossy: bool):
        """把一次 read 按软字节预算切 output 帧（对齐 observe.ts outputFrames）。

        超预算的单块整块带走；lossy 只随首帧标志。
        """
        batch: list = []
        batch_bytes = 0
        flagged_lossy = lossy
        for chunk in chunks:
            batch.append(chunk)
            batch_bytes += self._utf8_bytes(chunk.get("text", ""))
            if batch_bytes >= self.JOB_MAX_FRAME_BYTES:
                last = batch[-1]
                end = last.get("at", 0) + self._utf8_bytes(last.get("text", ""))
                frame = {"type": "output", "chunks": batch, "next": end}
                if flagged_lossy:
                    frame["lossy"] = True
                    flagged_lossy = False
                yield frame
                batch = []
                batch_bytes = 0
        if batch or flagged_lossy:
            frame = {"type": "output", "chunks": batch, "next": next_offset}
            if flagged_lossy:
                frame["lossy"] = True
            yield frame

    # ---------- session/control（宿主级 live control） ----------

    def _attach_control(self) -> None:
        if self._attached:
            return
        self._attached = True
        # 上游 control.ts 构造期订阅 `sessionProjections.onChanged`，只广播
        # projection 替换帧；queues/jobs 及其投影类型（SessionQueuedItem /
        # SessionJob）已从 rc.1 control wire 移除。
        registry = self.ctx.get("sessionProjections")
        if registry is None:
            self._disposers = []
            return
        self._disposers = [registry.on_changed(self._on_projection_changed)]

    async def _control(self, invocation: StreamInvocation):
        self._attach_control()
        # 上游 ControlQueue 无界（Deque）；projection 帧不经丢帧窗口。
        queue: asyncio.Queue = asyncio.Queue()
        self._control_queues[queue] = None
        signal = invocation.signal
        try:
            yield {"type": "baseline", "value": self._control_baseline()}
            while True:
                frame = await queue.get()
                yield frame
                if signal is not None and getattr(signal, "cancelled", lambda: False)():
                    return
        finally:
            self._control_queues.pop(queue, None)

    def _control_baseline(self) -> dict:
        """control baseline（上游 control.ts projectionBaseline）：全部 live 会话
        每会话一条 projections 块（空也放）；queues/jobs 已从 control wire 移除。"""
        projections: dict[str, dict] = {}
        stats = self.ctx.get("usageStats")
        registry = self.ctx.get("sessionProjections")
        for session in self.api.store.list():
            projections[session.session_id] = {
                "asOfSeq": session.seq - 1,
                "values": projection_values(session, stats, registry),
            }
        return {"projections": projections}

    def _on_projection_changed(self, session, key, value, seq) -> None:
        """投影单元视图变更 → 一条 `projection` 替换帧（control.ts:22-30）。"""
        frame = {"type": "projection", "sessionId": session.session_id,
                 "key": key, "value": value, "seq": seq}
        for queue in list(self._control_queues):
            queue.put_nowait(frame)

    # ---------- 生命周期 ----------

    def receive_result(self, result: dict) -> None:
        """把一条 `$events/result`（词法已由 stream_protocol 校验）交给注册表结算。"""
        self.events.receive_result(result)

    def dispose(self) -> None:
        for fn in reversed(self._disposers):
            fn()
        self._disposers.clear()
        self._attached = False
        self.events.dispose()
        self.approvals.dispose()
        self.questions.dispose()


async def _poll_new_events(api: Any, session_id: str, from_seq: int, signal=None):
    """短轮询一次捞出 `from_seq` 起的新事件（seq 非降，批量为空则 sleep）。

    对齐 history.follow 的 gap-free 逐帧语义；mini 用短轮询替代事件通知
    （会话日志在进程内 append 即现成可读）。事件 seq 为 0 基（`seq == 追加前
    日志长度`，对齐上游 EventLog），snapshot `cursor` = 最后已提交事件 seq
    （inclusive，上游 history.ts `sourceLog.at(-1)?.seq ?? -1`），下一条应达
    seq = cursor + 1；因此过滤用 `>= from_seq`，保证快照后第一条活体事件不
    漏判（旧 `>` 会永久吞掉该条）。一次返回全部新事件避免逐条重扫：调用方
    顺序 yield，seq 保证严格递增。心跳/取消信号与轮询间隔由调用方控制。
    """
    loop_ = api._agents.get(session_id)
    if loop_ is None:
        return None
    session = loop_.session
    while True:
        fresh = [event for event in list(session.events)
                 if event.get("seq", 0) >= from_seq]
        if fresh:
            return fresh
        if signal is not None and getattr(signal, "cancelled", lambda: False)():
            return None
        await asyncio.sleep(FOLLOW_POLL_INTERVAL)


async def _poll_new_event(api: Any, session_id: str, from_seq: int, signal=None):
    """轮询会话日志在 from_seq 之后的新事件（简化 follow，无 since 恢复游标）。

    对齐 history.follow 的 gap-free 逐帧语义：从 `from_seq` 起按 seq 严格递增
    提取。mini 用短轮询替代事件通知（会话日志在进程内 append 即现成可读），
    心跳/取消信号与轮询间隔由调用方控制。
    """
    loop_ = api._agents.get(session_id)
    if loop_ is None:
        return None
    session = loop_.session
    while True:
        next_event = None
        for event in list(session.events):
            if event.get("seq", 0) > from_seq:
                next_event = event
                break
        if next_event is not None:
            return next_event
        if signal is not None and getattr(signal, "cancelled", lambda: False)():
            return None
        await asyncio.sleep(FOLLOW_POLL_INTERVAL)
