"""Basic 压缩后端：自动压力检查 + context-overflow 恢复。

上游对照：packages/compaction/compaction-basic/src/index.ts（BasicCompactionEngine）。

接线语义（对齐上游 index.ts:147-223）：
  * agent/pre-step：request 派生前做压力检查（可选能力，不在 loop 脊柱上）；
    TargetPressureConfigError（缺 contextWindow 等）仅告警一次并继续回合。
  * agent/request-error：provider 报 CONTEXT_WINDOW_EXCEEDED 时绕过阈值强制减容；
    仅当 surface.replaceGeneration 前进才返回 {kind:'retry'}（重试一次只计一次
    overflowRetries，上限 maxOverflowRetries；assistant/message 或 turn/end 后复位）。
  * mini 装配序：apply_retry_planner 先于 install_compaction —— CONTEXT_WINDOW_EXCEEDED
    不在重试白名单（终局降级语义），由本引擎接管强制减容。
"""
from __future__ import annotations

from ..llm.token_meter import TokenMeter
from .config import TargetPressureConfigError, resolve_config, resolve_spec, resolve_target_policy
from .region import compact_surface_region, inspect_compaction_entry_state, select_compactable_range

__all__ = ["CompactionEngine", "CONTEXT_WINDOW_EXCEEDED"]

from ..llm import CONTEXT_WINDOW_EXCEEDED


