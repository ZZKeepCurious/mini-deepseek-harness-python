"""repeat-tool-reminder 守卫：逐 agent 重复调用检测（对齐 upstream packages/guard/repeat-tool-reminder）。

上游以 cordis `tools/post-execute` + `agent/pre-step` 监听器工作：per-agent
WeakMap 连续链（key = JSON([toolName, canonicalArgs])），命中配置阈值时在
post-execute 决策的 additionalContexts 前置 user/message 提醒（不否决、不
改写调用）；denied 调用同样计数（上游 deny 流经同一 post-execute 管线），
模型轰炸被拒调用正是值得打断的循环；用户消息打断 → pre-step 清链。

mini 差异（载体映射，见 verified-diffs §2.33）：
  * mini 的 tools/pre-execute deny 短路 post-execute（调度器拒绝后不再执行
    执行体）。为不丢 denied 计数，本守卫在 pre-execute 监听器先委派再仅在
    下游决策 kind=='deny' 时计数，并把提醒直接挂到 exec_.additional_contexts
    （deny 决策对象不被管线消费；拒绝结果显示后由调度器落为 user/message）。
  * accepted 路径照上游在 post-execute 折叠到决策 additionalContexts，
    由管线复制到 exec_.additional_contexts。
  * direct `ctx.tools.execute()` 调用（exec.agent 为空）与 include/exclude
    未命中的调用透明处理：既不计数也不重置（上游 tracked() 语义）。

契约（上游 index.ts:17-232，已核实）：
  * 阈值默认 [3,5,8]；fail-loud 校验（空 / 非整数 / <2 / 重复 → 抛错）；
    argumentsPreviewChars 默认 500（非整数或 <1 抛错）
  * canonical = 深键序 JSON.stringify；预览头截断 + '… (+N more chars)'
  * 命中 thresholds[0] 用 gentle 文本，其余用 detailed 文本（含 tool 名/
    run 长度/canonical 参数预览）
  * source：{kind:'plugin', plugin:'repeat-tool-reminder', form:'notice',
    summary:'{tool} × {count}'}
  * `*` 通配符 pattern（anchor 化，其余 regex 元字符字面匹配）
"""
from __future__ import annotations

import json
import re
import weakref
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core.scope import Context
from ..core.session import create_message, text_block

__all__ = [
    "Config",
    "install_repeat_tool_reminder",
    "name",
]

name = "repeat-tool-reminder"


def _reminder_block(text: str) -> list[dict]:
    return [text_block(text)]


@dataclass
class Config:
    """repeat-tool-reminder 配置（对齐上游 Config：thresholds/include/exclude/
    argumentsPreviewChars）。install 时 fail-loud 校验，缺省字段补齐默认值。"""

    thresholds: list[int] = field(default_factory=lambda: [3, 5, 8])
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    argumentsPreviewChars: int = 500


_GENTLE_REMINDER = (
    "You are repeating the exact same tool call with identical arguments. "
    "Carefully analyze the previous result before calling again: if the task is "
    "not complete, try a different approach or different arguments instead of "
    "repeating the call."
)


def _detailed_reminder(tool_name: str, count: int, canonical_arguments: str) -> str:
    return (
        "Repeated tool call detected:\n"
        f"- tool: {tool_name}\n"
        f"- consecutive_calls: {count}\n"
        f"- arguments: {canonical_arguments}\n"
        "The repeated calls are not making progress. Do not call this tool with "
        "these exact arguments again. Inspect the latest result and choose a "
        "different action, different arguments, or finish the task if enough "
        "evidence has been gathered."
    )


from collections.abc import Mapping

def sort_json_value(value: Any) -> Any:
    """深键序排序（上游 sortJsonValue：数组递归、对象键排序、其余原样）。

    mini 的执行参数经 deep_freeze 为 mappingproxy 冻结视图，此处先解包为
    dict 再递归键序——上游 JSON.parse 输出是普通对象，语义同构。
    """
    if isinstance(value, list):
        return [sort_json_value(item) for item in value]
    if isinstance(value, Mapping):
        return {key: sort_json_value(value[key]) for key in sorted(value.keys())}
    return value


def canonicalize(arguments_value: Any) -> str:
    """参数 canonical 串：深键序后 JSON 序列化（上游 canonicalize）。"""
    return json.dumps(sort_json_value(arguments_value))


def wildcard_to_regexp(pattern: str) -> re.Pattern[str]:
    """`*` 通配符 → 锚定正则（其余 regex 元字符全部字面转义，上游 wildcardToRegExp）。"""
    escaped = re.escape(pattern).replace(r"\*", ".*")
    return re.compile(f"^{escaped}$")


def preview_arguments(canonical: str, cap: int) -> str:
    """头截断 canonical 参数供 detailed 提醒引用（上游 previewArguments）。
    只限制模型可见文本——链键始终用完整 canonical 串。"""
    if len(canonical) <= cap:
        return canonical
    return f"{canonical[:cap]}… (+{len(canonical) - cap} more chars)"


def _validate_thresholds(values: list[Any]) -> list[int]:
    """fail-loud 校验 thresholds 并升序归一（上游 validateThresholds）。"""
    if not values:
        raise ValueError("repeat-tool-reminder: `thresholds` must not be empty")
    for value in values:
        if not isinstance(value, int) or isinstance(value, bool) or value < 2:
            raise ValueError(
                f"repeat-tool-reminder: invalid threshold {value!r} — every threshold must be an integer >= 2")
    if len(set(values)) != len(values):
        raise ValueError("repeat-tool-reminder: `thresholds` must not contain duplicates")
    return sorted(values)


