"""Windows ACL 工作区 / 私有 temp 能力的规范目录边界检查
（上游 path-boundary.ts 对应物）。

containsDirectory 经 realpath 规范化后比较相对关系（上游 realpathSync.native
同位；Windows 上 realpath 返回真实大小写路径，收敛拼写差异）。跨盘符的
relative 计算在 Python 抛 ValueError、在 Node 返回绝对路径——两种载体都归入
「不包含」，语义一致。
"""
from __future__ import annotations

import os


def _contains_directory(root: str, candidate: str) -> bool:
    """``root`` 与 ``candidate`` 是同一规范目录，或前者包含后者。

    路径不存在时 realpath(strict) 抛 OSError——fail closed（上游
    realpathSync.native 同为 ENOENT 异常）。
    """
    root_real = os.path.realpath(root, strict=True)
    candidate_real = os.path.realpath(candidate, strict=True)
    try:
        relation = os.path.relpath(candidate_real, root_real)
    except ValueError:
        # 跨盘符：Node relative() 给绝对路径 → isAbsolute → False，同语义
        return False
    return relation == "." or (
        not os.path.isabs(relation)
        and relation != ".."
        and not relation.startswith(".." + os.sep)
    )


def assert_temp_root_outside_workspace(workspace_root: str, temp_root: str) -> None:
    """拒绝 workspace 内的 temp 父目录：其下创建的每个子目录都会继承
    常驻工作区能力。"""
    if _contains_directory(workspace_root, temp_root):
        raise ValueError(
            f"Windows ACL temp root must be outside the workspace: "
            f"workspace={workspace_root}; temp={temp_root}")


def assert_private_temp_disjoint(writable_dirs: list[str], temp_dir: str) -> None:
    """拒绝实际私有 temp 目录与任何可写目录重叠：任一继承方向都会把两个
    能力合并。"""
    for writable_dir in writable_dirs:
        if _contains_directory(writable_dir, temp_dir) or _contains_directory(temp_dir, writable_dir):
            raise ValueError(
                f"AclSandbox private temp directory must be disjoint from writable directories: "
                f"writable={writable_dir}; temp={temp_dir}")
