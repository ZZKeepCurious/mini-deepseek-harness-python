"""会话标题（SessionTitleService + 投影 + 提供者注册表）。

上游：packages/session/session-title/src/index.ts（835 行）+ normalize.ts + types.ts，
+ packages/session/session-title-first-prompt-llm（首提示 LLM 提供者）。

标题状态是 durable 会话事件：`session/title`（{title, messageSeqs, source}，
log-only 非 surface，整值替换最后一条胜出）。`title` 与 `titleInput` 是两个
wire 投影单元（stateVersion 1 / 3）。

语义（已核实）：
  * normalize：OSC/CSI/ESC/控制字符/方向控制剥离 + 空白折叠 + trim + UTF-8
    截断（绝不劈开码点）+ trimEnd。fallback：前 maxWords 词 + 字节截断。
  * 资格：`user/message` 且 source.kind=='user'，text 块 '\n' 连接后
    normalize 非空。`titleInput` fold：{first, count, lastSeq}。
  * 自动调度（first-prompt）：仅非 fork（parentSession===undefined）、恰 1
    条资格消息、尚无标题时安排提供者；fallback 恒先落。request/header 事件
    （throughSeq < event.seq）触发路由后的提供者执行。
  * rename（用户钉扎）：normalize + 非空校验 + 追加 `session/title`
    {messageSeqs:[], source:{kind:'user'}}；自动生成停止调度。
  * refresh（显式取消钉扎）：fallback-only 模式重派生 fallback；否则重跑提供者。
  * 提供者注册表：register(provider) 幂等注册；generate(request) 返回
    {title, messageSeqs, model?}；校验消息 seq 是请求快照的有序唯一子集。

载体差异：上游 `llm/stream` 事件（unchanged-route 重触发）与 async 微任务/
AbortSignal 竞争；mini 在 request/header 落日志后同步触发提供者（同步门面），
无 `ctx.llm` 服务——提供者构造注入适配器直接调用 `adapter.stream`。
"""
from __future__ import annotations

import re
from types import MappingProxyType
from typing import Any, Callable

from ..core.scope import Context, Service
from ..core.session import Session
from ..llm import BlockAssembler, LlmFailure
from ..llm.deepseek import DeepSeekAdapter
from ..session_projection import ProjectionDefinition

__all__ = [
    "SessionTitleError",
    "SessionTitleProvider",
    "SessionTitleService",
    "clean_title_text",
    "fallback_title",
    "install_session_title",
    "normalize_title",
    "register_first_prompt_llm_provider",
    "truncate_title_utf8",
]

_OSC_SEQUENCE = re.compile(
    r"(?:\u001b\]|\u009d)(?:(?!\u0007|\u001b\\)[\s\S])*(?:\u0007|\u001b\\|$)", re.UNICODE)
_CSI_SEQUENCE = re.compile(r"(?:\u001b\[|\u009b)[0-?]*[ -/]*[@-~]", re.UNICODE)
_ESC_SEQUENCE = re.compile(r"\u001b[@-_]", re.UNICODE)
_CONTROL_CHARACTER = re.compile(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]",
                                re.UNICODE)
_DIRECTIONAL_CONTROL = re.compile(
    r"[\u200b\u200e\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff]", re.UNICODE)
_WS_RE = re.compile(r"\s+", re.UNICODE)


def clean_title_text(input_text: str) -> str:
    """五类正则剥离 + 空白折叠 + trim（normalize.ts:22-31）。"""
    text = _OSC_SEQUENCE.sub("", input_text)
    text = _CSI_SEQUENCE.sub("", text)
    text = _ESC_SEQUENCE.sub("", text)
    text = _CONTROL_CHARACTER.sub("", text)
    text = _DIRECTIONAL_CONTROL.sub("", text)
    return _WS_RE.sub(" ", text).strip()


