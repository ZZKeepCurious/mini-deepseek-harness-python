"""web 组合条目：agent-default-model（部署默认模型选择服务，L1 core）。

config.provider / config.model 为必填旋钮，config.reasoningEffort 可选；
profile patch 的保存面由同 id 条目的 ConfigEditor 持有。
"""


def apply(ctx, **config):
    from ...core.agent_default_model import install_agent_default_model

    install_agent_default_model(
        ctx,
        provider=config.get("provider"),
        model=config.get("model"),
        reasoning_effort=config.get("reasoningEffort"),
    )
