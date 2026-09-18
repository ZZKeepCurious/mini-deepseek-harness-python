"""Agent 作用域 Schedule 串行化（对齐 packages/schedule/schedule/src/transaction.ts）。

per-agent 尾链：同一 agent 的多个事务（工具 FIFO 前写 + 运行时 driveOnce）
严格按发起序串行——后者等待前者的完成 Promise；跨 agent 天然并行。上游用
WeakMap<Agent, Promise<void>>；mini 键载体：可弱引用的 owner 直接进
WeakKeyDictionary（agent 回收即自动清理，等价 WeakMap）；不可弱引用的合成
owner 以 ``("session-id", id)`` 进强引用 dict 并在事务闭合时摘除（同样无泄漏——
载体差异登记 verified-diffs §2.35）。
"""
from __future__ import annotations

import asyncio
import inspect
import weakref
from typing import Any, Awaitable, Callable

__all__ = ["run_schedule_transaction"]

#: per-agent 尾链（agent → 最近一次事务完成后的 Future）。弱引用：agent 回收即清。
_tails_weak: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
#: 不可弱引用 owner 的强引用尾链（键含 session_id，事务闭合即摘）。
_tails_strong: dict[Any, Any] = {}


def _serial_key(agent: Any) -> tuple[Any, Any]:
    """返回 (串行化键, 尾链表)：可弱引用 owner 用对象本身进 WeakKeyDictionary
    （agent 回收即自动清理，等价上游 WeakMap<Agent, Promise>）；不可弱引用的
    合成 owner 用 (``session-id``, session_id) 进强引用 dict（事务闭合即摘）。"""
    try:
        weakref.ref(agent)
    except TypeError:
        return ("session-id", getattr(agent, "session_id", None)), _tails_strong
    return agent, _tails_weak


async def run_schedule_transaction(agent: Any,
                                   operation: Callable[[], Any]) -> Awaitable[Any]:
    """在其精确 agent 的前一个事务完成后执行一个完整事务。

    @param agent - Schedule 精确 owner 与串行化键（AgentLoop 或携带唯一
        ``session_id`` 的等价物）。
    @param operation - 完整 preflight/fold/mutation/postflight 操作（可同步或
        async，结果以此风格返回）。
    @returns 可 await 的执行结果；operation 异常向上冒泡（调用方各自映射）。
    """
    loop = asyncio.get_running_loop()
    key, store = _serial_key(agent)

    prior = store.get(key)
    if prior is None or prior.done():
        prior = loop.create_future()
        prior.set_result(None)

    result = loop.create_future()
    tail = loop.create_future()
    store[key] = tail

    async def runner() -> None:
        try:
            await prior
        except BaseException:  # 前序失败不阻断本事务（上游 prior.then(operation)）
            pass
        try:
            value = operation()
            if inspect.isawaitable(value):
                value = await value
        except BaseException as error:  # 向上冒泡；尾链照常闭合
            if not result.done():
                result.set_exception(error)
        else:
            if not result.done():
                result.set_result(value)
        finally:
            # 尾链闭合与清理绑定在 runner 完成上，而非 await result 的调用方：
            # awaiter 被取消不摧毁事务，后继事务仍须等本事务跑完（上游 Promise
            # 不可取消语义）。仅当无人接棒时才摘除，避免踩掉新尾链。
            if not tail.done():
                tail.set_result(None)
            if store.get(key) is tail:
                store.pop(key, None)

    loop.create_task(runner())

    return await result
