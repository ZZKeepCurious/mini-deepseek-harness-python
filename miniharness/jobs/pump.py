"""注册表自有的 pull 泵（对齐 jobs-local/src/pump.ts）。

按有界节奏把一条作业的 JobOutputSource 拷进其 ring，并在 producer 结算后再排干
一次，使 ring 在结算裁剪并关闭前持有全部字节。lossy 源读落成 gap 块；源每次读上报
的落盘文件交给 sink（该引用比任何 chunk 都长寿）。纯工具：无 cordis、结算后不留
定时器。
"""
from __future__ import annotations

import threading

__all__ = ["PumpHandle", "start_pump"]


class PumpHandle:
    """一次泵运行；`done` 在最后一次 post-settlement 排干后置位。"""

    __slots__ = ("done",)

    def __init__(self, done: threading.Event) -> None:
        self.done = done

    def wait(self) -> None:
        self.done.wait()


def start_pump(sources: list, sink, poll_ms: int,
               until: threading.Event) -> PumpHandle:
    """按数组顺序排干每个源，等待 `poll_ms` 或 `until` 置位，重复，最后再排干一次。

    lossy 源读以 `gapBefore` 追加幸存尾部，使不连续对观察者保持可见。源每轮按数组
    序排干，故一个轮询窗内两个源产生的字节按该序落地而非写入序：ring 是尽力而为的
    实时视图，跨源重排以 `poll_ms` 为界。

    @param sources - producer 流，各按自身偏移泵取。
    @param sink - 接收每个拷贝块与每个源当前落盘文件 (`append`/`spill`)。
    @param poll_ms - 轮询间隔（正整数毫秒）。
    @param until - 置位即表示 producer 已结束（或注册表强制结算）；只读其置位状态。
    @returns 句柄，`done` 在最后排干后置位。
    """
    if not isinstance(poll_ms, (int, float)) or isinstance(poll_ms, bool) \
            or poll_ms <= 0 or not _finite(poll_ms):
        raise ValueError(
            f"invalid pump pollMs: expected a positive finite number of milliseconds, "
            f"got {poll_ms!r}")
    states = [{"source": source, "cursor": 0} for source in sources]

    def drain() -> None:
        for index, state in enumerate(states):
            source = state["source"]
            read = source["read"](state["cursor"])
            state["cursor"] = read["nextOffset"]
            sink["spill"](index, read.get("spillPath"))
            text = read["text"]
            if len(text) == 0:
                continue
            channel = source.get("channel")
            if channel is None and not read["lossy"]:
                sink["append"](text)
            else:
                options: dict = {}
                if channel is not None:
                    options["channel"] = channel
                if read["lossy"]:
                    options["gapBefore"] = True
                sink["append"](text, options)

    # 首次排干同步完成：注册提交（registered）先于泵，故泵的首次 append 一定
    # announce 在 registered 之后（对齐上游 async IIFE 首个 await 前的同步段）。
    drain()
    done = threading.Event()

    def run() -> None:
        while not until.wait(poll_ms / 1000):
            drain()
        drain()
        done.set()

    threading.Thread(target=run, name="jobs-pump", daemon=True).start()
    return PumpHandle(done)


def _finite(value) -> bool:
    """数值有限（拒绝 inf/nan）。"""
    try:
        import math
        return math.isfinite(value)
    except TypeError:
        return False
