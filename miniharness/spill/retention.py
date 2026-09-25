"""有序 head/tail 保留：文本可按 token 预算切分，图片必须整块保留或整块省略。

上游对照：packages/spill/spill-policy/src/retention.ts（retainContent / textSlice /
fitText）。每端各得内容预算的一半；落在不可分图片上的剩余预算不被挪用（图片
要么整体进入某端，要么整体计入省略）。

载体差异（登记）：上游按 UTF-16 码元切分并在代理对中间切断时回退一个码元；
Python `str` 以码点为原子单位，天然不会在代理对中间切分，故 textSlice 的代理
保护在 mini 上无对应现象，不承载。
"""
from __future__ import annotations

__all__ = ["retain_content"]


def _text_slice(text: str, length: int, tail: bool) -> str:
    """取文本的前 length 个或后 length 个码点（对齐 textSlice）。"""
    if tail:
        return text[len(text) - length:] if length > 0 else ""
    return text[:length]


def _fit_text(text: str, budget: int, tail: bool, price) -> str:
    """在单调 token 计价下，求能装进 budget 的最大连续文本端（对齐 fitText）。"""
    low = 0
    high = len(text)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = _text_slice(text, middle, tail)
        if len(candidate) == 0 or price({"type": "text", "text": candidate}) <= budget:
            low = middle
        else:
            high = middle - 1
    return _text_slice(text, low, tail)


def retain_content(content: list, budget: int, price) -> dict:
    """在 token 预算内保留有序内容的两端，不移动也不部分保留图片。

    @param content - 总价超预算的 text/image 块有序序列。
    @param budget - 预留提示后剩余的 token 预算（非负）。
    @param price - 单个保留块的模型路由成本（含其框架开销）。
    @returns {head, tail, omittedBytes, omittedImages}：保留两端 + 省略的
        UTF-8 文本字节数与整图数。
    """
    head: list = []
    tail: list = []
    first = 0
    last = len(content) - 1
    head_characters = 0
    remaining = -(-budget // 2)  # ceil(budget / 2)
    while first <= last:
        block = content[first]
        cost = price(block)
        if cost <= remaining:
            head.append(block)
            remaining -= cost
            first += 1
            continue
        if block.get("type") == "text":
            text = _fit_text(block.get("text", ""), remaining, False, price)
            head_characters = len(text)
            if len(text) > 0:
                head.append({"type": "text", "text": text})
        break
    remaining = budget // 2  # floor(budget / 2)
    while last >= first:
        original = content[last]
        block = (
            {"type": "text", "text": original.get("text", "")[head_characters:]}
            if last == first and original.get("type") == "text" else original
        )
        cost = price(block)
        if cost <= remaining:
            tail.append(block)
            remaining -= cost
            last -= 1
            continue
        if block.get("type") == "text":
            text = _fit_text(block.get("text", ""), remaining, True, price)
            if len(text) > 0:
                tail.append({"type": "text", "text": text})
        break
    tail.reverse()

    def text_bytes(blocks: list) -> int:
        return sum(len(block.get("text", "").encode("utf-8"))
                   for block in blocks if block.get("type") == "text")

    def image_count(blocks: list) -> int:
        return sum(1 for block in blocks if block.get("type") == "image")

    return {
        "head": head,
        "tail": tail,
        "omittedBytes": text_bytes(content) - text_bytes(head) - text_bytes(tail),
        "omittedImages": image_count(content) - image_count(head) - image_count(tail),
    }
