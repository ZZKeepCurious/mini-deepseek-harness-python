"""workflow 能力接缝：WorkflowEngine 服务定义 + 事件词表 + 错误分类。

上游：packages/workflow/workflow/src/index.ts（203 行）+ types.ts + runtime-types.ts。

语义（已核实）：
  * `WorkflowEngine`（`ctx.workflowEngine`）抽象服务：`start(request)` 返回
    `WorkflowRun`（id/meta/result/cancel/dispose）。`workflow/*` 事件是
    **只读数据快照**（永远不是 live run；workflow/end 刻意省略 value）。
  * 六个事件：workflow/start、phase、log、agent-start、agent-end、end。
  * `WorkflowError` + 11 错误码闭集（SCRIPT_PARSE/META_INVALID/INVALID_ARGUMENT/
    UNSUPPORTED_OPTION/UNSUPPORTED_SCHEMA/AGENT_CAP/ITEM_CAP/AGENT_START/
    AGENT_RESULT/RESULT_UNSERIALIZABLE/CANCELLED），缺省 fatal。
  * `WorkflowRunId` 品牌工厂、`WorkflowMeta`（name/description/whenToUse/phases）
    shape 校验（META_INVALID）。

载体差异：mini 工作流脚本为 **Python**（上游为 JavaScript）；执行引擎经
`PythonPtcRuntime` 绑定面承载（见 workflow_ptc）。本层纯声明，无执行逻辑。
"""
from __future__ import annotations

import uuid
from typing import Any, Callable

from ..core.scope import Context, Service

__all__ = [
    "META_FIELDS",
    "WorkflowEngine",
    "WorkflowError",
    "WorkflowRunId",
    "WorkflowStopReasons",
    "is_fatal_workflow_error",
    "validate_meta",
]

#: 事件名闭集（index.ts:36-90）。
WORKFLOW_EVENT_NAMES = (
    "workflow/start",
    "workflow/phase",
    "workflow/log",
    "workflow/agent-start",
    "workflow/agent-end",
    "workflow/end",
)

#: stop reason 闭集（types.ts:63）。
WorkflowStopReasons = ("completed", "cancelled", "error")

#: WorkflowError 码闭集（index.ts:108-119）。
WORKFLOW_ERROR_CODES = (
    "SCRIPT_PARSE",
    "META_INVALID",
    "INVALID_ARGUMENT",
    "UNSUPPORTED_OPTION",
    "UNSUPPORTED_SCHEMA",
    "AGENT_CAP",
    "ITEM_CAP",
    "AGENT_START",
    "AGENT_RESULT",
    "RESULT_UNSERIALIZABLE",
    "CANCELLED",
)

#: meta 可识别字段（meta.ts:13-65）。
META_FIELDS = ("name", "description", "whenToUse", "phases")
META_PHASE_FIELDS = ("title", "detail", "provider", "model")


def WorkflowRunId(id: str) -> str:
    """品牌工厂：铸一个 WorkflowRunId（上游 types.ts:20，返回同一字符串）。"""
    return id


class WorkflowError(Exception):
    """工作流失败：code 在闭集内、缺省 fatal；fatality 由宿主判读。"""

    def __init__(self, message: str, code: str,
                 cause: BaseException | None = None, fatal: bool = True):
        super().__init__(message)
        self.name = "WorkflowError"
        self.code = code
        self.fatal = fatal
        if cause is not None:
            self.__cause__ = cause


def is_fatal_workflow_error(error: Any) -> bool:
    """fatality 判定（index.ts:146-148）：宿主侧 instanceof 等价（脚本 realm
    造不出伪造对象）。"""
    return isinstance(error, WorkflowError) and error.fatal


