"""web 组合条目：approval（审批策略服务，L3 interaction）。

permission-presets 经 inject: [approval] 依赖本条目（上游 web-app 组合同样
先 compose user-approval 再 compose permission-presets）。
"""


def apply(ctx, **config):
    from ...interaction.approval import ApprovalService

    ctx.provide("approval", ApprovalService(ctx))