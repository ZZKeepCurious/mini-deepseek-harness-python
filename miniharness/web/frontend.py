"""web 静态服务：SPA 前端载体（对齐 `packages/host/frontend-static/src/index.ts`）。

契约（已核实，frontend-static 逐条对应）：
  * 只服务 dist 目录内的文件；`..` 上跳把候选路径推出根外 → 403（fail-closed）。
  * 未命中文件（含目录路径）→ 回退 `index.html` 200（SPA 客户端路由）；
    `/` 与空路径直接取 index。index 响应经 index taps 加工（上游注入 boot-manifest
    的 script；mini 无 bundle 清单 → identity，标注简化）。
  * MIME 按扩展名：.html→text/html; charset=utf-8、.js→text/javascript; charset=utf-8、
    .css→text/css; charset=utf-8、.svg→image/svg+xml、.json/.map→application/json、
    .webmanifest→application/manifest+json；未知扩展 → application/octet-stream。
  * GET/HEAD 之外的方法由载体层拒绝（405）；遍历 403 之外路径均不外泄目录结构。

纯函数：`serve_static(pathname, dist_root, dist_index)` → (status, headers, body)
或 None（= 404）。浏览器 GUI（`web/static/`）本身是教学简化：vanilla SPA，
无构建步、无 React monorepo、无 slot 组合系统。
"""
from __future__ import annotations

import os

__all__ = ["serve_static", "DIST_ROOT", "DIST_INDEX"]

#: 前端静态根：web/static/（随包分发，无构建产物）
DIST_ROOT = os.path.join(os.path.dirname(__file__), "static")
DIST_INDEX = "index.html"

#: frontend-static 的 MIME 表（index.ts 同款；'.html'/'.js' 带 charset）
_MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".map": "application/json",
    ".webmanifest": "application/manifest+json",
}

_FORBIDDEN = (403, {"content-type": "text/plain; charset=utf-8"}, b"forbidden")


def _mime_for(path: str) -> str:
    return _MIME_TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


def _serve_index(dist_root: str, dist_index: str):
    index_path = os.path.join(dist_root, dist_index)
    if not os.path.isfile(index_path):
        return None
    with open(index_path, "rb") as handle:
        body = handle.read()
    return (200, {"content-type": _mime_for(index_path)}, body)


def serve_static(pathname: str, dist_root: str, dist_index: str = DIST_INDEX):
    """按 pathname 服务 dist 内文件；返回 (status, headers, body) 或 None（404）。

    @param pathname - 以 '/' 开头的 URL 路径（不含 query）。
    @param dist_root - 静态文件根目录。
    @param dist_index - SPA 回退入口文件名。
    """
    if pathname == "/":
        return _serve_index(dist_root, dist_index)

    parts = [part for part in pathname.strip("/").split("/") if part]
    if any(part == ".." for part in parts):
        return _FORBIDDEN

    candidate = os.path.join(dist_root, *parts) if parts else dist_root
    # 推出根外（形如 root/./.. 的残余上跳）→ 403，绝不回退 index
    root_abs = os.path.abspath(dist_root)
    candidate_abs = os.path.abspath(candidate)
    if os.path.commonpath([root_abs, candidate_abs]) != root_abs:
        return _FORBIDDEN

    if os.path.isfile(candidate):
        with open(candidate, "rb") as handle:
            body = handle.read()
        return (200, {"content-type": _mime_for(candidate)}, body)

    # 未命中 → SPA 回退 index（客户端路由；frontend-static 同款 200）
    return _serve_index(dist_root, dist_index)