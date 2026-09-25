"""后台作业共享类型与常量（对齐 packages/jobs/jobs/src/{types,brand}.ts）。

契约要点（与上游逐条一致）：
  * JobStatus = 'running' | 'stopping' | 'completed' | 'killed' | 'failed'
  * id 由注册表生成 `<kind>-N`（品牌化 JobId；可预测，授权靠 owner 会话 id 而非保密）
  * JobOutcome.status 只允许三种终态；detail 是终态原因；result 是"值型结果"
    （workflow 渲染结果 / 子代理报告），由结算后第一次 read 交出一次；输出流走
    ring，不再经 outcome 承载
  * JobSpec.run(handle) 收到 JobHandle（id / append / updateProgress）；producer
    在 handle 上推送叙述型输出，pull 源（JobOutputSource）由注册表按 cadence 泵入
  * settlement 由 cause（producer|kill|teardown）+ awaited 描述；`reported` 标志删除
  * JobEvents.subscribe(filter, listener)：{owner SessionId} 或 {owners:'all'|'scope'}

载体说明（登录 verified-diffs）：mini 无原生 Promise，done 用 JobDoneBox（Future）承载；
SessionId 在 mini 是裸字符串（上游为品牌化 string，语义等价）。
"""
from __future__ import annotations

import concurrent.futures
from typing import Any, Callable

__all__ = [
    "DEFAULT_MAX_CONCURRENT_JOBS_PER_OWNER",
    "DEFAULT_PUMP_POLL_MS",
    "DEFAULT_RETAIN_BYTES",
    "DEFAULT_SETTLED_RETAIN_BYTES",
    "JOB_CHANNELS",
    "JobDoneBox",
    "JobHandle",
    "SESSION_ID_QUERY_UNSUPPORTED",
    "TERMINAL_STATUSES",
    "TASK_WAIT_TIMEOUT",
    "job_id",
]

#: SessionId 在 mini 是裸字符串（会话 id）；上游为 brand.ts 的品牌类型。
SessionId = str

#: 三个终态（对齐 jobs/src/view.ts JobStatus 的终态子集 + jobs-local isTerminal）。
TERMINAL_STATUSES = frozenset({"completed", "killed", "failed"})
#: 输出块流标签（对齐 view.ts JobChannel）。
JOB_CHANNELS = frozenset({"stdout", "stderr", "log"})

#: 默认每精确 owner（或共享 unowned 桶）的 running+stopping 上限（jobs-local/index.ts:33）。
DEFAULT_MAX_CONCURRENT_JOBS_PER_OWNER = 10
#: 默认活作业 ring 保留字节（jobs-local/index.ts:36）。
DEFAULT_RETAIN_BYTES = 256 * 1024
#: 结算后保留的 ring 字节（jobs-local/index.ts:39）。
DEFAULT_SETTLED_RETAIN_BYTES = 16 * 1024
#: pull 源轮询间隔（ms，jobs-local/index.ts:42）。
DEFAULT_PUMP_POLL_MS = 150
#: 区分"等待超时"与"调用方取消"的 scoped deadline 码（jobs-local/index.ts:30）。
TASK_WAIT_TIMEOUT = "TASK_WAIT_TIMEOUT"

#: mini 载波：JobEventFilter 的 `{owners:'scope'}` 由订阅 ctx 的 scope 解析，
#: 上游 SessionId 的品牌查询能力不适用（保留常量以备清单引用）。
SESSION_ID_QUERY_UNSUPPORTED = "session-id query is not a mini carrier capability"


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


class JobHandle:
    """producer 面（对齐 types.ts JobHandle）：id + append + updateProgress。

    append 的 options 是 `{channel?: 'stdout'|'stderr'|'log', gapBefore?: True}`；
    空 chunk 被丢弃且不惊动观察者。所有方法同步。注册提交前（starter 调用内）
    暂存的写保留在 ring/state 中，随注册提交对观察者可见；结算后写入记日志并丢弃。
    """

    __slots__ = ("id", "_append", "_update_progress")

    def __init__(self, id_: str,
                 append: Callable[[str, dict | None], None],
                 update_progress: Callable[[str], None]) -> None:
        self.id = id_
        self._append = append
        self._update_progress = update_progress

    def append(self, text: str, options: dict | None = None) -> None:
        self._append(text, options)

    def update_progress(self, line: str) -> None:
        self._update_progress(line)


def job_id(kind: str, count: int) -> str:
    """注册表 id 生成：`<kind>-N`（对齐 brand.ts JobId 语义）。"""
    return f"{kind}-{count}"