class CompactionEngine:
    """依赖 token 计量的压缩后端：压力触发 + overflow 恢复。

    构造即解析配置；auto=True 时注册 agent/pre-step 与 agent/request-error
    监听器（对齐上游插件 apply 时挂载，AgentLoop 构造无副作用）。
    """

    def __init__(self, ctx, config: dict | None = None):
        self.ctx = ctx
        self.config = resolve_config(config)
        self.meter = TokenMeter()
        self._overflow_state: dict[int, list] = {}
        self._warned_pressure_targets: set[str] = set()
        if self.config["auto"]:
            self._register_automatic()

    def _target_policy(self, target: dict) -> dict:
        """按 target 合并全局策略 + modelPolicies 精确覆盖（对齐 resolveTargetPolicy）。"""
        return resolve_target_policy(self.config, target)

    # ---------- 自动压缩监听 ----------

    def _register_automatic(self) -> None:
        ctx = self.ctx

        async def on_pre_step(payload: dict, next_fn):
            agent = payload.get("agent")
            signal = payload.get("signal")
            if agent is not None and not (signal is not None and getattr(signal, "aborted", False)):
                try:
                    result = await self.compact_if_needed(agent, "pressure")
                    if result is not None:
                        self._log_result(result, "step pressure")
                except TargetPressureConfigError as error:
                    if error.target_key not in self._warned_pressure_targets:
                        self._warned_pressure_targets.add(error.target_key)
                        # 上游 index.ts:156-161：TargetPressureConfigError 首次也 warn，
                        # 之后同一 target 静默（warnedPressureConfigTargets 去重）
                        ctx.logger.warn(
                            f"step compaction failed: {error}; continuing the turn"
                        ) if hasattr(ctx, "logger") else None
                except Exception as error:  # noqa: BLE001 - 压缩失败不中断回合（上游同语义）
                    ctx.logger.warn(f"step compaction failed: {error}; continuing the turn") \
                        if hasattr(ctx, "logger") else None
            return next_fn()

        async def on_request_error(payload: dict, next_fn):
            failure = payload.get("failure")
            agent = payload.get("agent")
            signal = payload.get("signal")
            if agent is None or failure is None or failure.code != CONTEXT_WINDOW_EXCEEDED:
                return next_fn()
            if signal is not None and getattr(signal, "aborted", False):
                return next_fn()
            if self._routed_target(agent) is None:
                return next_fn()
            state = self._overflow_state.setdefault(id(agent), [0, None])
            boundary = self._reset_boundary_seq(agent.session)
            # 成功响应（assistant/message）或回合结束（turn/end）已推进边界 → 复位计数
            if boundary is not None and boundary != state[1]:
                state[0] = 0
            retries = state[0]
            if retries >= self.config["maxOverflowRetries"]:
                return next_fn()
            generation = agent.session.replace_generation
            try:
                result = await self.compact_if_needed(agent, "context-overflow")
            except Exception as error:  # noqa: BLE001
                if not getattr(signal, "aborted", False) \
                        and agent.session.replace_generation > generation:
                    state[0] = retries + 1
                    state[1] = boundary
                    return {"kind": "retry"}
                return next_fn()
            if getattr(signal, "aborted", False) \
                    or agent.session.replace_generation <= generation:
                return next_fn()
            if result is not None:
                self._log_result(result, "context overflow recovery")
            state[0] = retries + 1
            state[1] = boundary
            return {"kind": "retry"}

        ctx.on("agent/pre-step", on_pre_step)
        ctx.on("agent/request-error", on_request_error)

    @staticmethod
    def _reset_boundary_seq(session) -> int | None:
        """最近一个成功/结束边界（assistant/message | turn/end）的 seq，无则 None。

        mini 简化标注：上游在 status idle 与 assistant/message 两个监听点显式
        复位 overflow 计数；mini 无 session 事件总线（Session 为纯日志，从不
        派发 session/event），改为在 request-error 时对照边界 seq 惰性复位，
        语义等价。
        """
        for ev in reversed(session.events):
            if ev["type"] in ("assistant/message", "turn/end"):
                return ev["seq"]
        return None

    # ---------- 触发入口 ----------

    async def compact_if_needed(self, agent, trigger: str):
        """按触发类型考虑自动压缩；不需要/无安全区间返回 None。

        trigger: 'pressure'（step 边界压力）| 'context-overflow'（provider 确认溢出）。
        """
        if trigger not in ("pressure", "context-overflow"):
            raise ValueError(f"unknown compaction trigger: {trigger}")
        target = self._routed_target(agent)
        if target is None:
            return None
        # 可选阶段：toolResultPruner 未安装则跳过模型无关裁剪（上游
        # compaction-basic index.ts:281 经 ctx.get('toolResultPruner') 取用）
        prune = self._tool_result_pruner()
        measurement = self.meter.measure(agent.session)
        if trigger == "context-overflow":
            if prune is not None:
                prune.prune_session(agent.session)
                measurement = self.meter.measure(agent.session)
            range_ = select_compactable_range(agent.session, measurement, 0)
            if range_ is None:
                return None
            return await self._compact_region(agent, range_["start"], range_["end"])
        # 压力检查：缺 contextWindow 视为配置失败（上游 index.ts:296-302 抛
        # TargetPressureConfigError，pre-step 捕获后 warn 一次并继续回合）
        context_window = getattr(agent.adapter, "context_window", None)
        if context_window is None:
            target_key = f"{target['provider']}/{target['model']}"
            raise TargetPressureConfigError(
                target_key,
                f"compaction-basic: no context capacity for {target_key}; "
                "configure contextWindow on that adapter model",
            )
        merged = self._target_policy(target)
        spec = resolve_spec(merged, context_window)
        if measurement["totalTokens"] < spec["thresholdTokens"]:
            return None
        # 压力达标后先落模型无关裁剪，再重新测量；若已降到阈值以下则无需摘要
        if prune is not None:
            prune.prune_session(agent.session)
            measurement = self.meter.measure(agent.session)
        if measurement["totalTokens"] < spec["thresholdTokens"]:
            return None
        result = None
        for _attempt in range(spec["compactionRetries"] + 1):
            range_ = select_compactable_range(agent.session, measurement, spec["retainTokens"])
            if range_ is None:
                return result
            result = await self._compact_region(agent, range_["start"], range_["end"])
            measurement = self.meter.measure(agent.session)
            if measurement["totalTokens"] < spec["thresholdTokens"]:
                return result
        raise RuntimeError(
            f"compaction still above threshold after {spec['compactionRetries'] + 1} "
            f"compaction attempts ({measurement['totalTokens']} estimated tokens >= "
            f"threshold {spec['thresholdTokens']})"
        )

    async def compact_region(self, agent, start: int, end: int) -> dict:
        """强制压缩一个 surface 区间（start/end 为 seq；边界必须配对平衡）。"""
        return await compact_surface_region(agent.session, self.meter, agent, self.config, start, end)

    # ---------- 内部 ----------

    async def _compact_region(self, agent, start: int, end: int) -> dict:
        return await compact_surface_region(agent.session, self.meter, agent, self.config, start, end)

    def _tool_result_pruner(self):
        """取用可选的 toolResultPruner 服务（未安装返回 None）。"""
        return self.ctx.get("toolResultPruner")

    def _routed_target(self, agent):
        """最新 durable 路由请求的 provider/model（request/header 信封
        config，无则 None；对齐上游 compaction-basic routedTarget 读
        session.requestHeader().config）。"""
        for ev in reversed(agent.session.events):
            if ev["type"] != "request/header":
                continue
            config = ev["data"].get("header", {}).get("config", {})
            provider = config.get("provider")
            model = config.get("model")
            if provider and model:
                return {"provider": provider, "model": model}
            return None
        return None

    def _log_result(self, result: dict, trigger: str) -> None:
        count = len(result["shadowedSeqs"])
        shadowed = result["shadowedRange"]
        message = (
            f"compaction ({trigger}): shadowed {count} surface nodes "
            f"(seqs {shadowed['start']}-{shadowed['end']}, "
            f"~{result['shadowedTokenCount']} tokens)"
        )
        # 对齐上游 compaction-basic/src/index.ts:140 `ctx.logger.info(...)`，
        # 经 ctx.logger 门面（上游同款）；无 logger 服务时回退 print 不中断回合
        if hasattr(self.ctx, "logger"):
            self.ctx.logger.info(message)
        else:
            print(f"[compaction] {message}")