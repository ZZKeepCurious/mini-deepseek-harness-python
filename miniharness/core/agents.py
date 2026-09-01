"""进程内活动代理实例注册表：ctx.agents（对齐 packages/core/agent/src/index.ts 的 AgentRegistry）。

契约面（与上游逐条一致，index.ts:247 `class AgentRegistry extends Service`）：
  * 一条注册 = `ctx.agents` 服务（每 context 单实现，重复提供 fail loud）；构造即经
    ctx.provide("agents", self) 登记，随拥有 fiber 自动注销
  * register(agent, owner=None) 发布 live 实例：同一 id 已注册 → RuntimeError
    `agent "<id>" is already registered`（对齐 enter 的 get(id) 碰撞；
    id 须与会话 id 一致，否则 `agent id "x" does not match session id "y"`）
  * 查询面 get(id)/list()/roots()/is_owned_by(id, owner)（对齐 index.ts:575-609）
  * 生命周期事件（ctx 事件，非会话日志事件，不进 KNOWN_TYPES）：
      agent/created   发布（register 触发，载波=agent 自有载波）
      agent/disposed  注销（dispose 触发；条目随 agent scope 的 effect 自动卸载）
  * assert_live(agent)：get(agent.id) is agent 否则抛 AgentNotLive——job/goal 公共
    "当前注册实例"识别边界（对齐上游 agent-loop assertLive 的"陈旧实例拒绝"语义，
    载体不同：上游判 AbortSignal.aborted，mini 判 registry 精确实例）

实现载体（与上游差异，已在 verified-diffs §2.15 登记）：
  * 上游在 publish() 时 enter+announce 公告 agent（发出 agent/created）；mini 亦在
    AgentLoop.publish() 调用 register（对齐发布时机），但 mini 单同步进程、构造即
    运行态，agent/created 在 register 时立即派发（无 announce 两步；detach 补发
    agent/disposed 时同理）
  * 无 initiator AsyncLocalStorage 机制（currentInitiator/requireInitiator/withInitiator
    为上游异步驱动链归因，mini 同步模型不适用，属架构不适用项）
  * owner 缺省 None（root 级），subagent 运行时所有权仍由 SubagentContinuationManager
    ._live 承担（该注册表不承载运行时 owner 链）；roots() 当前即 list() 近似
  * assert_live 只对"安装 ctx.agents 的组合上下文"强制；裸单测装配（未安装 agents
    服务）不强制——生产组合一律 install_agents，达到"装配即强制"的边界
"""
from __future__ import annotations

from typing import Any

from .scope import Context, Service

__all__ = [
    "AgentNotLive",
    "AgentRegistry",
    "install_agents",
    "assert_live_agent",
]


class AgentNotLive(Exception):
    """目标 agent 不是 ctx.agents 中当前登记的 live 实例（陈旧/重复实例拒绝）。"""


class AgentRegistry(Service):
    """内存 agent 实例注册表（ctx.agents）：register + 查询面 + assert_live。

    构造即注册：super().__init__(ctx, "agents") 经 ctx.provide 登记，随拥有
    fiber 自动注销（对齐上游 `extends Service` + `super(ctx, 'agents')`）。
    """

    def __init__(self, ctx: Context):
        super().__init__(ctx, "agents")
        self._store: dict[str, dict] = {}

    # ---------- 发布 / 注销 ----------

    def register(self, agent: Any, owner: Any = None) -> Any:
        """登记一个进程内 live agent 实例，返回随 agent scope 卸载的 disposer。

        owner：运行时创建方 agent（上游 enter 的 owner；mini 缺省 None 即 root 级）。
        同一 id 已登记 → fail loud（碰撞边界，对齐 upstream enter:474）。
        发布 agent/created；agent scope 拆解时自动补发 agent/disposed。
        """
        id_ = getattr(agent, "id", None)
        if id_ != agent.session.session_id:
            raise RuntimeError(
                f'agent id "{id_}" does not match session id "{agent.session.session_id}"')
        if id_ in self._store:
            raise RuntimeError(f'agent "{id_}" is already registered')
        carrier = agent._carrier
        entry = {"id": id_, "agent": agent, "owner": owner, "carrier": carrier}
        self._store[id_] = entry

        def detach() -> None:
            if self._store.get(id_) is not entry:
                return
            self._store.pop(id_, None)
            self._emit("agent/disposed", {"agent": agent}, carrier)

        # 归 agent 自有 scope 所有：dispose()/scope.dispose() 逆序自动注销
        agent.ctx.effect(lambda: detach, f"agents.register({id_})")
        self._emit("agent/created", {"agent": agent}, carrier)
        return detach

    # ---------- 查询面（对齐 index.ts:575-609） ----------

    def get(self, id_: str) -> Any:
        entry = self._store.get(id_)
        return entry["agent"] if entry else None

    def list(self) -> list:
        return [e["agent"] for e in self._store.values()]

    def roots(self) -> list:
        return [e["agent"] for e in self._store.values() if e["owner"] is None]

    def is_owned_by(self, id_: str, owner: Any) -> bool:
        entry = self._store.get(id_)
        return entry is not None and entry["owner"] is owner

    # ---------- assert_live 边界 ----------

    def assert_live(self, agent: Any) -> None:
        """agent 必须是本注册表当前精确登记的实例（陈旧/重复拒绝）。"""
        if self.get(agent.id) is not agent:
            raise AgentNotLive(
                f'agent "{agent.id}" is not the live registered instance')

    # ---------- 内部 ----------

    def _emit(self, event: str, payload: dict, carrier: Any) -> None:
        try:
            self.ctx.emit(event, payload, this_arg=carrier)
        except Exception as error:
            logger = getattr(self.ctx, "logger", None)
            if logger is not None and hasattr(logger, "warn"):
                logger.warn(f"agent {payload.get('agent', {}).get('id')}: {event} dispatch threw: {error}")


def install_agents(ctx: Context) -> AgentRegistry:
    """幂等装配：创建 ctx.agents 服务。构造即自动注册（Service 基类经
    ctx.provide 登记）；首个调用生效；已存在时收养并直接返回。"""
    if getattr(ctx, "_miniharness_agents_installed", False):
        return ctx.get("agents")
    agents = ctx.get("agents")
    if agents is None:
        agents = AgentRegistry(ctx)
    ctx._miniharness_agents_installed = True
    return agents


def assert_live_agent(agent: Any) -> None:
    """job/goal 公共断言：agent 必须为 ctx.agents 中当前登记的 live 实例。

    未安装 ctx.agents 的组合上下文（裸单测装配）不强制——生产组合一律
    install_agents，达到"装配即强制"；实现与目标见模块 docstring。
    """
    ctx = getattr(agent, "ctx", None)
    if ctx is None:
        return
    agents = ctx.get("agents")
    if agents is None:
        return
    agents.assert_live(agent)
