from typing import Any, Callable

from ..core.scope import FiberState, Inject
from .model import ENTRY_KEY, GROUP_KEY
from .tree import EntryTree
from .utils import interpolate


def _parent_entry_of(fiber: Any) -> Any:
    parent_ctx = getattr(getattr(fiber, "context", None), "parent", None)
    return getattr(getattr(parent_ctx, "fiber", None), "entry", None)


class Loader(EntryTree):
    """加载条目树服务：持有 builtins、挂 internal/config|update|plugin 钩子，
    把配置条目导入并激活为插件 fiber（对齐 vendor/loader/src/index.ts）。

    - 自管 'loader' 服务（检查谓词不做 await 门控 —— 简化标注，见
      verified-diffs）；
    - 树载体（Group/Include）的配置保持字面，其它条目激活期 interpolate；
    - 条目根 fiber 自销毁时把行标记 disabled 并持久化（case 1-6 语义）。
    """

    def __init__(self, ctx: Any, config: dict | None = None):
        self.config = config or {}
        super().__init__(ctx)
        if self.config.get("baseUrl"):
            self.ctx.baseUrl = self.config["baseUrl"]
        self.name = "loader"
        self.builtins: dict = {}
        ctx.provide("loader", self, None)
        ctx.on("internal/config", self._on_config, prepend=True)
        ctx.on("internal/update", self._on_update_write, prepend=True)
        ctx.on("internal/update", self._on_update_log, prepend=True)
        ctx.on("internal/plugin", self._on_plugin, prepend=True)

    def write(self) -> None:
        pass

    def _on_config(self, fiber: Any, config: Any, next_func: Callable) -> Any:
        resolved = next_func()
        entry = getattr(fiber, "entry", None)
        if entry is None or _parent_entry_of(fiber) is entry:
            return resolved
        plugin = (fiber.runtime or {}).get("callback") if fiber.runtime is not None else None
        if plugin is not None and getattr(plugin, GROUP_KEY, False):
            return resolved
        return interpolate(resolved)

    def _on_update_write(self, fiber: Any, config: Any, no_save: bool,
                         next_func: Callable) -> Any:
        entry = getattr(fiber, "entry", None)
        if entry is None or no_save or _parent_entry_of(fiber) is entry:
            return next_func()
        unparse = (fiber.runtime or {}).get("Config") if fiber.runtime is not None else None
        simplified = unparse.simplify(config) if hasattr(unparse, "simplify") else config
        entry.options["config"] = simplified
        entry.parent.tree.write()
        return next_func()

    def _on_update_log(self, fiber: Any, config: Any, no_save: bool,
                       next_func: Callable) -> Any:
        entry = getattr(fiber, "entry", None)
        if entry is None or _parent_entry_of(fiber) is entry:
            return next_func()
        self.show_log(entry, "reload")
        return next_func()

    def _on_plugin(self, fiber: Any) -> None:
        parent_ctx = getattr(getattr(fiber, "context", None), "parent", None)
        own = getattr(parent_ctx, ENTRY_KEY, None) if parent_ctx is not None else None
        if own is not None and getattr(fiber, "entry", None) is None:
            fiber.entry = own
            Inject.resolve(own.options.get("inject"), fiber.inject)

        if fiber.uid:
            return
        if getattr(fiber, "entry", None) is None:
            return
        if _parent_entry_of(fiber) is fiber.entry:
            return
        runtime = fiber.runtime or {}
        if runtime.get("callback") is not None and not self.ctx.registry.has(
                runtime["callback"]):
            return
        tree_owner = fiber.entry.parent.tree.ctx.fiber
        if tree_owner.uid is None or tree_owner.state == FiberState.UNLOADING:
            return

        self.show_log(fiber.entry, "unload")

        if fiber.entry.disabled:
            return
        fiber.entry.options["disabled"] = True
        fiber.entry.parent.tree.write()

    def show_log(self, entry: Any, type_: str) -> None:
        if entry.options.get("group") or not entry.parent.tree.enable_logs:
            return
        logger = self.ctx.root.logger
        if logger is None:
            return
        logger("loader").info("%s plugin %C", type_, entry.options.get("name"))

    def locate(self, fiber: Any = None) -> str | None:
        fiber = fiber if fiber is not None else self.ctx.fiber
        seen = set()
        while True:
            if getattr(fiber, "entry", None):
                return fiber.entry.id
            if id(fiber) in seen:
                return None
            seen.add(id(fiber))
            next_fiber = getattr(
                getattr(getattr(fiber, "context", None), "parent", None),
                "fiber", None)
            if next_fiber is fiber:
                return None
            fiber = next_fiber

    def unwrap_exports(self, exports: Any) -> Any:
        if exports is None:
            return exports
        default = getattr(exports, "default", None)
        if default is not None:
            exports = default
        if not getattr(exports, "__esModule", False):
            return exports
        return getattr(exports, "default", None) or exports