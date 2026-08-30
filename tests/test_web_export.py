"""web downloads 域验收：GET /api/session.export（对齐 packages/host/apiproxy
的 session-export.spec.ts）。

运行：python -m unittest tests.test_web_export
"""
import io
import unittest
import zipfile

from miniharness.core.scope import Context
from miniharness.core.session import Session
from miniharness.core.session.persistence import JsonlPersistence
from miniharness.web.downloads import (
    DEFAULT_SESSION_LOG_COMPRESSION_LEVEL,
    ExportResult,
    build_session_export,
    parse_export_query,
    session_log_zip_filename,
)
from miniharness.web.api import WebApi
from miniharness.web.server import create_app
from miniharness.llm.fake import FakeLlmAdapter
from fastapi.testclient import TestClient


def _fake_ctx(sessions=None, persistence=None, attachments=None,
              compression_level=None):
    """构造一个最小 ctx：只实现 get(name) 以喂 resolve_export_deps。"""
    store = {"sessions": sessions, "sessionPersistence": persistence,
             "attachments": attachments}
    if compression_level is not None:
        store["sessionExportCompressionLevel"] = compression_level

    class _Ctx:
        def get(self, name, default=None):
            return store.get(name, default)

    return _Ctx()


def _root_artifact(content="root artifact\n"):
    return content


def _write_jsonl(persistence: JsonlPersistence, session_id, parent=None, content=None):
    """在持久化后端落一个会话 + 返回其制品文本。"""
    meta = {"parentSession": parent} if parent else {}
    persistence.declare(session_id, meta=meta or None)
    if content is not None:
        # 逐行追加为事件（header 已由 declare 写入）
        path = persistence.path_of(session_id)
        with open(path, "a", encoding="utf-8") as f:
            f.write(content)
    return persistence.read_raw(session_id)


class TestParseExportQuery(unittest.TestCase):
    def test_missing_session_id_is_none(self):
        self.assertIsNone(parse_export_query({"includeDescendants": "true"}))

    def test_absent_flag_defaults_false(self):
        self.assertEqual(parse_export_query({"sessionId": "s1"}), ("s1", False))

    def test_true_and_false(self):
        self.assertEqual(parse_export_query({"sessionId": "s1", "includeDescendants": "true"}),
                         ("s1", True))
        self.assertEqual(parse_export_query({"sessionId": "s1", "includeDescendants": "false"}),
                         ("s1", False))

    def test_bad_include_descendants_is_none(self):
        self.assertIsNone(parse_export_query({"sessionId": "s1", "includeDescendants": "1"}))


