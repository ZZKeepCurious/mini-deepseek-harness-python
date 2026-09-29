"""web 组合条目：session-title-first-prompt-llm（首条消息 LLM 标题提供者）。

依赖 session-title 服务（inject 声明，上游 session-title-first-prompt-llm
单独成包并依赖 session-title）；config 承载 LLM 预算旋钮
（targetWords/targetCjkCharacters/maxInputBytes/maxOutputTokens/timeoutMs，
SettingsForms 可见）。adapter 来自 boot env `ctx.adapter`。
"""

inject = ["sessionTitle"]


def apply(ctx, **config):
    from ...session_title import register_first_prompt_llm_provider

    adapter = ctx.get("adapter")
    register_first_prompt_llm_provider(ctx, adapter, config or None)