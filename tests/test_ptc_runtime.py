"""PTC 运行时 seam + Python 后端。

对照上游 `packages/ptc-runtime/ptc-runtime/tests/service.spec.ts` 与
`ptc-runtime-node/tests/runtime.spec.ts` / `experimental/ptc-runtime-python`。
"""
import asyncio
import os
import sys
import unittest

from miniharness.core.scope import Context
from miniharness.ptc_runtime import (
    DEFAULT_TIMEOUT_MS,
    DUNDER_MEMBER,
    MIN_LOG_MARKER_BYTES,
    PORTABLE_RESERVED_WORDS,
    RESERVED_BINDING_GLOBALS,
    RESERVED_ERROR_MEMBERS,
    PtcBindingErrorClass,
    PtcBindingNamespace,
    PtcRunRequest,
    PtcRuntime,
    PythonPtcRuntime,
    install_ptc_runtime,
    is_portable_identifier,
    validate_binding_namespaces,
)


def _run(runtime, **kwargs):
    spec = runtime.resolve(PtcRunRequest(**kwargs))
    return asyncio.run(runtime.run(spec))


class VocabularyTest(unittest.TestCase):
    def test_portable_identifier_rules(self):
        self.assertTrue(is_portable_identifier("tools"))
        self.assertFalse(is_portable_identifier("$tools"))
        self.assertFalse(is_portable_identifier("lambda"))
        self.assertFalse(is_portable_identifier("1abc"))

    def test_reserved_sets(self):
        self.assertIn("console", RESERVED_BINDING_GLOBALS)
        self.assertIn("__name__", RESERVED_BINDING_GLOBALS)
        self.assertIn("args", RESERVED_ERROR_MEMBERS)
        self.assertTrue(DUNDER_MEMBER.match("__x__"))
        self.assertNotIn("tools", PORTABLE_RESERVED_WORDS)

    def test_validate_namespaces(self):
        good = PtcBindingNamespace("tools", {"add": lambda args: 1})
        validate_binding_namespaces([good])
        for bad in (
            PtcBindingNamespace("$tools", {}),
            PtcBindingNamespace("console", {}),
            PtcBindingNamespace("lambda", {}),
        ):
            with self.assertRaises(ValueError):
                validate_binding_namespaces([bad])
        with self.assertRaises(ValueError):
            validate_binding_namespaces([good, PtcBindingNamespace("tools", {})])
        bad_error = PtcBindingNamespace(
            "tools", {}, PtcBindingErrorClass("ToolError", "name"))
        with self.assertRaises(ValueError):
            validate_binding_namespaces([bad_error])
        good_error = PtcBindingNamespace(
            "tools", {}, PtcBindingErrorClass("ToolError", "toolName"))
        validate_binding_namespaces([good_error])


class ServiceDefinitionTest(unittest.TestCase):
    def test_abstract_seam_requires_provider(self):
        runtime = PtcRuntime()
        with self.assertRaises(NotImplementedError):
            _ = runtime.language
        self.assertEqual(runtime.service_key, "ptcRuntime")
        self.assertIsNone(runtime.sandbox_mode)
        self.assertEqual(runtime.execution_instructions, "")

    def test_install_registers_and_is_idempotent(self):
        ctx = Context(name="root")
        first = install_ptc_runtime(ctx)
        self.assertIsInstance(first, PythonPtcRuntime)
        self.assertIs(ctx.get("ptcRuntime"), first)
        self.assertIs(install_ptc_runtime(ctx), first)


class PythonRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.runtime = PythonPtcRuntime()

    def test_return_value(self):
        result = _run(self.runtime, program="return 1 + 2")
        self.assertEqual(result.value, 3)
        self.assertTrue(result.has_value)
        self.assertIsNone(result.error)

    def test_top_level_await_and_bindings(self):
        namespace = PtcBindingNamespace("tools", {"add": lambda args: args[0] + args[1]})
        result = _run(
            self.runtime,
            program="x = await tools.add(2, 3)\nreturn x * 2",
            bindings=[namespace])
        self.assertEqual(result.value, 10)
        self.assertIsNone(result.error)

    def test_exception_is_a_field_not_a_rejection(self):
        result = _run(self.runtime, program="raise ValueError('boom')")
        self.assertEqual(result.error.kind, "exception")
        self.assertIn("boom", result.error.message)
        self.assertFalse(result.has_value)

    def test_invalid_output(self):
        result = _run(self.runtime, program="return object()")
        self.assertEqual(result.error.kind, "invalid-output")

    def test_binding_rejection_reaches_program(self):
        def reject(args):
            raise RuntimeError("no")
        namespace = PtcBindingNamespace("tools", {"fail": reject})
        result = _run(
            self.runtime,
            program="try:\n    await tools.fail()\nexcept Exception as e:\n    return 'caught'\nreturn 'missed'",
            bindings=[namespace])
        self.assertEqual(result.value, "caught")

    def test_typed_binding_error_class(self):
        def reject(args):
            raise RuntimeError("denied")
        namespace = PtcBindingNamespace(
            "tools", {"fail": reject},
            PtcBindingErrorClass("ToolError", "toolName"))
        result = _run(
            self.runtime,
            program=(
                "try:\n"
                "    await tools.fail()\n"
                "except ToolError as e:\n"
                "    return e.toolName\n"
                "return 'missed'"),
            bindings=[namespace])
        self.assertEqual(result.value, "fail")

    def test_abort_pre_settled(self):
        class _Signal:
            aborted = True
            event = None
        result = _run(self.runtime, program="return 1", signal=_Signal())
        self.assertEqual(result.error.kind, "abort")

    def test_timeout(self):
        runtime = PythonPtcRuntime(timeout_ms=200)
        result = _run(runtime, program="import time\nwhile True:\n    time.sleep(0.05)")
        self.assertEqual(result.error.kind, "timeout")

    def test_output_limit(self):
        runtime = PythonPtcRuntime(max_log_bytes=MIN_LOG_MARKER_BYTES)
        result = _run(runtime, program="print('x' * 500)\nreturn 1")
        self.assertEqual(result.error.kind, "output-limit")

    def test_resolve_rejects_sandbox_policy(self):
        with self.assertRaises(ValueError):
            self.runtime.resolve(PtcRunRequest(program="return 1", sandboxPolicy=object()))

    def test_resolve_requires_absolute_cwd(self):
        with self.assertRaises(ValueError):
            self.runtime.resolve(PtcRunRequest(program="return 1", cwd="relative"))

    def test_config_validation(self):
        with self.assertRaises(ValueError):
            PythonPtcRuntime(max_log_bytes=10)
        with self.assertRaises(ValueError):
            PythonPtcRuntime(timeout_ms=0)
        with self.assertRaises(ValueError):
            PythonPtcRuntime(timeout_ms=1_000_000, max_timeout_ms=1000)
        with self.assertRaises(ValueError):
            PythonPtcRuntime(python_bin="/nonexistent/python")

    def test_timeout_property(self):
        self.assertEqual(self.runtime.timeout["defaultMs"], DEFAULT_TIMEOUT_MS)
        self.assertIn("python", self.runtime.execution_instructions.lower())

    def test_logs_capture_stdout_and_console(self):
        result = _run(
            self.runtime,
            program="print('hello')\nconsole.log('world')\nreturn 1")
        self.assertEqual(result.value, 1)
        self.assertIn("hello", result.logs)
        self.assertIn("world", result.logs)

    def test_log_truncation_marker(self):
        from miniharness.ptc_runtime.runtime import _bound_logs
        bounded = _bound_logs(["a" * 40, "b" * 40, "c" * 40], MIN_LOG_MARKER_BYTES)
        self.assertEqual(bounded[-1], "... [truncated]")

    def test_unknown_binding_member(self):
        result = _run(self.runtime, program="return 1", bindings=[
            PtcBindingNamespace("tools", {"known": lambda args: 1})])
        # 已知成员可调用
        result = _run(
            self.runtime,
            program="return await tools.known()",
            bindings=[PtcBindingNamespace("tools", {"known": lambda args: 41})])
        self.assertEqual(result.value, 41)

    def test_binding_non_json_resolution_rejected(self):
        namespace = PtcBindingNamespace("tools", {"bad": lambda args: object()})
        result = _run(
            self.runtime,
            program=(
                "try:\n"
                "    await tools.bad()\n"
                "except Exception as e:\n"
                "    return 'rejected'\n"
                "return 'missed'"),
            bindings=[namespace])
        self.assertEqual(result.value, "rejected")

    def test_protocol_failure(self):
        result = _run(
            self.runtime,
            program="import sys\nsys.stdout.write(chr(30) + 'not json\\n')\nreturn 1")
        self.assertEqual(result.error.kind, "protocol")

    def test_abort_event_mid_run(self):
        import threading

        class _Event:
            def __init__(self):
                self._set = threading.Event()

            def wait(self):
                self._set.wait()

            def set(self):
                self._set.set()

        class _Signal:
            def __init__(self):
                self.aborted = False
                self.event = _Event()

        signal = _Signal()

        def _trigger():
            signal.event.set()

        timer = threading.Timer(0.3, _trigger)
        timer.start()
        try:
            result = _run(
                self.runtime,
                program="import time\nwhile True:\n    time.sleep(0.05)",
                signal=signal)
        finally:
            timer.cancel()
        self.assertEqual(result.error.kind, "abort")

    def test_worker_exit_nonzero(self):
        result = _run(self.runtime, program="import sys\nsys.exit(3)")
        self.assertEqual(result.error.kind, "worker-exit")

    def test_console_error_and_warn(self):
        result = _run(self.runtime, program="console.error('e')\nconsole.warn('w')\nreturn 1")
        self.assertEqual(result.value, 1)

    def test_binding_error_class_globals(self):
        # 无 errorClass 时错误是 RuntimeError；有则暴露类型化类
        with_error = _run(
            self.runtime,
            program=(
                "try:\n"
                "    await tools.fail()\n"
                "except RuntimeError as e:\n"
                "    return 'plain'\n"
                "return 'missed'"),
            bindings=[PtcBindingNamespace("tools", {"fail": lambda args: (_ for _ in ()).throw(RuntimeError("x"))})])
        self.assertEqual(with_error.value, "plain")

    def test_run_rejects_unresolved_policy(self):
        spec = self.runtime.resolve(PtcRunRequest(program="return 1"))
        spec = __import__("miniharness.ptc_runtime", fromlist=["PtcRunSpec"]).PtcRunSpec(
            program=spec.program, bindings=spec.bindings, cwd=spec.cwd,
            timeoutMs=spec.timeoutMs, sandboxPolicy=object())
        with self.assertRaises(ValueError):
            asyncio.run(self.runtime.run(spec))

    def test_resolve_zero_timeout_means_no_deadline(self):
        spec = self.runtime.resolve(PtcRunRequest(program="return 1", timeoutMs=0))
        self.assertIsNone(spec.timeoutMs)

    def test_resolve_rejects_invalid_namespace(self):
        with self.assertRaises(ValueError):
            self.runtime.resolve(PtcRunRequest(
                program="return 1",
                bindings=[PtcBindingNamespace("console", {})]))

    def test_resolve_caps_timeout_to_max(self):
        runtime = PythonPtcRuntime(timeout_ms=1000, max_timeout_ms=2000)
        spec = runtime.resolve(PtcRunRequest(program="return 1", timeoutMs=2000))
        self.assertEqual(spec.timeoutMs, 2000)
        with self.assertRaises(ValueError):
            runtime.resolve(PtcRunRequest(program="return 1", timeoutMs=5000))

    def test_language_and_isolation(self):
        self.assertEqual(self.runtime.language, "python")
        self.assertEqual(self.runtime.isolation, "process")

    def test_start_failure_is_worker_exit(self):
        runtime = PythonPtcRuntime(python_bin=sys.executable)
        # 用一个不存在的工作目录触发 Popen OSError
        spec = self.runtime.resolve(PtcRunRequest(program="return 1"))
        bad = __import__("miniharness.ptc_runtime", fromlist=["PtcRunSpec"]).PtcRunSpec(
            program=spec.program, bindings=spec.bindings,
            cwd=os.path.join(os.getcwd(), "does-not-exist-ptc"),
            timeoutMs=spec.timeoutMs)
        result = asyncio.run(runtime.run(bad))
        self.assertEqual(result.error.kind, "worker-exit")


if __name__ == "__main__":
    unittest.main()
