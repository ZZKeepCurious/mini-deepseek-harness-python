"""fs-observation-policy 验收（对齐 packages/fs/fs-observation-policy）。"""
import unittest

from miniharness.core.scope import Context
from miniharness.fs import (
    FsError,
    FsObservation,
    FsTarget,
    FsWriteIntent,
    install_fs_observation_policy,
)


class _Session:
    pass


class _Agent:
    def __init__(self, session):
        self.session = session


class _Actor:
    def __init__(self, session):
        self.agent = _Agent(session) if session is not None else None


class TestObservationPolicy(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="root")
        self.gate = install_fs_observation_policy(self.ctx)
        self.session = _Session()
        self.actor = _Actor(self.session)
        self.target = FsTarget(target_key="k", display_path="/tmp/a.txt")

    def tearDown(self):
        self.ctx.dispose()

    def _write_intent(self, actor):
        return self.ctx.waterfall("fs/write-intent", (self.target, actor),
                                  base=lambda payload: None)

    def _edit_intent(self, actor):
        return self.ctx.waterfall("fs/edit-intent", (self.target, actor),
                                  base=lambda payload: None)

    def test_unseen_write_is_create_if_absent(self):
        self.assertEqual(self._write_intent(self.actor), FsWriteIntent("createIfAbsent"))

    def test_present_observation_yields_replace_guard(self):
        self.ctx.emit("fs/observed",
                      (self.target, FsObservation("present", "v1"), self.actor))
        self.assertEqual(self._write_intent(self.actor),
                         FsWriteIntent("replaceIfVersion", "v1"))
        self.assertEqual(self._edit_intent(self.actor), {"version": "v1"})

    def test_absent_observation_rejects_edit(self):
        self.ctx.emit("fs/observed", (self.target, FsObservation("absent"), self.actor))
        self.assertEqual(self._write_intent(self.actor), FsWriteIntent("createIfAbsent"))
        with self.assertRaises(FsError) as error:
            self._edit_intent(self.actor)
        self.assertEqual(error.exception.code, "FS_NOT_FOUND")

    def test_unseen_edit_rejected(self):
        with self.assertRaises(FsError) as error:
            self._edit_intent(self.actor)
        self.assertEqual(error.exception.code, "FS_NOT_OBSERVED")

    def test_no_owner_cannot_satisfy_policy(self):
        self.ctx.emit("fs/observed",
                      (self.target, FsObservation("present", "v1"), None))
        self.assertEqual(self._write_intent(None), FsWriteIntent("createIfAbsent"))
        with self.assertRaises(FsError) as error:
            self._edit_intent(None)
        self.assertEqual(error.exception.code, "FS_NOT_OBSERVED")

    def test_disposal_clears_state(self):
        self.ctx.emit("fs/observed",
                      (self.target, FsObservation("present", "v1"), self.actor))
        self.assertTrue(len(self.gate._observed))
        self.ctx.dispose()
        self.assertEqual(len(self.gate._observed), 0)


if __name__ == "__main__":
    unittest.main()
