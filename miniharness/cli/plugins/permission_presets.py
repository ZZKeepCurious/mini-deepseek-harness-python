"""web 组合条目：permission-presets（权限预设 M11，L3 interaction）。

依赖 approval（inject 声明；上游 permission-presets 依赖 user-approval）。
config.presets / config.defaultPreset 为可配置旋钮（SettingsForms 可见）；
缺省三预设同既有 _web_main 装配（read-only / workspace-write /
danger-full-access）。defaultPreset 必须透传：设置对话框「权限」行写的就是
它，丢弃会让写入持久化却不生效。
"""

inject = ["approval"]

_DEFAULT_PRESETS = {
    "read-only": {"sandbox": "read-only", "approval": "ask"},
    "workspace-write": {"sandbox": "workspace-write", "approval": "ask"},
    "danger-full-access": {"sandbox": "danger-full-access", "approval": "never"},
}


def apply(ctx, **config):
    from ...interaction.permission_presets import install_permission_presets

    settings = dict(config)
    settings["presets"] = settings.get("presets") or _DEFAULT_PRESETS
    install_permission_presets(ctx, settings)