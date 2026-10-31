import asyncio
import unittest

from miniharness.core.scope import Context
from miniharness.core.tools import (
    Tool,
    ToolActivationContext,
    ToolActivationError,
    ToolRegistry,
)


class _FakeAgent:
    def __init__(self, ctx: Context):
        self.ctx = ctx


class _FakeSignal:
    aborted = False


class ToolActivationTests(unittest.IsolatedAsyncioTestCase):
    async def test_when_false_excludes_from_schemas(self):
        root = Context()
        reg = ToolRegistry(root)
        reg.register(Tool(name="always", description="a", execute=lambda a, e: None, parameters={}))
        reg.register(Tool(
            name="conditional",
            description="c",
            execute=lambda a, e: None,
            parameters={},
            when=lambda _ctx: False,
        ))
        agent = _FakeAgent(root)
        ctx = ToolActivationContext(agent=agent, model_selection={"provider": "p", "model": "m"}, signal=_FakeSignal())
        tool_set = await reg.resolve_activation(ctx)
        self.addCleanup(tool_set.dispose)
        schemas = reg.schemas(root, tool_set=tool_set)
        names = {s["name"] for s in schemas}
        self.assertEqual(names, {"always"})

    async def test_when_true_includes_tool(self):
        root = Context()
        reg = ToolRegistry(root)
        reg.register(Tool(
            name="conditional",
            description="c",
            execute=lambda a, e: None,
            parameters={},
            when=lambda _ctx: True,
        ))
        agent = _FakeAgent(root)
        ctx = ToolActivationContext(agent=agent, model_selection={}, signal=_FakeSignal())
        tool_set = await reg.resolve_activation(ctx)
        self.addCleanup(tool_set.dispose)
        self.assertIn("conditional", tool_set.view(root))

    async def test_when_raises_tool_activation_error(self):
        root = Context()
        reg = ToolRegistry(root)

        def boom(_ctx):
            raise ValueError("bad condition")

        reg.register(Tool(
            name="bad",
            description="b",
            execute=lambda a, e: None,
            parameters={},
            when=boom,
        ))
        agent = _FakeAgent(root)
        ctx = ToolActivationContext(agent=agent, model_selection={}, signal=_FakeSignal())
        with self.assertRaises(ToolActivationError) as caught:
            await reg.resolve_activation(ctx)
        self.assertEqual(caught.exception.tool_name, "bad")
        self.assertEqual(ToolActivationError.code, "TOOL_ACTIVATION_FAILED")

    def test_executable_without_tool_set_hides_conditional(self):
        root = Context()
        reg = ToolRegistry(root)
        reg.register(Tool(
            name="conditional",
            description="c",
            execute=lambda a, e: None,
            parameters={},
            when=lambda _ctx: True,
        ))
        self.assertIsNone(reg.executable("conditional", root, None))
        self.assertIsNotNone(reg.resolve("conditional", root))


if __name__ == "__main__":
    unittest.main()