_PLUGIN_SOURCE = {"kind": "plugin", "plugin": "repeat-tool-reminder"}


def _reminder_message(tool_name: str, count: int, text: str) -> dict:
    """构造提醒 user/message 形状（{role, content, source}，对齐上游
    createUserMessage：source 携 form/summary——label 是承重的，无标签
    上下文在派生历史里会渲染成用户提示）。"""
    return create_message(
        "user",
        _reminder_block(text),
        {**_PLUGIN_SOURCE, "form": "notice", "summary": f"{tool_name} × {count}"},
    )


class _Chain:
    __slots__ = ("key", "count")

    def __init__(self, key: str, count: int) -> None:
        self.key = key
        self.count = count


def install_repeat_tool_reminder(ctx: Context, config: Config | None = None) -> None:
    """在 ctx 上安装三个监听器（upstream apply；disposer 随 ctx 存续）。

    校验 fail-loud：空 thresholds / 非整数 / <2 / 重复 / argumentsPreviewChars
    非整数或 <1 → 抛 ValueError（对齐上游加载时抛错，不静默回退）。
    返回 ctx.on 的 disposer 列表（每个监听器一个）。
    """
    cfg = config or Config()
    thresholds = _validate_thresholds(cfg.thresholds)
    threshold_set = set(thresholds)
    include_patterns = [wildcard_to_regexp(p) for p in cfg.include or []]
    exclude_patterns = [wildcard_to_regexp(p) for p in cfg.exclude or []]
    preview_chars = cfg.argumentsPreviewChars
    if not isinstance(preview_chars, int) or isinstance(preview_chars, bool) or preview_chars < 1:
        raise ValueError(
            f"repeat-tool-reminder: invalid argumentsPreviewChars {preview_chars!r} — must be an integer >= 1")

    chains: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

    def tracked(tool_name: str) -> bool:
        """工具是否参与链（未命中透明：既不计数也不重置，上游 tracked()）。"""
        if include_patterns and not any(p.match(tool_name) for p in include_patterns):
            return False
        return not any(p.match(tool_name) for p in exclude_patterns)

    def canonical_key(exec_: Any) -> str:
        return json.dumps([exec_.name, canonicalize(exec_.arguments)])

    def observe(exec_: Any) -> dict | None:
        """为一次 attempt 推进调用方 agent 的链，命中阈值返回提醒消息。

        direct 调用（无 agent）无模型可提醒、无键可依，不参与（上游
        exec.agent 缺失即 return undefined）。
        """
        agent = getattr(exec_, "agent", None)
        if agent is None:
            return None
        if not tracked(exec_.name):
            return None
        key = canonical_key(exec_)
        chain = chains.get(agent)
        count = chain.count + 1 if chain is not None and chain.key == key else 1
        chains[agent] = _Chain(key, count)
        if count not in threshold_set:
            return None
        text = _GENTLE_REMINDER if count == thresholds[0] else _detailed_reminder(
            exec_.name, count, preview_arguments(json.loads(key)[1], preview_chars))
        return _reminder_message(exec_.name, count, text)

    def _decision_is_block(decision: Any) -> bool:
        return isinstance(decision, dict) and (
            decision.get("kind") == "block" or decision.get("action") == "block")

    def _on_pre_execute(payload: Any, next_: Callable) -> Any:
        # 先委派（下游可能 block/ask/deny），再仅对 kind=='deny' 计数——
        # mini 的 deny 短路 post-execute；正常调用由 post-execute 计数。
        decision = next_()
        if not (isinstance(decision, dict) and decision.get("kind") == "deny"):
            return decision
        exec_ = payload.get("exec")
        if exec_ is None or getattr(exec_, "agent", None) is None:
            return decision
        if not tracked(payload.get("tool")):
            return decision
        reminder = observe(exec_)
        if reminder is None:
            return decision
        exec_.additional_contexts.append(reminder)
        return decision

    async def _on_post_execute(payload: Any, next_: Callable) -> Any:
        exec_ = payload.get("exec")
        # 先计数（状态无条件推进，见上游 observe 在 delegate 前），再委派，
        # 然后把提醒前置到下游 additionalContexts（blocked 调用同样被提醒）
        reminder = observe(exec_) if exec_ is not None else None
        downstream = await next_()
        if reminder is None:
            return downstream
        if _decision_is_block(downstream):
            return {
                "kind": "block",
                "feedback": downstream.get("feedback"),
                "additionalContexts": [reminder, *(downstream.get("additionalContexts") or [])],
            }
        if isinstance(downstream, dict):
            return {
                **downstream,
                "additionalContexts": [reminder, *(downstream.get("additionalContexts") or [])],
            }
        return {"kind": "accept", "additionalContexts": [reminder]}

    def _on_pre_step(payload: Any, next_: Callable) -> Any:
        # 用户消息打断 → 清链（纯重置钩子：不附加、不否决）
        messages = payload.get("messages", [])
        if any(isinstance(m, dict) and (m.get("source") or {}).get("kind") == "user"
               for m in messages):
            chains.clear()
        return next_()

    ctx.on("tools/pre-execute", _on_pre_execute)
    ctx.on("tools/post-execute", _on_post_execute)
    ctx.on("agent/pre-step", _on_pre_step)