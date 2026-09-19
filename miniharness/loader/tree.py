import importlib
import secrets
from typing import Any, Iterator

from .entry import _settle_result
from .group import EntryGroup
from .model import SEP
from .utils import base_url_of, settle_gathered


class EntryTree:
    """可持久化的加载条目树（对齐 vendor/loader/src/config/tree.ts）。

    store 为扁平 id → Entry 映射（含组内条目的全 ids）；嵌套解析经
    store[part].subtree 下钻（Include 挂载后其条目带 subtree），resolve 因此
    支持 'include:candies' 这类跨子树 id。
    """

    sep = SEP

    def __init__(self, ctx: Any):
        base_url = base_url_of(ctx)
        self.ctx = ctx.extend({"baseUrl": base_url})
        self.enable_logs: bool | None = None
        self.store: dict = {}
        self.root = EntryGroup(self.ctx, self)
        entry = getattr(self.ctx.fiber, "entry", None)
        if entry is not None:
            entry.subtree = self

    @property
    def context(self) -> Any:
        return self.ctx

    def entries(self) -> Iterator[Any]:
        for entry in list(self.store.values()):
            yield entry
            if entry.subtree is not None:
                yield from entry.subtree.entries()

    def get_tasks(self) -> list:
        tasks = []
        for entry in self.entries():
            if entry._init_task is not None:
                tasks.append(entry._init_task)
            elif entry.fiber is not None and getattr(entry.fiber, "inertia", None) is not None:
                tasks.append(entry.fiber.inertia)
        return tasks

    def await_all(self) -> None:
        while True:
            tasks = self.get_tasks()
            if not tasks:
                return
            results = settle_gathered(tasks)
            for result in results:
                if isinstance(result, BaseException):
                    logger = self.ctx.root.logger
                    if logger is not None:
                        logger.error(result)

    def ensure_id(self, options: dict) -> str:
        if not options.get("id"):
            while True:
                candidate = secrets.token_hex(4)
                if candidate not in self.store:
                    options["id"] = candidate
                    break
        return options["id"]

    def resolve(self, id_: str):
        parts = id_.split(SEP)
        final = parts.pop()
        tree = self
        for part in parts:
            entry = tree.store.get(part)
            tree = entry.subtree if entry is not None else None
            if tree is None:
                raise RuntimeError(f"cannot resolve entry {id_}")
        entry = tree.store.get(final)
        if entry is None:
            raise RuntimeError(f"cannot resolve entry {id_}")
        return entry

    def resolve_group(self, id_: str | None) -> EntryGroup:
        if not id_:
            return self.root
        entry = self.resolve(id_)
        if entry.subgroup is None:
            raise RuntimeError(f"entry {id_} is not a group")
        return entry.subgroup

    def create(self, options: dict, parent: str | None = None,
               position: int | None = None) -> str:
        group = self.resolve_group(parent)
        if position is None:
            group.data.append(options)
        else:
            group.data.insert(position, options)
        _settle_result(group.tree.write())
        return group.create(options)

    def remove(self, id_: str) -> None:
        entry = self.resolve(id_)
        entry.parent.remove(id_)
        entry.parent.tree.write()

    def update(self, id_: str, options: dict, parent: str | None = None,
               position: int | None = None) -> Any:
        entry = self.resolve(id_)
        source = entry.parent
        if parent is not None:
            target = self.resolve_group(parent)
            source.unlink(entry.options)
            if position is None:
                target.data.append(entry.options)
            else:
                target.data.insert(position, entry.options)
            _settle_result(target.tree.write())
            entry.parent = target
        source.tree.write()
        return entry.update(options, False, True)

    def import_(self, name: str) -> Any:
        if name.startswith("cordis:"):
            return self.ctx.get("loader").builtins[name[7:]]
        return importlib.import_module(name)

    def write(self) -> None:
        raise NotImplementedError