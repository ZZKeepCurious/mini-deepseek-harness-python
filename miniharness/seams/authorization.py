"""P2-21（2026-09-06）：`ctx.authorization` —— 凭据获取的授权 seam。

对应 dsh 真实源码：packages/credentials/authorization/src/{index,types}.ts。

上游语义（已核实，authorization/src/index.ts + types.ts）：
  * 能力 seam：获得「配置之外谁也供不出」的凭据——需要与人类对话才能拿到
    （打开此页、粘贴那个码、选一个账号）。seam 拥有对话与生命周期，不拥有
    协议：能获得自身凭据的插件按它将写入的 CredentialKey 注册一个 flow，
    flow 以一套中性的 notices/prompts 词汇与任意启动它的 surface 对话——
    第二个授权协议到来时是另一个 flow，而不是另一个 seam。
  * registerFlow(flow)：一键一 flow（两插件认领同一 key 会各自写自己的
    payload 格式，后跑的留下前者读不懂的记录）；重复 → AuthorizationError
    code `DUPLICATE_FLOW`。返回 disposer：注销 flow 并中止其 in-flight
    attempt。经 ctx.effect 登记，拥有 fiber 拆卸时自动收走。
  * list() / describe(key)：注册序逐 flow 条目 {key, label, methods,
    inFlight}（describe 找不到 → None）。
  * cancel(key)：中止该 key 的 in-flight attempt（与请求自身 signal 分离：
    request/response 传输的 Cancel 按钮落在第二个调用上，拿不到第一个的
    signal）。
  * begin(request)：一键同时只能一个 attempt。校验序：NO_FLOW → 方法
    （缺省 flow.methods[0].id）→ UNKNOWN_METHOD → ALREADY_IN_FLIGHT →
    请求已中止（未开始前不占槽、不跑 flow，但校验先行）。随后占槽、run
    flow；撤销或人类 decline → 'cancelled'；flow 抛错按分类：止于
    aborted/declined → 'cancelled'，否则错误上抛给 caller。
  * commit 契约：flow 必须在 run 内「经 ctx.credentials 把 key 的记录提交」
    ——seam 在 run 期间监听 `credentials/record-updated`（key 命中即记
    observed.committed），run 结束后账目不对 → NOT_COMMITTED（"resolved
    without committing..."）；随后 describe_record 确认记录「现在仍在」，
    被删 → NOT_COMMITTED（"deleted its credential record..."）。presence
    不足以证明：re-auth 时旧记录已存在，presence 会让只读遍的 flow 蒙混。
  * settlement fan-out：槽释放后 `authorization/settled`(key, settlement)
    事件，contained listener 失败（每个监听器都跑；sync throw 记日志，
    INVARIANT code rethrow——attempt 已终结、key 已释放，坏 watcher 绝不
    能反过来把 calller 的结算结果变成自己的失败）。每个终局（含 'failed'）
    都发，第三方浏览器页也能知道 attempt 结束了。
  * 错误码闭集：DUPLICATE_FLOW / NO_FLOW / UNKNOWN_METHOD /
    ALREADY_IN_FLIGHT / NOT_COMMITTED / DECLINED。

载体简化（须在文档标注）：mini 同步单进程——flow.run 与 interaction 回调
同步执行（上游 Promise/async）；AbortSignal 复用 commands 的最小面
（aborted + reason，无 addEventListener 事件机；协作式轮询同 upstream
withAbort 的退出检查）；begin 同步返回 outcome，不返回 pending attempt。
events dispatch 以 mini `ctx.emit` 载波承载（payload = {key, settlement}，
上游为 (key, settlement) 双参签名——mini 单 payload 载体的等价打包，见
`authorization/settled` 文档）。settle 的 contained-dispatch 逐监听器
try/except 实现（上游 events.dispatch 同款）；对返回 awaitable 的监听器
做 fire-and-forget suppression。install_credentials/install_authorization
是 mini 装配点（上游为 cordis plugin 体系），为教学接线。
"""
from __future__ import annotations

from typing import Any, Callable

from ..commands import AbortSignal
from ..core.scope import Context, Service

__all__ = [
    "ALREADY_IN_FLIGHT",
    "DECLINED",
    "DUPLICATE_FLOW",
    "NO_FLOW",
    "NOT_COMMITTED",
    "UNKNOWN_METHOD",
    "AuthorizationDeclinedError",
    "AuthorizationError",
    "AuthorizationSession",
    "AuthorizationService",
    "install_authorization",
]

#: 稳定错误码闭集（上游 AuthorizationError 的 code 字段）。
DUPLICATE_FLOW = "DUPLICATE_FLOW"
NO_FLOW = "NO_FLOW"
UNKNOWN_METHOD = "UNKNOWN_METHOD"
ALREADY_IN_FLIGHT = "ALREADY_IN_FLIGHT"
NOT_COMMITTED = "NOT_COMMITTED"
DECLINED = "DECLINED"


