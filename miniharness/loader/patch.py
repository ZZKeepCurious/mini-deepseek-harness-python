import copy
from typing import Any, Callable

WarnSink = Callable[[str], None]


def apply_entry_patches(
    data: list[dict],
    patches: list[dict] | None = None,
    warn: WarnSink | None = None,
) -> list[dict]:
    """对条目列表应用补丁层（对齐 vendor/include/src/index.ts applyEntryPatches）。

    - 输入只读：反复应用（热重载）必须能还原，故先深拷贝；
    - insert 定向 id 时要求目标是组（group），否则 warn 跳过；无 id 追加到顶层；
    - 新插入条目立即入索引，后续补丁可继续命中（每层叠一源语义）；
    - replace 语义由调用方拆成 {id, ...overrides}（boot 把旧 {replace:...} 叠层
      转成这形态）；target 缺失 / name 不匹配 → warn 跳过。
    """
    if not patches:
        return list(data)
    data = copy.deepcopy(data)

    entry_map: dict[str, dict] = {}

    def build_map(entries: list[dict]) -> None:
        for entry in entries:
            entry_id = entry.get("id")
            if entry_id:
                entry_map[entry_id] = entry
            if entry.get("group") and isinstance(entry.get("config"), list):
                build_map(entry["config"])

    build_map(data)

    def _warn(message: str, *args: Any) -> None:
        if warn is not None:
            warn(message, *args)

    for patch in patches:
        if not isinstance(patch, dict):
            raise TypeError("patch 必须是对象")
        pid = patch.get("id")
        insert = patch.get("insert")
        name = patch.get("name")
        overrides = {k: v for k, v in patch.items() if k not in ("id", "insert", "name")}

        if insert:
            if pid:
                target = entry_map.get(pid)
                if target is None:
                    _warn("patch insert: entry %C not found", pid)
                    continue
                if not target.get("group"):
                    _warn("patch insert: entry %C is not a group", pid)
                    continue
                if not isinstance(target.get("config"), list):
                    target["config"] = []
                target["config"].extend(insert)
            else:
                data.extend(insert)
            build_map(insert)
            continue

        if not pid:
            _warn("patch: id is required for non-insert patches")
            continue

        target = entry_map.get(pid)
        if target is None:
            _warn("patch: entry %C not found", pid)
            continue

        if name and name != target.get("name"):
            _warn("patch: name mismatch for %C (expected %C, got %C), skipping",
                  pid, target.get("name"), name)
            continue

        for key, value in overrides.items():
            if key == "id":
                continue
            target[key] = value

    return data