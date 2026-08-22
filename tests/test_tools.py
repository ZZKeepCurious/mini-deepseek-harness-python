"""第 3 章验收：工具注册表 + 执行管线。运行：python -m unittest discover -s tests -t ."""

import time
import unittest

from miniharness.core.scope import Context
from miniharness.core.tools import Tool, ToolRegistry, run_pipeline, validate_schema


def _make(execute, name="t", **kw):
    return Tool(name=name, description="d", execute=execute, **kw)


class TestSchema(unittest.TestCase):
    def test_required_and_type(self):
        schema = {
            "type": "object",
            "properties": {"cmd": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["cmd"],
        }
        self.assertEqual(validate_schema({"cmd": "ls"}, schema), [])
        errors = validate_schema({"limit": 1}, schema)
        self.assertEqual(len(errors), 1)
        errors = validate_schema({"cmd": 3}, schema)
        self.assertEqual(len(errors), 1)

    def test_enum(self):
        schema = {"type": "string", "enum": ["a", "b"]}
        self.assertEqual(validate_schema("a", schema), [])
        self.assertEqual(len(validate_schema("c", schema)), 1)


class TestRegistry(unittest.TestCase):
    def test_register_resolve(self):
        ctx = Context()
        reg = ToolRegistry(ctx)
        tool = _make(lambda a, e: "ok")
        reg.register(tool)
        self.assertIs(reg.resolve("t"), tool)
        self.assertEqual(reg.resolve("nope"), None)

    def test_duplicate_register_fails(self):
        ctx = Context()
        reg = ToolRegistry(ctx)
        reg.register(_make(lambda a, e: 1))
        with self.assertRaises(RuntimeError):
            reg.register(_make(lambda a, e: 2))

    def test_scope_chain_visibility(self):
        ctx = Context()
        reg = ToolRegistry(ctx)
        reg.register(_make(lambda a, e: "global", name="gtool"), scope=None)
        a = ctx.create_scope("a")
        reg.register(_make(lambda a, e: "a-local", name="atool"), scope=a)
        sub = a.create_scope("sub")
        # 自身 → 祖先链 → 全局
        self.assertEqual(reg.resolve("atool", scope=sub).name, "atool")
        self.assertEqual(reg.resolve("gtool", scope=sub).name, "gtool")
        b = ctx.create_scope("b")
        self.assertIsNone(reg.resolve("atool", scope=b))  # 兄弟作用域不可见
        self.assertIsNotNone(reg.resolve("gtool", scope=b))  # 全局层可见
        # 作用域注册卸载后不可见
        disposer = reg.register(_make(lambda a, e: "temp", name="ttool"), scope=a)
        self.assertIsNotNone(reg.resolve("ttool", scope=sub))
        disposer()
        self.assertIsNone(reg.resolve("ttool", scope=sub))

    def test_restrict_deny_wins(self):
        ctx = Context()
        reg = ToolRegistry(ctx)
        pred = reg.restrict(allow={"bash", "read_file"}, deny={"bash"})
        self.assertTrue(pred("read_file"))
        self.assertFalse(pred("bash"))
        self.assertFalse(pred("write_file"))

    def test_default_lookup_uses_registry_root_scope(self):
        # 回归：注册表建在作用域上时，缺省 resolve/names 必须从 root 的 scope
        # 解析（上游 dispatch 的 exec.agent 视角），而非只看全局层。
        ctx = Context()
        scope = ctx.create_scope("agent:x")
        scope._isolate.setdefault("tools", object())
        reg = ToolRegistry(scope)
        reg.register(_make(lambda a, e: "ok"))
        self.assertEqual(reg.names(), ["t"])
        self.assertIsNotNone(reg.resolve("t"))

    def test_fiber_dispose_unregisters_tool(self):
        # 注册即 effect（HMR 契约）：目标 fiber 拆解自动注销。
        ctx = Context()
        scope = ctx.create_scope("agent:x")
        scope._isolate.setdefault("tools", object())
        reg = ToolRegistry(scope)
        reg.register(_make(lambda a, e: 1, name="scoped"), scope=scope)
        self.assertIn("scoped", reg.names(scope))
        scope.dispose()
        self.assertNotIn("scoped", reg.names(scope))

    def test_nearest_scope_shadows_global(self):
        ctx = Context()
        reg = ToolRegistry(ctx)
        reg.register(_make(lambda a, e: "global", name="x"))
        a = ctx.create_scope("a")
        reg.register(_make(lambda a, e: "local", name="x"), scope=a)
        self.assertEqual(reg.resolve("x", scope=a).execute(None, None), "local")
        self.assertEqual(reg.resolve("x").execute(None, None), "global")  # 缺省视角无键 → 全局


class TestPipeline(unittest.TestCase):
    def test_success_frozen_result(self):
        ctx = Context()
        tool = _make(lambda a, e: {"out": [1, 2]})
        result = run_pipeline(ctx, tool, {})
        self.assertTrue(result.ok)
        with self.assertRaises(TypeError):
            result.content["out"] = 3

    def test_schema_violation_is_error(self):
        ctx = Context()
        tool = _make(lambda a, e: "never", parameters={"type": "object", "required": ["cmd"]})
        result = run_pipeline(ctx, tool, {})
        self.assertTrue(result.is_error)
        self.assertIn("cmd", result.error)

    def test_deny_by_pre_execute(self):
        ctx = Context()
        ctx.on("tools/pre-execute", lambda p, nxt: {"kind": "deny"})
        tool = _make(lambda a, e: "ran")
        result = run_pipeline(ctx, tool, {})
        self.assertTrue(result.is_error)
        self.assertIn("denied", result.error)

    def test_ask_approval_flow(self):
        ctx = Context()
        ctx.on("tools/pre-execute", lambda p, nxt: {"kind": "ask"})
        ctx.on("tools/ask", lambda p, nxt: True)
        result = run_pipeline(ctx, _make(lambda a, e: "ok"), {})
        self.assertTrue(result.ok)
        ctx2 = Context()
        ctx2.on("tools/pre-execute", lambda p, nxt: {"kind": "ask"})
        ctx2.on("tools/ask", lambda p, nxt: False)
        result2 = run_pipeline(ctx2, _make(lambda a, e: "ok"), {})
        self.assertTrue(result2.is_error)

    def test_exception_normalized(self):
        ctx = Context()
        tool = _make(lambda a, e: (_ for _ in ()).throw(RuntimeError("boom")))
        result = run_pipeline(ctx, tool, {})
        self.assertTrue(result.is_error)
        self.assertIn("boom", result.error)

    def test_non_json_value_normalized(self):
        ctx = Context()
        tool = _make(lambda a, e: object())
        result = run_pipeline(ctx, tool, {})
        self.assertTrue(result.is_error)
        self.assertIn("JSON", result.error)

    def test_timeout_enforced(self):
        ctx = Context()

        def slow(a, e):
            time.sleep(0.5)
            return "late"

        tool = _make(slow, timeout_ms=50)
        result = run_pipeline(ctx, tool, {})
        self.assertTrue(result.is_error)
        self.assertIn("timeout", result.error)

    def test_post_execute_block(self):
        ctx = Context()
        ctx.on("tools/post-execute", lambda p, nxt: {"action": "block", "feedback": "策略拒绝"})
        result = run_pipeline(ctx, _make(lambda a, e: "ok"), {})
        self.assertTrue(result.is_error)
        self.assertIn("策略拒绝", result.error)

    def test_render_separates_canonical_value(self):
        # 对齐上游 output.render：execute 返回 canonical 值，render 转模型可见 content
        ctx = Context()
        tool = _make(lambda a, e: {"approved": True},
                    render=lambda value: "Plan approved — exit")
        result = run_pipeline(ctx, tool, {})
        self.assertTrue(result.ok)
        self.assertEqual(result.content, "Plan approved — exit")

    def test_no_render_passes_value_through(self):
        # 无 render 时 content 即 canonical 值（向后兼容）
        ctx = Context()
        result = run_pipeline(ctx, _make(lambda a, e: {"approved": True}), {})
        self.assertTrue(result.ok)
        self.assertEqual(result.content, {"approved": True})


if __name__ == "__main__":
    unittest.main()