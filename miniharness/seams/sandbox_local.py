"""第 6 章：真沙箱后端 —— 平台链选择 + 功能探测 + fail-closed 包裹。

对应 dsh 真实源码：packages/sandbox/sandbox（Service Definition）+ packages/
sandbox/sandbox-local（LocalSandboxProvider）+ packages/sandbox/sandbox-windows-acl。

上游语义（已核实，sandbox-local/src/index.ts + profiles.ts + roots.ts +
sandbox/src/index.ts）：
  * 按平台选择链：linux ['bwrap', 'landlock']（多候选按序探测仲裁）、
    darwin ['seatbelt']、win32 ['windows-acl']（单候选不探测，其执行期
    拒绝仍是 fail-closed 终点）；无链或候选全不可用 → 抛
    SandboxUnavailableError（code SANDBOX_UNAVAILABLE），命令绝不裸跑。
  * bwrap 探测：`bwrap --ro-bind / / --dev /dev --proc /proc
    --die-with-parent -- true`；seatbelt 探测：真实 read-only 剖面 +
    `-- true`；windows-acl 探测：runner --workspace tmp --temp tmp
    --mode read-only -- cmd /c exit 0。
  * enforcement：bwrap/landlock/seatbelt 按构造承诺全部文件效果（探测
    通过即 'full'）；windows-acl 恒 'partial'（WRITE_RESTRICTED 须保留
    Everyone、NTFS hard link 可跨路径别名同一文件对象）。
  * ConfinedArgv = {argv, enforcement, denialSignatures, runnerFailureRules}；
    denial 方言与 runner 失败规则每后端各自独立，消费者先匹配 fatal
    特征（含 exit 门）再查 denial——"命令没跑起来"与"被沙箱拦住"必须
    可区分。
  * runnerCommand 配置：与 runnerFailureSignatures 必须成对（空/非空
    互斥），非空时跳过内置选择与探测、断言 full 强制。

载体形态（2026-08-23 Phase C 后）：landlock 经 `seams/landlock_run.py` ctypes
自限制执行器真执行（同上游 CLI 契约，无编译产物；内核 <5.13 或非 Linux 宿主
探测干净失败 → fail-closed）；bwrap/seatbelt 为外部程序 argv 包装（上游同款）；
windows-acl 经 `seams/sandbox_windows_acl/` 包真执行——ctypes FFI 物化上游
windows-acl-restrict-poc @ 10e4dfb 的全部机制（WRITE_RESTRICTED 双列表令牌、
DACL 授权、CreateProcessAsUserW spawn、kill-on-close job），runner 以
`python -m miniharness.seams.sandbox_windows_acl.runner` 缺省调用（稳定 argv
契约与 exit 127 失败签名同上游）；非 win32 宿主 FFI 加载失败 → 探测恒失败，
fail-closed 行为不变。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from typing import Callable

from .sandbox_windows_acl import (AclWriteGrant, assert_temp_root_outside_workspace,
                                  temp_write_sid, workspace_write_sid)

SANDBOX_UNAVAILABLE = "SANDBOX_UNAVAILABLE"

# landlock ctypes 执行器模块名（landlock_launcher_prefix 缺省前缀的组成部分）
LANDLOCK_LAUNCHER_MODULE = "miniharness.seams.landlock_run"

# windows-acl runner CLI 模块名（缺省 invocation 的组成部分）
WINDOWS_ACL_RUNNER_MODULE = "miniharness.seams.sandbox_windows_acl.runner"

CONFINED_MODES = ("read-only", "workspace-write")

# 平台链：按平台选择，探测只在多候选时仲裁（上游 PLATFORM_CHAINS）
PLATFORM_CHAINS: dict[str, list[str]] = {
    "linux": ["bwrap", "landlock"],
    "darwin": ["seatbelt"],
    "win32": ["windows-acl"],
}

# 单候选免探测时的强制声明（上游 STATIC_ENFORCEMENT）
STATIC_ENFORCEMENT: dict[str, str] = {
    "bwrap": "full",
    "landlock": "full",
    "seatbelt": "full",
    "windows-acl": "partial",
}

# 每后端被拒文件效果的 stderr 方言（上游 DENIAL_SIGNATURES；windows-acl 追加
# 中文 Windows 的 cmd 本地化输出「拒绝访问」——上游仅英文，教学扩展）
DENIAL_SIGNATURES: dict[str, tuple[str, ...]] = {
    "bwrap": ("read-only file system",),
    "landlock": ("permission denied",),
    "seatbelt": ("operation not permitted",),
    "windows-acl": ("access is denied", "access to the path", "permission denied",
                    "拒绝访问"),
    "runnerCommand": ("read-only file system", "permission denied"),
}

# landlock launcher 失败退出码与 windows-acl runner 失败退出码（上游常量）
LAUNCHER_FAILURE_EXIT = 125
WINDOWS_ACL_RUNNER_FAILURE_EXIT = 127

# 每后端 runner 失败证据规则：exit 门 + fatal 特征 + 信息行排除
RUNNER_FAILURE_RULES: dict[str, list[dict]] = {
    "bwrap": [{"fatalSignatures": ["bwrap: "]}],
    "landlock": [{
        "allowedExitCodes": [LAUNCHER_FAILURE_EXIT],
        "fatalSignatures": ["landlock-run: "],
        "informationalLines": ["landlock-run: partial enforcement (older Landlock ABI)"],
    }],
    "seatbelt": [{"fatalSignatures": ["sandbox-exec: "]}],
    "windows-acl": [{
        "allowedExitCodes": [WINDOWS_ACL_RUNNER_FAILURE_EXIT],
        "fatalSignatures": ["windows-acl-run: "],
    }],
}


def _create_session_temp_dir() -> str:
    """会话私有 temp 目录：继承 %TEMP% 的 DACL（上游 mkdtempSync(join(tmpdir(),
    'dsh-')) 语义）。不能用 CPython 的 tempfile.mkdtemp——它以 mode=0700 显式
    构造安全描述符（SYSTEM/Admins/OWNER RIGHTS，无 user ACE），而 OWNER RIGHTS
    对 WRITE_RESTRICTED 受限子进程无效（两遍求值中受限主体不被视为 owner），
    子进程连自己的私有 temp 都写不了。"""
    path = os.path.join(tempfile.gettempdir(), f"dsh-{uuid.uuid4().hex[:12]}")
    os.mkdir(path)  # 默认 0777 → CPython 不构造 SD，纯继承
    return path


class SandboxUnavailableError(Exception):
    """请求的受限模式无可用后端：fail closed，命令绝不裸跑。

    与上游同名错误同语义（code SANDBOX_UNAVAILABLE 穿过 tool/result 通道）。
    """

    def __init__(self, mode: str, detail: str | None = None):
        message = (
            f'sandbox mode "{mode}" is requested but no sandbox backend is usable on this host; '
            "refusing to run the command unconfined. Install bubblewrap or run a Landlock-enforcing "
            "kernel (Linux), ensure sandbox-exec is usable (macOS), or ensure the ACL "
            "restricted-token runner can start (Windows) — otherwise switch the consumer to "
            "danger-full-access."
        )
        if detail is not None:
            message += f" Runner failure: {detail}"
        super().__init__(message)
        self.code = SANDBOX_UNAVAILABLE


def canonical_path(path: str) -> str:
    """解析为强制层实际比较的规范路径；失败回退原样（缺失根匹配不到任何东西）。"""
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def writable_roots(policy: dict) -> list[str]:
    """模式的可写根白名单：workspace-write → 工作区根 + /tmp + 平台临时目录。

    与上游 roots.ts writableRoots 同一语义（规范 + 去重）；read-only 为空。
    """
    if policy.get("mode") != "workspace-write":
        return []
    roots = [policy.get("workspaceRoot", ""), "/tmp", tempfile.gettempdir()]
    return list(dict.fromkeys(canonical_path(r) for r in roots if r))


def bwrap_profile_args(policy: dict) -> list[str]:
    """bwrap 剖面参数（上游 profiles.ts bwrapProfileArgs）。"""
    args = ["--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--die-with-parent"]
    if policy.get("mode") == "workspace-write":
        args += ["--tmpfs", "/tmp", "--bind", policy["workspaceRoot"], policy["workspaceRoot"]]
    return args


def landlock_profile_args(policy: dict) -> dict:
    """Landlock launcher grant 参数（上游 landlockProfileArgs → grantArgs）。"""
    read_write = ["/dev/null"]
    if policy.get("mode") == "workspace-write":
        read_write += ["/tmp", policy["workspaceRoot"]]
    return {"readOnly": ["/"], "readWrite": read_write}


def _sbpl_string(path: str) -> str:
    """SBPL 字符串字面量转义（上游 profiles.ts sbplString）。"""
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def seatbelt_profile_args(policy: dict) -> list[str]:
    """sandbox-exec 参数与 SBPL 剖面（上游 profiles.ts seatbeltProfileArgs）。

    返回 ['-p', <SBPL 单行剖面>]；可写根来自 writable_roots，与进程内
    fs fence 共用同一推导，杜绝"写工具写不了 /tmp 但 bash 能写"的分裂。
    """
    forms = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        f"(allow file-write* (literal {_sbpl_string('/dev/null')}))",
    ]
    roots = writable_roots(policy)
    if roots:
        subpaths = " ".join(f"(subpath {_sbpl_string(r)})" for r in roots)
        forms.append(f"(allow file-write* {subpaths})")
    return ["-p", " ".join(forms)]


def windows_acl_runner_args(runner_invocation: list[str], policy: dict,
                            temp_dir: str | None = None) -> list[str]:
    """windows-acl runner 调用的基础形态：--workspace/--temp/--mode 三参数。

    与上游 windowsAclRunnerArgv 的 agentless/read-only 分支同形状；带会话的
    workspace-write 走 provider._runner_argv（先物化授权再拼 --write-sid 对）。
    temp 缺省用平台临时根（上游无会话时即 tmpdir()）。
    """
    return [
        *runner_invocation,
        "--workspace", policy["workspaceRoot"],
        "--temp", temp_dir or tempfile.gettempdir(),
        "--mode", policy["mode"],
    ]


# ---------- 探测（上游 defaultProbe*，同形状的 subprocess 探测） ----------

def probe_bwrap(timeout_ms: int = 5000) -> bool:
    probe = subprocess.run(
        ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
         "--die-with-parent", "--", "true"],
        timeout=timeout_ms, capture_output=True,
    )
    return probe.returncode == 0


def probe_seatbelt(seatbelt_exec: str = "sandbox-exec", timeout_ms: int = 5000) -> bool:
    probe = subprocess.run(
        [seatbelt_exec, *seatbelt_profile_args({"mode": "read-only", "workspaceRoot": "/"}),
         "--", "true"],
        timeout=timeout_ms, capture_output=True,
    )
    return probe.returncode == 0


def landlock_launcher_prefix(internals: dict | None = None) -> list[str]:
    """landlock launcher 调用前缀（Phase A：ctypes 自限制执行器，无外部二进制）。

    internals.landlockLauncher 可注入覆盖：字符串视为可执行文件路径，列表
    视为完整前缀；缺省经 `python -m miniharness.seams.landlock_run` 复刻上游
    node-addon-landlock-run 的 CLI 契约（--ro/--rw/--/--probe、exit 125、
    报告行逐字一致，见 seams/landlock_run.py 模块 docstring）。
    """
    override = (internals or {}).get("landlockLauncher")
    if override is None:
        return [sys.executable, "-m", LANDLOCK_LAUNCHER_MODULE]
    if isinstance(override, str):
        return [override]
    return list(override)


def probe_landlock(launcher: list[str], timeout_ms: int = 5000) -> str:
    """功能探测：跑真 --probe 并解析报告行 → 'full' | 'partial' | 'unusable'。

    对齐上游 entry 包 probe() 映射：非零退出 = unusable（fail-closed 终点）。
    """
    try:
        proc = subprocess.run([*launcher, "--probe"], timeout=timeout_ms,
                              capture_output=True)
    except (OSError, subprocess.TimeoutExpired):
        return "unusable"
    if proc.returncode != 0:
        return "unusable"
    line = proc.stdout.decode(errors="replace").strip()
    if line == "landlock: fully enforced":
        return "full"
    if line == "landlock: partially enforced (older ABI)":
        return "partial"
    return "unusable"


def probe_windows_acl(runner_invocation: list[str], timeout_ms: int = 5000) -> bool:
    program = runner_invocation[0] if runner_invocation else None
    if program is None:
        return False
    probe = subprocess.run(
        [*runner_invocation, "--workspace", tempfile.gettempdir(),
         "--temp", tempfile.gettempdir(), "--mode", "read-only", "--", "cmd", "/c", "exit", "0"],
        timeout=timeout_ms, capture_output=True,
    )
    return probe.returncode == 0


class LocalSandboxProvider:
    """本地沙箱后端：按平台链选择 runner，缓存裁决，fail closed。

    对齐上游 LocalSandboxProvider 的 confine/selectRunner/chainVerdict/
    probeRunner 语义。internals 注入钩子与上游 SandboxInternals 同用途：
    platform/chain/probes/launcher 路径可替换（约定测试的载体）。
    """

    def __init__(self, internals: dict | None = None,
                 runner_command: list[str] | None = None,
                 runner_failure_signatures: list[str] | None = None,
                 probe_timeout_ms: int = 5000):
        self.internals = dict(internals or {})
        runner = runner_command or []
        signatures = runner_failure_signatures or []
        if runner and not signatures:
            raise ValueError("runnerCommand requires at least one runnerFailureSignatures entry")
        if signatures and not runner:
            raise ValueError("runnerFailureSignatures requires runnerCommand")
        bad = [s for s in signatures if not s.strip() or "\r" in s or "\n" in s]
        if bad:
            raise ValueError("runnerFailureSignatures entries must be non-empty single-line strings")
        if not (isinstance(probe_timeout_ms, int) and probe_timeout_ms > 0):
            raise ValueError("probeTimeoutMs must be a positive finite number")
        self._runner_command = runner or None
        self._runner_failure_signatures = signatures
        self._probe_timeout_ms = probe_timeout_ms
        self._selected: dict | str | None = None
        # ACL 授权缓存：sessionId → {grant, temp_dir}（上游 provider 的
        # grants/tempDirs 两张表在 mini 合一——每会话恰一把 grant）。
        self._acl_grants: dict[str, dict] = {}

    # ---------- 主入口 ----------

    def confine(self, argv: list[str], policy: dict) -> dict:
        """包裹 argv：返回 {argv, enforcement, denialSignatures, runnerFailureRules}。

        argv 必须是精确的待 spawn 参数（程序 + 参数），不是 shell 字符串；
        shell 形态的消费者传 ['bash', '-c', command]（上游同约定）。
        """
        if self._runner_command is not None:
            return {
                "argv": [*self._runner_command, *bwrap_profile_args(policy), "--", *argv],
                "enforcement": "full",
                "denialSignatures": DENIAL_SIGNATURES["runnerCommand"],
                "runnerFailureRules": [{"fatalSignatures": self._runner_failure_signatures}],
            }
        selected = self._select_runner(policy["mode"])
        runner_argv = self._runner_argv(selected["runner"], policy)
        return {
            "argv": [*runner_argv, "--", *argv],
            "enforcement": selected["enforcement"],
            "denialSignatures": DENIAL_SIGNATURES[selected["runner"]],
            "runnerFailureRules": RUNNER_FAILURE_RULES[selected["runner"]],
        }

    # ---------- 链选择与探测 ----------

    def _select_runner(self, mode: str):
        if self._selected is None:
            self._selected = self._chain_verdict()
        if self._selected == "unavailable":
            raise SandboxUnavailableError(mode)
        return self._selected

    def _chain_verdict(self) -> dict | str:
        chain = self.internals.get("chain") or PLATFORM_CHAINS.get(
            self.internals.get("platform") or self._host_platform(), [])
        first = chain[0] if chain else None
        if first is None:
            return "unavailable"
        if len(chain) == 1:
            # 单候选无需仲裁；其执行期拒绝仍是 fail-closed 终点
            return {"runner": first, "enforcement": STATIC_ENFORCEMENT[first]}
        for runner in chain:
            enforcement = self._probe_runner(runner)
            if enforcement != "unusable":
                return {"runner": runner, "enforcement": enforcement}
        return "unavailable"

    def _host_platform(self) -> str:
        if os.name == "nt":
            return "win32"
        if os.uname().sysname == "Darwin":  # pragma: no cover - POSIX 平台
            return "darwin"
        return "linux"  # pragma: no cover - POSIX 平台

    def _probe_runner(self, runner: str) -> str:
        if runner == "bwrap":
            probe = self.internals.get("probeBwrap") or (lambda: probe_bwrap(self._probe_timeout_ms))
            return "full" if probe() else "unusable"
        if runner == "landlock":
            launcher = landlock_launcher_prefix(self.internals)
            probe = self.internals.get("probeLandlock")
            if probe is None:
                return probe_landlock(launcher, self._probe_timeout_ms)
            return probe(launcher)
        if runner == "seatbelt":
            probe = self.internals.get("probeSeatbelt")
            seatbelt_exec = self.internals.get("seatbeltExec") or "sandbox-exec"
            if probe is None:
                return "full" if probe_seatbelt(seatbelt_exec, self._probe_timeout_ms) else "unusable"
            return "full" if probe(seatbelt_exec) else "unusable"
        if runner == "windows-acl":
            probe = self.internals.get("probeWindowsAcl")
            if probe is None:
                return "partial" if probe_windows_acl(self._windows_acl_runner_invocation(),
                                                      self._probe_timeout_ms) else "unusable"
            return "partial" if probe() else "unusable"
        return "unusable"

    def _runner_argv(self, runner: str, policy: dict) -> list[str]:
        if runner == "bwrap":
            return ["bwrap", *bwrap_profile_args(policy)]
        if runner == "landlock":
            grants = landlock_profile_args(policy)
            # 旗标拼写对齐上游 cli-contract.md 语法：`--ro <path>` / `--rw <path>`
            argv = landlock_launcher_prefix(self.internals)
            for path in grants["readOnly"]:
                argv += ["--ro", path]
            for path in grants["readWrite"]:
                argv += ["--rw", path]
            return argv
        if runner == "seatbelt":
            seatbelt_exec = self.internals.get("seatbeltExec") or "sandbox-exec"
            return [seatbelt_exec, *seatbelt_profile_args(policy)]
        if runner == "windows-acl":
            invocation = self._windows_acl_runner_invocation()
            if policy.get("sessionId") is not None and policy.get("mode") == "workspace-write":
                record = self._materialize_acl_grant(policy["sessionId"], policy["workspaceRoot"])
                return [
                    *invocation,
                    "--workspace", policy["workspaceRoot"],
                    "--temp", record["temp_dir"],
                    "--mode", policy["mode"],
                    "--write-sid", record["grant"].write_sid,
                    "--temp-write-sid", temp_write_sid(record["temp_dir"]),
                ]
            return windows_acl_runner_args(invocation, policy)
        raise ValueError(f"unknown runner: {runner}")

    # ---------- ACL 授权物化（上游 materializeAclGrant / rmTempDir /
    # revokeAclGrants 对应物） ----------

    def _materialize_acl_grant(self, session_id: str, workspace_root: str) -> dict:
        """为会话物化 ACL 授权：常驻 workspace ACE + 私有 temp 目录与其可撤销
        授权。每会话缓存；同会话重复调用返回既有记录。temp 根 ⊄ workspace
        断言在任何授权动作之前（provider bug 在边界大声失败）。fail-closed：
        物化中途失败即 dispose 已授予路径并上抛。"""
        existing = self._acl_grants.get(session_id)
        if existing is not None:
            return existing
        assert_temp_root_outside_workspace(workspace_root, tempfile.gettempdir())
        # workspace_root 与 _runner_argv 拼进 --workspace 的是同一份 policy 值
        # （sandbox-policy 已先 canonical）——SID 派生与 runner 校验同源。
        grant = AclWriteGrant.create(workspace_write_sid(workspace_root))
        try:
            grant.add(workspace_root, standing=True)
            temp_dir = _create_session_temp_dir()
            grant.add(temp_dir)
        except BaseException:
            grant.dispose()
            raise
        record = {"grant": grant, "temp_dir": temp_dir}
        self._acl_grants[session_id] = record
        return record

    def remove_temp_dir(self, session_id: str) -> None:
        """删除会话私有 temp 目录（上游 internals.rmTempDir 注入点的载体）。
        标准流程不调用它：目录由 revoke_acl_grants 在**撤销 ACE 之后**统一
        删除（上游 revokeAclGrants 顺序——先 dispose 再 rmSync）。未知会话
        静默返回。"""
        record = self._acl_grants.get(session_id)
        if record is None:
            return
        hook = self.internals.get("rmTempDir")
        if hook is not None:
            hook(record["temp_dir"])
            return
        shutil.rmtree(record["temp_dir"], ignore_errors=True)

    def revoke_acl_grants(self) -> None:
        """provider 关停：撤销全部可撤销（temp）授权，随后删除各会话私有
        temp 目录——常驻 workspace ACE 保留作复用缓存。顺序对齐上游
        revokeAclGrants：先 grant.dispose()（撤销时路径必须在场）再删目录。
        单项失败不阻断其余，最后聚合上报。"""
        failures: list[BaseException] = []
        for record in self._acl_grants.values():
            try:
                record["grant"].dispose()
            except Exception as error:  # noqa: BLE001 - 聚合一切撤销失败
                failures.append(error)
        for record in self._acl_grants.values():
            try:
                shutil.rmtree(record["temp_dir"], ignore_errors=True)
            except OSError as error:  # noqa: BLE001 - 同上
                failures.append(error)
        self._acl_grants.clear()
        if failures:
            raise Exception(
                f"revokeAclGrants completed with {len(failures)} cleanup failure(s)", failures)

    def _windows_acl_runner_invocation(self) -> list[str]:
        override = self.internals.get("windowsAclRunnerArgs")
        if override is not None:
            return list(override)
        # 缺省经 `python -m miniharness.seams.sandbox_windows_acl.runner`
        # 复刻上游打包 runner 入口（Phase C）：非 win32 宿主 FFI 加载失败 →
        # 探测/执行干净失败 → fail closed。
        return [sys.executable, "-m", WINDOWS_ACL_RUNNER_MODULE]