def truncate_title_utf8(input_text: str, max_bytes: int) -> str:
    """UTF-8 字节截断（normalize.ts:39-51）：maxBytes 非法抛错；逐码点累计
    字节，绝不劈开码点。"""
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        raise ValueError("maxBytes must be a positive integer")
    if len(input_text.encode("utf-8")) <= max_bytes:
        return input_text
    out: list[str] = []
    used = 0
    for char in input_text:
        size = len(char.encode("utf-8"))
        if used + size > max_bytes:
            break
        out.append(char)
        used += size
    return "".join(out)


def normalize_title(input_text: str, max_bytes: int) -> str:
    """normalizeSessionTitle（normalize.ts:59-61）：清洗 + 截断 + trimEnd。"""
    return truncate_title_utf8(clean_title_text(input_text), max_bytes).rstrip()


def fallback_title(input_text: str, max_words: int, max_bytes: int) -> str:
    """fallbackSessionTitle（normalize.ts:70-74）：前 maxWords 词 + 字节截断。"""
    if not isinstance(max_words, int) or isinstance(max_words, bool) or max_words <= 0:
        raise ValueError("maxWords must be a positive integer")
    words = clean_title_text(input_text).split(" ")
    words = [w for w in words if w][:max_words]
    return truncate_title_utf8(" ".join(words), max_bytes).rstrip()


class SessionTitleError(RuntimeError):
    """标题无效（rename 失败 blame 输入；wire 译 `session/title-invalid`）。"""


def _title_user_message_of(event: dict) -> dict | None:
    """资格判定（index.ts:131-140）：user/message + 人类 source + 非空文本。"""
    if event.get("type") != "user/message":
        return None
    data = event.get("data") or {}
    source = data.get("source") or {}
    if not isinstance(source, (dict, MappingProxyType)) or source.get("kind") != "user":
        return None
    text = "\n".join(
        b.get("text", "") for b in (data.get("content") or [])
        if isinstance(b, (dict, MappingProxyType)) and b.get("type") == "text"
        and isinstance(b.get("text"), str))
    if normalize_title(text, 2 ** 63 - 1) == "":
        return None
    return {"seq": event.get("seq"), "text": text}


def collect_title_messages(events: list, through_seq: int | None = None) -> list:
    """按 seq 序扫描资格消息（index.ts:247-258）。"""
    result = []
    for event in events:
        if through_seq is not None and event["seq"] > through_seq:
            break
        message = _title_user_message_of(event)
        if message is not None:
            result.append(message)
    return result


def fold_session_title(events: list) -> dict | None:
    """foldSessionTitle（index.ts:282-292）：最后一条 session/title 胜出。"""
    for event in reversed(events):
        if event["type"] == "session/title":
            data = event.get("data") or {}
            source = dict(data.get("source") or {})
            return {
                "title": data.get("title"),
                "messageSeqs": list(data.get("messageSeqs") or []),
                "source": source,
                "eventSeq": event.get("seq"),
                "updatedAt": event.get("time"),
            }
    return None


class SessionTitleProvider:
    """标题提供者（index.ts:117-128）：{id, automatic, generate}。"""

    def __init__(self, provider_id: str, automatic: str, generate: Callable):
        self.id = provider_id
        self.automatic = automatic
        self.generate = generate


