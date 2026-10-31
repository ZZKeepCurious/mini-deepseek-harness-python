import asyncio
import os
import tempfile
import unittest

from miniharness.core.scope import Context
from miniharness.core.tools import ToolRegistry
from miniharness.fs import install_fs_tools, install_local_fs


def _patch(*parts: str) -> str:
    body = "\n".join(parts)
    return f"*** Begin Patch\n{body}\n*** End Patch\n"


class ApplyPatchToolTest(unittest.TestCase):
    def test_apply_patch_add_and_update(self):
        ctx = Context()
        self.addCleanup(ctx.dispose)
        with tempfile.TemporaryDirectory() as tmp:
            install_local_fs(ctx, {"cwd": tmp})
            reg = ToolRegistry(ctx)
            install_fs_tools(ctx, apply_patch=True)
            tool = reg.resolve("apply_patch")
            self.assertIsNotNone(tool)

            class Exec:
                agent = None
                signal = None
                call_id = "c1"

            exec_ = Exec()
            exec_.agent = type("A", (), {"session": object()})()

            async def run():
                add = _patch("*** Add File: created.txt", "+new")
                value = await tool.execute({"input": add}, exec_)
                self.assertEqual(value["phase"], "commit")
                self.assertTrue(os.path.isfile(os.path.join(tmp, "created.txt")))
                with open(os.path.join(tmp, "created.txt"), encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), "new\n")
                update = _patch("*** Update File: created.txt", "@@", "-new", "+done")
                value2 = await tool.execute({"input": update}, exec_)
                self.assertFalse(value2["cancelled"])
                with open(os.path.join(tmp, "created.txt"), encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), "done\n")

            asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
