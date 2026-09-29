"""web profile 组合条目插件（`cli/plugins/*`，装配面在 cli —— 同 _web_main 先例）。

这些模块是 loader 插件条目（`module` 指向这里、`apply(ctx, **config)` 安装对应
服务），供 `cli/web_profile.yml` 默认 web 组合装载。每个条目一个 id，构成
`ConfigEditor.entries()` 的可配置命名空间（SettingsForms 读面）。载体上
仍是 mini 既有 install_* 装配函数，插件只是把「手写 _web_main 装配」声明化为
loader 条目树——触发条件「production web 装配接 profile boot」（tasks.md）
据此闭合。
"""