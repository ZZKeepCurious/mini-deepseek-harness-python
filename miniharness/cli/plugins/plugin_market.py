"""web 组合条目：plugin-market + pluginRegistryProbe（Discover / 安装对话框 Host 面）。

服务在 `configEditor` 就绪后装配（web profile 在 boot 之后才 install_config_editor）。
"""


def apply(ctx, **config):
    stored = dict(config)

    def mount(scope):
        scope.effect(lambda: _install(scope, stored), "plugin-market services")

    ctx.inject(["configEditor"], mount)


def _install(ctx, config):
    from ...boot.config_editor import ConfigEditor
    from ...boot.plugin_market import install_plugin_market
    from ...boot.plugin_registry_probe import install_plugin_registry_probe

    editor = ctx.get("configEditor")
    if not isinstance(editor, ConfigEditor):
        raise RuntimeError("plugin-market requires configEditor on ctx")
    install_plugin_registry_probe(ctx, **config)
    install_plugin_market(ctx, editor, **config)
