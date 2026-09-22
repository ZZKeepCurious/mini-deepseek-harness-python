"""file-reference / file-reference-local：`@file` 语法、模糊索引与发现服务。

对齐 packages/context/{file-reference,file-reference-local}。
"""

import os
import tempfile
import unittest
from types import SimpleNamespace

from miniharness.context.file_reference import (
    FILE_REFERENCE_PROMPT,
    active_at_token,
    format_file_mention,
)
from miniharness.context.file_reference_local import (
    LocalFileReferenceService,
    WorkspaceFileSearch,
    install_file_reference_local,
    resolve_search_config,
)
from miniharness.core.scope import Context


class TestGrammar(unittest.TestCase):
    def test_plain_token(self):
        self.assertEqual(active_at_token("see @src/ma", 11),
                         {"prefix": "@src/ma", "query": "src/ma", "quoted": False})
        self.assertEqual(active_at_token("@", 1),
                         {"prefix": "@", "query": "", "quoted": False})

    def test_quoted_token(self):
        token = active_at_token('see @"my file', 13)
        self.assertEqual(token, {"prefix": '@"my file', "query": "my file", "quoted": True})

    def test_email_is_not_a_trigger(self):
        self.assertIsNone(active_at_token("mail a@b.com", 12))

    def test_format_plain_and_directory(self):
        self.assertEqual(format_file_mention({"path": "src/a.ts", "kind": "file"}, False),
                         "@src/a.ts")
        self.assertEqual(format_file_mention({"path": "src", "kind": "directory"}, False),
                         "@src/")

    def test_format_quotes_whitespace_and_preserve(self):
        self.assertEqual(format_file_mention({"path": "a b.txt", "kind": "file"}, False),
                         '@"a b.txt"')
        self.assertEqual(format_file_mention({"path": "my dir", "kind": "directory"}, False),
                         '@"my dir/')
        self.assertEqual(format_file_mention({"path": "src/a.ts", "kind": "file"}, True),
                         '@"src/a.ts"')

    def test_format_rejects_unsafe_paths(self):
        self.assertIsNone(format_file_mention({"path": 'a"b', "kind": "file"}, False))

    def test_prompt_constant(self):
        self.assertIn("Tokens prefixed with @", FILE_REFERENCE_PROMPT)


class TestSearch(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self._file("src/main.py", "m")
        self._file("src/util.py", "u")
        self._file("README.md", "r")
        self._file("docs/guide.md", "g")
        self._file("node_modules/x.js", "x")
        self._file(".hidden.txt", "h")

    def _file(self, name, content):
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)

    def _search(self, **config):
        return WorkspaceFileSearch(self.root, config or {})

    def test_empty_query_lists_top_level_directories_first(self):
        paths = [c["path"] for c in self._search().list("")]
        self.assertEqual(paths, ["docs", "src", "README.md"])
        self.assertNotIn(".hidden.txt", paths)

    def test_bare_query_ranks_directory_then_paths(self):
        paths = [c["path"] for c in self._search().list("src")]
        self.assertEqual(paths[0], "src")
        self.assertIn("src/main.py", paths)
        self.assertIn("src/util.py", paths)

    def test_directory_scoped_query_reads_live_state(self):
        self.assertEqual([c["path"] for c in self._search().list("src/")],
                         ["src/main.py", "src/util.py"])
        self.assertEqual([c["path"] for c in self._search().list("src/m")],
                         ["src/main.py"])

    def test_excluded_and_hidden_rules(self):
        search = self._search()
        self.assertEqual(search.list("node"), [])
        self.assertIn(".hidden.txt", [c["path"] for c in search.list(".")])

    def test_invalidate_rebuilds_the_index(self):
        search = self._search()
        self.assertNotIn("later.py", [c["path"] for c in search.list("later")])
        self._file("later.py", "l")
        self.assertNotIn("later.py", [c["path"] for c in search.list("later")])
        search.invalidate()
        self.assertIn("later.py", [c["path"] for c in search.list("later")])

    def test_dispose_stops_returning_candidates(self):
        search = self._search()
        search.dispose()
        self.assertEqual(search.list("src"), [])

    def test_config_validation(self):
        with self.assertRaises(ValueError):
            resolve_search_config({"maxResults": 0})
        with self.assertRaises(ValueError):
            resolve_search_config({"excludedDirectories": ["a/b"]})


class _Agent:
    def __init__(self, cwd):
        self.session = SimpleNamespace(meta={"cwd": cwd})


class TestService(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        with open(os.path.join(self.root, "main.py"), "w", encoding="utf-8") as handle:
            handle.write("x")
        self.ctx = Context(name="file-reference-test")
        self.addCleanup(self.ctx.dispose)
        self.agent = _Agent(self.root)

    def test_service_lists_candidates_from_session_cwd(self):
        service = install_file_reference_local(self.ctx)
        self.assertEqual([c["path"] for c in service.list(self.agent, "")], ["main.py"])
        self.assertIs(install_file_reference_local(self.ctx), service)

    def test_service_is_idempotent_and_validates_config(self):
        with self.assertRaises(ValueError):
            LocalFileReferenceService(self.ctx, {"maxEntries": -1})


if __name__ == "__main__":
    unittest.main()
