"""图像卸载 message 投影 + 执行器 + 目录 surface。

对照上游：
  * `packages/compaction/compaction-image-offload/tests/projection.spec.ts`；
  * `packages/session/session-format-catalog/tests/catalog.spec.ts`（readHeader / 链）。
"""
import unittest

from miniharness.compaction import install_image_offload, offload_oldest_images
from miniharness.core.session import (
    Session,
    create_message,
    default_message_projections,
    derive_messages,
    fold_projections,
    image_block,
    offload_message_images,
    thaw,
    tool_result_message,
)
from miniharness.core.session.released import read_released_header
from miniharness.llm import LlmFailure
from miniharness.llm.protocol import IMAGE_OFFLOAD_REQUIRED

_AID = "sha256:" + "a" * 64


def _ref():
    return {"attachmentId": _AID, "mediaType": "image/png", "bytes": 1,
            "width": 1, "height": 1}


def _img():
    return image_block(_ref())


def _user(content):
    return create_message("user", content, {"kind": "user"})


def _floor(session):
    """每个 surface 节点派生消息顶层的图片 offloaded 标记（深度优先索引）。"""
    marks = []

    def visit(blocks):
        for block in blocks:
            if block.get("type") == "image":
                marks.append(block.get("offloaded") is True)

    for message in derive_messages(session.events, default_message_projections()):
        visit(thaw(message.get("content") or []))
    return marks


class ProjectionTest(unittest.TestCase):
    def test_offload_marks_selected_depth_first_occurrence(self):
        session = Session("proj")
        event = session.append("user/message", _user([
            {"type": "text", "text": "before"}, _img(),
            _img(), {"type": "text", "text": "after"},
            {"type": "text", "text": "unchanged"},
            _img(),
        ]), surfaceOp="append")
        before = derive_messages(session.events, default_message_projections())
        session.append("image/offload", {"targets": [{"seq": event["seq"], "imageIndexes": [1]}]})
        self.assertEqual(_floor(session), [False, True, False])
        after = derive_messages(session.events, default_message_projections())
        # 身份保留：投影消息 id 与原始一致
        self.assertEqual(after[0]["id"], before[0]["id"])

    def test_offload_message_images_rejects_missing_or_repeat(self):
        message = _user([_img(), _img()])
        with self.assertRaises(ValueError):
            offload_message_images(message, [5])
        once = offload_message_images(message, [0])
        with self.assertRaises(ValueError):
            offload_message_images(once, [0])

    def test_fold_projections_only_current_nodes(self):
        session = Session("shadow")
        source = session.append("user/message", _user([_img(), _img()]), surfaceOp="append")
        session.append("image/offload", {"targets": [{"seq": source["seq"], "imageIndexes": [0]}]})
        nodes = session.surface_nodes()
        projected = fold_projections(list(session.events), nodes, default_message_projections())
        self.assertIn(source["seq"], projected)

    def test_append_validates_bad_targets(self):
        session = Session("validate")
        event = session.append("user/message", _user([_img()]), surfaceOp="append")
        for data in (
            {"targets": []},
            {"targets": [{"seq": 99, "imageIndexes": [0]}]},
            {"targets": [{"seq": event["seq"], "imageIndexes": []}]},
            {"targets": [{"seq": event["seq"], "imageIndexes": [0, 0]}]},
        ):
            with self.assertRaises(ValueError, msg=str(data)):
                session.append("image/offload", data)
        session.append("image/offload", {"targets": [{"seq": event["seq"], "imageIndexes": [0]}]})
        with self.assertRaises(ValueError):
            session.append("image/offload", {"targets": [{"seq": event["seq"], "imageIndexes": [0]}]})

    def test_tool_result_target(self):
        session = Session("tool")
        # V4：tool/result 消息 role 'tool' 平铺 content + 顶层 toolCallId
        message = tool_result_message("shot", [_img(), _img()], is_error=False)
        # tool/result 事件用 {turn, step, message} 包裹
        event = session.append("tool/result",
                               {"turn": 1, "step": 1, "message": message},
                               surfaceOp="append")
        session.append("image/offload", {"targets": [{"seq": event["seq"], "imageIndexes": [1]}]})
        self.assertEqual(_floor(session), [False, True])


