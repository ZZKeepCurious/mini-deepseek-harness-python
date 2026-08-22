"""Bundled `dsh-badge` skill provider（对齐 packages/skill/skill-badge）。

上游对照：packages/skill/skill-badge/src/index.ts（一个内置 provider，注册名为
`dsh-badge` 的 bundled skill；`list` 返回单候选、`get` 读入资源正文）。

mini 简化（须在文档标注）：上游 badge 资源是 `../assets/dsh-badge.md` 文件、经
`fileURLToPath` 读入；mini 内联同一份正文（仓库不含二进制/资源目录，教学边界），
语义等价。watch（chokidar 热失效）与 `ctx.fs` 文件系统能力服务仍按 P1-3 简化清单
保留（见 verified-diffs.md §3.3）。
"""
from __future__ import annotations

from typing import Any

from .registry import BUNDLED_SKILL_RANK

PROVIDER_NAME = "dsh-badge"

#: badge skill 的模型可见正文（上游 assets/dsh-badge.md 的同义内联等价物）。
_BADGE_BODY = """\
# Powered by DeepSeek Harness

Add the official "powered by dsh" badge to documents, pull requests, merge
requests, and other content produced with DeepSeek Harness.

Use whenever creating a pull request or merge request. Also use when the user
asks for a dsh badge, powered-by-dsh attribution, or a reusable dsh badge
asset or snippet.

## When to use

- Creating a pull request or merge request
- The user asks for dsh attribution or a reusable badge asset
"""

DESCRIPTION = (
    'Add the official "powered by dsh" badge to documents, pull requests, '
    "merge requests, and other content produced with DeepSeek Harness. Use "
    "whenever creating a pull request or merge request. Also use when the user "
    "asks for a dsh badge, powered-by-dsh attribution, or a reusable dsh badge "
    "asset or snippet."
)

INVOCATION = {"modelInvocable": True, "userInvocable": True}

_RESOURCE_BASE = {"kind": "directory", "path": "<bundled:dsh-badge>"}


class BadgeSkillProvider:
    """内置 `dsh-badge` provider（对齐上游 skill-badge index.ts:36-50）。"""

    name = PROVIDER_NAME

    def list(self, options: dict | None = None) -> list[dict]:
        return [{
            "name": "dsh-badge",
            "description": DESCRIPTION,
            "invocation": INVOCATION,
            "provider": PROVIDER_NAME,
            "source": "bundled",
            "rank": BUNDLED_SKILL_RANK,
            "locator": {"kind": "bundled", "name": "dsh-badge"},
            "resourceBase": dict(_RESOURCE_BASE),
        }]

    def get(self, candidate: dict, options: dict | None = None) -> dict | None:
        if candidate.get("name") != "dsh-badge":
            return None
        return {
            "name": "dsh-badge",
            "description": DESCRIPTION,
            "invocation": INVOCATION,
            "provider": PROVIDER_NAME,
            "source": "bundled",
            "resourceBase": dict(_RESOURCE_BASE),
            "content": _BADGE_BODY,
        }

    def invalidate(self) -> None:
        pass


def install_badge_skill(ctx: Any) -> None:
    """在 ctx.skills 注册内置 `dsh-badge` provider（幂等；重复 provider 名 fail loud）。"""
    skills = ctx.get("skills")
    if skills is None:
        return
    skills.register_provider(lambda control: BadgeSkillProvider())
