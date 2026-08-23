"""windows-acl 受限执行 runner：沙箱 seam 用 argv 前缀包装替代调用方命令的
那层（上游 runner.ts 对应物）。它创建带工作区写 SID allowlist 的
WRITE_RESTRICTED 令牌，在其下 spawn 包装 argv（调用方 stdio **直通**），镜像
子进程退出码，退出时撤销自己的 temp 授权（workspace ACE 保持常驻作复用缓存）。

稳定 argv 契约（seam 构建；原生 exe 替换也保持同一契约）::

    [python, -m miniharness.seams.sandbox_windows_acl.runner,
     '--workspace', <dir>, '--temp', <dir>,
     '--mode', <read-only|workspace-write>,
     ['--write-sid', <S-1-4-…>, '--temp-write-sid', <S-1-4-…>],
     '--', <argv...>]

模式：
  * workspace-write：工作区与 temp 目录携带独立的能力 SID Write 授权；其余
    ACL 可寻址写一律拒绝，Everyone 与 NTFS hard link 边界除外（文档化 partial）。
  * read-only：无能力 SID 授权；restricting 列表不带能力 SID，早前
    workspace-write 时段的常驻授权 ACE 保持惰性。两种模式都丢弃
    Authenticated Users（CIM 不可用）与 INTERACTIVE/LOCAL（Public 树写被拒）；
    两列表共享 keep-alive 组（logon SID, EVERYONE），差异只在能力 SID。

``--write-sid`` + ``--temp-write-sid`` 成对出现 = seam 的授权契约——**调用方**
已物化独立的工作区与私有 temp ACE 并持有撤销权，runner 既不授予也不撤销
（manage_dacls=False）。两个值都对照各自路径校验。没有该对（standalone /
agentless 用法）时 workspace-write 把 ``--temp`` 当 ROOT：自建随机私有子目录、
派生自己的 temp SID、子进程退出后删除该目录。两种流程下 runner 都先把自己
环境里的 TMP/TEMP 重写到私有目录再 spawn（子进程继承该块；lpEnvironment NULL
——FFI 显式环境块会在 CreateProcessAsUserW 触发 ERROR_INVALID_PARAMETER，上游
实证）；read-only 不动 ambient temp 条目（那里反正写不进去）。

失败契约：runner 侧一切失败（坏参数、目录缺失、令牌/授权/spawn 错误）向
stderr 打印 ``windows-acl-run: <detail>`` 并 exit 127——seam 的
RUNNER_FAILURE_RULES 匹配该签名。子进程**绝不**以未受限形态 spawn。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import stat
import sys

from .index import AclSandbox, AclSandboxOptions, AclSandboxSpawnOptions, UNSET
from .ffi import win32_sync
from .path_boundary import assert_temp_root_outside_workspace
from .workspace_sid import temp_write_sid, workspace_write_sid

RUNNER_SIGNATURE = "windows-acl-run"
RUNNER_FAILURE_EXIT = 127


class RunnerFailure(Exception):
    """runner 参数/前置校验失败（签名行已打印，顶层不再重复）。"""


def _fail(detail: str):
    """打印 runner 失败签名行并展开。"""
    sys.stderr.write(f"{RUNNER_SIGNATURE}: {detail}\n")
    raise RunnerFailure(detail)


class ParsedArgs:
    def __init__(self):
        self.workspace: str | None = None
        self.temp: str | None = None
        self.mode: str | None = None
        self.write_sid: str | None = None
        self.temp_write_sid: str | None = None
        self.command: str | None = None
        self.args: list[str] = []


def parse_args(raw: list[str]) -> ParsedArgs:
    parsed = ParsedArgs()
    index = 0
    while index < len(raw):
        token = raw[index]
        if token == "--":
            index += 1
            break
        index += 1
        value = raw[index] if index < len(raw) else None
        if value is None:
            _fail(f"missing value after {token}")
        if token == "--workspace":
            parsed.workspace = value
        elif token == "--temp":
            parsed.temp = value
        elif token == "--mode":
            parsed.mode = value
        elif token == "--write-sid":
            parsed.write_sid = value
        elif token == "--temp-write-sid":
            parsed.temp_write_sid = value
        else:
            _fail(f"unknown argument: {token}")
        index += 1
    if parsed.workspace is None:
        _fail("missing --workspace")
    if parsed.temp is None:
        _fail("missing --temp")
    if parsed.mode not in ("read-only", "workspace-write"):
        _fail(f"unknown mode: {parsed.mode}")
    argv = raw[index:]
    if not argv:
        _fail("missing command after --")
    parsed.command = argv[0]
    parsed.args = argv[1:]
    return parsed


def require_directory(label: str, path: str) -> None:
    if not os.path.exists(path) or not stat.S_ISDIR(os.stat(path).st_mode):
        _fail(f"{label} is not an existing directory: {path}")


async def _run(parsed: ParsedArgs) -> int:
    # 两个目录在两种模式下都校验：provider bug 传进来的伪根必须在 runner 边界
    # 大声失败，绝不能拖到子进程中间。
    require_directory("--workspace", parsed.workspace)
    require_directory("--temp", parsed.temp)

    seam_managed = parsed.write_sid is not None or parsed.temp_write_sid is not None
    if parsed.mode == "read-only" and seam_managed:
        _fail("read-only does not accept --write-sid or --temp-write-sid")
    if parsed.mode == "workspace-write" and (parsed.write_sid is None) != (parsed.temp_write_sid is None):
        _fail("workspace-write requires --write-sid and --temp-write-sid together")
    if parsed.mode == "workspace-write":
        assert_temp_root_outside_workspace(parsed.workspace, parsed.temp)

    api = win32_sync()
    # 忽略本进程自己的 CTRL+C：受限子进程（同控制台）继续处理自己的；runner
    # 必须活到撤销授权并镜像子进程退出码之后。
    if api.setConsoleCtrlHandler(None, 1) == 0:
        _fail(f"SetConsoleCtrlHandler failed (Win32 {api.getLastError()})")

    owned_temp_dir: str | None = None
    sandbox: AclSandbox | None = None
    initialized = False
    try:
        private_temp_dir: str | None = None
        write_sid: str | None = None
        private_temp_sid: str | None = None
        if parsed.mode == "workspace-write":
            write_sid = workspace_write_sid(parsed.workspace)
            if seam_managed:
                if parsed.write_sid != write_sid:
                    _fail("--write-sid does not match --workspace")
                private_temp_dir = parsed.temp
                private_temp_sid = temp_write_sid(private_temp_dir)
                if parsed.temp_write_sid != private_temp_sid:
                    _fail("--temp-write-sid does not match --temp")
            else:
                owned_temp_dir = _mkdtemp(parsed.temp)
                private_temp_dir = owned_temp_dir
                private_temp_sid = temp_write_sid(private_temp_dir)
        sandbox = AclSandbox(AclSandboxOptions(
            writable_dirs=[] if parsed.mode == "read-only" else [parsed.workspace],
            temp_dir=private_temp_dir if private_temp_dir is not None else (
                UNSET if parsed.mode == "workspace-write" else None),
            mode=parsed.mode,
            write_sid=write_sid,
            temp_write_sid=private_temp_sid,
            manage_dacls=not seam_managed,
        ))
        sandbox.init()
        initialized = True

        if private_temp_dir is not None:
            # 重写本进程环境的 TMP/TEMP（os.environ 赋值即更新真实进程环境块）；
            # 子进程整块继承（lpEnvironment NULL 语义）。
            os.environ["TMP"] = private_temp_dir
            os.environ["TEMP"] = private_temp_dir

        child = sandbox.spawn(AclSandboxSpawnOptions(
            command=parsed.command, args=parsed.args, stdio="inherit"))
        result = await child.wait()
        return result.exit_code
    finally:
        # 清理失败不得掩盖子进程退出码：报告后继续。
        if initialized:
            try:
                sandbox.dispose()
            except Exception as error:  # noqa: BLE001 - 清理失败只报告
                sys.stderr.write(f"{RUNNER_SIGNATURE}: cleanup: {error}\n")
        if owned_temp_dir is not None:
            try:
                shutil.rmtree(owned_temp_dir, ignore_errors=True)
            except OSError as error:
                sys.stderr.write(f"{RUNNER_SIGNATURE}: cleanup: {error}\n")


def _mkdtemp(root: str) -> str:
    # 对齐上游 node fs.mkdtempSync：不构造安全描述符，继承父目录 DACL。
    # CPython 的 tempfile.mkdtemp 以 mode=0700 显式建 SD（SYSTEM/Admins/
    # OWNER RIGHTS，无 user ACE）；OWNER RIGHTS 对 WRITE_RESTRICTED 子进程
    # 无效（两遍求值中受限主体不被视为 owner），pass-1 必败 → 子进程一律拒写。
    import uuid

    while True:
        candidate = os.path.join(root, f"dsh-{uuid.uuid4().hex[:12]}")
        try:
            os.mkdir(candidate)  # 默认 0777 → CPython 不构造 SD，纯继承
            return candidate
        except FileExistsError:  # pragma: no cover - uuid 碰撞，重试即可
            continue


def main(argv: list[str]) -> int:
    """CLI 入口：解析 → 校验 → 受限 spawn → 镜像退出码。

    Windows 退出码全宽镜像（上游在本机实证）：子进程以 NTSTATUS
    0xC0000005 退出经 GetExitCodeProcess 读回 uint32 3221225477，
    process.exitCode / ExitProcess 全程无截断无掩码——PowerShell/cmd 显示
    有符号视图但位型一致，无需重映射。"""
    try:
        parsed = parse_args(argv)
    except RunnerFailure:
        return RUNNER_FAILURE_EXIT
    try:
        return asyncio.run(_run(parsed))
    except RunnerFailure:
        return RUNNER_FAILURE_EXIT


def cli() -> None:
    """``python -m miniharness.seams.sandbox_windows_acl.runner <args...>``：
    ``-m`` 调用下 sys.argv[0] 是模块文件、argv[1:] 起才是 runner 参数。
    RunnerFailure 已打过签名行；其它异常按失败契约补打并退 127。"""
    try:
        code = main(sys.argv[1:])
    except Exception as error:  # noqa: BLE001 - 失败契约：任何异常都是 runner 失败
        if not isinstance(error, RunnerFailure):
            sys.stderr.write(f"{RUNNER_SIGNATURE}: {error}\n")
        code = RUNNER_FAILURE_EXIT
    sys.exit(code)


if __name__ == "__main__":
    cli()
