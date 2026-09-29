"""web 组合条目：usage-stats（telemetry usage 统计，L2 telemetry）。"""


def apply(ctx, **config):
    from ...telemetry import install_usage_stats

    install_usage_stats(ctx)