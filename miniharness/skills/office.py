"""Bundled Office skill provider（对齐 packages/skill/skill-office）。

上游对照：packages/skill/skill-office/src/index.ts —— 一个注册 `dsh-office`
provider 的 bundled 技能族：三个技能（office-docx / office-pptx /
office-xlsx），资源为 `<assetRoot>/<name>/SKILL.md` + 共享 `scripts/check_office.py`
（纯 stdlib 只读检查脚本），`list` 返回三候选、`get` 读入正文并追加
"Installed LibreOffice Kit" 段（含 libreofficeKit.node / cli 绝对路径）。

mini 载体差异（登记）：
  * 无 `@deepseek-ai/libreoffice-kit`（Node CLI + LibreOffice 二进制）——按上游
    `cli: false` 的既定降级分支语义，正文追加 "LibreOffice Kit is disabled in
    this deployment."；渲染/PDF 导出/公式重算登记为触发条件（环境有 soffice 时
    立项）。
  * 上游默认 `assetRoot` = 包 assets 目录；mini 用 `miniharness/skills/assets/`
    （随包数据，同 preset 目录载体）。
  * 上游注册在 sdk-app bundle（DSH_PRIMARY_RUNTIME 门控）；mini 在 SDK 入口 opt-in
    装配（`install_office_skills`，幂等）。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .registry import BUNDLED_SKILL_RANK

__all__ = [
    "OFFICE_SKILL_NAMES",
    "install_office_skills",
]

PROVIDER_NAME = "dsh-office"
OFFICE_SKILL_NAMES = ("office-docx", "office-pptx", "office-xlsx")
INVOCATION = {"modelInvocable": True, "userInvocable": True}

#: 上游 `officeRuntime()` 在 `cli: false` 时追加的段（index.ts:48）。
_DISABLED_RUNTIME = "\n\nLibreOffice Kit is disabled in this deployment."

#: 资源根：miniharness/skills/assets/（随包数据，同 preset 目录载体）。
_ASSET_ROOT = Path(__file__).resolve().parent / "assets"

_FRONTMATTER = re.compile(r"^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)")


def _parse_skill(raw: str, path: str) -> dict:
    """解析 SKILL.md frontmatter 取 description；正文去 frontmatter（index.ts:37-45）。"""
    match = _FRONTMATTER.match(raw)
    if match is None:
        raise RuntimeError(f"skill-office: {path} has no YAML frontmatter")
    import yaml
    metadata = yaml.safe_load(match.group(1))
    description = ""
    if isinstance(metadata, dict):
        description = metadata.get("description") or ""
    if not isinstance(description, str) or not description:
        raise RuntimeError(f"skill-office: {path} has no description")
    return {"description": description, "content": raw[match.end():].strip()}


class OfficeSkillProvider:
    """内置 `dsh-office` provider（对齐上游 skill-office index.ts:76-96）。"""

    name = PROVIDER_NAME

    def __init__(self, asset_root: Path | None = None):
        self._asset_root = Path(asset_root) if asset_root is not None else _ASSET_ROOT
        if not self._asset_root.is_absolute():
            raise RuntimeError("skill-office: assetRoot must be an absolute directory")
        checker = self._asset_root / "scripts" / "check_office.py"
        if not checker.is_file():
            raise RuntimeError("skill-office: assets must contain scripts/check_office.py")
        self._candidates = []
        for skill_name in OFFICE_SKILL_NAMES:
            directory = self._asset_root / skill_name
            path = directory / "SKILL.md"
            parsed = _parse_skill(path.read_text(encoding="utf-8"), str(path))
            self._candidates.append({
                "name": skill_name, "description": parsed["description"],
                "invocation": dict(INVOCATION),
                "provider": PROVIDER_NAME, "source": "bundled",
                "rank": BUNDLED_SKILL_RANK,
                "resourceBase": {"kind": "directory", "path": str(directory)},
                "locator": str(path),
            })

    def list(self, options: dict | None = None) -> list[dict]:
        return [dict(candidate) for candidate in self._candidates]

    def get(self, candidate: dict, options: dict | None = None) -> dict | None:
        locator = candidate.get("locator")
        if candidate.get("provider") != PROVIDER_NAME or locator is None:
            return None
        path = Path(locator)
        parsed = _parse_skill(path.read_text(encoding="utf-8"), locator)
        summary = {k: v for k, v in candidate.items()
                   if k not in ("rank", "locator")}
        return {
            **summary,
            "content": parsed["content"] + _DISABLED_RUNTIME,
        }

    def invalidate(self) -> None:
        pass


def install_office_skills(ctx: Any, *, asset_root: str | None = None,
                          cli: str | bool | None = None) -> None:
    """在 ctx.skills 注册内置 `dsh-office` provider（幂等；重复 provider 名 fail loud）。

    @param ctx 带 skills 服务的上下文。
    @param asset_root 可选外部资产目录（打包应用用；缺省随包 assets）。
    @param cli 上游 Config.cli（LibreOffice Kit CLI）。mini 无此载体，接受但
        语义为 disabled（只影响正文段；保留签名供未来 soffice 载体立项）。
    """
    skills = ctx.get("skills")
    if skills is None:
        return
    skills.register_provider(lambda control: OfficeSkillProvider(
        Path(asset_root) if asset_root is not None else None))