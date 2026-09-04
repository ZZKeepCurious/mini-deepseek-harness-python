"""步骤 12 验收：SessionStore 会话管理服务（对齐 packages/core/session/src/index.ts）。

覆盖：create/prepare/enter/announce/flush/get/list、fork 五错误码、
session/created|disposed|event|flush 四事件、announce throw 回滚、缺省 id mint。
"""
from __future__ import annotations

import os
import unittest

from miniharness.core.scope import Context
from miniharness.core.session import Session, create_message, text_block
from miniharness.core.session_store import (
    INVALID_BOUNDARY,
    OPEN_TURN,
    SESSION_ALREADY_EXISTS,
    SESSION_NOT_FOUND,
    SESSION_NOT_LIVE,
    SessionForkError,
    SessionStore,
    install_sessions,
)


def _fresh_store(ctx=None) -> tuple[Context, SessionStore]:
    ctx = ctx or Context(name="store-test")
    return ctx, SessionStore(ctx)


def _with_turn(session):
    session.append("turn/start", {"turn": 1})
    session.append("user/message", create_message(
        "user", [text_block("hi")], {"kind": "user"},
    ), surfaceOp="append")
    session.append("turn/end", {"turn": 1, "reason": {"kind": "completed"}})


class TestLifecycle(unittest.TestCase):
    def test_create_enters_and_announces(self):
        ctx, store = _fresh_store()
        seen = []
        ctx.on("session/created", lambda p: seen.append(p["session"]))
        s = store.create("s1")
        self.assertIs(store.get("s1"), s)
        self.assertEqual(store.list(), [s])
        self.assertEqual(seen, [s])

    def test_create_mints_default_id(self):
        ctx, store = _fresh_store()
        a = store.create()
        b = store.create()
        self.assertEqual(a.session_id, "session-1")
        self.assertEqual(b.session_id, "session-2")

    def test_duplicate_id_rejected(self):
        ctx, store = _fresh_store()
        store.create("s1")
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            store.create("s1")

    def test_prepare_does_not_enter(self):
        ctx, store = _fresh_store()
        s = store.prepare("s1")
        self.assertIsNone(store.get("s1"))
        with self.assertRaisesRegex(RuntimeError, "not live"):
            store.flush(s)

    def test_enter_then_announce(self):
        ctx, store = _fresh_store()
        created = []
        ctx.on("session/created", lambda p: created.append(p["session"]))
        s = store.prepare("s1")
        detach = store.enter(s)
        self.assertIs(store.get("s1"), s)
        self.assertEqual(created, [])  # enter 不公告
        store.announce(s)
        self.assertEqual(created, [s])

    def test_announce_twice_rejected(self):
        ctx, store = _fresh_store()
        s = store.prepare("s1")
        detach = store.enter(s)
        store.announce(s)
        with self.assertRaisesRegex(RuntimeError, "already announced"):
            store.announce(s)

    def test_detach_emits_disposed_when_announced(self):
        ctx, store = _fresh_store()
        disposed = []
        ctx.on("session/disposed", lambda p: disposed.append(p["session"]))
        s2 = store.prepare("s2")
        detach2 = store.enter(s2)
        store.announce(s2)
        detach2()
        self.assertIsNone(store.get("s2"))
        self.assertEqual(disposed, [s2])

    def test_detach_before_announce_no_disposed(self):
        ctx, store = _fresh_store()
        disposed = []
        ctx.on("session/disposed", lambda p: disposed.append(p["session"]))
        s = store.prepare("s1")
        detach = store.enter(s)
        detach()
        self.assertIsNone(store.get("s1"))
        self.assertEqual(disposed, [])

    def test_announce_throw_rolls_back_with_paired_disposal(self):
        ctx, store = _fresh_store()
        created = []
        disposed = []
        ctx.on("session/created", lambda p: (_ for _ in ()).throw(RuntimeError("veto")))
        ctx.on("session/disposed", lambda p: disposed.append(p["session"]))
        with self.assertRaisesRegex(RuntimeError, "veto"):
            store.create("s1")
        self.assertIsNone(store.get("s1"))
        self.assertEqual(created, [])
        self.assertEqual(len(disposed), 1)
        self.assertEqual(disposed[0].session_id, "s1")

    def test_meta_validation(self):
        ctx, store = _fresh_store()
        with self.assertRaisesRegex(RuntimeError, "absolute"):
            store.create("s1", {"meta": {"cwd": "relative"}})
        with self.assertRaisesRegex(RuntimeError, "subagent"):
            store.create("s1", {"meta": {"origin": "user"}})
        with self.assertRaisesRegex(RuntimeError, "isSeeded must be a boolean"):
            store.create("s1", {"meta": {"isSeeded": "yes"}})
        with self.assertRaisesRegex(RuntimeError, "string"):
            store.create("s1", {"meta": {"parentSession": 42}})


