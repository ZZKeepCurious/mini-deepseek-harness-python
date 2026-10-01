"""文件后缀 → 语法高亮语言共享表（对齐 packages/util/code-language）。

上游对照：packages/util/code-language/src/index.ts（233 行）——Client 代码预览、
diff 审阅与 Host read 工具持久化 `lang` 提示共用的一张表。

- `LANGUAGE_EXTENSIONS`：规范语言 id → 后缀元组（`LANGUAGE_EXTENSIONS`）。
- `language_for_path`：后缀 → 规范语言 id（大小写不敏感；两种路径分隔符；
  前导点仍是分隔符，故 `.env` → `dotenv`）。
- `read_lang_hint_for_path`：Host read 卡片持久化的短 `lang` 值。可识别后缀
  取语言短名；少数后缀（tsx/jsx/tf/tfvars/gradle）以自身名作为更好标签，
  由 `READ_LANG_BY_EXTENSION` 覆盖。既会有会话已持有的值必须逐字保留，
  未列后缀取短名（ps1/csv/bat/env/log/proto/tf/tex/jl/v/gradle 等）。

载体差异（登记）：上游用 `Map` 规避 `Object.prototype` 键（`foo.constructor`
不能命中继承成员）；Python dict 无该缺陷。
"""
from __future__ import annotations

LANGUAGE_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "typescript": ("ts", "tsx", "mts", "cts"),
    "javascript": ("js", "jsx", "mjs", "cjs"),
    "shellscript": ("sh", "bash", "zsh"),
    "fish": ("fish",),
    "json": ("json", "jsonc", "jsonl", "ndjson", "ipynb"),
    "csv": ("csv",),
    "python": ("py", "pyw", "pyi"),
    "ruby": ("rb", "rake", "gemspec"),
    "go": ("go",),
    "rust": ("rs",),
    "java": ("java",),
    "c": ("c", "h"),
    "cpp": ("cc", "cpp", "cxx", "hh", "hpp", "hxx"),
    "csharp": ("cs",),
    "kotlin": ("kt", "kts"),
    "swift": ("swift",),
    "php": ("php",),
    "yaml": ("yaml", "yml"),
    "toml": ("toml",),
    "ini": ("ini", "conf", "cfg", "properties"),
    "dotenv": ("env",),
    "log": ("log",),
    "diff": ("diff", "patch"),
    "http": ("http",),
    "markdown": ("md", "markdown"),
    "mdx": ("mdx",),
    "rst": ("rst",),
    "latex": ("tex", "sty", "cls"),
    "bibtex": ("bib",),
    "asciidoc": ("adoc",),
    "html": ("html", "htm", "xhtml"),
    "css": ("css",),
    "scss": ("scss",),
    "less": ("less",),
    "sql": ("sql",),
    "xml": ("xml", "xsd", "xsl", "xslt", "plist", "svg"),
    "lua": ("lua",),
    "bat": ("bat", "cmd"),
    "powershell": ("ps1", "psm1", "psd1"),
    "r": ("r",),
    "julia": ("jl",),
    "dart": ("dart",),
    "scala": ("scala",),
    "clojure": ("clj", "cljs", "edn"),
    "erlang": ("erl", "hrl"),
    "elixir": ("ex", "exs"),
    "haskell": ("hs",),
    "fsharp": ("fs", "fsi", "fsx"),
    "vb": ("vb",),
    "perl": ("pl", "pm"),
    "verilog": ("v",),
    "system-verilog": ("sv", "svh"),
    "graphql": ("graphql", "gql"),
    "proto": ("proto",),
    "hcl": ("tf", "tfvars", "hcl"),
    "nix": ("nix",),
    "vue": ("vue",),
    "svelte": ("svelte",),
    "make": ("makefile", "mk"),
    "cmake": ("cmake",),
    "groovy": ("gradle", "groovy"),
}