class SessionTitleService(Service):
    """ctx.sessionTitle：get/rename/refresh/register + 自动生成调度。"""

    provide = "sessionTitle"

    def __init__(self, ctx: Context, config: dict | None = None,
                 adapter: DeepSeekAdapter | None = None):
        config = dict(config or {})
        fallback_max_words = config.get("fallbackMaxWords")
        fallback_max_bytes = config.get("fallbackMaxBytes")
        max_title_bytes = config.get("maxTitleBytes")
        if fallback_max_words is None or fallback_max_bytes is None \
                or max_title_bytes is None:
            raise TypeError("session-title: configuration is required")
        for name, value in (("fallbackMaxWords", fallback_max_words),
                            ("fallbackMaxBytes", fallback_max_bytes),
                            ("maxTitleBytes", max_title_bytes)):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise TypeError(f"session-title: {name} must be a positive integer")
        if fallback_max_bytes > max_title_bytes:
            raise ValueError(
                "session-title: fallbackMaxBytes must not exceed maxTitleBytes")
        self.config = config
        self.adapter = adapter
        self._providers: dict[str, SessionTitleProvider] = {}
        super().__init__(ctx, "sessionTitle")
        # titleInput + title 投影
        self._register_title_projection()
        self._register_title_input_projection()
        # 事件钩子：user/message → 自动调度（fallback 恒先）；request/header
        # → 路由后执行提供者
        self._disposers = [
            ctx.on("session/event", self._on_session_event),
        ]

    # ---------- 读面 ----------

    def get(self, session: Session) -> dict | None:
        return fold_session_title(list(session.events))

    # ---------- rename / refresh ----------

    def rename(self, session: Session, title: str) -> dict:
        """用户钉扎（index.ts:401-421）：normalize + 非空校验 + 追加 user 源。"""
        normalized = normalize_title(title, self.config["maxTitleBytes"])
        if normalized == "":
            raise SessionTitleError("session title must contain visible characters")
        session.append("session/title", {
            "title": normalized,
            "messageSeqs": [],
            "source": {"kind": "user"},
        })
        return self.get(session)

    def refresh(self, session: Session) -> dict | None:
        """显式取消钉扎（index.ts:430-463）：fallback-only 重派生；否则重跑提供者。"""
        current = self.get(session)
        first = self._title_input(session).get("first")
        provider = next((p for p in self._providers.values()
                         if p.automatic == "first-prompt"), None)
        if provider is None or first is None:
            if current is not None and current["source"].get("kind") == "user" \
                    and first is not None:
                self._append_fallback(session, first)
                return self.get(session)
            self._ensure_fallback(session)
            return self.get(session)
        messages = collect_title_messages(list(session.events))
        result = self._run_provider(session, provider, messages)
        if result is None:
            self._ensure_fallback(session)
        return self.get(session)

    # ---------- 注册表 ----------

    def register(self, provider: SessionTitleProvider) -> Callable[[], None]:
        if not isinstance(provider, SessionTitleProvider):
            raise TypeError("session-title provider must be a SessionTitleProvider")
        if not provider.id:
            raise ValueError("session-title provider id must be a non-empty string")
        if provider.automatic not in ("first-prompt", "all-prompts"):
            raise ValueError("session-title provider automatic mode is invalid")
        if not callable(provider.generate):
            raise TypeError(f'session-title provider "{provider.id}" requires generate()')
        if provider.id in self._providers:
            raise RuntimeError(
                f'session-title provider "{provider.id}" is already registered')
        self._providers[provider.id] = provider

        def dispose() -> None:
            self._providers.pop(provider.id, None)

        return dispose

    # ---------- 自动调度 ----------

    def _on_session_event(self, payload: dict) -> None:
        session = payload.get("session")
        event = payload.get("event")
        if session is None or event is None:
            return
        etype = event.get("type")
        if etype == "user/message":
            self._on_user_message(session, event)
        elif etype == "request/header":
            self._on_request_header(session, event)

    def _on_user_message(self, session: Session, event: dict) -> None:
        if not self._live(session):
            return
        message = _title_user_message_of(event)
        if message is None:
            return
        current = self.get(session)
        if current is not None and current["source"].get("kind") == "user":
            return  # 用户钉扎：停止自动生成调度
        # 先按「尚无标题」判定是否调度提供者（对齐上游 onUserMessage：pending
        # 在 fallback 落之前设置——上游 ensureFallback 是 deferred 任务）；随后
        # fallback 恒先落。
        self._maybe_schedule_provider(session, event.get("seq"))
        self._ensure_fallback(session)

    def _maybe_schedule_provider(self, session: Session, seq: int) -> None:
        provider = next((p for p in self._providers.values()
                         if p.automatic == "first-prompt"), None)
        if provider is None:
            return
        if self.get(session) is not None:
            return
        header = session.meta
        if header.get("parentSession") is not None:
            return  # fork 不自动首提示
        state = self._title_input(session)
        if state["count"] != 1:
            return
        self._pending = {"session": session, "throughSeq": seq, "provider": provider}

    def _on_request_header(self, session: Session, event: dict) -> None:
        pending = getattr(self, "_pending", None)
        if pending is None or pending["session"] is not session:
            return
        if pending["throughSeq"] >= event["seq"]:
            return
        self._pending = None
        self._start_provider(session, pending["provider"])

    def _start_provider(self, session: Session, provider: SessionTitleProvider) -> None:
        messages = collect_title_messages(list(session.events))
        try:
            self._run_provider(session, provider, messages)
        except Exception as error:  # noqa: BLE001 - 自动生成失败保留 fallback
            logger = getattr(self.ctx, "logger", None)
            if logger is not None and hasattr(logger, "warn"):
                logger.warn(f'session "{session.session_id}": automatic title '
                            f'generation failed: {error}')

    def _run_provider(self, session: Session, provider: SessionTitleProvider,
                      messages: list) -> dict | None:
        if self.adapter is None:
            return None
        request = {"session": session, "messages": messages,
                   "route": self._route_of(session)}
        result = provider.generate(request)
        validated = self._validate_result(provider, result, messages)
        if validated is None:
            return None
        accepted = validated["title"]
        source: dict = {"kind": "provider", "provider": provider.id}
        if validated.get("model"):
            source["model"] = validated["model"]
        session.append("session/title", {
            "title": accepted,
            "messageSeqs": list(validated["messageSeqs"]),
            "source": source,
        })
        return validated

    def _validate_result(self, provider: SessionTitleProvider, result: Any,
                         messages: list) -> dict | None:
        if not isinstance(result, dict):
            raise RuntimeError("session-title provider returned an invalid result")
        title = result.get("title")
        if not isinstance(title, str):
            raise RuntimeError("session-title provider title must be a string")
        normalized = normalize_title(title, self.config["maxTitleBytes"])
        if normalized == "":
            raise RuntimeError("session-title provider returned an empty title")
        seqs = result.get("messageSeqs")
        if not isinstance(seqs, (list, tuple)) or len(seqs) == 0:
            raise RuntimeError(
                "session-title provider must identify at least one source message seq")
        message_seqs = [s for s in messages]
        allowed = {m["seq"] for m in message_seqs}
        previous = None
        for seq in seqs:
            if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0 \
                    or seq not in allowed:
                raise RuntimeError(
                    "session-title provider messageSeqs must be unique, ordered seqs "
                    "from the request")
            if previous is not None and seq <= previous:
                raise RuntimeError(
                    "session-title provider messageSeqs must be unique, ordered seqs "
                    "from the request")
            previous = seq
        model = result.get("model")
        if model is not None:
            if not isinstance(model, dict) \
                    or not isinstance(model.get("provider"), str) \
                    or not isinstance(model.get("model"), str) \
                    or model["provider"] == "" or model["model"] == "":
                raise RuntimeError(
                    "session-title provider result model must contain non-empty "
                    "provider and model strings")
        return {"title": normalized, "messageSeqs": list(seqs), "model": model}

    def _route_of(self, session: Session) -> dict | None:
        context = session.request_context()
        if context is not None:
            route = {"provider": context.get("provider"), "model": context.get("model")}
            if route["provider"] and route["model"]:
                return route
        return None

    # ---------- fallback ----------

    def _ensure_fallback(self, session: Session) -> None:
        if self.get(session) is not None:
            return
        first = self._title_input(session).get("first")
        if first is None:
            return
        self._append_fallback(session, first)

    def _append_fallback(self, session: Session, first: dict) -> None:
        title = fallback_title(first["text"], self.config["fallbackMaxWords"],
                               self.config["fallbackMaxBytes"])
        if title == "":
            return
        session.append("session/title", {
            "title": title,
            "messageSeqs": [first["seq"]],
            "source": {"kind": "fallback"},
        })

    # ---------- 投影 ----------

    def _title_input(self, session: Session) -> dict:
        registry = self.ctx.get("sessionProjections")
        if registry is not None:
            state = registry.state_of(session, "titleInput")
            if state is not None:
                return state
        state = {"first": None, "count": 0, "lastSeq": None}
        for event in session.events:
            message = _title_user_message_of(event)
            if message is None:
                continue
            if state["first"] is None:
                state["first"] = message
            state["count"] += 1
            state["lastSeq"] = message["seq"]
        return state

    def _register_title_input_projection(self) -> None:
        registry = self.ctx.get("sessionProjections")
        if registry is None:
            return

        def init(header, inherited_event_count):
            return {"first": None, "count": 0, "lastSeq": None}

        def apply(state, event):
            message = _title_user_message_of(event)
            if message is None:
                return state
            return {"first": state["first"] if state["first"] is not None else message,
                    "count": state["count"] + 1,
                    "lastSeq": message["seq"]}

        self._title_input_disposer = registry.register(ProjectionDefinition(
            "titleInput", init=init, apply=apply, state_version=3))

    def _register_title_projection(self) -> None:
        registry = self.ctx.get("sessionProjections")
        if registry is None:
            return

        def init(header, inherited_event_count):
            return None

        def apply(state, event):
            if event.get("type") == "session/title":
                return (event.get("data") or {}).get("title")
            return state

        self._title_disposer = registry.register(ProjectionDefinition(
            "title", init=init, apply=apply, state_version=1,
            state_schema=lambda v: v if v is None or isinstance(v, str) and v else
            (_ for _ in ()).throw(ValueError("session title must be a string")),
            view=lambda state: state))

    # ---------- 生命周期 ----------

    def _live(self, session: Session) -> bool:
        sessions = self.ctx.get("sessions")
        if sessions is None:
            return True
        return sessions.get(session.session_id) is session

    def dispose(self) -> None:
        for fn in reversed(self._disposers):
            fn()