class AuthorizationError(Exception):
    """授权失败的统一异常：携带上游稳定错误码。

    对齐上游 {@link AuthorizationError}（extends HarnessError，带 code）。
    mini 的 HarnessError 等价物为 exception + code 字段约定（同 LlmFailure）。
    """

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.message = message
        self.code = code
        self.name = "AuthorizationError"


class AuthorizationDeclinedError(AuthorizationError):
    """人类拒绝 prompt——拒绝，而不是 surface 坏了。只有人类的「不」才用本类。

    提示词被自身 signal 撤回（flow 主动让竞速中的输家退场）必须抛别的错误，
    否则随后的真失败会被误读为 decline。
    """

    def __init__(self, message: str = "the authorization prompt was declined"):
        super().__init__(message, DECLINED)
        self.name = "AuthorizationDeclinedError"


class AuthorizationSession:
    """运行中 flow 与人类对话的窗口：全部成员限定一次 attempt。

    对齐上游 AuthorizationSession：method 是调用方拣选的方法 id，signal 是
    撤销信号（调用方撤回或 cancel(key)），notify 只报告进度（fire-and-
    forget——surface 渲染不了通知不许拖停 flow），prompt 等待人类回答（拒绝
    → AuthorizationDeclinedError）。
    """

    __slots__ = ("method", "signal", "notify", "prompt")

    def __init__(self, method: str, signal: AbortSignal,
                 notify: Callable[[dict], None], prompt: Callable[[dict], str]):
        self.method = method
        self.signal = signal
        self.notify = notify
        self.prompt = prompt