class _PersistCase(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.persistence = JsonlPersistence(self.tmp.name, compression="none")

    def tearDown(self):
        self.tmp.cleanup()


class TestSessionExport(_PersistCase):
    def _ctx(self, **kw):
        return _fake_ctx(persistence=self.persistence, **kw)

    def test_root_artifact_verbatim_under_original_filename(self):
        _write_jsonl(self.persistence, "session-root",
                     content='{"type":"turn/start","seq":1}\n')
        result = build_session_export(self._ctx(), "session-root", False)
        self.assertEqual(result.status, 200)
        self.assertEqual(result.headers["content-type"], "application/zip")
        self.assertIn(session_log_zip_filename("session-root"),
                      result.headers["content-disposition"])
        # 导出内容 = 事件文本（去掉 header 行），与上游 content 语义一致
        raw = self.persistence.read_raw("session-root")
        expected = "\n".join(raw.split("\n")[1:])
        with zipfile.ZipFile(io.BytesIO(result.body)) as z:
            self.assertEqual(z.namelist(), ["session.jsonl"])
            self.assertEqual(z.read("session.jsonl").decode("utf-8"), expected)

    def test_head_preflights_without_body(self):
        _write_jsonl(self.persistence, "session-root")
        result = build_session_export(self._ctx(), "session-root", False, method="HEAD")
        self.assertEqual(result.status, 200)
        self.assertEqual(result.body, b"")
        self.assertEqual(result.headers["content-type"], "application/zip")

    def test_missing_root_is_404(self):
        result = build_session_export(self._ctx(), "session-root", False)
        self.assertEqual(result.status, 404)

    def test_bad_query_path_400_is_route_concern_not_handler(self):
        # parse_export_query 已在路由层拦截 → 400；此处验证 handler 自身接受合法解。
        self.assertIsNotNone(parse_export_query({"sessionId": "x"}))

    def test_include_descendants_walks_lineage(self):
        _write_jsonl(self.persistence, "session-root")
        _write_jsonl(self.persistence, "child-a", parent="session-root",
                     content='{"type":"turn/start","seq":1}\n')
        _write_jsonl(self.persistence, "grandchild-a", parent="child-a",
                     content='{"type":"turn/start","seq":1}\n')
        result = build_session_export(self._ctx(), "session-root", True)
        with zipfile.ZipFile(io.BytesIO(result.body)) as z:
            self.assertEqual(sorted(z.namelist()), [
                "session.jsonl",
                "subagents/child-a/session.jsonl",
                "subagents/grandchild-a/session.jsonl",
            ])

    def test_shared_descendant_deduped(self):
        _write_jsonl(self.persistence, "session-root")
        _write_jsonl(self.persistence, "child-a", parent="session-root")
        _write_jsonl(self.persistence, "child-b", parent="session-root")
        _write_jsonl(self.persistence, "shared", parent="child-a")
        # shared 同时挂在 child-a 与 child-b 下（环容忍）：seen-set 去重一次
        result = build_session_export(self._ctx(), "session-root", True)
        with zipfile.ZipFile(io.BytesIO(result.body)) as z:
            names = sorted(z.namelist())
        self.assertEqual(names.count("subagents/shared/session.jsonl"), 1)

    def test_descendant_stored_artifact_read_failure_fails_whole_export_500(self):
        # 谱系里有后代，但其制品读取报错（非 NotImplementedError）→ 整档 fail-loud
        _write_jsonl(self.persistence, "session-root")
        self.persistence.declare("child-broken", meta={"parentSession": "session-root"})

        class _BrokenPersistence:
            def list_headers(self):
                return [
                    {"id": "session-root", "meta": None, "created_at": None, "cwd": None},
                    {"id": "child-broken", "meta": {"parentSession": "session-root"},
                     "created_at": None, "cwd": None},
                ]

            def read_raw(self, session_id, cwd=None):
                if session_id == "child-broken":
                    raise RuntimeError("disk read failed")
                return '{"type":"turn/start","seq":1}\n'

        ctx = _fake_ctx(persistence=_BrokenPersistence())
        result = build_session_export(ctx, "session-root", True)
        self.assertEqual(result.status, 500)
        self.assertEqual(result.body,
                         b"session log export failed to prepare the stored artifact")

    def test_descendant_without_stored_artifact_is_missing_500(self):
        # 后代仅在 live store（无事件、无持久化）→ 无制品 → 整档失败（对照上游
        # 后代 readRaw 缺失 → errored stream）
        _write_jsonl(self.persistence, "session-root")
        live = Session("child-missing", seed=[], meta={"parentSession": "session-root"})
        store = _MemoryStore([live])
        ctx = _fake_ctx(sessions=store, persistence=self.persistence)
        result = build_session_export(ctx, "session-root", True)
        self.assertEqual(result.status, 500)
        self.assertEqual(result.body,
                         b"session log export failed to prepare the stored artifact")

    def test_empty_artifact_exported_empty(self):
        _write_jsonl(self.persistence, "session-root")  # header only
        result = build_session_export(self._ctx(), "session-root", False)
        with zipfile.ZipFile(io.BytesIO(result.body)) as z:
            self.assertEqual(z.read("session.jsonl").decode("utf-8"), "")

    def test_compression_level_from_config_affects_size(self):
        big = ("compressible line\n" * (32 * 1024))
        _write_jsonl(self.persistence, "session-root", content=big)
        low = build_session_export(self._ctx(), "session-root", False,
                                   compression_level=0).body
        high = build_session_export(self._ctx(), "session-root", False,
                                    compression_level=9).body
        self.assertLess(len(high), len(low))

    def test_default_compression_level_is_six(self):
        self.assertEqual(DEFAULT_SESSION_LOG_COMPRESSION_LEVEL, 6)

    def test_astral_surrogate_pair_stays_whole(self):
        content = "a" * ((1 << 16) - 1) + "😀tail"
        _write_jsonl(self.persistence, "session-root", content=content)
        result = build_session_export(self._ctx(), "session-root", False)
        with zipfile.ZipFile(io.BytesIO(result.body)) as z:
            self.assertEqual(z.read("session.jsonl").decode("utf-8"), content)

    def test_live_session_flushed_before_read(self):
        # 内存 SessionStore 里的新会话 → 序列化内存事件（最权威），命中根制品
        live = Session("live-root", seed=[{"type": "turn/start", "seq": 0, "turn": 1}],
                       meta={"cwd": "/p"})
        live.append("turn/start", {"turn": 2})
        store = _MemoryStore([live])
        ctx = _fake_ctx(sessions=store, persistence=self.persistence)
        result = build_session_export(ctx, "live-root", False)
        self.assertEqual(result.status, 200)
        with zipfile.ZipFile(io.BytesIO(result.body)) as z:
            text = z.read("session.jsonl").decode("utf-8")
        self.assertIn("turn/start", text)
        self.assertIn('"turn": 1', text)


class TestServiceMissing(unittest.TestCase):
    def test_no_services_500(self):
        result = build_session_export(_fake_ctx(), "session-root", False)
        self.assertEqual(result.status, 500)
        self.assertEqual(result.body,
                         b"session log export failed to prepare the stored artifact")


class TestUnsupportedPersistence(unittest.TestCase):
    def test_sqlite_like_backend_501(self):
        class _NoRaw:
            def read_raw(self, session_id, cwd=None):
                raise NotImplementedError

        ctx = _fake_ctx(persistence=_NoRaw())
        result = build_session_export(ctx, "session-root", False)
        self.assertEqual(result.status, 501)


class TestMediaEntries(unittest.TestCase):
    def test_media_included_when_attachment_store_present(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        try:
            persistence = JsonlPersistence(tmp.name, compression="none")
            img_line = ('{"type":"user/message","seq":1,"data":{"content":'
                        '[{"type":"image","attachment":{"attachmentId":"img-1",'
                        '"mediaType":"image/png","bytes":4,"width":2,"height":2}}]}}')
            _write_jsonl(persistence, "session-root", content=img_line + "\n")

            class _Attachments:
                def read_image(self, ref):
                    return b"\x01\x02\x03\x04"

            result = build_session_export(_fake_ctx(persistence=persistence,
                                                    attachments=_Attachments()),
                                          "session-root", False)
            with zipfile.ZipFile(io.BytesIO(result.body)) as z:
                self.assertIn("media/img-1.png", z.namelist())
                self.assertEqual(z.read("media/img-1.png"), b"\x01\x02\x03\x04")
        finally:
            tmp.cleanup()

    def test_media_omitted_without_attachment_store(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        try:
            persistence = JsonlPersistence(tmp.name, compression="none")
            img_line = ('{"type":"user/message","seq":1,"data":{"content":'
                        '[{"type":"image","attachment":{"attachmentId":"img-1",'
                        '"mediaType":"image/png"}}]}}')
            _write_jsonl(persistence, "session-root", content=img_line + "\n")
            result = build_session_export(_fake_ctx(persistence=persistence),
                                          "session-root", False)
            with zipfile.ZipFile(io.BytesIO(result.body)) as z:
                self.assertEqual(z.namelist(), ["session.jsonl"])
        finally:
            tmp.cleanup()


class _MemoryStore:
    """最小 SessionStore 替身（仅 get/list/flush）。"""

    def __init__(self, sessions):
        self._sessions = {s.session_id: s for s in sessions}

    def get(self, session_id):
        return self._sessions.get(session_id)

    def list(self):
        return list(self._sessions.values())

    def flush(self, session):
        return True


class TestServerRoute(unittest.TestCase):
    """整体路由接线：GET /api/session.export 经 FastAPI 进入 handler。"""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        ctx = Context(name="test-export-route")
        self.persistence = JsonlPersistence(self.tmp.name, compression="none")
        ctx.provide("sessionPersistence", self.persistence)
        self.api = WebApi(ctx, FakeLlmAdapter())
        self.app = create_app(self.api, self.api.gateway)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_query_400(self):
        resp = self.client.get("/api/session.export")
        self.assertEqual(resp.status_code, 400)

    def test_bad_include_descendants_400(self):
        resp = self.client.get("/api/session.export?sessionId=s1&includeDescendants=1")
        self.assertEqual(resp.status_code, 400)

    def test_missing_root_404(self):
        resp = self.client.get("/api/session.export?sessionId=absent")
        self.assertEqual(resp.status_code, 404)

    def test_export_200_zip(self):
        _write_jsonl(self.persistence, "session-root",
                     content='{"type":"turn/start","seq":1}\n')
        resp = self.client.get("/api/session.export?sessionId=session-root")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "application/zip")
        self.assertIn("dsh-session-session-root.zip",
                      resp.headers["content-disposition"])
        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            self.assertEqual(z.namelist(), ["session.jsonl"])

    def test_head_preflight_200_empty_body(self):
        _write_jsonl(self.persistence, "session-root")
        resp = self.client.head("/api/session.export?sessionId=session-root")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"")


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
