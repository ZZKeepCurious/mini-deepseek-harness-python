"""miniharness.jobs — 后台作业家族（对齐 packages/jobs/：seam + jobs-local + tool-jobs）。

契约面（已在 registry.py / tools.py 实现，与上游逐条一致）：
  * ctx.jobs 服务：start / list / get / read / kill / wait / onJobDone /
    onJobsChanged / attachController；owned 按会话 id 栅栏、结算 first-wins、
    teardown cancel force-fail 只改记录、无 `job/*` 会话事件
  * 模型侧三工具 job_output / job_list / job_kill + 完成 notice（busy 注入 /
    idle 唤醒，maxConsecutiveWakes 封顶）
  * 并发上限 maxConcurrentJobsPerOwner（默认 10，running+stopping 计）

装配约定（镜像 install_compaction）：`apply_retry_planner(ctx)` →
`install_compaction(ctx)` → `install_jobs(ctx)`（幂等；创建注册表 + 挂 controller
+ 装 notice 投递）。三工具注册走 `register_job_tools(reg, ctx.jobs)` —— ctx.tools
服务在 headless/demo/ACP/SDK 各入口的创建时机不同，不在 install_jobs 内强绑。
"""
from __future__ import annotations

from .registry import LocalJobRegistry
from .types import (
    DEFAULT_MAX_CONCURRENT_JOBS_PER_OWNER,
    TERMINAL_STATUSES,
    JobDoneBox,
    TASK_WAIT_TIMEOUT,
)
from . import tools as _tools

__all__ = [
    "DEFAULT_MAX_CONCURRENT_JOBS_PER_OWNER",
    "JobDoneBox",
    "LocalJobRegistry",
    "TASK_WAIT_TIMEOUT",
    "TERMINAL_STATUSES",
    "fit_completion_notice",
    "fit_with_suffix",
    "install_jobs",
    "job_kill_tool",
    "job_list_tool",
    "job_output_tool",
    "public_job",
    "register",
    "register_job_tools",
    "resolve_config",
    "status_line",
    "validate_job_id",
]

fit_completion_notice = _tools.fit_completion_notice  # noqa: F401
fit_with_suffix = _tools.fit_with_suffix  # noqa: F401
job_kill_tool = _tools.job_kill_tool  # noqa: F401
job_list_tool = _tools.job_list_tool  # noqa: F401
job_output_tool = _tools.job_output_tool  # noqa: F401
public_job = _tools.public_job  # noqa: F401
register = _tools.register  # noqa: F401
resolve_config = _tools.resolve_config  # noqa: F401
status_line = _tools.status_line  # noqa: F401
validate_job_id = _tools.validate_job_id  # noqa: F401

_REGISTRY_KEYS = ("maxConcurrentJobsPerOwner",)


def install_jobs(ctx, config: dict | None = None) -> LocalJobRegistry:
    """幂等装配：创建 ctx.jobs 注册表 + 挂 controller + 装完成 notice 投递。

    config 可混合 registry 键（maxConcurrentJobsPerOwner）与 tool-jobs 键
    （waitTimeoutMs / maxWaitTimeoutMs / completionDelivery / maxConsecutiveWakes）。
    首个调用生效（后续调用忽略新 config）；已存在 ctx.jobs 服务时"收养"它，
    补挂 controller 与 notice 投递后直接返回。
    """
    if getattr(ctx, "_miniharness_jobs_installed", False):
        return ctx.inject("jobs")
    config = config or {}
    try:
        registry = ctx.inject("jobs")
    except KeyError:
        registry_config = {k: config[k] for k in _REGISTRY_KEYS if k in config}
        registry = LocalJobRegistry(ctx, registry_config)
    ctx._miniharness_jobs_installed = True
    registry.attach_controller("tool-jobs")
    tool_config = {k: v for k, v in config.items() if k not in _REGISTRY_KEYS}
    _tools.install_completion_delivery(registry, tool_config)
    return registry


def register_job_tools(tool_registry, jobs, config: dict | None = None) -> None:
    """把 job_output / job_list / job_kill 注册进现有 ToolRegistry（装配点显式调用）。"""
    tool_config = {k: v for k, v in (config or {}).items() if k not in _REGISTRY_KEYS}
    _tools.register(tool_registry, jobs, tool_config)