class TestEvents(unittest.TestCase):
    def test_append_fires_session_event_after_commit(self):
        ctx, store = _fresh_store()
        seen = []
        ctx.on("session/event", lambda p: seen.append((p["session"], p["event"])))
        s = store.create("s1")
        ev = s.append("turn/start", {"turn": 1})
        self.assertEqual(seen, [(s, ev)])
        # 事件即落日志后的记录本身
        self.assertEqual(seen[0][1]["seq"], 0)
        self.assertEqual(s.events[0], ev)

    def test_prepare_session_not_in_store_no_event(self):
        ctx, store = _fresh_store()
        seen = []
        ctx.on("session/event", lambda p: seen.append(p))
        s = store.prepare("s1")
        s.append("turn/start", {"turn": 1})
        self.assertEqual(seen, [])

    def test_seed_replay_does_not_fire_events(self):
        ctx, store = _fresh_store()
        parent = store.create("p")
        _with_turn(parent)
        seen = []
        ctx.on("session/event", lambda p: seen.append(p))
        child = store.create("c", {"seed": list(parent.events)})
        # 构造期回放不触发；之后 live append 才触发
        self.assertEqual(seen, [])
        child.append("turn/start", {"turn": 2})
        self.assertEqual(len(seen), 1)

    def test_listener_failure_contained(self):
        ctx, store = _fresh_store()

        class _Logger:
            def __init__(self):
                self.warns = []

            def warn(self, msg):
                self.warns.append(msg)

        ctx.logger = _Logger()
        def boom(p):
            raise RuntimeError("observer down")
        ctx.on("session/event", boom)
        s = store.create("s1")
        s.append("turn/start", {"turn": 1})  # 不能使 append 失败
        self.assertEqual(s.seq, 1)
        self.assertEqual(len(ctx.logger.warns), 1)

    def test_flush_dispatches_parallel_and_reports_participation(self):
        ctx, store = _fresh_store()
        s = store.create("s1")
        flushed = []
        ctx.on("session/flush", lambda p: flushed.append(p["session"]))
        self.assertTrue(store.flush(s))
        self.assertEqual(flushed, [s])

    def test_flush_no_listeners_returns_false(self):
        ctx, store = _fresh_store()
        s = store.create("s1")
        self.assertFalse(store.flush(s))

    def test_flush_not_live_rejected(self):
        ctx, store = _fresh_store()
        s = store.prepare("s1")
        with self.assertRaisesRegex(RuntimeError, "not live"):
            store.flush(s)


