"""assistant/chunk 增量串的无损存储打包（StorageRecord 层）。

对应 dsh 真实源码：packages/core/session/src/chunk-rows.ts。

上游动机：provider 按 token 粒度流式出增量，日志里是成百条几乎相同的
事件行，JSON 信封体积远超载荷（实测 ~56×）。本模块把「连续同类同块」的
增量事件串打包为一行存储记录 —— text-chunks / reasoning-chunks /
tool-call-chunks，读回时逐字节还原原始事件序列。

存储记录是**耐久编码词表，不是会话事件**：不进 Session.events、无
SessionEventMap 条目、用无斜杠裸类型名以防与事件分类混淆（先例：JSONL
header 行的 session 标签）。编码端只白名单完全认识的形状 —— 任何没认全的
输入原样落盘（丢压缩率，绝不丢数据）；解码端先校验再展开，畸形存储行
响亮报错而不是静默丢弃整串。

读取侧无条件支持两种布局（打不打包由写入配置决定，读取永远兼容）。
"""
from __future__ import annotations

from typing import Any

# 低于该成员数的串不值得打包（行信封体积与被替换的事件行相当）。
# 这是格式常量不是调优项：两种布局解码结果一致，改动它不使既有日志失效。
MIN_RUN = 3

_DELTA_KINDS = ("text-delta", "reasoning-delta", "tool-call-delta")
_ROW_TAGS = ("text-chunks", "reasoning-chunks", "tool-call-chunks")

# Number.isSafeInteger 边界（2^53 - 1）：间隙编解码走浮点加减，
# 超界即可能舍入到另一个数 —— 静默损坏。
_SAFE_INT_MAX = 2**53 - 1


def _is_safe_int(value: Any) -> bool:
    """JS Number.isSafeInteger 等价：整数且 |x| <= 2^53-1。"""
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and -_SAFE_INT_MAX <= value <= _SAFE_INT_MAX
    )


