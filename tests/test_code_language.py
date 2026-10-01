"""code-language 共享表验收（对齐 packages/util/code-language/tests/code-language.spec.ts）。"""
import unittest

from miniharness.core.code_language import (
    CODE_HIGHLIGHT_EXTENSIONS,
    LANGUAGE_EXTENSIONS,
    SHORT_BY_LANGUAGE,
    language_for_path,
    read_lang_hint_for_path,
)


class TestLanguageForPath(unittest.TestCase):
    def test_windows_script_config_and_markup(self):
        cases = {
            "build.bat": "bat", "build.cmd": "bat", "deploy.ps1": "powershell",
            "module.psm1": "powershell", "config.fish": "fish",
            "app.properties": "ini", "app.conf": "ini", "app.cfg": "ini",
            ".env": "dotenv", "server.log": "log", "change.diff": "diff",
            "api.http": "http", "notebook.ipynb": "json", "table.csv": "csv",
            "guide.rst": "rst", "paper.tex": "latex", "refs.bib": "bibtex",
            "Info.plist": "xml", "logo.svg": "xml", "manual.adoc": "asciidoc",
            "analysis.r": "r", "model.jl": "julia", "main.dart": "dart",
            "Main.scala": "scala", "core.clj": "clojure", "app.erl": "erlang",
            "app.ex": "elixir", "Main.hs": "haskell", "Types.fs": "fsharp",
            "Form.vb": "vb", "script.pl": "perl", "top.v": "verilog",
            "top.sv": "system-verilog", "schema.graphql": "graphql",
            "message.proto": "proto", "main.tf": "hcl", "stack.hcl": "hcl",
            "build.groovy": "groovy", "flake.nix": "nix", "App.vue": "vue",
            "App.svelte": "svelte", "build.mk": "make", "CMakeLists.cmake": "cmake",
            "build.gradle": "groovy",
        }
        for path, expected in cases.items():
            self.assertEqual(language_for_path(path), expected, path)

    def test_case_insensitive_and_windows_separators(self):
        self.assertEqual(language_for_path("C:\\path\\X.PS1"), "powershell")
        self.assertEqual(language_for_path("C:\\Proj\\Build.CMD"), "bat")
        self.assertEqual(language_for_path("dir\\sub\\Main.SCALA"), "scala")

    def test_unknown_and_dotfiles(self):
        for path in (".gitignore", "/etc/hosts", "trailingdot.",
                     "data.unknownext", "a.py.bak", "archive.tar.gz",
                     "/dir.py/plain", "table.tsv", "secret.pem", "x.lock"):
            self.assertIsNone(language_for_path(path), path)

    def test_object_prototype_names_miss(self):
        for name in ("constructor", "__proto__", "toString", "hasOwnProperty"):
            self.assertIsNone(language_for_path(f"foo.{name}"))


class TestReadLangHint(unittest.TestCase):
    #: 迁移前 mini 手写表的 43 个后缀 → 持久值，必须逐字保留。
    LEGACY_BYTE_IDENTICAL = {
        "ts": "ts", "tsx": "tsx", "mts": "ts", "cts": "ts",
        "js": "js", "jsx": "jsx", "mjs": "js", "cjs": "js",
        "json": "json", "jsonc": "json",
        "py": "py", "rb": "rb", "go": "go", "rs": "rs", "java": "java",
        "c": "c", "h": "c", "cc": "cpp", "cpp": "cpp", "hpp": "cpp", "cxx": "cpp",
        "cs": "cs", "kt": "kotlin", "swift": "swift", "php": "php",
        "sh": "sh", "bash": "sh", "zsh": "sh",
        "yaml": "yaml", "yml": "yaml", "toml": "toml", "ini": "ini",
        "md": "md", "markdown": "md", "mdx": "mdx",
        "html": "html", "htm": "html", "css": "css", "scss": "scss", "less": "less",
        "sql": "sql", "xml": "xml", "lua": "lua",
    }

    def test_legacy_values_stay_byte_identical(self):
        self.assertEqual(len(self.LEGACY_BYTE_IDENTICAL), 43)
        for extension, expected in self.LEGACY_BYTE_IDENTICAL.items():
            self.assertEqual(read_lang_hint_for_path(f"file.{extension}"),
                             expected, extension)

    def test_previously_unlisted_suffixes_get_short_names(self):
        cases = {
            "build.ps1": "ps1", "table.csv": "csv", "deploy.bat": "bat",
            ".env": "env", "server.log": "log", "message.proto": "proto",
            "infra.tf": "tf", "paper.tex": "tex", "model.jl": "jl",
            "top.v": "v", "build.gradle": "gradle",
            "app.conf": "ini", "task.rake": "rb", "events.jsonl": "json",
            "page.xhtml": "html", "logo.svg": "xml", "notebook.ipynb": "json",
            "nomad.hcl": "hcl", "terraform.tfvars": "tfvars",
            "build.groovy": "groovy",
        }
        for path, expected in cases.items():
            self.assertEqual(read_lang_hint_for_path(path), expected, path)

    def test_undefined_when_unrecognized(self):
        for path in (".gitignore", "/etc/hosts", "trailingdot.",
                     "data.unknownext", "foo.constructor"):
            self.assertIsNone(read_lang_hint_for_path(path), path)

    def test_every_listed_suffix_persists_a_short_id(self):
        allowed = set(SHORT_BY_LANGUAGE.values()) | {"tsx", "jsx", "tf", "tfvars", "gradle"}
        for extension in CODE_HIGHLIGHT_EXTENSIONS:
            hint = read_lang_hint_for_path(f"file.{extension}")
            self.assertIsNotNone(hint, extension)
            self.assertIn(hint, allowed, extension)

    def test_extension_table_has_no_duplicate_suffix(self):
        flat = [ext for exts in LANGUAGE_EXTENSIONS.values() for ext in exts]
        self.assertEqual(len(flat), len(set(flat)))


if __name__ == "__main__":
    unittest.main()