class TestFork(unittest.TestCase):
    def _forked_source(self):
        ctx, store = _fresh_store()
        parent = store.create("p")
        _with_turn(parent)
        return ctx, store, parent

    def test_fork_default_boundary(self):
        ctx, store, parent = self._forked_source()
        child = store.fork(parent, child_session_id="c")
        self.assertEqual(child.meta["parentSession"], "p")
        self.assertIs(child.is_seeded, True)
        self.assertEqual(child.inherited_event_count, len(parent.events))
        # 子会话 = 父日志回放 + 自动补记的 session/end-seed 标记
        self.assertEqual(list(child.events[:-1]), list(parent.events))
        self.assertEqual(child.events[-1]["type"], "session/end-seed")
        self.assertEqual(child.events[-1]["data"], {"inherited": True})
        self.assertEqual(child.session_id, "c")

    def test_fork_by_id(self):
        ctx, store, parent = self._forked_source()
        child = store.fork("p")
        self.assertEqual(child.session_id, "session-1")
        self.assertEqual(child.events[-1]["type"], "session/end-seed")

    def test_fork_empty_source_forks_empty_child(self):
        ctx, store = _fresh_store()
        parent = store.create("p")
        child = store.fork(parent)
        # V2：空源 fork = 显式空 seed + isSeeded，构造器恒补 {inherited:true} 标记
        self.assertIs(child.is_seeded, True)
        self.assertEqual(child.inherited_event_count, 0)
        self.assertEqual(len(child.events), 1)
        self.assertEqual(child.events[0]["type"], "session/end-seed")
        self.assertEqual(child.events[0]["data"], {"inherited": True})

    def test_fork_specific_boundary(self):
        ctx, store, parent = self._forked_source()
        child = store.fork(parent, boundary=2, child_session_id="c")
        self.assertIs(child.is_seeded, True)
        self.assertEqual(child.inherited_event_count, 3)  # slice(0, 3) = seq 0..2
        self.assertEqual(len(child.events), 4)  # 3 条回放 + 自动 end-seed 标记
        self.assertEqual(child.events[-1]["data"], {"inherited": True})

    def test_fork_not_found(self):
        ctx, store = _fresh_store()
        with self.assertRaises(SessionForkError) as cm:
            store.fork("ghost")
        self.assertEqual(cm.exception.code, SESSION_NOT_FOUND)

    def test_fork_source_object_not_live(self):
        ctx, store, parent = self._forked_source()
        detached = Session("ghost")
        with self.assertRaises(SessionForkError) as cm:
            store.fork(detached)
        self.assertEqual(cm.exception.code, SESSION_NOT_FOUND)

    def test_fork_stale_object_not_live(self):
        ctx, store, parent = self._forked_source()
        stale = Session("p")  # 同 id 但非 store 的 live 实例
        with self.assertRaises(SessionForkError) as cm:
            store.fork(stale)
        self.assertEqual(cm.exception.code, SESSION_NOT_LIVE)

    def test_fork_child_already_exists(self):
        ctx, store, parent = self._forked_source()
        store.create("c")
        with self.assertRaises(SessionForkError) as cm:
            store.fork(parent, child_session_id="c")
        self.assertEqual(cm.exception.code, SESSION_ALREADY_EXISTS)

    def test_fork_boundary_out_of_range(self):
        ctx, store, parent = self._forked_source()
        with self.assertRaises(SessionForkError) as cm:
            store.fork(parent, boundary=99)
        self.assertEqual(cm.exception.code, INVALID_BOUNDARY)

    def test_fork_boundary_negative(self):
        ctx, store, parent = self._forked_source()
        with self.assertRaises(SessionForkError) as cm:
            store.fork(parent, boundary=-1)
        self.assertEqual(cm.exception.code, INVALID_BOUNDARY)

    def test_fork_boundary_in_open_turn(self):
        ctx, store = _fresh_store()
        parent = store.create("p")
        parent.append("turn/start", {"turn": 1})
        parent.append("user/message", create_message(
            "user", [text_block("hi")], {"kind": "user"},
        ), surfaceOp="append")
        with self.assertRaises(SessionForkError) as cm:
            store.fork(parent, boundary=1)
        self.assertEqual(cm.exception.code, OPEN_TURN)

    def test_fork_inherits_cwd(self):
        ctx, store = _fresh_store()
        cwd = os.path.abspath("proj")   # 双平台均为绝对路径
        parent = store.create("p", {"meta": {"cwd": cwd}})
        _with_turn(parent)
        child = store.fork(parent)
        self.assertEqual(child.meta["cwd"], cwd)


class TestInstall(unittest.TestCase):
    def test_install_provides_service(self):
        ctx = Context(name="store-test")
        store = install_sessions(ctx)
        self.assertIs(ctx.get("sessions"), store)

    def test_install_idempotent(self):
        ctx = Context(name="store-test")
        first = install_sessions(ctx)
        second = install_sessions(ctx)
        self.assertIs(first, second)

    def test_install_adopts_existing(self):
        ctx = Context(name="store-test")
        mine = SessionStore(ctx)
        self.assertIs(install_sessions(ctx), mine)

    def test_constructor_auto_registers_service(self):
        ctx = Context(name="store-test")
        store = SessionStore(ctx)
        self.assertIs(ctx.get("sessions"), store)

    def test_service_removed_with_owning_fiber(self):
        root = Context(name="store-test")
        scope = root.create_scope("agent:1")
        mine = SessionStore(scope)
        self.assertIs(scope.get("sessions"), mine)
        self.assertIs(root.get("sessions"), mine)
        scope.dispose()
        self.assertIsNone(root.get("sessions"))
        self.assertIsNone(scope.get("sessions"))


if __name__ == "__main__":
    unittest.main()