#: 后缀 → 规范语言 id（大小写不敏感；键已小写）。
LANGUAGES: dict[str, str] = {
    extension: language
    for language, extensions in LANGUAGE_EXTENSIONS.items()
    for extension in extensions
}

#: 已识别后缀全集（每项恰一次）。预览注册表用它认领 Code 渲染体。
CODE_HIGHLIGHT_EXTENSIONS: tuple[str, ...] = tuple(LANGUAGES.keys())

#: 语言级短 id 无法表达的持久 `lang` 值（键 = 产生它的后缀）。其余后缀落
#: `SHORT_BY_LANGUAGE`。值是已记录会话持有的精确字符串，**不得变更**。
READ_LANG_BY_EXTENSION: dict[str, str] = {
    "tsx": "tsx",
    "jsx": "jsx",
    "tf": "tf",
    "tfvars": "tfvars",
    "gradle": "gradle",
}

#: 规范语言 id → Host read 卡片持久化的短 `lang` id。
SHORT_BY_LANGUAGE: dict[str, str] = {
    "typescript": "ts",
    "javascript": "js",
    "shellscript": "sh",
    "fish": "fish",
    "json": "json",
    "csv": "csv",
    "python": "py",
    "ruby": "rb",
    "go": "go",
    "rust": "rs",
    "java": "java",
    "c": "c",
    "cpp": "cpp",
    "csharp": "cs",
    "kotlin": "kotlin",
    "swift": "swift",
    "php": "php",
    "yaml": "yaml",
    "toml": "toml",
    "ini": "ini",
    "dotenv": "env",
    "log": "log",
    "diff": "diff",
    "http": "http",
    "markdown": "md",
    "mdx": "mdx",
    "rst": "rst",
    "latex": "tex",
    "bibtex": "bib",
    "asciidoc": "adoc",
    "html": "html",
    "css": "css",
    "scss": "scss",
    "less": "less",
    "sql": "sql",
    "xml": "xml",
    "lua": "lua",
    "bat": "bat",
    "powershell": "ps1",
    "r": "r",
    "julia": "jl",
    "dart": "dart",
    "scala": "scala",
    "clojure": "clj",
    "erlang": "erl",
    "elixir": "ex",
    "haskell": "hs",
    "fsharp": "fs",
    "vb": "vb",
    "perl": "pl",
    "verilog": "v",
    "system-verilog": "sv",
    "graphql": "graphql",
    "proto": "proto",
    "hcl": "hcl",
    "nix": "nix",
    "vue": "vue",
    "svelte": "svelte",
    "make": "make",
    "cmake": "cmake",
    "groovy": "groovy",
}

__all__ = [
    "CODE_HIGHLIGHT_EXTENSIONS",
    "LANGUAGES",
    "LANGUAGE_EXTENSIONS",
    "READ_LANG_BY_EXTENSION",
    "SHORT_BY_LANGUAGE",
    "extension_for_path",
    "language_for_path",
    "read_lang_hint_for_path",
]


def extension_for_path(path: str) -> str | None:
    """取末段最后一个点之后的后缀（小写）；无点 → None。前导点仍是分隔符。"""
    slash = max(path.rfind("/"), path.rfind("\\"))
    base = path[slash + 1:]
    dot = base.rfind(".")
    if dot < 0:
        return None
    return base[dot + 1:].lower()


def language_for_path(path: str) -> str | None:
    """文件名/路径 → 规范语言 id；未识别或无后缀 → None。"""
    extension = extension_for_path(path)
    if extension is None:
        return None
    return LANGUAGES.get(extension)


def read_lang_hint_for_path(path: str) -> str | None:
    """Host read 卡片持久化的短 `lang` 值；未识别后缀 → None。"""
    extension = extension_for_path(path)
    if extension is None:
        return None
    canonical = LANGUAGES.get(extension)
    if canonical is None:
        return None
    return READ_LANG_BY_EXTENSION.get(extension) or SHORT_BY_LANGUAGE.get(canonical)
