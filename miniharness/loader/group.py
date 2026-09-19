from typing import Any, Callable

from .entry import Entry, _settle_result
from .model import GROUP_KEY


class EntryGroup:
    """一条加载条目下的一级子列表运行时宿主（对齐 vendor/loader config/group.ts）。"""

    key = GROUP_KEY

    def __init__(self, ctx: Any, tree: "EntryTree"):
        self.ctx = ctx
        self.tree = tree
        self.data: list[dict] = []
        entry = getattr(ctx.fiber, "entry", None)
        if entry is not None:
            entry.subgroup = self

    @property
    def context(self) -> Any:
        return self.ctx

    def create(self, options: dict) -> str:
        id_ = self.tree.ensure_id(options)
        loader = self.ctx.get("loader")
        entry = self.tree.store.setdefault(id_, Entry(loader))
        entry.parent = self
        _settle_result(entry.update(options, True, True))
        return entry.id

    def unlink(self, options: dict) -> None:
        self.data[:] = [entry for entry in self.data if entry is not options]

    def remove(self, id_: str, is_dispose: bool = False) -> None:
        entry = self.tree.store.get(id_)
        if entry is None:
            return
        if entry.fiber is not None:
            entry.fiber.dispose()
        if not is_dispose:
            self.unlink(entry.options)
        del self.tree.store[id_]
        self.context.emit("loader/partial-dispose", (entry, entry.options, False))

    def update(self, config: list[dict]) -> None:
        old_config = self.data
        self.data = config
        old_map = {options.get("id"): options for options in old_config}
        new_map: dict = {}
        for options in config:
            key = options.get("id") if options.get("id") is not None else object()
            new_map[key] = options
        ids = list(old_map) + [key for key in new_map if key not in old_map]
        for key in ids:
            if key in new_map:
                try:
                    _settle_result(self.create(new_map[key]))
                except BaseException as error:
                    logger = self.ctx.logger
                    if logger is not None:
                        logger.error(error)
            else:
                self.remove(key)

    def stop(self) -> None:
        for options in list(self.data):
            self.remove(options.get("id"), True)


class Group(EntryGroup):
    """把一段条目列表挂到父树下的组插件（对齐 group.ts Group；carrier 标记
    GROUP_KEY 使 Loader 的 internal/config 对其配置保持字面）。"""

    initial: list = []

    def __init__(self, ctx: Any, config: list[dict]):
        super().__init__(ctx, ctx.fiber.entry.parent.tree)
        self.config = config
        ctx.on("internal/update", self._update_handler)

    def _update_handler(self, fiber: Any, config: Any, no_save: bool,
                        next_func: Callable) -> Any:
        self.update(config)
        return None

    def init(self) -> None:
        # 先登记 stop（上游 Service.init 生成器先 yield disposer 再 update）。
        self.context.effect(lambda: self.stop(), "loader:group.stop")
        _settle_result(self.update(self.config))
        return None


setattr(Group, GROUP_KEY, True)