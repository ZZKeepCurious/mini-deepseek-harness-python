"""session-projection 注册 API v2 验收（对齐 packages/session/session-projection）。"""
import unittest

from miniharness.core.scope import Context
from miniharness.core.session_store import install_sessions
from miniharness.session_projection import (
    ProjectionDefinition,
    SessionProjectionRegistry,
    install_session_projections,
)
from miniharness.telemetry import (
    install_usage_stats,
    projection_values,
    register_telemetry_projections,
)


class TestSessionProjectionRegistry(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="root")
        self.store = install_sessions(self.ctx)
        self.registry = install_session_projections(self.ctx)
        self.session = self.store.create("s1", {"meta": {}})

    def tearDown(self):
        self.ctx.dispose()

    def _counter(self, version=1):
        return ProjectionDefinition(
            "count",
            init=lambda header, inherited: {"n": 0},
            apply=lambda state, event: (
                {"n": state["n"] + 1} if event["type"] == "turn/start" else state),
            view=lambda state: {"n": state["n"]},
            state_version=version,
        )

    def test_install_idempotent_and_service_visible(self):
        self.assertIs(install_session_projections(self.ctx), self.registry)
        self.assertIsInstance(self.registry, SessionProjectionRegistry)

    def test_state_and_snapshot_drive(self):
        dispose = self.registry.register(self._counter())
        try:
            self.assertEqual(self.registry.state_of(self.session, "count"), {"n": 0})
            self.assertEqual(self.registry.snapshot(self.session)["values"],
                             {"count": {"n": 0}})
            self.session.append("turn/start", {"turn": 1})
            self.assertEqual(self.registry.state_of(self.session, "count"), {"n": 1})
            self.assertEqual(self.registry.snapshot(self.session)["values"],
                             {"count": {"n": 1}})
            self.assertEqual(self.registry.snapshot(self.session)["asOfSeq"],
                             self.session.seq - 1)
        finally:
            dispose()

    def test_shared_key_refcount(self):
        first = self.registry.register(self._counter())
        second = self.registry.register(self._counter())
        first()
        self.assertIn("count", self.registry._registrations)
        second()
        self.assertNotIn("count", self.registry._registrations)

    def test_version_conflict_and_invalid(self):
        self.registry.register(self._counter())
        with self.assertRaisesRegex(ValueError, "already registered"):
            self.registry.register(self._counter(version=2))
        with self.assertRaisesRegex(ValueError, "stateVersion"):
            ProjectionDefinition("bad", init=lambda header, inherited: 0,
                                  apply=lambda state, event: state, state_version=-1)

    def test_on_changed_fires_on_view_change(self):
        changes = []
        dispose_listener = self.registry.on_changed(
            lambda session, key, value, seq: changes.append((key, value, seq)))
        dispose = self.registry.register(self._counter())
        try:
            self.session.append("turn/start", {"turn": 1})
            self.session.append("turn/start", {"turn": 2})
            self.assertEqual([c[0] for c in changes], ["count", "count"])
            self.assertEqual([c[1] for c in changes], [{"n": 1}, {"n": 2}])
        finally:
            dispose()
            dispose_listener()

    def test_host_only_unit_state_without_snapshot(self):
        host_only = ProjectionDefinition(
            "internal",
            init=lambda header, inherited: {"seen": 0},
            apply=lambda state, event: {**state, "seen": state["seen"] + 1},
            state_version=1,
        )
        dispose = self.registry.register(host_only)
        try:
            self.session.append("turn/start", {"turn": 1})
            self.assertEqual(self.registry.state_of(self.session, "internal"), {"seen": 1})
            self.assertNotIn("internal", self.registry.snapshot(self.session)["values"])
        finally:
            dispose()

    def test_late_registration_builds_from_history(self):
        self.session.append("turn/start", {"turn": 1})
        self.session.append("turn/start", {"turn": 2})
        dispose = self.registry.register(self._counter())
        try:
            self.assertEqual(self.registry.state_of(self.session, "count"), {"n": 2})
        finally:
            dispose()

    def test_checkpoint_restore_and_view_checkpoint(self):
        dispose = self.registry.register(self._counter())
        try:
            self.session.append("turn/start", {"turn": 1})
            self.session.append("turn/start", {"turn": 2})
            checkpoint = self.registry.checkpoint(self.session)
            self.assertEqual(checkpoint["count"],
                             {"ver": 1, "seq": 1, "val": {"n": 2}})
            self.assertEqual(self.registry.view_checkpoint(checkpoint),
                             {"count": {"n": 2}})
            self.assertEqual(self.registry.restore_floor(checkpoint), 1)
            tail = [{"type": "turn/start", "seq": 2, "data": {}}]
            restored = self.registry.restore(checkpoint, tail, 2, {}, 0)
            self.assertEqual(restored["snapshot"]["asOfSeq"], 2)
            self.assertEqual(restored["checkpoint"]["count"]["val"], {"n": 3})
        finally:
            dispose()

    def test_restore_requires_full_read_on_unusable_row(self):
        dispose = self.registry.register(self._counter())
        try:
            with self.assertRaisesRegex(ValueError, "re-read from seq 0"):
                self.registry.restore({}, [], 3, {}, 0)
        finally:
            dispose()

    def test_telemetry_units_back_projection_values(self):
        install_usage_stats(self.ctx)
        register_telemetry_projections(self.registry)
        values = projection_values(self.session, None, self.registry)
        self.assertEqual(set(values), {"sessionStats", "tokenUsage"})
        snapshot = self.registry.snapshot(self.session, ["sessionStats", "tokenUsage"])
        self.assertEqual(set(snapshot["values"]), {"sessionStats", "tokenUsage"})


if __name__ == "__main__":
    unittest.main()
