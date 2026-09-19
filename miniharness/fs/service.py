"""文件系统 Service Definition（对齐 packages/fs/fs/src/index.ts）。

`FileSystem` 是抽象后端：目标稳定身份、进程路径与 file URI、包含判定、文本读取、
解码、二进制拒绝、原子变更都由后端拥有。读窗口与观测态策略留在消费者与策略插件；
`edit_text` 留在本 seam，使版本检查、字面匹配与重写共享同一临界区。

事件（`fs/write-intent` / `fs/edit-intent` waterfall、`fs/observed` emit）由策略插件
（fs-observation-policy）与沙箱层（fs-sandbox）消费。
"""
from __future__ import annotations

from typing import Any, AsyncIterator

from ..core.scope import Context, Service
from .types import (
    FsDirEntry,
    FsEditOutcome,
    FsEditRequest,
    FsInfo,
    FsPathInfo,
    FsTarget,
    FsWriteIntent,
    FsWriteOutcome,
)

__all__ = ["FileSystem"]


class FileSystem(Service):
    """抽象文件系统后端（`ctx.fs`）。"""

    provide = "fs"

    def __init__(self, ctx: Context):
        super().__init__(ctx, "fs")

    @property
    def sandbox_mode(self) -> str | None:
        """本后端对变更默认强制的沙箱档位；不设限 → None（对齐上游 `sandboxMode`）。

        裸本地后端返回 None；沙箱后端覆写为部署缺省。会话覆盖可能收窄/放宽，故
        严格升级加宽按调用检查，不编码进本默认事实。"""
        return None

    async def resolve(self, path: str, opts: dict | None = None) -> FsTarget:
        """把模型/插件给出的路径解析成稳定目标（可做 I/O）。"""
        raise NotImplementedError

    def process_path(self, target: FsTarget) -> str:
        """子进程可打开的规范绝对路径。"""
        raise NotImplementedError

    def process_path_from_host_path(self, host_path: str) -> str | None:
        """把宿主绝对路径映射到本执行世界；基类无映射 → None。"""
        return None

    def file_url(self, target: FsTarget) -> str:
        """目标的规范 `file:` URI。"""
        raise NotImplementedError

    def contains(self, parent: FsTarget, child: FsTarget) -> bool:
        """规范包含判定：child 是 parent 或其后代。"""
        raise NotImplementedError

    async def stat(self, target: FsTarget, signal: Any = None) -> FsInfo | None:
        """目标元数据；缺失 → None（不读内容）。"""
        raise NotImplementedError

    async def lstat(self, path: str, opts: dict | None = None,
                    signal: Any = None) -> FsPathInfo | None:
        """路径级元数据（不跟随末段符号链接）；缺失 → None。"""
        raise NotImplementedError

    async def read_text(self, target: FsTarget, signal: Any = None) -> str:
        """整文件解码为文本。"""
        raise NotImplementedError

    def stream_text(self, target: FsTarget,
                    signal: Any = None) -> AsyncIterator[str]:
        """整文件解码为文本块（大文件；跨块解码与二进制拒绝由后端负责）。"""
        raise NotImplementedError

    async def read_bytes(self, target: FsTarget, signal: Any, max_bytes: int) -> bytes:
        """整文件原始字节；超 `max_bytes` → FS_TOO_LARGE（不返回截断）。"""
        raise NotImplementedError

    async def read_byte_range(self, target: FsTarget, offset: int, length: int,
                              signal: Any = None) -> bytes:
        """读取 `[offset, offset+length)` 窗口字节（不整文件缓冲）。"""
        raise NotImplementedError

    async def list_dir(self, target: FsTarget, signal: Any = None) -> list[FsDirEntry]:
        """稳定名字序列出直接子项（仅元数据）。"""
        raise NotImplementedError

    async def write_text(self, target: FsTarget, content: str,
                         expected: FsWriteIntent | None = None, signal: Any = None,
                         sandbox_policy: Any = None) -> FsWriteOutcome:
        """原子创建或替换 UTF-8 文本；`expected` 守卫意图与陈旧。"""
        raise NotImplementedError

    async def edit_text(self, target: FsTarget, edit: FsEditRequest,
                        expected: dict | None = None, signal: Any = None,
                        sandbox_policy: Any = None) -> FsEditOutcome:
        """原子字面编辑；`expected={'version': ...}` 先于匹配检查。"""
        raise NotImplementedError
