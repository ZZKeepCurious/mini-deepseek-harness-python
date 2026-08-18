"""`miniharness` 命令行启动器：对齐上游 `apps/cli` 的 launcher 语义（args.ts）。

上游 launcher 只解析自己拥有的东西——启动哪个 profile、附带哪些 --patch、
配置 dump——并把之后的所有参数原样交给被启动的应用（apps/cli/src/args.ts:5）。
mini 只复现 headless 一个 profile（`--profile web` 对应 web 表层未复现，fail loud）；
无参数时回退到 demo（教学演示入口，非上游语义）。

launcher 选项（对齐 args.ts，已核实）：
  --profile <name>            启动 profile（mini 仅 headless）
  --patch <path>              可重复 overlay 补丁（YAML/JSON）
  --dump-config               只读打印最终组合（boot-free）
  --dump-default-config       只打印内置默认组合；与 --patch 互斥
  --config <path>             指定组合文件（mini 教学扩展：上游用 profile 目录机制）

mini 扩展/简化（须标注）：
  - --config 为 mini 教学扩展（上游无此标志）
  - mini 内置默认组合为空（headless 不走插件树，见 headless.py 简化标注）
  - 组合层与 headless 运行时解耦：带 --config/--patch 跑任务时先 boot 验证，
    headless 运行时仍为内置 adapter
  - sessions 子命令为 mini 教学扩展（上游会话管理在 web 表层）

用法：
  miniharness --profile headless "run the tests"        # 一次性任务，对齐上游
  miniharness --dump-config                             # 打印最终组合（只读）
  miniharness --patch patch.yml --profile headless "t"  # 组合验证 + 任务
  miniharness sessions / sessions resume <id> [task] / sessions delete <id>
  miniharness                                           # 端到端演示（教学入口）
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from ..boot import apply_patch
from ..boot.composition import load_composition, load_patch_list, render_composition_dump, resolve_js_exprs

USAGE = (
    "Usage:\n"
    '  miniharness --profile headless "task"    answer one task, print the final assistant text, and exit\n'
    "  miniharness --dump-config                print the final composed configuration (read-only)\n"
    "  miniharness --dump-default-config        print only the built-in default composition\n"
    "  miniharness --patch <path> --profile headless \"task\"\n"
    "  miniharness sessions [list | resume <id> [task...] | delete <id>]\n"
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
        elif a in ("--dump-config", "--dump-default-config"):
            mode = "config" if a == "--dump-config" else "default"
            if parsed["dump"] is not None:
                raise _UsageError("--dump-config and --dump-default-config are mutually exclusive")
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
    parsed = _parse_launcher(args)
    if parsed.get("help"):
        sys.stdout.write(USAGE)
        return
    if parsed["dump"] is not None:
        if parsed["profile"] is not None and parsed["profile"] != "headless":
            sys.stderr.write(f"error: unknown profile {parsed['profile']!r} (mini 仅提供 headless；web 未复现)\n")
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
    if profile is not None and profile != "headless":
        sys.stderr.write(f"error: unknown profile {profile!r} (mini 仅提供 headless；web 未复现)\n")
        sys.exit(1)
    if profile is None and (parsed["configs"] or parsed["patches"]):
        profile = "headless"
    if profile is None:
        from ..demo import main as demo_main

        demo_main()
        return

    _validate_composition(parsed)
    from .headless import headless_main

    task = " ".join(parsed["task"])
    if task.strip() == "":
        sys.stderr.write(
            'error: a task is required, for example: miniharness --profile headless "run the tests"\n'
        )
        sys.exit(1)
    headless_main(task)


if __name__ == "__main__":
    main()