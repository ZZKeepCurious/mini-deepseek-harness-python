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

载体简化（须在文档标注）：landlock 走真实 node-addon launcher，mini 只
生成同构 grant 参数（readOnly/readWrite 列表）；windows-acl 的 ACL/SID
物化（workspaceWriteSid / tempWriteSid / AclWriteGrant）mini 以参数形状
保留、无真实系统调用——两种后端在无对应二进制的宿主机上探测恒失败，
fail-closed 行为与真实宿主一致。
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from typing import Callable

SANDBOX_UNAVAILABLE = "SANDBOX_UNAVAILABLE"

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

# 每后端被拒文件效果的 stderr 方言（上游 DENIAL_SIGNATURES）
DENIAL_SIGNATURES: dict[str, tuple[str, ...]] = {
    "bwrap": ("read-only file system",),
    "landlock": ("permission denied",),
    "seatbelt": ("operation not permitted",),
    "windows-acl": ("access is denied", "access to the path", "permission denied"),
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
    """windows-acl runner 调用：--workspace/--temp/--mode（+ 会话写 SID）。

    与上游 windowsAclRunnerArgv 同形状：agentless 或 read-only 只带三个
    基础参数；sessionId + workspace-write 追加 --write-sid 与
    --temp-write-sid（mini 以派生占位保留参数契约，无真实 ACL 物化——
    简化标注）。temp 缺省用平台临时根（上游无会话时即 tmpdir()）。
    """
    args = [
        *runner_invocation,
        "--workspace", policy["workspaceRoot"],
        "--temp", temp_dir or tempfile.gettempdir(),
        "--mode", policy["mode"],
    ]
    if policy.get("sessionId") is not None and policy.get("mode") == "workspace-write":
        args += ["--write-sid", _workspace_write_sid(policy["workspaceRoot"]),
                 "--temp-write-sid", f"temp:{policy['sessionId']}"]
    return args


def _workspace_write_sid(workspace_root: str) -> str:
    """按规范工作区路径派生的每工作区写 SID 身份（上游 workspaceWriteSid）。

    上游为每个工作区分配真实 SID 并物化 ACE；mini 保留"同一工作区恒为
    同一身份"的派生语义，字符串形态为确定性占位（简化标注）。
    """
    return "ws-" + format(abs(hash(canonical_path(workspace_root))), "x")


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


def probe_windows_acl(runner_invocation: list[str], timeout_ms: int = 5000) -> bool:
    program = runner_invocation[0] if runner_invocation else None
    if program is None:
        return False
    probe = subprocess.run(
        [*runner_invocation[1:], "--workspace", tempfile.gettempdir(),
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
            probe = self.internals.get("probeLandlock")
            launcher = self.internals.get("landlockLauncher") or "landlock-run"
            if probe is None:
                # 无 node-addon launcher 的宿主机：探测恒不可用（fail closed）
                return "unusable"
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
            launcher = self.internals.get("landlockLauncher") or "landlock-run"
            # 旗标拼写对齐上游 entry/index.ts:96-97：`--ro <path>` / `--rw <path>`
            grant_args = landlock_profile_args(policy)
            return [launcher, *sum(([ "--ro", r] for r in grant_args["readOnly"]), []),
                    *sum(([ "--rw", r] for r in grant_args["readWrite"]), [])]
        if runner == "seatbelt":
            seatbelt_exec = self.internals.get("seatbeltExec") or "sandbox-exec"
            return [seatbelt_exec, *seatbelt_profile_args(policy)]
        if runner == "windows-acl":
            return windows_acl_runner_args(self._windows_acl_runner_invocation(), policy)
        raise ValueError(f"unknown runner: {runner}")

    def _windows_acl_runner_invocation(self) -> list[str]:
        override = self.internals.get("windowsAclRunnerArgs")
        if override is not None:
            return list(override)
        # mini 无打包 runner 入口：缺省探测恒失败（fail closed），真实宿主
        # 由消费者以 internals.windowsAclRunnerArgs 提供 runner 前缀。
        return []