def _is_number(value: Any) -> bool:
    """JS typeof x === 'number' 等价（bool 不是 number）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _has_exact_keys(value: dict, keys: tuple[str, ...]) -> bool:
    """精确键检查：value 的键集合与 keys 完全一致。"""
    return len(value) == len(keys) and all(k in value for k in keys)


def _is_record(value: Any) -> bool:
    return isinstance(value, dict)


def classify(event: Any) -> str | None:
    """把事件分类为可打包的增量种类；形状没认全返回 None（原样落盘）。

    输入既可能是内存里刚 append 的带类型事件，也可能是解析自夹具文件的
    裸 dict，所以检查是结构性的，不信任静态类型。整数 time 才能保证间隙
    编码精确：分数时间经浮点减加重构后未必回得来。
    """
    if not _is_record(event) or event.get("type") != "assistant/chunk":
        return None
    if not _has_exact_keys(event, ("type", "seq", "time", "data")):
        return None
    if not _is_safe_int(event["seq"]) or event["seq"] < 0:
        return None
    if not _is_safe_int(event["time"]):
        return None
    data = event["data"]
    if not _is_record(data) or not _has_exact_keys(data, ("turn", "step", "chunk")):
        return None
    if not _is_number(data["turn"]) or not _is_number(data["step"]):
        return None
    chunk = data["chunk"]
    if not _is_record(chunk) or not _is_number(chunk.get("index")):
        return None
    kind = chunk.get("type")
    if kind in ("text-delta", "reasoning-delta"):
        ok = _has_exact_keys(chunk, ("type", "index", "text")) and isinstance(
            chunk["text"], str
        )
        return kind if ok else None
    if kind == "tool-call-delta":
        shape_ok = _has_exact_keys(chunk, ("type", "index", "id", "argumentsDelta")) or (
            _has_exact_keys(chunk, ("type", "index", "id", "name", "argumentsDelta"))
            and isinstance(chunk["name"], str)
        )
        if shape_ok and isinstance(chunk["id"], str) and isinstance(
            chunk["argumentsDelta"], str
        ):
            return kind
        return None
    # 白名单漏底：block-start/end、usage、finish 与未来增量变体保持一行一事。
    return None


def _continues(prev: dict, nxt: dict, kind: str) -> bool:
    """next 是否延续以 prev 结尾的串（同类已由调用方保证）。"""
    if nxt["seq"] != prev["seq"] + 1:
        return False
    gap = nxt["time"] - prev["time"]
    if not _is_safe_int(gap):
        return False
    prev_data = prev["data"]
    next_data = nxt["data"]
    if prev_data["turn"] != next_data["turn"] or prev_data["step"] != next_data["step"]:
        return False
    if next_data["chunk"]["index"] != prev_data["chunk"]["index"]:
        return False
    if kind != "tool-call-delta":
        return True
    a = prev_data["chunk"]
    b = next_data["chunk"]
    # name 必须在场性与取值都一致 —— 混合串不可表示。
    return (
        a["id"] == b["id"]
        and ("name" in a) == ("name" in b)
        and (a.get("name") == b.get("name"))
    )


def build_row(kind: str, run: list[dict]) -> dict:
    """为完成的串（len >= MIN_RUN 且逐对 continues 一致）构造存储行。

    键序对齐上游 JSON.stringify 插入序：{type, seq0, time0,
    data:{turn, step, index, dt[, id][, name], texts|args}}。
    """
    first = run[0]
    first_chunk = first["data"]["chunk"]
    base: dict[str, Any] = {
        "turn": first["data"]["turn"],
        "step": first["data"]["step"],
        "index": first_chunk["index"],
        "dt": [ev["time"] - run[i]["time"] for i, ev in enumerate(run[1:])],
    }
    envelope: dict[str, Any] = {"seq0": first["seq"], "time0": first["time"]}
    if kind == "tool-call-delta":
        data: dict[str, Any] = {
            **base,
            "id": first_chunk["id"],
            **({"name": first_chunk["name"]} if "name" in first_chunk else {}),
            "args": [ev["data"]["chunk"]["argumentsDelta"] for ev in run],
        }
        return {"type": "tool-call-chunks", **envelope, "data": data}
    data = {**base, "texts": [ev["data"]["chunk"]["text"] for ev in run]}
    tag = "text-chunks" if kind == "text-delta" else "reasoning-chunks"
    return {"type": tag, **envelope, "data": data}


def pack_chunk_runs(events: list[dict]) -> list[dict]:
    """把一批事件打包为待写存储记录：每段 >= MIN_RUN 的连续白名单同类同块
    增量串折成一行 ChunkRow，其余事件按序原样直通。

    纯函数无状态 —— 对任意数组安全，包括被 flush 边界切断的串（切断的串
    就按批各自打包）。
    """
    out: list[dict] = []
    kind: str | None = None
    run: list[dict] = []

    def flush() -> None:
        nonlocal kind, run
        if kind is not None and len(run) >= MIN_RUN:
            out.append(build_row(kind, run))
        else:
            out.extend(run)
        kind = None
        run = []

    for event in events:
        k = classify(event)
        if k is None:
            flush()
            out.append(event)
            continue
        last = run[-1] if run else None
        if k == kind and last is not None and _continues(last, event, k):
            run.append(event)
            continue
        flush()
        kind = k
        run = [event]
    flush()
    return out


def _malformed(tag: str, why: str) -> None:
    raise ValueError(f"malformed {tag} storage row: {why}")


def _validate_run_data(tag: str, data: dict, payload_key: str) -> list[str]:
    """校验共享 run-data 字段与载荷/dt 元数；返回成员载荷。"""
    if not (_is_number(data.get("turn")) and _is_number(data.get("step")) and _is_number(data.get("index"))):
        _malformed(tag, "turn/step/index must be numbers")
    payload = data.get(payload_key)
    if (
        not isinstance(payload, list)
        or len(payload) == 0
        or any(not isinstance(entry, str) for entry in payload)
    ):
        _malformed(tag, f"{payload_key} must be a non-empty string array")
    dt = data.get("dt")
    if not isinstance(dt, list) or any(not _is_safe_int(gap) for gap in dt):
        _malformed(tag, "dt must be an array of safe integers")
    if len(dt) != len(payload) - 1:
        _malformed(tag, f"dt length {len(dt)} does not match {len(payload)} members")
    return payload


def validate_row(value: dict, tag: str) -> dict:
    """校验行信封与 data，任何畸形都抛错；返回原值。"""
    if not _has_exact_keys(value, ("type", "seq0", "time0", "data")):
        _malformed(tag, "envelope must be exactly {type, seq0, time0, data}")
    if not _is_safe_int(value["seq0"]) or value["seq0"] < 0:
        _malformed(tag, "seq0 must be a non-negative safe integer")
    if not _is_safe_int(value["time0"]):
        _malformed(tag, "time0 must be a safe integer")
    data = value["data"]
    if not _is_record(data):
        _malformed(tag, "data must be an object")
    if tag == "tool-call-chunks":
        with_name = _has_exact_keys(
            data, ("turn", "step", "index", "id", "name", "dt", "args")
        )
        without_name = _has_exact_keys(
            data, ("turn", "step", "index", "id", "dt", "args")
        )
        if not with_name and not without_name:
            _malformed(tag, "data must be exactly {turn, step, index, id, name?, dt, args}")
        if not isinstance(data.get("id"), str) or (
            with_name and not isinstance(data.get("name"), str)
        ):
            _malformed(tag, "id (and name when present) must be strings")
        payload = _validate_run_data(tag, data, "args")
    else:
        if not _has_exact_keys(data, ("turn", "step", "index", "dt", "texts")):
            _malformed(tag, "data must be exactly {turn, step, index, dt, texts}")
        payload = _validate_run_data(tag, data, "texts")
    # 重构边界：编码端只会打包成员 seq/time 全为安全整数的串，运行值离开
    # 安全范围即在任意编码端的像之外 —— 浮点运算会把它舍入成别的数，属静默
    # 损坏。安全范围内每一步都精确，所以首个越界点必被抓到。
    if not _is_safe_int(value["seq0"] + len(payload) - 1):
        _malformed(tag, "member seqs must stay safe integers")
    time = value["time0"]
    for gap in data["dt"]:
        time += gap
        if not _is_safe_int(time):
            _malformed(tag, "member times must stay safe integers")
    return value


def expand_row(row: dict) -> list[dict]:
    """把校验过的行展开回精确的原始事件序列。"""
    tag = row["type"]
    members: list[str] = row["data"]["args"] if tag == "tool-call-chunks" else row["data"]["texts"]
    events: list[dict] = []
    time = row["time0"]
    for k, member in enumerate(members):
        if k > 0:
            time += row["data"]["dt"][k - 1]
        if tag == "text-chunks":
            chunk: dict[str, Any] = {
                "type": "text-delta",
                "index": row["data"]["index"],
                "text": member,
            }
        elif tag == "reasoning-chunks":
            chunk = {
                "type": "reasoning-delta",
                "index": row["data"]["index"],
                "text": member,
            }
        else:
            chunk = {
                "type": "tool-call-delta",
                "index": row["data"]["index"],
                "id": row["data"]["id"],
                **({"name": row["data"]["name"]} if "name" in row["data"] else {}),
                "argumentsDelta": member,
            }
        events.append(
            {
                "type": "assistant/chunk",
                "seq": row["seq0"] + k,
                "time": time,
                "data": {"turn": row["data"]["turn"], "step": row["data"]["step"], "chunk": chunk},
            }
        )
    return events


def decode_storage_record(value: Any) -> list[dict]:
    """把一行解析后的 JSONL 值解码为其存储的事件（可能多条）。

    行标签值先校验再展开（畸形行抛错 —— 那是损坏的存储，当事件处理会静默
    丢掉整串）；其余值原样作为单事件直通、不做校验。
    """
    if not _is_record(value):
        return [value]
    tag = value.get("type")
    if tag not in _ROW_TAGS:
        return [value]
    return expand_row(validate_row(value, tag))
