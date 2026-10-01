"""Windows 沙箱 ACL 诊断技能（对齐 packages/sandbox/sandbox-windows-acl/src/acl-skill.ts）。

注册 bundled 技能 `diagnose-windows-sandbox-acl`：provider `dsh-windows-acl`、
source `bundled`、rank `BUNDLED_SKILL_RANK`、模型+用户均可调用、资源为一个目录
（`assets/diagnose-windows-sandbox-acl/`，含 `SKILL.md` + `scripts/diagnose-windows-sandbox-acl.ps1`）。

载体差异（登记）：上游把资源复制到 fiber 私有的临时目录（ASAR/SEA 打包下外部
PowerShell 也能执行，dispose 删目录）；mini 无打包资产层，直接以随包目录作
`resourceBase`（真实文件系统目录，脚本可直接执行），dispose 无需清理。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ...skills.registry import BUNDLED_SKILL_RANK

__all__ = ["ACL_DIAGNOSIS_SKILL", "install_acl_diagnosis_skill"]

#: bundled 技能名（acl-skill.ts:21）。
ACL_DIAGNOSIS_SKILL = "diagnose-windows-sandbox-acl"

#: provider 名（acl-skill.ts:24）。
PROVIDER = "dsh-windows-acl"

INVOCATION = {"modelInvocable": True, "userInvocable": True}

#: 资源根：miniharness/seams/sandbox_windows_acl/assets/（随包目录）。
_ASSET_ROOT = Path(__file__).resolve().parent / "assets"

_FRONTMATTER = re.compile(r"^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)")


def _parse_skill(raw: str, path: str) -> dict:
    """解析 SKILL.md frontmatter 取 description；正文去 frontmatter（acl-skill.ts:26-37）。"""
    match = _FRONTMATTER.match(raw)
    if match is None:
        raise RuntimeError(
            f"dsh-sandbox-windows-acl: {path} has no YAML frontmatter")
    import yaml
    metadata = yaml.safe_load(match.group(1))
    description = ""
    if isinstance(metadata, dict):
        description = metadata.get("description") or ""
    if not isinstance(description, str) or not description:
        raise RuntimeError(f"dsh-sandbox-windows-acl: {path} has no description")
    return {"description": description, "content": raw[match.end():].strip()}


class AclDiagnosisSkillProvider:
    """内置 `dsh-windows-acl` provider（对齐 acl-skill.ts:65-73）。"""

    name = PROVIDER

    def __init__(self, asset_root: Path | None = None):
        self._asset_root = Path(asset_root) if asset_root is not None else _ASSET_ROOT
        if not self._asset_root.is_absolute():
            raise RuntimeError("dsh-sandbox-windows-acl: assetRoot must be an absolute directory")
        directory = self._asset_root / ACL_DIAGNOSIS_SKILL
        locator = directory / "SKILL.md"
        script = directory / "scripts" / "diagnose-windows-sandbox-acl.ps1"
        if not locator.is_file():
            raise RuntimeError(
                f"dsh-sandbox-windows-acl: assets must contain "
                f"{ACL_DIAGNOSIS_SKILL}/SKILL.md")
        if not script.is_file():
            raise RuntimeError(
                f"dsh-sandbox-windows-acl: assets must contain "
                f"{ACL_DIAGNOSIS_SKILL}/scripts/diagnose-windows-sandbox-acl.ps1")
        parsed = _parse_skill(locator.read_text(encoding="utf-8"), str(locator))
        self._candidate = {
            "name": ACL_DIAGNOSIS_SKILL,
            "description": parsed["description"],
            "invocation": dict(INVOCATION),
            "provider": PROVIDER,
            "source": "bundled",
            "rank": BUNDLED_SKILL_RANK,
            "resourceBase": {"kind": "directory", "path": str(directory)},
            "locator": str(locator),
        }

    def list(self, options: dict | None = None) -> list[dict]:
        return [dict(self._candidate)]

    def get(self, candidate: dict, options: dict | None = None) -> dict | None:
        locator = candidate.get("locator")
        if candidate.get("provider") != PROVIDER or locator is None:
            return None
        parsed = _parse_skill(Path(locator).read_text(encoding="utf-8"), locator)
        summary = {k: v for k, v in candidate.items() if k not in ("rank", "locator")}
        return {**summary, "content": parsed["content"]}

    def invalidate(self) -> None:
        pass


def install_acl_diagnosis_skill(ctx: Any, *, asset_root: str | None = None) -> Any:
    """在 `ctx.skills` 注册内置 `dsh-windows-acl` provider（幂等；缺 skills 服务返回 None）。

    对齐 upstream `registerAclDiagnosisSkill`（acl-skill.ts:44-75）：仅在 win32
    内建 runner 上注册（见 `sandbox_local.LocalSandboxProvider`）。
    """
    skills = ctx.get("skills")
    if skills is None:
        return None
    return skills.register_provider(lambda control: AclDiagnosisSkillProvider(
        Path(asset_root) if asset_root is not None else None))