def install_session_title(ctx: Context, config: dict | None = None,
                          adapter: DeepSeekAdapter | None = None) -> SessionTitleService:
    """装配 ctx.sessionTitle（幂等，重复装返回既有实例）。"""
    existing = ctx.get("sessionTitle")
    if existing is not None:
        return existing
    return SessionTitleService(ctx, config, adapter=adapter)


def register_first_prompt_llm_provider(ctx: Context, adapter: DeepSeekAdapter,
                                       config: dict | None = None) -> Callable[[], None]:
    """注册 first-prompt LLM 标题提供者（session-title-first-prompt-llm）。

    @param config - {targetWords, targetCjkCharacters, maxInputBytes,
    maxOutputTokens, timeoutMs, provider?, model?}；provider/model 可缺省（走
    请求路由），必须成对。
    """
    from ..llm import LlmFailure as _LF  # noqa: F401

    config = dict(config or {})
    for name in ("targetWords", "targetCjkCharacters", "maxInputBytes",
                 "maxOutputTokens", "timeoutMs"):
        if not isinstance(config.get(name), int) or isinstance(config.get(name), bool) \
                or config[name] <= 0:
            raise ValueError(f"session-title-llm: {name} must be a positive integer")
    if (config.get("provider") is None) != (config.get("model") is None):
        raise ValueError("session-title-llm: provider and model must be supplied together")

    def generate(request: dict) -> dict:
        messages = request["messages"]
        if not messages:
            raise RuntimeError("first-prompt title provider requires one human message")
        first = messages[0]
        return _generate_with_llm(ctx, adapter, config, request, [first])

    service = ctx.get("sessionTitle")
    if service is None:
        raise RuntimeError(
            "session-title-first-prompt-llm: the sessionTitle service is required")
    return service.register(SessionTitleProvider(
        "session-title-first-prompt-llm", "first-prompt", generate))


