"""miniharness.skills — skill 能力缝（对齐 packages/skill/：skill + skill-filesystem + tool-skill）。

契约面（已在 registry.py / filesystem.py / tool_skill.py 实现，与上游逐条一致）：
  * ctx.skills 服务：分层注册表（全局 + scope 链），list / snapshot / get /
    register / register_provider / invalidate；collect 按 revision 栅栏缓存、
    incomplete 观察不缓存、rank → providerOrder → localOrder 决胜、近层同名遮蔽
  * skill-filesystem provider：项目/自定义/用户/bundled 六类根发现、目录 bundle
    与扁平 .md、frontmatter 解析、kebab-case 名校验、invocation 策略
  * tool-skill：`skill` 工具（三态错误）+ `/name` 手势注入 + durable catalog
    注入（catalog-form context、digest 幂等、replacement update 语义）

装配约定（镜像 install_jobs）：`install_skills(ctx)`（幂等；创建注册表 + 挂
filesystem provider + 挂手势/catalog 两个 pre-step listener）。`skill` 工具注册走
`register_skill_tools(reg, ctx.skills)` —— ctx.tools 服务在 headless/demo/ACP/SDK
各入口的创建时机不同，不在 install_skills 内强绑。
"""
from __future__ import annotations

from .registry import (
    BUNDLED_SKILL_RANK,
    RUNTIME_PROVIDER,
    RUNTIME_RANK,
    SkillRegistry,
    digest_catalog_entries,
    escape_attr,
    escape_text,
    is_model_invocable,
    is_skill_name,
    is_user_invocable,
    render_resource_hint,
    render_skill_content,
)
from .filesystem import FileSystemSkillProvider
from . import tool_skill as _tool_skill

__all__ = [
    "BUNDLED_SKILL_RANK",
    "FileSystemSkillProvider",
    "RUNTIME_PROVIDER",
    "RUNTIME_RANK",
    "SKILL_GESTURE",
    "SkillRegistry",
    "SkillTool",
    "digest_catalog_entries",
    "escape_attr",
    "escape_text",
    "install_skills",
    "is_model_invocable",
    "is_skill_name",
    "is_user_invocable",
    "register_skill_tools",
    "render_resource_hint",
    "render_skill_content",
]

SKILL_GESTURE = _tool_skill.SKILL_GESTURE
SkillTool = _tool_skill.SkillTool


def install_skills(ctx, config: dict | None = None) -> SkillRegistry:
    """幂等装配：创建 ctx.skills 注册表 + filesystem provider + 两个 pre-step listener。

    config 键（对齐上游 tool-skill Config + skill-filesystem Config）：
      * catalogDescriptionMaxLength（默认 500，最小 3）
      * filesystem（dict：providerName / includeDefaultRoots / dshHome /
        agentsHome / customSkillDirs / bundledSkillDir）
    首个调用生效（后续调用忽略新 config）；已存在 ctx.skills 服务时"收养"它，
    补挂 provider 与 listener 后直接返回。
    """
    if getattr(ctx, "_miniharness_skills_installed", False):
        return ctx.get("skills")
    config = config or {}
    registry = ctx.get("skills")
    if registry is None:
        registry = SkillRegistry(ctx)
    ctx._miniharness_skills_installed = True
    filesystem_config = config.get("filesystem") or {}
    if not isinstance(filesystem_config, dict):
        raise TypeError("skills config['filesystem'] must be an object")
    registry.register_provider(
        lambda control: FileSystemSkillProvider(control, filesystem_config)
    )
    tool_skill = _tool_skill.install_skill_tool(ctx, registry, config)
    registry._skill_tool = tool_skill.skill_tool
    return registry


def register_skill_tools(tool_registry, skills) -> None:
    """把 `skill` 工具注册进现有 ToolRegistry（装配点显式调用）。

    skills 是 install_skills 返回的注册表（内部持有 skill 工具定义）。
    """
    skill_tool = getattr(skills, "_skill_tool", None)
    if skill_tool is not None:
        tool_registry.register(skill_tool)
