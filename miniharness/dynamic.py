"""第 11 章：运行时自我修改 —— 动态插件（进程内存生命周期）。

对应 dsh 真实源码：packages/extensions/（tool-cordis + cordis-host-runner）。

上游语义（已核实）：
  * 七工具两族：inspect_list / inspect_query / inspect_self（检查）
    + define / run / stop / undefine（修改）
  * 临时插件只存进程内存，明确不写 cordis.yml、不落盘、重启不恢复
    （tool-cordis/README.md:19）—— "agent 直接改配置"这条路径是设计上排除的
  * 完整流程（cordis-host-runner/tests/runner.spec.ts:150-176）：
    define（kind new，mint dyn-1/pkg-1）→ run（立即 status:'running'、
    run-1、ctx.provide 生效）→ invoke 返回 42

载体简化说明：上游工具经模型调用（tool-cordis）并带审批往返；mini 复现
"进程内注册表 + 生命周期语义"本身，模型侧与审批不做。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .bus import Context


@dataclass
class DynamicPlugin:
    pkg_id: str
    kind: str
    source: str
    provides: list[str] = field(default_factory=list)
    apply: Callable[[Context], None] | None = None


class DynamicPluginRegistry:
    """进程内动态插件注册表：define → run → stop / undefine。

    生命周期不变量（与上游对齐）：
      1. define 只登记定义，不生效；run 才激活（mint runId，status running）
      2. run 在隔离 scope 激活，apply 经 ctx.provide 提供服务
      3. 提供祖先链上已有的服务 key → 拒绝（fail loud，进程级冲突）
      4. 只存进程内存：新 registry 即"重启"，一切不恢复
    """

    def __init__(self, root: Context):
        self.root = root
        self._defs: dict[str, DynamicPlugin] = {}
        self._runs: dict[str, Context] = {}
        self._pkg_runs: dict[str, str] = {}   # pkg_id -> run_id
        self._def_counter = 0
        self._run_counter = 0

    # ---------- 检查族（inspect） ----------

    def list(self) -> list[str]:
        return sorted(self._defs)

    def query(self, pkg_id: str) -> dict:
        p = self._defs[pkg_id]
        return {"pkgId": p.pkg_id, "kind": p.kind, "source": p.source,
                "provides": list(p.provides), "running": p.pkg_id in self._pkg_runs}

    def inspect_self(self) -> dict:
        return {
            "defs": [self.query(p) for p in self.list()],
            "runs": sorted(self._runs),
        }

    # ---------- 修改族（define / run / stop / undefine） ----------

    def define(self, kind: str, source: str, provides: list[str] | None = None,
               apply: Callable[[Context], None] | None = None) -> str:
        """定义新包：mint pkg id，仅登记。kind 限定 'new'（上游 kind new）。"""
        if kind != "new":
            raise ValueError(f"未知插件 kind: {kind!r}（上游仅 kind new）")
        self._def_counter += 1
        pkg_id = f"pkg-{self._def_counter}"
        self._defs[pkg_id] = DynamicPlugin(
            pkg_id=pkg_id, kind=kind, source=source,
            provides=list(provides or []), apply=apply,
        )
        return pkg_id

    def run(self, pkg_id: str) -> dict:
        """激活：隔离 scope + apply + 提供服务。返回 {runId, status, pkgId}。"""
        if pkg_id not in self._defs:
            raise KeyError(f"未知包: {pkg_id}（先 define）")
        if pkg_id in self._pkg_runs:
            raise RuntimeError(f"包 {pkg_id} 已在运行")
        p = self._defs[pkg_id]
        # 进程级冲突检查：声明的 provides 不得已存在于祖先链（apply 负责实际 provide）
        for key in p.provides:
            try:
                self.root.inject(key)
            except KeyError:
                continue
            raise RuntimeError(f"包 {pkg_id} 提供进程级服务 {key}，host 已存在（拒绝）")
        scope = self.root.create_scope(f"dyn:{pkg_id}")
        if p.apply is not None:
            p.apply(scope)
        self._run_counter += 1
        run_id = f"run-{self._run_counter}"
        self._runs[run_id] = scope
        self._pkg_runs[pkg_id] = run_id
        return {"runId": run_id, "status": "running", "pkgId": pkg_id}

    def invoke(self, run_id: str, key: str, *args: Any, **kwargs: Any) -> Any:
        """调用运行中插件提供的服务（演示语义：ctx.provide 生效后可用）。"""
        if run_id not in self._runs:
            raise KeyError(f"未知 run: {run_id}")
        fn = self._runs[run_id].inject(key)
        return fn(*args, **kwargs)

    def stop(self, run_id: str) -> None:
        """停止：dispose 隔离 scope（副作用逆序回滚），服务消失。"""
        if run_id not in self._runs:
            raise KeyError(f"未知 run: {run_id}")
        pkg_id = next(p for p, r in self._pkg_runs.items() if r == run_id)
        del self._pkg_runs[pkg_id]
        self._runs.pop(run_id).dispose()

    def undefine(self, pkg_id: str) -> None:
        """回收定义：若在运行先拒绝（fail loud）。"""
        if pkg_id in self._pkg_runs:
            raise RuntimeError(f"包 {pkg_id} 仍在运行，先 stop 再 undefine")
        if pkg_id not in self._defs:
            raise KeyError(f"未知包: {pkg_id}")
        del self._defs[pkg_id]