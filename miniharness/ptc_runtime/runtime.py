"""CPython 子进程 PTC 运行时（dsh-ptc-runtime seam 的 Python 后端）。

对应 dsh 真实源码：packages/ptc-runtime/ptc-runtime-node（Node worker/subprocess
载体）+ packages/experimental/ptc-runtime-python（CPython 子进程 + fd-3 wire）。

mini 载体：每个请求在一个全新的 CPython 3.10+ 子进程里跑模型写的 Python——
程序可用顶层 await（异步 main 包装）、可 return 完成值、可写 stdout/stderr，
并收到显式完成/失败结果。绑定经父子管道上的行 JSON 协议桥接（请求/响应）。

**安全边界声明**（上游同款）：子进程不是安全边界——直接 Python 操作没有文件系统
沙箱、无跨运行状态、无 shipped profile 默认启用。资源预算（墙钟截止 + 输出字节
上限）与进程组拆除可限制失控工作。

失败分类（kind 正交）：exception（程序抛错/解析失败）/ timeout（实现拥有的预算到期）
/ abort（signal 触发）/ worker-exit（基底在结算前死亡）/ invalid-output（完成值非
无损 JSON）/ output-limit（序列化外层日志/值/诊断超配置上限）/ protocol（程序发送非法
或超预算控制流量）/ sandbox-unavailable（所需约束无法建立）。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
from typing import Any

from .service import PtcRuntime, validate_binding_namespaces
from .types import (
    PtcRunFailure,
    PtcRunRequest,
    PtcRunResult,
    PtcRunSandbox,
    PtcRunSpec,
)

__all__ = [
    "DEFAULT_MAX_LOG_BYTES",
    "DEFAULT_MAX_TIMEOUT_MS",
    "DEFAULT_TIMEOUT_MS",
    "MIN_LOG_MARKER_BYTES",
    "PythonPtcRuntime",
    "install_ptc_runtime",
]

#: 缺省单次执行墙钟预算（毫秒）。
DEFAULT_TIMEOUT_MS = 120_000
#: provider 允许的最大墙钟预算（毫秒）。
DEFAULT_MAX_TIMEOUT_MS = 600_000
#: 外层日志/值/诊断的缺省字节上限。
DEFAULT_MAX_LOG_BYTES = 1024 * 1024
#: 截断标记的下限（低于此值的 maxLogBytes 在加载期拒绝）。
MIN_LOG_MARKER_BYTES = 64

_TRUNCATION_MARKER = "... [truncated]"

#: 程序引导：把 bindings 以全局对象注入，运行异步 main。
_BOOTSTRAP = r'''
import asyncio, json, sys, traceback, types

def _emit(payload):
    sys.stdout.write("\x1e" + json.dumps(payload) + "\n")
    sys.stdout.flush()

async def _invoke(global_name, member, args):
    _emit({"kind": "call", "global": global_name, "member": member, "args": args})
    line = await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
    if not line:
        raise RuntimeError("binding channel closed")
    response = json.loads(line)
    if response.get("ok"):
        return response.get("value")
    raise RuntimeError(response.get("error") or "binding rejected")

def _make_error_class(name, member_property):
    class _BindingError(RuntimeError):
        def __init__(self, member):
            super().__init__(member)
            setattr(self, member_property, member)
    _BindingError.__name__ = name
    return _BindingError

def _typed_member(raw_fn, member, binding_error):
    async def call(*args):
        try:
            return await raw_fn(*args)
        except RuntimeError as error:
            raise binding_error(member) from error
    return call

def _install_bindings(spec):
    for namespace in spec:
        global_name = namespace["global"]
        members = namespace["functions"]
        functions = {}
        for member in members:
            def make(g, m):
                async def call(*args):
                    # 程序按 `await tools.name(args)` 调用：单个位置参数即工具参数
                    # 对象（上游 binding 收 rawArgs）；多位置参数时按列表传递。
                    payload = args[0] if len(args) == 1 else list(args)
                    return await _invoke(g, m, payload)
                return call
            functions[member] = make(global_name, member)
        error_class = namespace.get("errorClass")
        if error_class is not None:
            binding_error = _make_error_class(error_class["name"],
                                              error_class["memberNameProperty"])
            globals()[error_class["name"]] = binding_error
            for member in members:
                raw = functions[member]
                functions[member] = _typed_member(raw, member, binding_error)
        globals()[global_name] = types.SimpleNamespace(**functions)
    # console 槽位：stdout/stderr 直写（后端拥有）
    class _Console:
        @staticmethod
        def log(*args):
            sys.stdout.write(" ".join(str(a) for a in args) + "\n"); sys.stdout.flush()
        @staticmethod
        def error(*args):
            sys.stderr.write(" ".join(str(a) for a in args) + "\n"); sys.stderr.flush()
        @staticmethod
        def warn(*args):
            sys.stderr.write(" ".join(str(a) for a in args) + "\n"); sys.stderr.flush()
    globals()["console"] = _Console()

def main():
    spec = json.loads(sys.stdin.readline())
    _install_bindings(spec.get("bindings", []))
    source = spec.get("program", "")
    _emit({"kind": "ready"})
    try:
        async def _main():
            return eval(compile("async def __dsh_main__():\n" + _indent(source), "<ptc>", "exec"))
        # 运行异步主体：支持顶层 await 与 return。
        namespace = {}
        exec(compile("async def __dsh_main__():\n" + _indent(source), "<ptc>", "exec"), globals(), namespace)
        value = asyncio.run(namespace["__dsh_main__"]())
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError):
            _emit({"kind": "result", "ok": True, "invalid": True})
        else:
            _emit({"kind": "result", "ok": True, "value": value})
    except Exception:
        _emit({"kind": "result", "ok": False, "error": traceback.format_exc()})

def _indent(text):
    return "\n".join("    " + line for line in text.splitlines()) or "    pass"

main()
'''


class PythonPtcRuntime(PtcRuntime):
    """CPython 子进程 PTC 运行时（上游 experimental-ptc-runtime-python 语义）。"""

    language = "python"
    isolation = "process"

    def __init__(self, *, python_bin: str | None = None,
                 timeout_ms: int = DEFAULT_TIMEOUT_MS,
                 max_timeout_ms: int = DEFAULT_MAX_TIMEOUT_MS,
                 max_log_bytes: int = DEFAULT_MAX_LOG_BYTES,
                 cwd: str | None = None) -> None:
        super().__init__()
        resolved_bin = python_bin if python_bin is not None else sys.executable
        if not resolved_bin or not os.path.isfile(resolved_bin):
            raise ValueError(
                f"ptc-runtime-python: pythonBin {resolved_bin!r} is not an executable regular file")
        if not isinstance(max_log_bytes, int) or isinstance(max_log_bytes, bool) \
                or max_log_bytes < MIN_LOG_MARKER_BYTES:
            raise ValueError(
                f"ptc-runtime-python: maxLogBytes must be an integer no smaller than "
                f"{MIN_LOG_MARKER_BYTES}")
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or timeout_ms <= 0:
            raise ValueError("ptc-runtime-python: timeoutMs must be a positive integer")
        if not isinstance(max_timeout_ms, int) or isinstance(max_timeout_ms, bool) \
                or max_timeout_ms <= 0:
            raise ValueError("ptc-runtime-python: maxTimeoutMs must be a positive integer")
        if timeout_ms > max_timeout_ms:
            raise ValueError(
                "ptc-runtime-python: timeoutMs must not exceed maxTimeoutMs")
        self._python = resolved_bin
        self._timeout_ms = timeout_ms
        self._max_timeout_ms = max_timeout_ms
        self._max_log_bytes = max_log_bytes
        self._cwd = cwd if cwd is not None else os.getcwd()

    @property
    def execution_instructions(self) -> str:
        return (
            "Write Python. Top-level `await` and `return` are available; the return "
            "value becomes the result. Call host functions through the binding globals."
        )

    @property
    def timeout(self) -> dict:
        return {"defaultMs": min(self._timeout_ms, self._max_timeout_ms),
                "maxMs": self._max_timeout_ms}

    def resolve(self, request: PtcRunRequest) -> PtcRunSpec:
        """校验绑定并补全目录/截止（上游 resolve；显式 sandboxPolicy 不支持拒绝）。"""
        validate_binding_namespaces(request.bindings)
        if request.sandboxPolicy is not None:
            raise ValueError("ptc-runtime-python does not support an explicit sandbox policy")
        cwd = request.cwd if request.cwd is not None else self._cwd
        if not os.path.isabs(cwd):
            raise ValueError("ptc-runtime-python: run requires an absolute cwd")
        if request.timeoutMs is None:
            timeout_ms: int | None = min(self._timeout_ms, self._max_timeout_ms)
        elif request.timeoutMs == 0:
            timeout_ms = None
        else:
            budget = request.timeoutMs
            if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0 \
                    or budget > self._max_timeout_ms:
                raise ValueError("ptc-runtime-python: run requires a valid resolved timeout")
            timeout_ms = min(budget, self._max_timeout_ms)
        return PtcRunSpec(
            program=request.program, bindings=list(request.bindings),
            cwd=cwd, timeoutMs=timeout_ms,
            sandboxPolicy=None, signal=request.signal)

    async def run(self, spec: PtcRunSpec) -> PtcRunResult:
        """在一个全新 CPython 子进程中执行程序（上游 run；结局作为字段 resolve）。"""
        if spec.sandboxPolicy is not None:
            raise ValueError("ptc-runtime-python: run requires resolved inputs without policy")
        return await asyncio.to_thread(self._run_blocking, spec)

    # ---------- 同步子进程载体（在工作线程内跑，避免阻塞事件循环） ----------

    def _run_blocking(self, spec: PtcRunSpec) -> PtcRunResult:
        handle = _BindingHost(spec)
        spec_wire = {
            "program": spec.program,
            "bindings": [namespace.to_wire() for namespace in spec.bindings],
        }
        try:
            process = subprocess.Popen(
                [self._python, "-u", "-c", _BOOTSTRAP],
                cwd=spec.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                errors="replace", bufsize=1,
                **(_subprocess_flags()),
            )
        except OSError as error:
            return PtcRunResult(error=PtcRunFailure(
                "worker-exit", f"Python subprocess failed to start: {error}"))
        try:
            process.stdin.write(json.dumps(spec_wire) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

        abort_flag = {"aborted": False}
        abort_event = getattr(spec.signal, "event", None) if spec.signal is not None else None
        if spec.signal is not None and getattr(spec.signal, "aborted", False):
            abort_flag["aborted"] = True
        controller = threading.Event()
        watcher = None
        if abort_event is not None:
            def _watch_abort():
                abort_event.wait()
                abort_flag["aborted"] = True
                controller.set()
            watcher = threading.Thread(target=_watch_abort, daemon=True)
            watcher.start()

        deadline = None if spec.timeoutMs is None else spec.timeoutMs / 1000.0
        try:
            result = _collect(process, handle, abort_flag, controller, deadline,
                              self._max_log_bytes)
            if process.poll() is None:
                _terminate(process)
            return result
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except (OSError, ValueError):
                    pass


class _BindingHost:
    """把绑定调用派发回宿主可调用（在 worker 线程内同步执行）。"""

    def __init__(self, spec: PtcRunSpec) -> None:
        self._functions: dict[tuple[str, str], Any] = {}
        for namespace in spec.bindings:
            for member, fn in namespace.functions.items():
                self._functions[(namespace.global_, member)] = fn

    def invoke(self, global_name: str, member: str, args: list) -> dict:
        fn = self._functions.get((global_name, member))
        if fn is None:
            return {"ok": False, "error": f"unknown binding {global_name}.{member}"}
        try:
            value = fn(args)
            if asyncio.iscoroutine(value):
                value = asyncio.run(value)
            if not _is_lossless_json(value):
                return {"ok": False,
                        "error": "binding resolution must be lossless JSON"}
            return {"ok": True, "value": value}
        except Exception as error:  # noqa: BLE001 - 绑定拒绝回传程序
            return {"ok": False, "error": str(error)}


def _collect(process: subprocess.Popen, handle: _BindingHost, abort_flag: dict,
             controller: threading.Event, deadline: float | None,
             max_log_bytes: int) -> PtcRunResult:
    import time
    logs: list[str] = []
    value: Any = None
    has_value = False
    failure: PtcRunFailure | None = None
    stderr_text = ""
    started = time.monotonic()
    total_bytes = [0]

    # stdout 读取放到后台线程 + 队列：主循环按小步轮询，使截止/中止能在
    # readline 阻塞期间生效（否则无限循环程序永远读不到行，截止永不到达）。
    queue: "queue.Queue" = __import__("queue").Queue()

    def _reader() -> None:
        try:
            for line in process.stdout:
                queue.put(line)
        except (OSError, ValueError):
            pass
        queue.put(None)

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    def append_log(text: str) -> None:
        total_bytes[0] += len(text.encode("utf-8"))
        if total_bytes[0] > max_log_bytes:
            raise _OutputLimit()
        logs.append(text)

    try:
        while True:
            if abort_flag["aborted"] or controller.is_set():
                failure = PtcRunFailure("abort", "execution aborted")
                break
            if deadline is not None and time.monotonic() - started >= deadline:
                failure = PtcRunFailure(
                    "timeout", f"execution deadline reached ({int(deadline * 1000)}ms)")
                break
            try:
                line = queue.get(timeout=0.05)
            except Exception:
                continue
            if line is None:
                break
            if not line.startswith("\x1e"):
                append_log(line.rstrip("\n"))
                continue
            try:
                message = json.loads(line[1:])
            except ValueError:
                failure = PtcRunFailure("protocol", "program sent invalid control traffic")
                break
            kind = message.get("kind")
            if kind == "ready":
                continue
            if kind == "call":
                response = handle.invoke(message.get("global"), message.get("member"),
                                         message.get("args") or [])
                try:
                    process.stdin.write(json.dumps(response) + "\n")
                    process.stdin.flush()
                except (BrokenPipeError, OSError):
                    failure = PtcRunFailure("worker-exit", "binding channel closed")
                    break
                continue
            if kind == "result":
                if message.get("ok"):
                    if message.get("invalid"):
                        failure = PtcRunFailure(
                            "invalid-output", "program completion must be lossless JSON")
                    else:
                        candidate = message.get("value")
                        if not _is_lossless_json(candidate):
                            failure = PtcRunFailure(
                                "invalid-output",
                                "program completion must be lossless JSON")
                        else:
                            value = candidate
                            has_value = True
                else:
                    failure = PtcRunFailure("exception", str(message.get("error")))
                break
            failure = PtcRunFailure("protocol", f"unknown control message {kind!r}")
            break
    except _OutputLimit:
        failure = PtcRunFailure(
            "output-limit", f"outer output exceeded {max_log_bytes} bytes")

    # stdin 关闭让子进程自行退出；正常完成时给它一点时间结算，只有超时/中止/
    # 仍在运行才强制终止——否则会误杀刚产出结果、尚在退出的进程。
    try:
        process.stdin.close()
    except (OSError, ValueError):
        pass
    if failure is None:
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _terminate(process)
    elif process.poll() is None:
        _terminate(process)
    try:
        stderr_text = _read_stderr_nonblocking(process)
    except (OSError, ValueError):
        stderr_text = ""
    exit_code = process.poll()

    if failure is None and exit_code not in (0, None):
        failure = PtcRunFailure(
            "worker-exit",
            f"Python process exited before completing ({exit_code})"
            + (f": {stderr_text.strip()}" if stderr_text.strip() else ""))
    if failure is None and not has_value:
        failure = PtcRunFailure("worker-exit", "Python process output did not close cleanly")

    if stderr_text and total_bytes[0] < max_log_bytes:
        logs.append(stderr_text.rstrip("\n"))
    logs = _bound_logs(logs, max_log_bytes)
    return PtcRunResult(logs=logs, value=value, has_value=has_value, error=failure)


class _OutputLimit(Exception):
    pass


def _bound_logs(logs: list[str], max_log_bytes: int) -> list[str]:
    encoded = 0
    bounded: list[str] = []
    for entry in logs:
        size = len(entry.encode("utf-8"))
        if encoded + size > max_log_bytes:
            bounded.append(_TRUNCATION_MARKER)
            break
        encoded += size
        bounded.append(entry)
    return bounded


def _is_lossless_json(value: Any) -> bool:
    try:
        json.dumps(value, allow_nan=False)
        return True
    except (TypeError, ValueError):
        return False


def _read_stderr_nonblocking(process: subprocess.Popen) -> str:
    """进程已终止后排干 stderr（避免在活进程上永久阻塞）。"""
    import queue as _queue
    stream = process.stderr
    if stream is None:
        return ""
    result: list[str] = []
    q: "_queue.Queue" = _queue.Queue()

    def _read() -> None:
        try:
            q.put(stream.read() or "")
        except (OSError, ValueError):
            q.put("")

    thread = threading.Thread(target=_read, daemon=True)
    thread.start()
    try:
        result.append(q.get(timeout=2))
    except Exception:  # noqa: BLE001 - 排干超时按空处理
        return ""
    return "".join(result)


def _terminate(process: subprocess.Popen) -> None:
    try:
        process.terminate()
    except OSError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _subprocess_flags() -> dict:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def install_ptc_runtime(ctx, *, python_bin: str | None = None, **options) -> PythonPtcRuntime:
    """在 ctx 上装配 PythonPtcRuntime（幂等；上游 apply）。"""
    if getattr(ctx, "_miniharness_ptc_runtime", None) is not None:
        return ctx._miniharness_ptc_runtime
    runtime = PythonPtcRuntime(python_bin=python_bin, **options)
    ctx.provide("ptcRuntime", runtime)
    ctx._miniharness_ptc_runtime = runtime
    return runtime
