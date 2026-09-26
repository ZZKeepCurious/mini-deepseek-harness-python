"""第 5/7 章：启动与组合 —— 根 Include 条目 + Loader 活树装载 + 依 id 补丁。

对应 dsh 真实源码：packages/boot/app-boot + vendor/loader + vendor/include。

组合不变量：
  1. 配置与补丁按条目树装载：根 Include（cordis:include）读文件 → 应用补丁
     层（旧叠层补丁在 boot 侧转成 applyEntryPatches 形态）→ 导入并激活
  2. include 文件内 group 条目成为嵌套子树；!!js 表达式在各自条目激活期求值
  3. 启动结束必须断言"条目已激活"，否则 fail loud：ACTIVE 通过、导入缺失重抛
     原错误、FAILED 重抛装载错误、PENDING 点名缺失的注入服务（对齐
     app-boot inactiveEntries / auditStartupEntries）
  4. 配置载体支持 JSON 与 YAML（pyyaml 硬依赖）；!!js 表达式子集见 composition

激活语义（对齐 vendor/cordis registry）：每个 entry 经 ctx.plugin() 铸造一
枚 fiber；entry 的 inject 依赖缺失时 fiber 保持 PENDING，提供方在 apply 期
动态 ctx.provide() 后经依赖追踪器唤醒 → LOADING → ACTIVE。同步 body 同步
激活；异步 body 由启动阶段用瞬态事件循环排空（mini 同步门面简化）。
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import os
import weakref
from typing import Any, Callable

from ..core.hmr import Hmr
from ..core.scope import CordisError, Context, FiberState, INACTIVE_EFFECT

from ..loader import Group, Include, Loader
from ..loader.utils import settle_gathered
from .composition import load_patch_list

__all__ = [
    "boot",
    "load_plugin",
    "load_optional_patches",
    "mount_root_include",
    "watch_user_patches",
]

#: root ctx → 根 Include 条目（对齐 app-boot 的 bootstrapIncludes WeakMap）：
#: `watch_user_patches` 经它把用户补丁层事务性重应用到装载树。
_BOOTSTRAP_INCLUDES: "weakref.WeakKeyDictionary[Context, Any]" = weakref.WeakKeyDictionary()


def load_plugin(entry: dict) -> dict:
    """从 'module' 导入插件：模块内须定义 apply(ctx, **config)。

    插件形态为对象插件 {name, inject, apply}（对齐上游 registry 的插件形态）；
    apply 以 (ctx, config) 调用（对齐上游插件 body 签名）；服务在 apply 期经
    ctx.provide() 动态登记，不再使用声明式 provides 字段。
    """
    module = importlib.import_module(entry["module"])
    return {
        "name": entry.get("id", module.__name__),
        "inject": entry.get("inject") or getattr(module, "inject", None),
        "apply": lambda ctx, config, m=module: m.apply(ctx, **config),
    }


def _overlay_to_entry_patches(patches: list[dict]) -> list[dict]:
    """旧叠层补丁形态 → applyEntryPatches 形态。

    {replace:{id, config}} → {id, config}；{insert:[...]} → {insert:[...]}；
    其它形态 fail loud。不做表达式求值 —— !!js 由对应条目激活期求值（对齐
    app-boot 装载路径的懒求值语义，composition 同款简化）。
    """
    result: list[dict] = []
    for patch in patches:
        if "insert" in patch:
            result.append({
                "insert": patch["insert"],
                **({"id": patch["id"]} if patch.get("id") else {}),
            })
            continue
        if "replace" in patch:
            replace = patch["replace"]
            if not isinstance(replace, dict) or not replace.get("id"):
                raise ValueError(
                    f"invalid overlay patch: 'replace' 必须含目标 id, got {replace!r}")
            result.append(dict(replace))
            continue
        raise ValueError(f"invalid overlay patch: 无法识别的字段 {sorted(patch)}")
    return result


def mount_root_include(
    root: Context,
    absolute_config_path: str,
    patches: list[dict] | None = None,
    bin_name: str = "miniharness",
) -> Any:
    """挂载根 Include 条目：注册 include/group 内建插件后装载根配置条目（对齐
    app-boot mountRootInclude）。

    返回 include 条目。config 缺省清点只活 unmount 错误路径使用。
    """
    loader = root.get("loader")
    if loader is None:
        raise RuntimeError(f"{bin_name}: loader service unavailable")
    loader.builtins["include"] = Include
    loader.builtins["group"] = Group
    include_config = {"path": absolute_config_path}
    if patches:
        include_config["patches"] = list(patches)
    include_id = loader.create({
        "id": "include",
        "name": "cordis:include",
        "config": include_config,
    })
    entry = loader.resolve(include_id)
    _BOOTSTRAP_INCLUDES[root] = entry
    return entry


def _settle_loader(loader: Loader) -> None:
    """排空装载期在途转换：同步门面下以瞬态事件循环结算 async body 的 inertia。"""
    while True:
        tasks = loader.get_tasks()
        if not tasks:
            return
        results = settle_gathered(tasks)
        for result in results:
            if isinstance(result, BaseException):
                logger = loader.context.root.logger
                if logger is not None:
                    logger.error(result)


def _contain_update(root: Context) -> Callable:
    """根 internal/update 观察者：任何条目重载异常 -> 记录并保持运行树（对齐
    app-boot ctx.on('internal/update')：fiber 更新错误在后端状态 FAILED，由
    _assert_loader_activated 兜底）；成功路径原样委托，awaitable 结果在无 loop
    时以瞬态循环结算。"""
    def observer(fiber: Any, config: Any, no_save: bool, next_func: Callable) -> Any:
        try:
            result = next_func()
        except BaseException as error:
            logger = root.logger
            if logger is not None:
                logger.error(error)
            return None
        if inspect.isawaitable(result):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                settle_gathered([result])
                return None
        return result
    return observer


def inactive_entries(loader: Loader) -> list[tuple[Any, str]]:
    """收集未激活条目与 disabled 表达式错误（对齐 app-boot inactiveEntries）。

    逐条判定：disabled 跳过（表达式抛错 → disabled expression failed 诊断）；
    无 fiber → failed to import；FAILED → 渲染 fiber 错误；PENDING → 点名缺失
    的注入服务；其余 → fiber state。返回 (entry, diagnostic) 列表。
    """
    failures: list[tuple[Any, str]] = []
    for entry in loader.entries():
        name = entry.options.get("name") or entry.options.get("module")
        subject = f"{entry.options.get('id')} ({name})"
        try:
            if entry.disabled:
                continue
        except BaseException as error:
            failures.append((entry, f"{subject}: disabled expression failed: {error}"))
            continue
        fiber = entry.fiber
        if fiber is None:
            failures.append((entry, f"{subject}: failed to import"))
            continue
        state = fiber.state
        if state == FiberState.ACTIVE:
            continue
        if state == FiberState.FAILED:
            failures.append((entry, f"{subject}: {fiber._error}"))
            continue
        if state == FiberState.PENDING:
            missing = [n for n in fiber.inject if fiber.context.get(n) is None]
            noun = "service" if len(missing) == 1 else "services"
            failures.append((
                entry,
                f"{subject}: pending (waiting for {noun}: {', '.join(missing) or 'unknown'})",
            ))
        else:
            failures.append((entry, f"{subject}: fiber state {state}"))
    return failures


def _activation_diagnostic(bin_name: str, severity: str,
                           failures: list[tuple[Any, str]]) -> str:
    """渲染审计诊断（对齐 app-boot activationDiagnostic）。"""
    noun = "entry" if len(failures) == 1 else "entries"
    prefix = "" if bin_name == "" else f"{bin_name}: "
    lines = [f"{prefix}{severity}: {len(failures)} {noun} did not activate"]
    lines.extend(diagnostic for _, diagnostic in failures)
    return "\n".join(lines) + "\n"


def _assert_loader_activated(loader: Loader, bin_name: str) -> None:
    """启动审计（对齐 auditStartupEntries）：bootstrap Include 与必需条目未激活
    即 fail loud。**mini 宿主策略**：无 app 级必需 id 白名单，故全部未激活条目
    都按必需处理（上游白名单见 app-boot index.ts:711-719）。诊断措辞逐条对齐。"""
    failures = inactive_entries(loader)
    if failures:
        raise RuntimeError(
            _activation_diagnostic("", "required startup failure", failures).rstrip("\n"))


def boot(
    config_path: str,
    *patch_paths: str,
    env: dict[str, Any] | None = None,
    bin_name: str = "miniharness",
) -> tuple[Context, list[tuple[str, Callable]]]:
    """boot()：装载根配置 → 依序应用补丁 → 动态激活插件 → 断言全部就绪。

    config_path 与补丁支持 .json/.yaml/.yml；YAML 内 !!js 表达式在对应条目
    激活期求值。返回 (root, activations)：activations 为 [(entry_id,
    fiber.dispose)]，按配置条目创建序（含补丁插入序），载波条目不列入。
    """
    env = env or {}
    root = Context(name="root")
    root.baseUrl = os.path.dirname(os.path.abspath(config_path))
    for key, value in env.items():
        root.provide(key, value)
    root.on("internal/update", _contain_update(root), prepend=True)

    overlay_patches: list[dict] = []
    for pp in patch_paths:
        overlay_patches.extend(
            _overlay_to_entry_patches(load_patch_list(pp, bin_name, label="overlay")))
    try:
        root.plugin(Loader, {"baseUrl": root.baseUrl})
        mount_root_include(root, os.path.abspath(config_path), overlay_patches, bin_name)
        loader = root.get("loader")
        _settle_loader(loader)
        _assert_loader_activated(loader, bin_name)
        activations = [
            (entry.options.get("id"), entry.fiber.dispose)
            for entry in loader.entries()
            if entry.fiber is not None
            and entry.fiber.state == FiberState.ACTIVE
            and entry.subtree is None
            and not entry.options.get("group")
        ]
        return root, activations
    except BaseException:
        try:
            root.fiber.dispose()
        except BaseException:
            pass
        raise


def load_optional_patches(patch_path: str, bin_name: str = "miniharness") -> list[dict]:
    """加载可选补丁层：缺失 → []；不可读/不可解析/非数组 → fail loud。

    对齐上游 loadOptionalPatches（app-boot index.ts:278-）："没有补丁文件"
    是合法态，"存在但坏掉"是 misconfiguration——启动期与热重载期都绝不静默跳过。
    """
    if not os.path.exists(patch_path):
        return []
    return load_patch_list(patch_path, bin_name)


def reconcile_profile_patches(
    ctx: Context,
    patches: list[dict],
    *,
    bin_name: str = "miniharness",
    required_ids: list[str] | None = None,
) -> list[str]:
    """对账式应用补丁世代（对齐 app-boot reconcileProfilePatches，index.ts:271-300）。

    语义：快照当前未激活条目（entry/fiber/options/diagnostic 四元）与全部旧
    fiber 的前状态 → 根 Include `entry.update` 应用新补丁 → 等旧 fiber 结算 +
    loader.await → 收集新未激活条目 → introduced 判定（requiredIds 点名的 /
    与 previous 逐项不一致的新失败）→ 有 introduced 即抛；旧 fiber 中被拒但
    原先未失败的按原样重抛 → 通过则 emit `app-boot/config-reload`，返回
    unchanged 的未激活诊断列表。

    @param ctx 已 boot 的根上下文（须含 loader + 根 Include entry）。
    @param patches 完整有序补丁列表。
    @param bin_name 诊断前缀。
    @param required_ids 显式点名的启用目标（其既有失败也拒绝对账）。
    @returns unchanged 的 pre-existing 未激活条目诊断（供调用方展示）。
    """
    entry = _BOOTSTRAP_INCLUDES.get(ctx)
    if entry is None:
        raise RuntimeError(f"{bin_name}: profile reload requires the root Include entry")
    loader = ctx.get("loader")
    if loader is None:
        raise RuntimeError(f"{bin_name}: profile reload requires the Loader service")
    required = set(required_ids or [])
    previous_failures = [
        {"entry": f_entry, "diagnostic": diagnostic,
         "fiber": f_entry.fiber, "options": _json_dumps(f_entry.options)}
        for f_entry, diagnostic in inactive_entries(loader)
    ]
    previous_fibers = [
        {"fiber": row.fiber,
         "failed": row.fiber.state in (FiberState.FAILED, FiberState.DISPOSED)}
        for row in loader.entries() if row.fiber is not None
    ]
    include_config = dict(entry.options.get("config") or {})
    include_config.pop("patches", None)
    include_config["patches"] = [dict(p) for p in patches]
    entry.update({"config": include_config})
    _settle_fibers([row["fiber"] for row in previous_fibers])
    loader.await_all()
    failures = inactive_entries(loader)
    failure_by_entry = {
        id(f_entry): (f_entry, diagnostic) for f_entry, diagnostic in failures}
    previous_keys = {
        id(prev["entry"]): prev for prev in previous_failures}
    introduced = []
    for f_entry, diagnostic in failures:
        if f_entry.options.get("id") in required:
            introduced.append((f_entry, diagnostic))
            continue
        prev = previous_keys.get(id(f_entry))
        if prev is None or prev["fiber"] is not f_entry.fiber \
                or prev["options"] != _json_dumps(f_entry.options) \
                or prev["diagnostic"] != diagnostic:
            introduced.append((f_entry, diagnostic))
    if introduced:
        raise RuntimeError(
            _activation_diagnostic(bin_name, "warning", introduced).rstrip("\n"))
    for row in previous_fibers:
        if row["failed"]:
            continue
        fiber = row["fiber"]
        f_entry = getattr(fiber, "entry", None)
        if f_entry is None:
            continue
        candidate = failure_by_entry.get(id(f_entry))
        # 旧 fiber 中被拒但原先未失败的条目 → 原样重抛（上游 results 拒绝分支）。
        if candidate is not None and candidate[0].fiber is fiber:
            raise RuntimeError(candidate[1])
    ctx.emit("app-boot/config-reload")
    return [diagnostic for _f_entry, diagnostic in failures]


def _json_dumps(value: Any) -> str:
    import json
    return json.dumps(value, sort_keys=True, default=str)


def _settle_fibers(fibers: list) -> None:
    """确认旧 fiber 已离开活跃态（上游 Promise.allSettled(fiber.await) 的同步等价）。

    mini 同步 loader 的 internal/update waterfall 在 update 内完成旧 fiber
    卸载/重装；此处仅触碰引用（防止 GC 在 diff 前回收），状态收敛由审计断言
    承载，运行中异常由内部观察者（_contain_update）记录。
    """
    for fiber in fibers:
        _ = fiber.state  # noqa: B018 - 触碰状态（同步模型无 await 语义）


def watch_user_patches(
    ctx: Context,
    filename: str,
    *,
    bin_name: str = "miniharness",
    compose: Callable[[list[dict]], list[dict]] | None = None,
) -> Callable:
    """watch 用户补丁层，变更时经 HMR 单飞循环事务性重应用到根 Include（对齐
    app-boot watchUserPatches，index.ts:253-296）。

    每次刷新：重读 include 的非补丁 config → 重读补丁文件（缺失=空层）→
    `compose`（缺省恒等，允许把用户层插入完整补丁序列中间）→ 根 Include
    `entry.update({config: {...includeConfig, patches}})` 走 internal/update
    waterfall 完成卸载/重装 → `loader.await_all()` 结算 → 经
    `reconcile_profile_patches` 对账（前后 diff 判定 introduced，emit
    `app-boot/config-reload`）。
    HMR 缺席或根 Include 缺席 fail loud；注册期 INACTIVE_EFFECT 表示应用正在
    退出，返回 no-op disposer（上游同款豁免）。
    """
    hmr = ctx.get("hmr")
    if hmr is None or not isinstance(hmr, Hmr):
        raise RuntimeError(f"{bin_name}: user patch-layer watching requires the Cordis HMR service")
    entry = _BOOTSTRAP_INCLUDES.get(ctx)
    if entry is None:
        raise RuntimeError(f"{bin_name}: user patch-layer watching requires the root Include entry")

    def refresh() -> None:
        user_patches = load_optional_patches(filename, bin_name)
        composed = compose(user_patches) if compose is not None else user_patches
        reconcile_profile_patches(ctx, _overlay_to_entry_patches(composed),
                                  bin_name=bin_name)

    try:
        return hmr.register_config(filename, refresh)
    except CordisError as error:
        if error.code == INACTIVE_EFFECT:
            return lambda: None
        raise