def _collect_meta_violations(value: Any) -> list[str]:
    """meta shape 校验（meta.ts:13-65）：逐条收集违规，绝不先失败。"""
    if not isinstance(value, dict):
        return ["meta must be an object"]
    violations: list[str] = []
    for key in value:
        if key not in META_FIELDS:
            violations.append(
                f"meta.{key} is not a recognized field "
                "(name/description/whenToUse/phases)")
    name = value.get("name")
    if not isinstance(name, str) or name == "":
        violations.append("meta.name must be a non-empty string")
    description = value.get("description")
    if not isinstance(description, str) or description == "":
        violations.append("meta.description must be a non-empty string")
    when_to_use = value.get("whenToUse")
    if when_to_use is not None and not isinstance(when_to_use, str):
        violations.append("meta.whenToUse must be a string")
    phases = value.get("phases")
    if phases is not None:
        if not isinstance(phases, (list, tuple)):
            violations.append("meta.phases must be an array")
        else:
            for index, phase in enumerate(phases):
                if not isinstance(phase, dict):
                    violations.append(f"meta.phases[{index}] must be an object")
                    continue
                for key in phase:
                    if key not in META_PHASE_FIELDS:
                        violations.append(
                            f"meta.phases[{index}].{key} is not a recognized field")
                title = phase.get("title")
                if not isinstance(title, str) or title == "":
                    violations.append(
                        f"meta.phases[{index}].title must be a non-empty string")
                for key in ("detail", "provider", "model"):
                    if phase.get(key) is not None and not isinstance(phase.get(key), str):
                        violations.append(
                            f"meta.phases[{index}].{key} must be a string")
    return violations


def validate_meta(value: Any) -> dict:
    """校验并归一化 meta（meta.ts:76-81）：违规聚合抛 META_INVALID。"""
    violations = _collect_meta_violations(value)
    if violations:
        raise WorkflowError(f"invalid meta: {'; '.join(violations)}", "META_INVALID")
    normalized: dict[str, Any] = {
        "name": value["name"],
        "description": value["description"],
    }
    if value.get("whenToUse") is not None:
        normalized["whenToUse"] = value["whenToUse"]
    if value.get("phases") is not None:
        normalized["phases"] = [
            {k: v for k, v in phase.items() if k in META_PHASE_FIELDS}
            for phase in value["phases"]
        ]
    return normalized


class WorkflowEngine(Service):
    """`ctx.workflowEngine` 服务定义（index.ts:157-187）：抽象 start + 事件发射。

    上游为抽象类，mini 同样只声明契约；执行引擎在 workflow_ptc 实现。
    """

    provide = "workflowEngine"

    def __init__(self, ctx: Context):
        super().__init__(ctx, "workflowEngine")

    def start(self, request: dict) -> Any:
        """开始一次工作流运行（实现类覆盖）。"""
        raise NotImplementedError

    # ---------- 事件发射（index.ts:175-186，各自 contain） ----------

    def emit_workflow_event(self, name: str, *args) -> None:
        """发射一个 workflow 事件：单参载荷原样；多参载荷以 tuple 派发
        （对齐上游 ctx.events.dispatch('emit', [name, ...args])）。监听器异常
        contained。"""
        payload = args[0] if len(args) == 1 else (args if args else None)
        try:
            self.ctx.emit(name, payload)
        except Exception as error:  # noqa: BLE001 - 上游各自 contain + warn
            logger = getattr(self.ctx, "logger", None)
            if logger is not None and hasattr(logger, "warn"):
                label = str(error) if error is not None else "[unrenderable thrown value]"
                logger.warn(f"workflow: {name} listener threw: {label}")


def new_workflow_run_id() -> str:
    return WorkflowRunId(str(uuid.uuid4()))


def observer_dispatch(engine: WorkflowEngine) -> Callable[[str, dict], None]:
    """把 observer 回调（phase/log/agent-start/agent-end）接到引擎事件发射。"""
    def dispatch(kind: str, payload: dict) -> None:
        info = payload.get("info")
        if kind == "phase":
            engine.emit_workflow_event("workflow/phase", info, payload.get("title"))
        elif kind == "log":
            engine.emit_workflow_event("workflow/log", info, payload.get("message"))
        elif kind == "agent-start":
            engine.emit_workflow_event("workflow/agent-start", info, payload.get("agent"))
        elif kind == "agent-end":
            engine.emit_workflow_event("workflow/agent-end", info, payload.get("agent"))
    return dispatch