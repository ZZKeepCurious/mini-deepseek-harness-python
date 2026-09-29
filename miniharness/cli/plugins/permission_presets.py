"""web 组合条目：permission-presets（权限预设 M11，L3 interaction）。

依赖 approval（inject 声明；上游 permission-presets 依赖 user-approval）。
config.presets 为可配置旋钮（SettingsForms 可见）；缺省三预设同既有
_web_main 装配（read-only / workspace-write / danger-full-access）。
"""

inject = ["approval"]

_DEFAULT_PRESETS = {
    "read-only": {"sandbox": "read-only", "approval": "ask"},
    "workspace-write": {"sandbox": "workspace-write", "approval": "ask"},
    "danger-full-access": {"sandbox": "danger-full-access", "approval": "never"},
}


def apply(ctx, **config):
    from ...interaction.permission_presets import install_permission_presets

    install_permission_presets(
        ctx, {"presets": config.get("presets") or _DEFAULT_PRESETS})