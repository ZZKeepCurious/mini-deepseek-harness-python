"""命令定义与执行的身份品牌类型。

对应上游 packages/interaction/commands/src/brand.ts：
  * CommandDefinitionId — 插件拥有的命令定义稳定身份
  * CommandId           — 命令执行的生命周期配对 id

上游 Branded<B> 是名义类型（nominal typing），Python 无原生支持，
故用 str 子类 + 显式构造函数模拟品牌效果：同底层字符串但不同类型，
避免跨域误用（如将 commandId 当作 definitionId 传）。
"""
from __future__ import annotations

__all__ = ["CommandDefinitionId", "CommandId"]


class CommandDefinitionId(str):
    """稳定、插件拥有的命令定义身份，独立于其名称和副本。

    品牌化字符串：底层值与 str 相同但类型不同，防止与普通命令名混淆。
    构造时不做校验（对齐上游 CommandDefinitionId 工厂函数）。
    """

    def __new__(cls, id: str) -> CommandDefinitionId:
        return super().__new__(cls, id)


class CommandId(str):
    """命令执行的生命周期配对 id。

    由执行器铸造，每个 service instance 单调递增；用于配对
    command/run 与 command/done 事件，并与 command.execute 准入响应关联。
    """

    def __new__(cls, id: str) -> CommandId:
        return super().__new__(cls, id)