class ExecutorTest(unittest.TestCase):
    def test_offload_oldest_images_across_nodes(self):
        session = Session("exec")
        session.append("user/message", _user([_img(), _img()]), surfaceOp="append")
        session.append("user/message", _user([_img()]), surfaceOp="append")
        self.assertTrue(offload_oldest_images(
            session, [n["seq"] for n in session.surface_nodes()], 2))
        self.assertEqual(_floor(session), [True, True, False])

    def test_offload_returns_false_when_nothing_retained(self):
        session = Session("none")
        session.append("assistant/message",
                       {"turn": 1, "step": 1, "stream": [],
                        "message": create_message("assistant", [], {"kind": "model"})},
                       surfaceOp="append")
        self.assertFalse(offload_oldest_images(
            session, [n["seq"] for n in session.surface_nodes()], 1))

    def test_install_listener_retries_on_image_offload_required(self):
        from miniharness.core.scope import Context

        ctx = Context(name="c")
        install_image_offload(ctx)
        session = Session("listener")
        session.append("user/message", _user([_img()]), surfaceOp="append")

        class _Agent:
            pass

        agent = _Agent()
        agent.session = session
        payload = {"agent": agent,
                   "failure": LlmFailure(IMAGE_OFFLOAD_REQUIRED, "need offload",
                                         offload_images=1)}
        outcome = ctx.waterfall("agent/request-error", payload)
        self.assertEqual(outcome, {"kind": "retry"})
        self.assertEqual(_floor(session), [True])

    def test_install_listener_passes_through_unrelated_failure(self):
        from miniharness.core.scope import Context

        ctx = Context(name="c")
        install_image_offload(ctx)
        session = Session("pass")
        session.append("user/message", _user([_img()]), surfaceOp="append")

        class _Agent:
            pass

        agent = _Agent()
        agent.session = session
        payload = {"agent": agent,
                   "failure": LlmFailure("RATE_LIMIT", "slow down")}
        outcome = ctx.waterfall("agent/request-error", payload)
        self.assertIs(outcome, payload)
        self.assertEqual(_floor(session), [False])


class CatalogSurfaceTest(unittest.TestCase):
    def test_read_header_classifies_current_and_migratable(self):
        old = {"type": "session", "version": 0, "id": "c", "createdAt": 1,
               "seedLength": 0, "delegationDepth": 0}
        result = read_released_header(old)
        self.assertEqual(result["status"], "migration-required")
        self.assertEqual(result["storedVersion"], 0)
        self.assertEqual(result["targetVersion"], 4)
        self.assertEqual(result["header"]["version"], 4)

        current = {"type": "session", "version": 4, "id": "c", "createdAt": 1,
                   "isSeeded": False, "delegationDepth": 0}
        now = read_released_header(current)
        self.assertEqual(now["status"], "current")
        self.assertEqual(now["header"]["id"], "c")

    def test_encode_current_header_and_event(self):
        from miniharness.core.session.released import SESSION_FORMAT_CATALOG

        header = SESSION_FORMAT_CATALOG.encode_current_header(
            {"version": 4, "id": "x", "createdAt": 1, "isSeeded": True,
             "delegationDepth": 2}, 4)
        self.assertEqual(header["type"], "session")
        self.assertTrue(header["isSeeded"])
        event = SESSION_FORMAT_CATALOG.encode_current_event(
            {"type": "user/message", "seq": 1, "time": 5, "data": {},
             "sourceEventSeqs": [0, 1, 2]})
        self.assertEqual(event["sourceEventSeqs"], [[0, 2]])


if __name__ == "__main__":
    unittest.main()
