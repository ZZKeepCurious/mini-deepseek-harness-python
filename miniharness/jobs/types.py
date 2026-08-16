"""后台作业共享类型与常量（对齐 packages/jobs/jobs/src/types.ts + brand.ts）。

契约要点（与上游逐条一致）：
  * JobStatus = 'running' | 'stopping' | 'completed' | 'killed' | 'failed'
  * id 由注册表生成 `<kind>-N`（品牌化 JobId；可预测，授权靠 owner 会话 id 而非保密）
  * JobOutcome.status 只允许三种终态；detail 是 kind 特有状态说明
  * JobHooks.done 由 producer 结算（reject → 注册表转 failed），readOutput 区分
    流式 job（每次读消费增量）与 final-output job（结算后幂等读）
  * JobSnapshot 是只读投影（每次调用新对象，绝不给活注册表状态）
  * reported：kill / 终态 read / wait / teardown cancel 后置位，完成 notice 据此抑制
"""
from __future__ import annotations

import concurrent.futures
import threading
from typing import Any, Callable

# 三个终态（对齐 jobs-local/src/index.ts:66 isTerminal）
TERMINAL_STATUSES = frozenset({"completed", "killed", "failed"})
# 默认每精确 owner（或共享 unowned 桶）的 running+stopping 上限（jobs-local/index.ts:28）
DEFAULT_MAX_CONCURRENT_JOBS_PER_OWNER = 10
# 区分"等待超时"与"调用方取消"的 scoped deadline 码（jobs-local/index.ts:25）
TASK_WAIT_TIMEOUT = "TASK_WAIT_TIMEOUT"


class JobDoneBox:
    """`JobHooks.done` 的 Promise 替身（mini 同步模型无原生 Promise）。

    producer 在其完成线程调用 :meth:`settle`（等价 resolve）或 :meth:`fail`
    （等价 reject）；注册表通过 :meth:`add_done_callback` 挂结算回调
    （等价上游 `hooks.done.then(...)`）。线程安全，仅结算一次。
    """

    def __init__(self) -> None:
        self._future: "concurrent.futures.Future" = concurrent.futures.Future()

    def settle(self, outcome: dict) -> None:
        """以终态 outcome 结算（first-wins：已结算则忽略）。"""
        if not self._future.done():
            self._future.set_result(outcome)

    def fail(self, error: BaseException) -> None:
        """以异常结算；注册表将把 reject 转成 {status:'failed', detail}。"""
        if not self._future.done():
            self._future.set_exception(error)

    def add_done_callback(self, fn: Callable[["JobDoneBox"], None]) -> None:
        """登记结算回调（收到已结算的 box 自身）；等价 done.then。"""
        self._future.add_done_callback(lambda _f: fn(self))

    def done(self) -> bool:
        return self._future.done()

    def wait(self, timeout: float | None = None) -> bool:
        """阻塞至结算或超时，返回是否已结算（异常结算也算）。"""
        try:
            self._future.result(timeout)
        except (concurrent.futures.TimeoutError, TypeError):
            return False
        except BaseException:
            return True
        return True

    def result(self) -> Any:
        """返回 outcome；reject 时抛异常（调用方须捕获）。"""
        return self._future.result()


def job_id(kind: str, count: int) -> str:
    """注册表 id 生成：`<kind>-N`（对齐 brand.ts JobId 语义）。"""
    return f"{kind}-{count}"