class AuthorizationService(Service):
    """`ctx.authorization`：凭据获取 flow 的注册表，每 key 同时一个 attempt。

    构造即经 ctx.provide 登记；begin 依赖 `ctx.credentials`（describe_record
    确认 commit）。mini 同步实现：全部方法同步返回，begin 直接跑完整个
    attempt。flow/method/notice/prompt/interaction 等数据形状以 dict 鸭子
    契约承载（键名对齐上游 types.ts）。
    """

    provide = "authorization"

    def __init__(self, ctx: Context):
        self._flows: dict[str, dict] = {}
        self._running: dict[str, dict] = {}
        super().__init__(ctx, "authorization")

    # ---------- flow 注册 ----------

    def registerFlow(self, flow: dict) -> Callable[[], None]:
        """登记一种获得凭据的方式，一键一 flow。

        返回 disposer：注销 flow 并中止其 in-flight attempt. 重复 key →
        AuthorizationError `DUPLICATE_FLOW`。经 ctx.effect 登记，拥有 fiber
        拆卸时自动收走。
        """
        key = flow["key"]

        def disposer() -> None:
            self._flows.pop(key, None)
            running = self._running.get(key)
            if running is not None:
                running["controller"].abort()

        def execute() -> Callable[[], None]:
            if key in self._flows:
                raise AuthorizationError(
                    f'an authorization flow for "{key}" is already registered', DUPLICATE_FLOW)
            self._flows[key] = flow
            return disposer

        return self.ctx.effect(execute, "authorization.registerFlow()")

    # ---------- 查询 ----------

    def list(self) -> list[dict]:
        """注册序逐 flow 的公开视图（含 inFlight 动态位）。"""
        return [self._entry(flow) for flow in self._flows.values()]

    def describe(self, key: str) -> dict | None:
        """单个 flow 的公开视图；无 flow 认领该 key → None。"""
        flow = self._flows.get(key)
        return None if flow is None else self._entry(flow)

    def _entry(self, flow: dict) -> dict:
        return {
            "key": flow["key"],
            "label": flow["label"],
            "methods": flow["methods"],
            "inFlight": self._running.get(flow["key"]) is not None,
        }

    def cancel(self, key: str) -> None:
        """中止该 key 的 in-flight attempt（无 attempt → no-op）。"""
        running = self._running.get(key)
        if running is not None:
            running["controller"].abort()

    # ---------- begin ----------

    def begin(self, request: dict) -> dict:
        """跑一次授权 attempt 并报告终局。

        校验序对齐上游：NO_FLOW → UNKNOWN_METHOD → ALREADY_IN_FLIGHT →
        请求已中止（占槽前）。同步返回 {"status": "authorized"} 或
        {"status": "cancelled"}；失败以 AuthorizationError 抛出。
        """
        key = request["key"]
        flow = self._flows.get(key)
        if flow is None:
            raise AuthorizationError(f'no authorization flow is registered for "{key}"', NO_FLOW)
        methods = flow["methods"]
        method = request.get("method") or methods[0]["id"]
        if not any(candidate["id"] == method for candidate in methods):
            raise AuthorizationError(
                f'authorization flow for "{key}" offers no method "{method}"', UNKNOWN_METHOD)
        if key in self._running:
            raise AuthorizationError(
                f'an authorization attempt for "{key}" is already running', ALREADY_IN_FLIGHT)
        request_signal = request.get("signal")
        if request_signal is not None and request_signal.aborted:
            return {"status": "cancelled"}
        controller = AbortSignal()
        self._running[key] = {"controller": controller}
        settlement: str = "failed"
        try:
            outcome = self._attempt(flow, method, controller, request.get("interaction"))
            settlement = outcome["status"]
            return outcome
        finally:
            self._running.pop(key, None)
            # 槽释放后才发（监听器若立刻再 begin 不会被本 attempt 顶掉）
            self._settle(key, settlement)

    # ---------- attempt ----------

    def _attempt(self, flow: dict, method: str, signal: AbortSignal,
                 interaction: dict | None) -> dict:
        """跑 flow，并把它钉在 commit 契约的这半边。"""
        observed = {"declined": False, "committed": False}

        def on_record_updated(payload: Any) -> None:
            # 事件 payload = 被写入的 CredentialKey；命中本 flow 的 key 即记账。
            if payload == flow["key"]:
                observed["committed"] = True

        watcher = self.ctx.on("credentials/record-updated", on_record_updated)
        runner_error: BaseException | None = None
        try:
            session = AuthorizationSession(
                method=method,
                signal=signal,
                notify=lambda notice: self._safe_notify(interaction, notice),
                prompt=lambda prompt: self._safe_prompt(interaction, prompt, observed),
            )
            try:
                flow["run"](session)
            except AuthorizationDeclinedError as error:
                observed["declined"] = True
                runner_error = error
        except BaseException as error:
            runner_error = error
        finally:
            watcher()
        if signal.aborted or observed["declined"]:
            # 撤销或人类说「不」是结局不是失败（即便 flow 抛错也如此归类）——
            # 不核 commit 账目，也不把该结局当成改写后的失败重抛。
            return {"status": "cancelled"}
        if runner_error is not None:
            raise runner_error
        if not observed["committed"]:
            raise AuthorizationError(
                f'authorization flow for "{flow["key"]}" resolved without committing a '
                "credential record in this attempt", NOT_COMMITTED)
        credentials = self.ctx.get("credentials")
        stored = credentials.describe_record(flow["key"])
        if not stored.get("configured"):
            raise AuthorizationError(
                f'authorization flow for "{flow["key"]}" deleted its credential record '
                "instead of committing one", NOT_COMMITTED)
        return {"status": "authorized"}

    def _safe_notify(self, interaction: dict | None, notice: dict) -> None:
        """Fire-and-forget 在 seam 守门：surface 渲染不了通知只是丢通知。"""
        if interaction is None:
            return
        try:
            interaction["notify"](notice)
        except BaseException as error:
            self.ctx.logger.warn("authorization: the interaction surface failed to render a notice")
            self.ctx.logger.warn(error)

    def _safe_prompt(self, interaction: dict | None, prompt: dict,
                     observed: dict) -> str:
        """把 surface 对 prompt 的拒绝标记为人类 decline（rewrite 遮蔽也不漏）。"""
        if interaction is None:
            raise AuthorizationDeclinedError()
        try:
            return interaction["prompt"](prompt)
        except AuthorizationDeclinedError as error:
            observed["declined"] = True
            raise error from None

    def _settle(self, key: str, settlement: str) -> None:
        """向 `authorization/settled` fan-out，contained listener 失败。

        每个监听器都跑；sync throw（INVARIANT 除外）只记日志，attempt 已
        终结、key 已释放——坏 watcher 不能反过来把调用方的结算变成失败。
        """
        payload = (key, settlement)
        for listener in self.ctx._hooks_for("authorization/settled", None):
            try:
                returned = listener(payload)
                if returned is not None and hasattr(returned, "__await__"):
                    # 异步监听器的拒绝只记日志（上游 addEventListener rejection
                    # suppression 同款），不等待、不改变结局。
                    import asyncio  # noqa: PLC0415 - 延迟导入避免模块首载成本
                    try:
                        asyncio.ensure_future(_consume_async_listener(returned.__await__()))
                    except BaseException:
                        pass
            except BaseException as error:
                if getattr(error, "code", None) == "INVARIANT":
                    raise
                self.ctx.logger.warn(
                    'authorization: an authorization/settled listener for "%s" failed', key)
                self.ctx.logger.warn(error)


async def _consume_async_listener(coro) -> None:
    """异步 settled 监听器：拒绝仅记日志（nothing awaits it）。"""
    try:
        await coro
    except BaseException:
        pass


def install_authorization(ctx: Context) -> AuthorizationService:
    """装配 `ctx.authorization` 服务（构造即登记；重复装配返回既有实例）。"""
    existing = ctx.get("authorization")
    if existing is not None:
        return existing
    return AuthorizationService(ctx)