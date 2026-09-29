"""web 组合条目：session-title（会话标题服务 M10，L2 session_title）。

需要 adapter（boot env 注入 `ctx.adapter`）。config 承载 fallback 旋钮
（fallbackMaxWords/fallbackMaxBytes/maxTitleBytes，SettingsForms 可见）。

LLM 标题提供者独立为 `session-title-first-prompt-llm` 条目（上游是两个
包：session-title + session-title-first-prompt-llm），本条目只装服务。
"""


def apply(ctx, **config):
    from ...session_title import install_session_title

    adapter = ctx.get("adapter")
    install_session_title(ctx, config or None, adapter=adapter)