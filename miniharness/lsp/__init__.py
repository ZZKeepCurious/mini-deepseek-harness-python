"""LSP 能力 seam（`ctx.lsp`，对齐 packages/lsp/lsp）。

Service Definition：语言服务 provider 注册表 + 按文件扩展名做**每查询、顺序无关**
的选择，暴露恰好四种语义查询（goToDefinition/findReferences/goToImplementation/
hover），无 JSON-RPC 逃生口。

契约（对齐上游 index.ts）：
  * provider 原子预留一个 id 与一组独占扩展名：`register_provider` 在改动前完成全部
    校验与冲突检查——非法或冲突的注册不发布任何东西（fail-loud、全有或全无）；
    返回的 disposer 一并释放 id 与全部扩展名。
  * 选择按文件**末段扩展名**路由（`final_extension`）；不依赖注册顺序。
  * 闭集结果 union：导航三类归一到 `locations`，hover 归一到内容或 None。

载体差异（登记）：
  * 上游 provider id 是 `Branded<'LspProviderId'>`；mini 以普通 str 承载（无类型品牌面）。
  * 上游 `register_provider` 经 `ctx.effect(function*)` 挂到调用 fiber；mini 以
    `ctx.effect(setup)` 等价（setup 登记并返回 disposer）。
  * 错误码 `LSP_WORKSPACE_REQUIRED` 由 tool-lsp 侧产出（本 seam 只认其余码）；
    `LSP_CANCEL_GRACE`/`LSP_SHUTDOWN` 是 lsp-stdio 内部 deadline 标签，非本 seam 码。
"""
from __future__ import annotations

import re
from typing import Any, Callable

from ..core.scope import Context, Service

__all__ = [
    "LSP_ERROR_CODES",
    "LSP_OPERATIONS",
    "Lsp",
    "LspError",
    "final_extension",
    "install_lsp",
]

#: 语义查询闭集（上游 LspOperation）。
LSP_OPERATIONS = ("goToDefinition", "findReferences", "goToImplementation", "hover")

#: 稳定错误码闭集（上游 LspError code 集合，含 tool-lsp 的 WORKSPACE_REQUIRED）。
LSP_ERROR_CODES = (
    "LSP_INVALID_PROVIDER",
    "LSP_CONFLICT",
    "LSP_UNAVAILABLE",
    "LSP_DISPOSED",
    "LSP_UNSUPPORTED_OPERATION",
    "LSP_MALFORMED_RESPONSE",
    "LSP_WORKSPACE_REQUIRED",
)

#: 合法扩展名：点 + 一个或多个非点、非分隔符字符。
_EXTENSION_PATTERN = re.compile(r"^\.[^./\\]+$")


class LspError(Exception):
    """结构化 LSP 失败（稳定 `code`，调用方按码路由而非解析 message）。"""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def final_extension(file_path: str) -> str:
    """取文件末段扩展名，规范为小写、带前导点（`Foo.TS`→`.ts`，`foo.d.ts`→`.ts`）。

    无扩展名或前导点 dotfile（`.bashrc`）→ `''`（永不匹配任何路由）。
    在 `/` 与 `\\` 上切分，使调用方路径分隔符不改变结果。
    """
    last_slash = max(file_path.rfind("/"), file_path.rfind("\\"))
    base = file_path[last_slash + 1:] if last_slash >= 0 else file_path
    dot = base.rfind(".")
    # dot <= 0 覆盖「无点」(-1) 与「前导点 dotfile」(0)：两者都无扩展名。
    if dot <= 0:
        return ""
    return base[dot:].lower()


class Lsp(Service):
    """`ctx.lsp`：持有 id 预留与扩展名→路由表，二者按 provider 一起增删。"""

    provide = "lsp"

    def __init__(self, ctx: Context):
        super().__init__(ctx, "lsp")
        self._provider_ids: set[str] = set()
        # ext -> {"provider": <LspProvider>, "languageId": str}
        self._routes: dict[str, dict] = {}

    def register_provider(self, provider: Any) -> Callable[[], None]:
        """注册一个 provider，原子预留其 id 与每个规范扩展名（对齐上游 registerProvider）。

        任何冲突或非法输入不发布任何东西并抛 `LspError`；返回的 disposer 释放全部预留。
        """
        pid = getattr(provider, "id", None)
        if not isinstance(pid, str) or pid.strip() == "":
            raise LspError("an LSP provider id must be a non-empty string",
                           "LSP_INVALID_PROVIDER")
        if pid in self._provider_ids:
            raise LspError(f'an LSP provider with id "{pid}" is already registered',
                           "LSP_CONFLICT")
        map_ = getattr(provider, "extension_to_language", None)
        if not isinstance(map_, dict):
            raise LspError(f'LSP provider "{pid}" extension map must be an object',
                           "LSP_INVALID_PROVIDER")
        if len(map_) == 0:
            raise LspError(f'LSP provider "{pid}" registers no file extensions',
                           "LSP_INVALID_PROVIDER")

        pending: dict[str, dict] = {}
        for raw_ext, language_id in map_.items():
            ext = _normalize_extension(raw_ext)
            if not _EXTENSION_PATTERN.match(ext):
                raise LspError(f'LSP provider "{pid}" maps an invalid extension "{raw_ext}"',
                               "LSP_INVALID_PROVIDER")
            if not isinstance(language_id, str) or language_id.strip() == "":
                raise LspError(
                    f'LSP provider "{pid}" maps extension "{ext}" to an empty language id',
                    "LSP_INVALID_PROVIDER")
            if ext in pending:
                raise LspError(
                    f'LSP provider "{pid}" maps extension "{ext}" more than once',
                    "LSP_INVALID_PROVIDER")
            pending[ext] = {"provider": provider, "languageId": language_id}
        for ext in pending:
            if ext in self._routes:
                raise LspError(
                    f'extension "{ext}" is already handled by another LSP provider',
                    "LSP_CONFLICT")

        def setup() -> Callable[[], None]:
            self._provider_ids.add(pid)
            for ext, route in pending.items():
                self._routes[ext] = route

            def dispose() -> None:
                self._provider_ids.discard(pid)
                for ext in pending:
                    self._routes.pop(ext, None)

            return dispose

        return self.ctx.effect(setup, "lsp.registerProvider()")

    async def query(self, request: dict, signal: Any = None) -> dict:
        """按文件扩展名选 provider 并跑一次查询（对齐上游 query）。

        选择每查询、顺序无关；无匹配 → `LSP_UNAVAILABLE`。
        """
        route = self._routes.get(final_extension(request["filePath"]))
        if route is None:
            raise LspError(f'no LSP provider handles "{request["filePath"]}"',
                           "LSP_UNAVAILABLE")
        provider_query = {
            "operation": request["operation"],
            "filePath": request["filePath"],
            "position": request["position"],
            "workspaceRoot": request["workspaceRoot"],
            "languageId": route["languageId"],
        }
        return await route["provider"].query(provider_query, signal)


def _normalize_extension(ext: Any) -> str:
    """小写并确保带前导点；`_EXTENSION_PATTERN` 拒绝其余。"""
    if not isinstance(ext, str):
        return ""
    lower = ext.lower()
    return lower if lower.startswith(".") else f".{lower}"


def install_lsp(ctx: Context) -> Lsp:
    """幂等装配 `ctx.lsp`（缺省不装：由 lsp-stdio 等 provider 插件按需挂）。"""
    existing = ctx.get("lsp")
    if existing is not None:
        return existing
    return Lsp(ctx)
