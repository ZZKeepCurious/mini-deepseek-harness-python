"""web 组合条目：sandbox-policy（沙箱策略服务，L3 seams）。

config.mode 为可配置旋钮（SettingsForms 可见）；优先级 = 环境
DSH_PERMISSION_MODE（既有部署覆盖）> config.mode > 缺省 workspace-write。
"""


def apply(ctx, **config):
    import os

    from ...seams.sandbox_local import LocalSandboxProvider
    from ...seams.sandbox_policy import SandboxPolicyService

    mode = os.environ.get("DSH_PERMISSION_MODE") or config.get("mode") or "workspace-write"
    ctx.provide("sandbox", LocalSandboxProvider(ctx=ctx))
    SandboxPolicyService(ctx, {"mode": mode})