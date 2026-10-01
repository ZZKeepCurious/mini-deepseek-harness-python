"""`miniharness` 命令行启动器：对齐上游 `apps/cli` 的 launcher 语义（args.ts）。

上游 launcher 只解析自己拥有的东西——启动哪个 profile、附带哪些 --patch、
配置 dump——并把之后的所有参数原样交给被启动的应用（apps/cli/src/args.ts:5）。
mini 复现 headless 与 web 两个 profile（`--profile web` 启动 FastAPI 服务表层，
对齐 host/webserver 监听契约）；无参数时回退到 demo（教学演示入口，非上游语义）。

launcher 选项（对齐 args.ts，已核实）：
  --profile <name>            启动 profile（mini 提供 headless / web）
  --patch <path>              可重复 overlay 补丁（YAML/JSON）
  --dump-config               只读打印最终组合（boot-free）
  --dump-default-config       只打印内置默认组合；与 --patch 互斥
  --dump-config-schema        打印 profile 组合的 JSON Schema 文档（boot-free）
  --config <path>             指定组合文件（mini 教学扩展：上游用 profile 目录机制）

  --host / --port             web profile 显式监听地址/端口（小写字面，P2-17）；
                              host ∈ {'127.0.0.1','0.0.0.0'}，port ∈ 0..65535
                              （0 = OS 分配）；缺省走环境/缺省（见 web/launcher）

mini 扩展/简化（须标注）：
  - --config 为 mini 教学扩展（上游无此标志）
  - mini 内置默认组合为空（headless 不走插件树，见 headless.py 简化标注）
  - 组合层与 headless 运行时解耦：带 --config/--patch 跑任务时先 boot 验证，
    headless 运行时仍为内置 adapter
  - **web profile 是 boot/profile 驱动**：`--profile web` 首次把默认 web 组合
    （cli/web_profile.yml 条目树，cli/plugins/* 插件）种子进
    $MINIHARNESS_HOME/profiles/web/cordis.yml（Loader 只写用户 include 文件，
    不写 shipped 资产），boot 之 → 装配 config-editor + SettingsForms
    （rc.1 settings 核心，命名空间 = 组合条目 id，写经 config-editor 持久化
    cordis.patch.yml）——对齐上游 web-app bundle 的插件组合面
  - web profile 的 host/port：`--host`/`--port` 显式参数 > 环境
    MINIHARNESS_WEB_HOST / MINIHARNESS_WEB_PORT（缺省 127.0.0.1 / 0，OS 分配），
    见 web/launcher.py
  - 不实现上游 `--no-open` / `--trusted-host`（mini 无浏览器自动打开 / trust 栅栏面）
  - sessions 子命令为 mini 教学扩展（上游会话管理在 web 表层）
  - presets 子命令为 mini 教学扩展（上游 preset 管理是 web 表层 Remote 服务）：
    名单/投影/选择/删除，投影与 PresetLockedError 语义对齐，见 cli/preset_cmds.py

用法：
  miniharness --profile headless "run the tests"        # 一次性任务，对齐上游
  miniharness --profile web                             # 启动 web 服务（需 fastapi/uvicorn）
  miniharness --dump-config                             # 打印最终组合（只读）
  miniharness --patch patch.yml --profile headless "t"  # 组合验证 + 任务
  miniharness sessions / sessions resume <id> [task] / sessions delete <id>
  miniharness presets [list | show [id] | select <id> [session] | delete <id>]
  miniharness                                           # 端到端演示（教学入口）
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from ..boot import apply_patch
from ..boot.composition import load_composition, load_patch_list, render_composition_dump, resolve_js_exprs

KNOWN_PROFILES = ("headless", "web")

#: shipped 默认 web 组合模板（首次 boot 种子进 profile include；Loader 只写
#: 用户 include 文件，本模板资产绝不回写）。
_WEB_PROFILE_PATH = os.path.join(os.path.dirname(__file__), "web_profile.yml")

USAGE = (
    "Usage:\n"
    '  miniharness --profile headless "task"    answer one task, print the final assistant text, and exit\n'
    "  miniharness --profile web [--host HOST] [--port PORT]\n"
    "                                          start the web service (FastAPI; requires fastapi/uvicorn)\n"
    "                                          --host 127.0.0.1|0.0.0.0, --port 0..65535 (0 = OS assign)\n"
    "  miniharness --dump-config                print the final composed configuration (read-only)\n"
    "  miniharness --dump-default-config        print only the built-in default composition\n"
    "  miniharness --dump-config-schema         print the composed configuration as a JSON Schema document\n"
    "  miniharness --patch <path> --profile headless \"task\"\n"
    "  miniharness sessions [list | resume <id> [task...] | delete <id>]\n"
    "  miniharness presets [list | show [id] | select <id> [session] | delete <id>]\n"
    "  miniharness                             end-to-end demo (fake model, no API key)\n"
)


class _UsageError(Exception):
    pass


def _parse_launcher(args: list[str]) -> dict[str, Any]:
    """launcher 选项解析（对齐上游 commander passThroughOptions/enablePositionalOptions，
    args.ts:123-129）：launcher 的 flags 在前，到第一个它不认识的 token 截止，
    其后全部（含选项形态）归 booted app（mini 即 headless 任务文本）。"""
    parsed: dict[str, Any] = {"profile": None, "configs": [], "patches": [], "dump": None, "task": []}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--profile":
            if i + 1 >= len(args):
                raise _UsageError(f"option {a!r} requires a value")
            parsed["profile"] = args[i + 1]
            i += 2
        elif a == "--host":
            if i + 1 >= len(args):
                raise _UsageError(f"option {a!r} requires a value")
            if args[i + 1] not in ("127.0.0.1", "0.0.0.0"):
                raise _UsageError(
                    f"--host must be one of ['127.0.0.1', '0.0.0.0'], got {args[i + 1]!r}")
            parsed["host"] = args[i + 1]
            i += 2
        elif a == "--port":
            if i + 1 >= len(args):
                raise _UsageError(f"option {a!r} requires a value")
            try:
                port = int(args[i + 1])
            except ValueError:
                raise _UsageError(f"--port must be an integer in 0..65535, got {args[i + 1]!r}")
            if not 0 <= port <= 65535:
                raise _UsageError(f"--port must be in 0..65535, got {port!r}")
            parsed["port"] = port
            i += 2
        elif a == "--config":
            if i + 1 >= len(args):
                raise _UsageError(f"option {a!r} requires a value")
            parsed["configs"].append(args[i + 1])
            i += 2
        elif a == "--patch":
            if i + 1 >= len(args):
                raise _UsageError(f"option {a!r} requires a value")
            parsed["patches"].append(args[i + 1])
            i += 2
        elif a in ("--dump-config", "--dump-default-config", "--dump-config-schema"):
            if a == "--dump-config-schema":
                mode = "schema"
            else:
                mode = "config" if a == "--dump-config" else "default"
            if parsed["dump"] is not None:
                raise _UsageError(
                    "--dump-config, --dump-default-config and --dump-config-schema "
                    "are mutually exclusive")
            parsed["dump"] = mode
            i += 1
        elif a in ("-h", "--help"):
            parsed["help"] = True
            i += 1
        else:
            # 第一个非 launcher 选项 token（positional 或未知选项）：其后全部归 app
            parsed["task"].extend(args[i:])
            break
    return parsed


def _builtin_headless_entries() -> list[dict]:
    """内置默认组合：空（mini headless 不走插件树 —— 简化标注）。"""
    return []


def _dump_configuration(parsed: dict[str, Any], warn: Any | None = None) -> None:
    warn = warn or sys.stderr
    if parsed["dump"] == "schema":
        _dump_config_schema(parsed, warn)
        return
    if parsed["dump"] == "default":
        sys.stdout.write(
            "# == builtin:headless (mini 内置默认组合；headless 不走插件树，为空)\n"
        )
        sys.stdout.write(render_composition_dump("miniharness", "builtin:headless", _builtin_headless_entries(), []))
        return
    configs = parsed["configs"]
    if not configs:
        base_label = "builtin:headless"
        base = _builtin_headless_entries()
    else:
        base_label = Path(configs[0]).name
        base = load_composition(configs[0])
    layers = [(Path(p).name, load_patch_list(p, label="overlay")) for p in parsed["patches"]]
    sys.stdout.write(render_composition_dump("miniharness", base_label, base, layers, warn=warn.write))


def _dump_config_schema(parsed: dict[str, Any], warn: Any) -> None:
    """--dump-config-schema：profile 组合的 JSON Schema 文档（boot-free）。

    对齐上游 apps/cli dump-config-schema.ts：需要已准备 profile（目录机制）；
    无 profile 时用 --config 提供的组合文件（mini 教学扩展：上游用 profile
    目录 + bundle 解析，mini 无 npm bundle）。层序：--config 基组合（缺省
    内置空）+ --patch overlays。
    """
    import json as _json

    from ..boot.config_schema import generate_config_schema
    from ..boot.profile import (
        Profile,
        load_profile_directory,
        resolve_profile_dir,
    )
    from ..core.home_paths import resolve_dsh_home

    home = resolve_dsh_home()
    profile_dir = resolve_profile_dir(parsed.get("profile") or "headless", home)
    layers: list[list[dict]] = []
    if parsed["configs"]:
        base = load_composition(parsed["configs"][0])
        layers.append([{"insert": [dict(e) for e in base]}])
    for pp in parsed["patches"]:
        layers.append(load_patch_list(pp, label="overlay"))
    profile = Profile(name=os.path.basename(profile_dir), dir=profile_dir,
                      layers=[], patch_path="", patches=[])
    schema = generate_config_schema(profile, layers, install_anchor="miniharness")
    sys.stdout.write(_json.dumps(schema, indent=2) + "\n")


def _validate_composition(parsed: dict[str, Any]) -> None:
    """组合验证模式：加载 → 补丁 → 激活 → 断言（fail loud），打印结果。"""
    configs = parsed["configs"]
    patches = parsed["patches"]
    if not configs and not patches:
        return
    if configs:
        from ..boot import boot

        _, activations = boot(configs[0], *patches)
        names = [n for n, _ in activations]
        sys.stdout.write(f"composition ok: {len(names)} entry(ies) active\n")
        for n in names:
            sys.stdout.write(f"  {n}\n")
        return

    entries: list[dict] = []
    for pp in patches:
        entries = apply_patch(entries, resolve_js_exprs(load_patch_list(pp, label="overlay")))
    sys.stdout.write(f"composition ok: {len(entries)} entry(ies) (patches over built-in empty base)\n")


def main(argv: list[str] | None = None) -> None:
    args = list(argv) if argv is not None else sys.argv[1:]
    try:
        _main(args)
    except _UsageError as e:
        sys.stderr.write(f"error: {e}\n")
        sys.exit(1)
    except Exception as e:  # 兜底：加载/激活/运行期错误 fail loud（对齐 launcher 行为）
        sys.stderr.write(f"error: {e}\n")
        sys.exit(1)


def _main(args: list[str]) -> None:
    if args and args[0] == "sessions":
        from .session_cmds import sessions_main

        sessions_main(args[1:])
        return
    if args and args[0] == "presets":
        from .preset_cmds import presets_main

        presets_main(args[1:])
        return
    parsed = _parse_launcher(args)
    if parsed.get("help"):
        sys.stdout.write(USAGE)
        return
    if parsed["dump"] is not None:
        if parsed["profile"] is not None and parsed["profile"] not in KNOWN_PROFILES:
            sys.stderr.write(
                f"error: unknown profile {parsed['profile']!r} (mini 提供 headless 与 web)\n"
            )
            sys.exit(1)
        if parsed["dump"] == "default" and (parsed["patches"] or parsed["configs"]):
            sys.stderr.write(
                "error: --dump-default-config cannot be combined with --patch/--config\n"
            )
            sys.exit(1)
        if parsed["task"]:
            sys.stderr.write("error: configuration dump takes no task arguments\n")
            sys.exit(1)
        _dump_configuration(parsed)
        return

    profile = parsed["profile"]
    if profile is not None and profile not in KNOWN_PROFILES:
        sys.stderr.write(
            f"error: unknown profile {profile!r} (mini 提供 headless 与 web)\n"
        )
        sys.exit(1)
    if profile is None and (parsed["configs"] or parsed["patches"]):
        profile = "headless"
    if profile is None:
        from ..demo import main as demo_main

        demo_main()
        return

    _validate_composition(parsed)

    if profile == "web":
        if parsed["task"]:
            sys.stderr.write("error: web profile takes no task arguments (starts a server instead)\n")
            sys.exit(1)
        _web_main(parsed.get("host"), parsed.get("port"))
        return

    from .headless import headless_main

    task = " ".join(parsed["task"])
    if task.strip() == "":
        sys.stderr.write(
            'error: a task is required, for example: miniharness --profile headless "run the tests"\n'
        )
        sys.exit(1)
    headless_main(task)


def _web_main(host: str | None = None, port: int | None = None) -> None:
    """web profile 组装：boot/profile 驱动的组合装配。

    cli→web 是 launcher 语义的单方向依赖（组装面在 cli，运行面在 web，
    test_dependencies.py §5 显式例外）。web profile 现在是 boot/profile 驱动：

      1. 解析/初始化 `$MINIHARNESS_HOME/profiles/web/`（首次自动落盘 manifest +
         空 cordis.patch.yml + 从 cli/web_profile.yml 种子 cordis.yml）；
      2. 读 profile + home + overlays 补丁层（read_profile_patches 5 层序）；
      3. boot 用户 include（profile_dir/cordis.yml，首次由 web_profile.yml 模板
         种子；每个条目是 cli/plugins/* 的 apply(ctx, **config)），补丁层经
         根 Include 应用；
      4. 在 boot 过的上下文安装 config-editor + SettingsForms + settings-
         controller——rc.1 settings 核心：命名空间 = ConfigEditor.entries()
         （唯一 profile entry id），写经 config-editor 持久化 cordis.patch.yml
         （filelock + reconcile + 原子写 + HMR runExclusive）；
      5. default_tools + ask_user_question 工具 + agentPreset 会话投影注册；
      6. 交给 web/launcher（WebApi + GatewayStreams + FastAPI）。

    host/port 为 `--host`/`--port` 显式参数（None 由 web/launcher 读环境/缺省，
    见 _resolve_bind）。装配缺省经组合条目 config 声明（SettingsForms 可改）：
    sandbox mode workspace-write、feedback maxNoteBytes 8192、session-title
    fallback/LLM 预算、permission-presets 三预设。上游依据 = 默认组合重评
    （tasks.md「production web 装配接 profile boot」，migration-log 步骤 184）。
    """
    import shutil

    from ..boot import boot
    from ..boot.config_editor import install_config_editor
    from ..boot.profile import (
        PROFILE_TEMPLATES,
        init_profile,
        load_profile_directory,
        read_profile_patches,
        resolve_profile_dir,
    )
    from ..core.home_paths import resolve_dsh_home
    from ..interaction import register_ask_user_question
    from ..llm import DeepSeekAdapter, LlmFailure
    from ..preset.presets import default_roster, register_agent_preset_projection
    from ..settings.forms import install_settings_forms
    from ..settings_controller import install_settings_controller
    from ..web.launcher import run_web
    from .default_tools import default_tools

    try:
        adapter = DeepSeekAdapter()
    except LlmFailure as e:
        sys.stderr.write(f"dsh: {e.failure['code']}: {e.failure['message']}\n")
        sys.exit(1)

    home = resolve_dsh_home()
    profile_dir = resolve_profile_dir("web", home)
    init_profile(profile_dir, PROFILE_TEMPLATES["web"])
    # 用户 include 文件：首次 boot 从 shipped 模板种子。Loader 的 unload 标记
    # 与 config-editor 的补丁回写都会写 include 文件，必须落在用户 profile
    # 而非 shipped 资产（shipped web_profile.yml 只当模板，绝不回写）。
    include_path = os.path.join(profile_dir, "cordis.yml")
    if not os.path.exists(include_path):
        shutil.copyfile(_WEB_PROFILE_PATH, include_path)
    profile = load_profile_directory("miniharness", profile_dir)
    patches = read_profile_patches("miniharness", profile, home=home)
    roster = default_roster()
    ctx, _activations = boot(include_path, patches=patches,
                             env={"adapter": adapter, "roster": roster})

    editor = install_config_editor(ctx, profile_dir=profile_dir,
                                   patch_path=profile.patch_path, home=home)
    install_settings_forms(ctx, config_editor=editor, profile_home=home)
    install_settings_controller(ctx, roster=roster)
    # userQuestions 工具：web 组合经 cli 挂载 ask_user_question 工具（上游经
    # agent presets 挂载；mini 仅在 web profile 注册，headless/sessions 不挂）。
    reg = default_tools(ctx)
    register_ask_user_question(reg, ctx)
    # agentPreset 会话投影单元（M7）：web-app 默认挂载 agent-preset-registry 的
    # agentPreset 投影；boot 组合只装了 roster/注册表，投影单元这里注册进
    # sessionProjections，使 `session/projections` 暴露该单元。
    projections = ctx.get("sessionProjections")
    if projections is not None:
        register_agent_preset_projection(projections)
    run_web(adapter, reg, ctx, host=host, port=port, roster=roster)


if __name__ == "__main__":  # pragma: no cover
    main()