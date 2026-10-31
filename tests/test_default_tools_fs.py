"""default_tools 在 ctx.fs 在场时收编 read/write/edit。"""
import os
import tempfile
import unittest

from miniharness.cli.default_tools import default_tools
from miniharness.core.scope import Context
from miniharness.fs import install_local_fs


class DefaultToolsFsTest(unittest.TestCase):
    def test_registers_read_write_edit_when_fs_mounted(self):
        ctx = Context()
        self.addCleanup(ctx.dispose)
        with tempfile.TemporaryDirectory() as tmp:
            install_local_fs(ctx, {"cwd": tmp})
            reg = default_tools(ctx)
            self.assertIsNotNone(reg.resolve("read"))
            self.assertIsNotNone(reg.resolve("write"))
            self.assertIsNotNone(reg.resolve("edit"))
            self.assertIsNotNone(reg.resolve("apply_patch"))


if __name__ == "__main__":
    unittest.main()
