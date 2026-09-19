import json
import os
import pathlib
from typing import Any, Callable

import yaml

from .model import GROUP_KEY
from .patch import apply_entry_patches
from .tree import EntryTree
from .utils import base_url_of

SUPPORTED = {".yaml", ".yml", ".json"}
WRITABLE = {".yaml": "yaml", ".yml": "yaml", ".json": "json"}

_JS_TAG = "tag:yaml.org,2002:js"


def _represent_dict(dumper: Any, data: dict) -> Any:
    """__jsExpr 单键节点原样输出为 !!js 标量；其它 dict 走默认代表器。"""
    if set(data) == {"__jsExpr"} and isinstance(data.get("__jsExpr"), str):
        return dumper.represent_scalar(_JS_TAG, data["__jsExpr"], style="")
    return dumper.represent_dict(data)


class _Dumper(yaml.SafeDumper):
    pass


_Dumper.add_representer(dict, _represent_dict)


def dump_js_expr_yaml(data: Any) -> str:
    """把条目树（含 __jsExpr 节点）序列化为单文档 YAML，!!js 原样保留。

    include 的文件回写用此函数（对齐上游 yaml.dump 的 __jsExpr → !!js）。
    """
    return yaml.dump(
        data,
        Dumper=_Dumper,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )


class Include(EntryTree):
    """文件背书的加载条目子树（对齐 vendor/include/src/index.ts）。

    构造/读取/写入均同步（上游含防抖与写队列 —— 简化标注，见
    verified-diffs）；YAML 以 dump_js_expr_yaml 保持 __jsExpr 原样回写；
    载波标记 GROUP_KEY 使整棵子树配置保持字面，行内 !!js 由各行 fiber 激活期
    求值。
    """

    def __init__(self, ctx: Any, config: dict):
        self.config = config
        super().__init__(ctx)

        self.enable_logs = config.get("enableLogs")
        if config.get("enableLogs") is None:
            owner = getattr(getattr(self.ctx, "fiber", None), "entry", None)
            parent_tree = owner.parent.tree if owner is not None else None
            self.enable_logs = parent_tree.enable_logs if parent_tree is not None else False

        base = base_url_of(self.ctx)
        self.filename = os.path.abspath(os.path.join(base, config["path"]))
        ext = os.path.splitext(self.filename)[1].lower()
        if ext not in SUPPORTED:
            raise RuntimeError(f'extension "{ext}" not supported')
        self.type = WRITABLE.get(ext)
        self.readonly: bool = self.type is None
        self.ctx.baseUrl = os.path.dirname(self.filename)
        self.content: str | None = None
        self.data: list | None = None

        ctx.on("internal/update", self._on_update)

    def _on_update(self, fiber: Any, config: dict, no_save: bool,
                   next_func: Callable) -> Any:
        if config.get("path") != self.config["path"]:
            return next_func()
        self.config = config
        try:
            self.root.update(self.apply_patches(self.data, config.get("patches")))
        except BaseException as error:
            logger = self.ctx.root.logger
            if logger is not None:
                logger("loader").warn("config update at %C failed", self.filename)
                logger("loader").warn(error)
        return None

    def apply_patches(self, data: list, patches: list | None = None) -> list:
        return apply_entry_patches(data, patches, self._warn)

    def _warn(self, message: str, *args: Any) -> None:
        logger = self.ctx.root.logger
        if logger is None:
            return
        logger("loader").warn(message, *args)

    def read(self, forced: bool = False) -> bool:
        content = pathlib.Path(self.filename).read_text(encoding="utf-8")
        if not forced and content == self.content:
            return False
        if self.type == "yaml":
            data = yaml.safe_load(content)
        elif self.type == "json":
            data = json.loads(content)
        else:
            raise RuntimeError(f'extension not supported: {self.filename}')
        if isinstance(data, dict) and isinstance(data.get("plugins"), list):
            data = data["plugins"]
        if not isinstance(data, list):
            raise TypeError(
                f"config file must be a top-level array of entries: {self.filename}")
        self.content = content
        self.data = data
        self.readonly = self.type is None or not os.access(self.filename, os.W_OK)
        return True

    def init(self) -> Callable[[], None]:
        try:
            self.read()
        except FileNotFoundError:
            if self.config.get("initial"):
                self._write_file(self.config["initial"])
                self.read(True)
            else:
                raise RuntimeError(f"config file not found: {self.filename}")
        self.root.update(self.apply_patches(self.data, self.config.get("patches")))
        return self.stop

    def stop(self) -> None:
        self.root.stop()

    def refresh(self) -> None:
        try:
            if not self.read():
                return
            self.root.update(self.apply_patches(self.data, self.config.get("patches")))
        except BaseException as error:
            logger = self.ctx.root.logger
            if logger is not None:
                logger("loader").warn(
                    "config reload at %C failed; keeping the running tree", self.filename)
                logger("loader").warn(error)

    def _write_file(self, config: list) -> None:
        if self.readonly:
            raise RuntimeError("cannot overwrite readonly config")
        if self.type == "yaml":
            self.content = dump_js_expr_yaml(config)
        elif self.type == "json":
            self.content = json.dumps(config, ensure_ascii=False, indent=2)
        tmp = self.filename + ".tmp"
        pathlib.Path(tmp).write_text(self.content, encoding="utf-8")
        os.replace(tmp, self.filename)

    def write(self) -> None:
        if self.data is None:
            return
        self.context.emit("loader/config-update")
        self._write_file(self.root.data)


setattr(Include, GROUP_KEY, True)