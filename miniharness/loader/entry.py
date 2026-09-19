import asyncio
import inspect
import types
from typing import Any, Callable

from ..core.scope import Inject
from .model import ENTRY_KEY, SEP
from .utils import evaluate, is_js_expr, settle_gathered, sort_keys

ModuleBridge = Callable[[Any, Any], Any]


def _module_bridge(module: types.ModuleType) -> ModuleBridge:
    """把 'module: xx' 旧方言插件的 apply(ctx, **config) 桥接成 ctx.plugin()
    的标准 (ctx, config) 调用（上游 loader 只吃 ESM 对象/函数形态）。"""

    def _apply(ctx: Any, config: dict | None = None) -> Any:
        if config:
            return module.apply(ctx, **config)
        return module.apply(ctx)

    return _apply


def _settle_result(value: Any) -> None:
    """结算一次 fiber 装载结果：无运行 loop 时用瞬态事件循环排空（mini 同步门面）。"""
    if not inspect.isawaitable(value):
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        settle_gathered([value])


class Entry:
    """加载树中的一个可配置插件节点（对齐 vendor/loader/src/config/entry.ts）。"""

    def __init__(self, loader: "Loader"):
        self.loader = loader
        self.ctx = loader.ctx.extend({ENTRY_KEY: self})
        self.context.emit("loader/entry-init", self)
        self.fiber = None
        self.parent = None
        self.options: dict = {}
        self.subgroup = None
        self.subtree = None
        self._init_task = None
        self._error: BaseException | None = None

    @property
    def context(self) -> Any:
        return self.ctx

    @property
    def id(self) -> str:
        id_ = self.options.get("id")
        owner = getattr(self.parent.tree.ctx.fiber, "entry", None) if self.parent else None
        return f"{owner.id}{SEP}{id_}" if owner else id_

    @property
    def disabled(self) -> bool:
        if self.options.get("group"):
            return False
        entry = self
        while entry is not None:
            if self._disabled_of(entry.options):
                return True
            parent = entry.parent
            entry = getattr(parent.ctx.fiber, "entry", None) if parent is not None else None
        return False

    @staticmethod
    def _disabled_of(options: dict) -> bool:
        disabled = options.get("disabled")
        if is_js_expr(disabled):
            return bool(evaluate(disabled["__jsExpr"]))
        return bool(disabled)

    def evaluate(self, expr: str) -> str:
        return evaluate(expr)

    def _patch_context(self, diff: list[str]) -> None:
        if self.fiber is not None and self.fiber.uid is not None and (
                "config" in diff or self.options.get("group")):
            _settle_result(self.fiber.update(self.options.get("config"), True))

    def refresh(self) -> None:
        if self.fiber is not None:
            return
        if self.disabled:
            return
        self.init()

    def update(self, options: dict, create: bool = False, force: bool = False) -> None:
        legacy = dict(self.options)

        if create:
            self.options = options
        else:
            for key, value in options.items():
                if value is None:
                    self.options.pop(key, None)
                else:
                    self.options[key] = value
        sort_keys(self.options)

        if self.disabled:
            if self.fiber is not None:
                self.fiber.dispose()
            return

        if self.fiber is not None and self.fiber.uid is not None:
            keys = set(self.options) | set(legacy)
            diff = [k for k in keys if self.options.get(k) != legacy.get(k)]
            if not diff and not force:
                return
            self.context.emit("loader/partial-dispose", (self, legacy, True))
            self._patch_context(diff)
        else:
            self.init()

    def init(self) -> None:
        try:
            if self._init_task is None:
                self._init_task = self._init()
        finally:
            self._init_task = None
        if not self.loader.get_tasks():
            self._notify_loader()

    def _notify_loader(self) -> None:
        root = self.ctx.root
        root._notify([("loader", root._label_of("loader"))])

    def _init(self) -> None:
        name = self.options.get("name") or self.options.get("module")
        try:
            exports = self.parent.tree.import_(name)
        except BaseException as error:
            self._error = error
            logger = self.ctx.logger
            if logger is not None:
                logger.error(error)
            return
        self._error = None
        plugin = self.loader.unwrap_exports(exports)
        if isinstance(exports, types.ModuleType) and callable(getattr(exports, "apply", None)):
            plugin = {
                "name": getattr(exports, "__name__", None),
                "inject": getattr(exports, "inject", None),
                "apply": _module_bridge(exports),
            }
        self._patch_context([])
        self.loader.show_log(self, "apply")
        self.fiber = self.ctx.registry.plugin(
            plugin, self.options.get("config"), parent=self.ctx)