def _generate_with_llm(ctx: Context, adapter: DeepSeekAdapter, config: dict,
                       request: dict, selected: list) -> dict:
    """generateSessionTitleWithLlm（session-title-llm/src/index.ts:238-303）。"""
    if not selected:
        raise RuntimeError("session-title-llm: at least one source message is required")
    route = _resolve_title_route(config, request)
    system = _title_system_prompt(config)
    framed = _frame_title_messages(selected)
    input_bytes = len(framed.encode("utf-8"))
    if input_bytes > config["maxInputBytes"]:
        raise RuntimeError(
            f"session-title-llm: input is {input_bytes} bytes, exceeding "
            f"maxInputBytes {config['maxInputBytes']}")
    session = request["session"]
    session.append("session/title-llm-request", {
        "titleProvider": "session-title-first-prompt-llm",
        "messageSeqs": [m["seq"] for m in selected],
        "route": route,
        "system": system,
        "messages": [{"seq": m["seq"], "text": m["text"]} for m in selected],
        "maxTokens": config["maxOutputTokens"],
    })
    messages = [{
        "id": f"title-{session.session_id}",
        "role": "user",
        "content": [{"type": "text", "text": framed}],
        "source": {"kind": "dsh-session-title-llm"},
    }]
    assembler = BlockAssembler()

    async def _consume() -> None:
        try:
            # session-title-llm/src/index.ts:267-268：盖会话身份 +
            # purpose='session-title'（deepseek 侧只映射 compaction 头）。
            stream = adapter.stream(
                messages, [], session_id=session.session_id,
                purpose="session-title")
            async for chunk in stream:
                assembler.push(chunk)
        except LlmFailure as error:
            raise RuntimeError(error.failure["message"]) from error

    from ..core.agent_loop.resident_loop import run_on_resident
    run_on_resident(_consume())

    finish = assembler.finish
    kind = finish.get("kind")
    if kind == "error" or kind == "aborted":
        failure = finish.get("failure") or {}
        raise RuntimeError(failure.get("message") or "session-title-llm: request failed")
    if kind == "max-tokens":
        raise RuntimeError("session-title-llm: title output reached maxOutputTokens")
    if kind == "tool-calls":
        raise RuntimeError("session-title-llm: title model unexpectedly requested a tool")
    blocks = assembler.blocks()
    for block in blocks:
        if block.get("type") == "tool-call":
            raise RuntimeError("session-title-llm: title output must contain text only")
    text = " ".join(b.get("text", "") for b in blocks
                    if b.get("type") == "text")
    normalized = normalize_title(text, 2 ** 63 - 1)
    if normalized == "":
        raise RuntimeError("session-title-llm: title model produced no text")
    return {"title": normalized, "messageSeqs": [m["seq"] for m in selected],
            "model": route}


def _resolve_title_route(config: dict, request: dict) -> dict:
    if config.get("provider") is not None:
        return {"provider": config["provider"], "model": config["model"]}
    route = request.get("route")
    if route is not None and route.get("provider") and route.get("model"):
        return route
    raise RuntimeError(
        "session-title-llm: no logged request route is available; "
        "configure provider and model together")


def _title_system_prompt(config: dict) -> str:
    return (
        "Create a concise title for an AI coding-assistant session from the "
        "supplied human messages.\nReturn only the title on one line, in plain "
        "text of natural language, with no quotes, prefix, explanation, Markdown, "
        "XML, or terminal control codes. No code is allowed.\nUse the language of "
        "the messages.\nAim for about "
        f"{config['targetWords']} words in non-CJK languages or "
        f"{config['targetCjkCharacters']} CJK characters."
    )


def _frame_title_messages(messages: list) -> str:
    import json
    return ("Generate the session title from this JSON array of human messages:\n"
            + json.dumps([{"seq": m["seq"], "text": m["text"]} for m in messages]))