"""常驻单事件循环 —— Python 载体对齐上游 Node 进程固有的单循环形态。

对应 dsh：无独立文件。上游运行在 Node.js 上，整个进程天然只有一个事件循环：
agent 泵、重试等待、流式传输、插件 effect 全部共享同一循环，跨调用的
asyncio 原语（AbortController 的 abort 信号、任务、定时器）生命周期连续。
mini 此前同步门面（followup/steer 无 driver 时）经一次性 asyncio.run 驱动——
每次调用都是新循环，取消事件每轮重建、跨调用状态不延续（verified-diffs §3.1
登记的载体简化）。本模块提供进程级**懒加载单例**循环：

  * get_resident_loop()：首次调用时在守护线程启动 run_forever；
  * run_on_resident(coro)：把协程提交到常驻循环并阻塞至完成（异常向上抛，
    对齐旧 asyncio.run 门面的冒泡契约）；主线程等待可被 Ctrl+C 中断——
    中断时协作式取消在途协程（泵的 finally 照常闭合回合），再上抛；
  * shutdown_resident_loop()：显式停机（测试卫生用；进程退出靠守护线程）。

AgentLoop 同步门面经此驱动后，"常驻单循环"语义与上游一致：取消事件绑定
同一循环跨回合复用、多代理并发交错在同一线程上多路复用。
"""
from __future__ import annotations

import asyncio
import concurrent.futures as futures
import threading

_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None

_POLL_SECONDS = 0.2


def get_resident_loop() -> asyncio.AbstractEventLoop:
    """返回进程级常驻事件循环（懒加载；先前实例已关闭则重建）。"""
    global _loop, _thread
    with _lock:
        if _loop is None or _loop.is_closed():
            _loop = asyncio.new_event_loop()
            asyncio.set_event_loop(_loop)
            _thread = threading.Thread(
                target=_loop.run_forever,
                name="miniharness-resident-loop",
                daemon=True,
            )
            _thread.start()
        return _loop


def _wait_interruptible(future: futures.Future) -> any:
    """阻塞等待结果，但保持主线程可中断（Windows 的 Condition.wait 不可被
    Ctrl+C 打断，故以短超时分片轮询）；KeyboardInterrupt 时协作式取消在途
    协程后上抛（对齐取消是协作式的既有契约）。"""
    while True:
        try:
            return future.result(timeout=_POLL_SECONDS)
        except futures.TimeoutError:
            continue
        except KeyboardInterrupt:
            future.cancel()
            raise


def run_on_resident(coro) -> any:
    """把协程提交到常驻循环并阻塞至完成；异常向上抛（含 LlmFailure 冒泡）。"""
    return _wait_interruptible(asyncio.run_coroutine_threadsafe(coro, get_resident_loop()))


def shutdown_resident_loop() -> None:
    """停止并丢弃常驻循环（幂等；后续 get_resident_loop 会重建新实例）。"""
    global _loop, _thread
    with _lock:
        loop, thread = _loop, _thread
        _loop, _thread = None, None
    if loop is None or loop.is_closed():
        return
    loop.call_soon_threadsafe(loop.stop)
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=5.0)
    loop.close()
