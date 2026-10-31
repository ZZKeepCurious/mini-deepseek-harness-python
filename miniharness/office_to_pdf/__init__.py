"""Office→PDF 转换服务（对齐 packages/document/office-to-pdf）。

浏览器经 `officeToPdf/render` 走 workspaceFiles 授权读源文件，进程内用 LibreOffice
`soffice --headless` 转成 PDF 字节返回；`officeToPdf/getGeneration` 暴露提供方
generation（引擎或配置变化时递增，供 Document Preview 失效缓存）。

载体：上游 `@deepseek-ai/libreoffice-kit`（Node 原生/WASM）；mini 在 PATH 或
`SOFFICE`/`LIBREOFFICE_PATH` 指向的 `soffice` 可用时转换，否则 render 以
`office-to-pdf/engine-unavailable` 拒绝（生产部署须安装 LibreOffice）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from ..core.scope import Context, Service

__all__ = [
    "OFFICE_EXTENSIONS",
    "OfficeToPdfFault",
    "OfficeToPdfService",
    "install_office_to_pdf",
    "resolve_soffice",
]

OFFICE_EXTENSIONS = frozenset({
    "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    "odt", "ods", "odp",
})


class OfficeToPdfFault(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def resolve_soffice() -> str | None:
    for key in ("SOFFICE", "LIBREOFFICE_PATH"):
        value = os.environ.get(key, "").strip()
        if value:
            path = Path(value)
            if path.is_file():
                return str(path)
            candidate = path / "soffice.exe" if os.name == "nt" else path / "soffice"
            if candidate.is_file():
                return str(candidate)
    for name in ("soffice", "soffice.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _extension(path: str) -> str:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    dot = name.rfind(".")
    if dot < 0:
        return ""
    return name[dot + 1:].lower()


class OfficeToPdfService(Service):
    """ctx.officeToPdf：generation + render（workspaceFiles 授权面）。"""

    provide = "officeToPdf"

    def __init__(self, ctx: Context):
        super().__init__(ctx)
        self._generation = f"gen_{uuid.uuid4().hex}"

    def bump_generation(self) -> None:
        self._generation = f"gen_{uuid.uuid4().hex}"

    def get_generation(self) -> dict[str, str]:
        return {"generation": self._generation}

    def render(self, workspace_files: Any, scope: dict, path: str,
               priority: str = "foreground") -> dict[str, Any]:
        del priority  # mini 同步转换，不区分队列优先级（登记简化）
        extension = _extension(path or "")
        if extension not in OFFICE_EXTENSIONS:
            raise OfficeToPdfFault(
                "office-to-pdf/invalid-input",
                f"unsupported office extension: {extension or '(none)'}",
                {"path": path},
            )
        soffice = resolve_soffice()
        if soffice is None:
            raise OfficeToPdfFault(
                "office-to-pdf/engine-unavailable",
                "LibreOffice (soffice) is not available in this deployment",
                {},
            )
        stat = workspace_files.stat(scope, path)
        source = workspace_files.read_bytes(scope, path, {})
        pdf_bytes = _convert_with_soffice(soffice, stat["absolutePath"], source["data"])
        return {
            "absolutePath": stat["absolutePath"],
            "version": stat.get("version"),
            "offset": 0,
            "eof": True,
            "bytes": len(pdf_bytes),
            "data": pdf_bytes,
            "missingFonts": [],
            "generation": self._generation,
        }


def _convert_with_soffice(soffice: str, absolute_path: str, data: bytes) -> bytes:
    with tempfile.TemporaryDirectory(prefix="dsh-office-pdf-") as tmp:
        tmp_path = Path(tmp)
        source_name = Path(absolute_path).name
        source_file = tmp_path / source_name
        source_file.write_bytes(data)
        cmd = [
            soffice,
            "--headless",
            "--norestore",
            "--convert-to",
            "pdf",
            "--outdir",
            str(tmp_path),
            str(source_file),
        ]
        completed = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            raise OfficeToPdfFault(
                "office-to-pdf/convert-failed",
                "LibreOffice conversion failed",
                {"stderr": stderr[:2000]},
            )
        pdf_path = tmp_path / f"{source_file.stem}.pdf"
        if not pdf_path.is_file():
            raise OfficeToPdfFault(
                "office-to-pdf/convert-failed",
                "LibreOffice did not produce a PDF output file",
                {},
            )
        return pdf_path.read_bytes()


def install_office_to_pdf(ctx: Context) -> OfficeToPdfService:
    existing = ctx.get("officeToPdf")
    if existing is not None:
        return existing
    service = OfficeToPdfService(ctx)
    ctx.set("officeToPdf", service)
    return service
