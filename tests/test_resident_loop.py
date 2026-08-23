"""resident_loop 常驻单循环的单元测试（教学扩展模块，上游无对应文件）。

覆盖：懒加载单例 + 循环身份稳定、协程提交与异常冒泡、Ctrl+C 协作取消分支、
显式停机幂等 + 重建。泵路径（run_on_resident 驱动同步门面）由 test_retry /
test_agent_loop 的 Loop 系列覆盖。
"""

import asyncio
import concurrent.futures as futures
import unittest

from miniharness.core.agent_loop.resident_loop import (
    _wait_interruptible,
    get_resident_loop,
    run_on_resident,
    shutdown_resident_loop,
)


class ResidentLoopTest(unittest.TestCase):
    def tearDown(self):
        shutdown_resident_loop()

    def test_lazy_singleton_and_stable_identity(self):
        loop1 = get_resident_loop()
        self.assertIs(loop1, get_resident_loop())

        async def which_loop():
            return asyncio.get_running_loop()

        self.assertIs(run_on_resident(which_loop()), loop1)

        # 显式停机后重建新实例（旧线程退出）
        shutdown_resident_loop()
        loop2 = get_resident_loop()
        self.assertIsNot(loop2, loop1)
        self.assertIs(run_on_resident(which_loop()), loop2)

    def test_coroutine_result_and_exception_propagate(self):
        async def ok():
            return 42

        async def boom():
            raise ValueError("boom")

        self.assertEqual(run_on_resident(ok()), 42)
        with self.assertRaises(ValueError):
            run_on_resident(boom())

    def test_wait_interruptible_reraises_keyboard_interrupt(self):
        fut = futures.Future()
        fut.set_exception(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            _wait_interruptible(fut)

    def test_shutdown_without_start_is_noop(self):
        shutdown_resident_loop()   # 从未启动：幂等无异常
        get_resident_loop()
        shutdown_resident_loop()
        shutdown_resident_loop()   # 已关闭后再关：幂等


if __name__ == "__main__":
    unittest.main()
