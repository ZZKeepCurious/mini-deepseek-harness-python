"""Profile 目录机制与补丁层组合。

对齐上游 `packages/boot/app-boot/src/profile.ts` + `profile-context.ts`：

- 一个 profile 是 `$MINIHARNESS_HOME/profiles/<name>/` 目录：`package.json`
  （manifest，含 `dsh.profile.bundles` 有序 bundle 列表）+ `cordis.patch.yml`
  （用户补丁层，应用于每个 bundle 层之后）。
- bundle 是声明 `dsh.bundle.patch`（一个文件或有序文件列表）的包；组合 =
  按 `dsh.profile.bundles` 序把每 bundle 的 patch 列表套在空条目表上，再套
  profile 自己的补丁，再套启动器层（`--patch` 文件与旗标派生补丁）。
- `read_profile_patches` 的层序（profile-context.ts:63-75）：bundle 层 →
  profile `cordis.patch.yml` → home 级 `$MINIHARNESS_HOME/cordis.patch.yml` →
  `--patch` overlays → telemetry 补丁，后压先。

载体差异（登记）：
- mini 无 npm bundle 包（`@deepseek-ai/dsh-base`/`web-app` 等）——bundle 层
  解析（`resolveBundleDir` 双锚点 + npm 包 manifest）架构不适用；mini 的
  profile 只承载用户 patch 层 + home 层 + overlays（bundle 层为 []）。
  设计上保留 `dsh.profile.bundles` 读面，读到 bundle 名时按不可解析跳过
  （同上游「不可读/不兼容 bundle 写 stderr 并跳过」）。
- mini 无 pnpm 安装面（plugin-manager 的 pnpm 子进程族登记触发条件）。
- `compose_entries` 复用 `loader/patch.py` 的 `apply_entry_patches`（空根单次
  应用，同上游 composeEntries 的 applyEntryPatches([], layers.flat())）。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.home_paths import resolve_dsh_home
from .composition import load_document, load_patch_list

__all__ = [
    "PROFILES_DIR",
    "PROFILE_PATCH_FILENAME",
    "PROFILE_TEMPLATES",
    "Profile",
    "bundle_patch_files",
    "compose_entries",
    "init_profile",
    "load_profile_directory",
    "read_profile_patches",
    "resolve_profile_dir",
]

#: harness 根下的 profiles 目录（上游 profile.ts PROFILES_DIR = 'profiles'）。
PROFILES_DIR = "profiles"

#: profile 目录内的用户补丁层文件名（上游 PROFILE_PATCH_FILENAME）。
PROFILE_PATCH_FILENAME = "cordis.patch.yml"

#: shipped profile 模板：首次使用自动初始化（上游 PROFILE_TEMPLATES）。
#: mini 的模板 bundle 列表保留（契约面），但 bundle 层在 mini 无 npm 解析
#: 载体（见模块头载体差异）。
PROFILE_TEMPLATES: dict[str, list[str]] = {
    "headless": ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-headless"],
    "web": ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app"],
}

_PROFILE_PATCH_TEMPLATE = (
    "# Your patch layer for this dsh profile, applied after every bundle layer:\n"
    "# a top-level YAML array of loader patch entries (id-targeted config\n"
    "# overrides, disables, and insert lists; `!!js` expressions allowed).\n"
    "[]\n"
)


@dataclass(frozen=True)
class ProfileLayer:
    """一个已解析的 bundle 层（上游 ProfileLayer）。

    mini 无 npm bundle 解析，`patches` 恒为 []；保留字段形状供未来 bundle
    载体落地。
    """
    package_name: str
    package_dir: str
    patch_paths: tuple[str, ...] = ()
    patches: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class Profile:
    """一个已加载的 profile（上游 Profile）：bundle 层 + 用户补丁层。"""
    name: str
    dir: str
    layers: list[ProfileLayer]
    patch_path: str
    patches: list[dict]


def resolve_profile_dir(name: str, home: str | None = None) -> str:
    """解析 profile 目录（上游 resolveProfileDir）：名字校验 + `home/profiles/<name>`。

    @param name profile 名（`--profile <name>`）。
    @param home harness 根；缺省 resolve_dsh_home()。
    @returns profile 目录绝对路径（可能尚不存在）。
    """
    if name == "" or "/" in name or "\\" in name \
            or name in (".", "..", "node_modules"):
        raise ValueError(f"invalid profile name {name!r}")
    base = home if home is not None else resolve_dsh_home()
    return os.path.join(base, PROFILES_DIR, name)


def init_profile(dir_path: str, bundles: list[str]) -> None:
    """初始化一个 profile 目录：manifest + 空用户补丁层（上游 initProfile）。

    已存在的文件绝不触碰（重跑是已初始化 profile 上的 no-op）。
    """
    Path(dir_path).mkdir(parents=True, exist_ok=True)
    manifest_path = os.path.join(dir_path, "package.json")
    if not os.path.exists(manifest_path):
        manifest: dict[str, Any] = {
            "name": f"dsh-profile-{os.path.basename(dir_path)}",
            "private": True,
            "dependencies": {},
            "dsh": {"profile": {"bundles": list(bundles)}},
        }
        with open(manifest_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(manifest, indent=2) + "\n")
    patch_path = os.path.join(dir_path, PROFILE_PATCH_FILENAME)
    if not os.path.exists(patch_path):
        with open(patch_path, "w", encoding="utf-8") as handle:
            handle.write(_PROFILE_PATCH_TEMPLATE)


def bundle_patch_files(bundle: dict) -> list[str]:
    """bundle 声明的补丁文件（上游 bundlePatchFiles）：string 或 string 列表。"""
    declared = bundle.get("patch")
    if isinstance(declared, str):
        return [declared]
    if isinstance(declared, list) and all(isinstance(f, str) for f in declared):
        return declared
    raise ValueError("dsh.bundle.patch must be a file path or a list of file paths")


def load_profile_directory(
    bin_name: str,
    dir_path: str,
    *,
    user_layer: bool = True,
) -> Profile:
    """加载一个已初始化的 profile 目录（上游 loadProfileDirectory）。

    读 manifest → `dsh.profile.bundles` 逐 bundle 解析（mini 无 npm 解析，
    读到 bundle 名即跳过并记 warn——同上游「不可解析 bundle 跳过」）→ 读
    用户 `cordis.patch.yml`（user_layer=False 跳过）。

    @param bin_name 诊断前缀。
    @param dir_path profile 目录。
    @param user_layer False 跳过用户补丁层（bundles-only 消费者不能因坏用户
        层失败）。
    @returns 加载的 profile（用户层跳过时 patches 为空）。
    """
    manifest = _read_manifest(bin_name, dir_path)
    bundles = (manifest.get("dsh") or {}).get("profile", {}).get("bundles", [])
    layers: list[ProfileLayer] = []
    for package_name in bundles:
        # mini 无 npm bundle 包载体：不可解析即跳过（同上游「unreadable bundle
        # 写 stderr 并跳过」）。保留契约面供未来 bundle 载体。
        if isinstance(package_name, str) and package_name:
            layers.append(ProfileLayer(package_name=package_name,
                                       package_dir="", patch_paths=(), patches=[]))
    patch_path = os.path.join(dir_path, PROFILE_PATCH_FILENAME)
    patches: list[dict] = []
    if user_layer and os.path.exists(patch_path):
        patches = load_patch_list(patch_path, bin_name)
    return Profile(name=os.path.basename(dir_path), dir=dir_path,
                   layers=layers, patch_path=patch_path, patches=patches)


def _read_manifest(bin_name: str, dir_path: str) -> dict:
    manifest_path = os.path.join(dir_path, "package.json")
    if not os.path.exists(manifest_path):
        raise FileNotFoundError(f"{bin_name}: profile directory has no package.json: {dir_path}")
    return load_document(manifest_path, bin_name, "profile manifest")


def compose_entries(layers: list[list[dict]]) -> list[dict]:
    """把补丁层组合成空根上的有效条目表（上游 composeEntries）。

    复用 `loader/patch.py` 的 apply_entry_patches（空根单次应用，同上游
    applyEntryPatches([], structuredClone(layers.flat()))）。
    """
    from ..loader.patch import apply_entry_patches
    flat = [dict(patch) for layer in layers for patch in layer]
    return apply_entry_patches([], flat)


def read_profile_patches(
    bin_name: str,
    profile: Profile,
    *,
    home: str | None = None,
    overlays: list[dict] | None = None,
) -> list[dict]:
    """读取当前 bundle + 用户层 + home 层 + overlays 的有序补丁（上游 readProfilePatches）。

    层序（后压先）：bundle 层 → profile `cordis.patch.yml` → home 级
    `$HOME/cordis.patch.yml` → `--patch` overlays。mini 无 telemetry 补丁面。

    @param bin_name 诊断前缀。
    @param profile 已加载的 profile。
    @param home harness 根（home 级补丁层）。
    @param overlays 命令行 overlay 补丁（应用在 profile + home 之上）。
    @returns 分离的有序补丁列表（不更新 Loader）。
    """
    patches: list[dict] = []
    for layer in profile.layers:
        patches.extend(layer.patches)
    patches.extend(profile.patches)
    home_path = home if home is not None else resolve_dsh_home()
    home_patch = os.path.join(home_path, PROFILE_PATCH_FILENAME)
    if os.path.exists(home_patch):
        patches.extend(load_patch_list(home_patch, bin_name))
    if overlays:
        patches.extend([dict(p) for p in overlays])
    return patches