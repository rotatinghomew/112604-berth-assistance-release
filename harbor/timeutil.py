"""时间解析与左闭右开区间运算。

交换资料中的时间一律为带 ``Z`` 的 UTC ISO 8601 文本；业务区间一律为
``[from, until)``。窗口末尾那一刻不属于该窗口，与 ``samples/windows.jsonl``
的约定一致。
"""

from datetime import datetime, timedelta, timezone

UTC = timezone.utc


class FeedError(ValueError):
    """外部资料无法解析或自相矛盾。"""


def parse_time(text, field="time"):
    """解析带 ``Z`` 的 UTC 时刻。"""
    if not isinstance(text, str):
        raise FeedError(f"{field}: 应为带 Z 的 ISO 8601 文本")
    if not text.endswith("Z"):
        raise FeedError(f"{field}: 时刻必须以 Z 结尾，表示 UTC：{text!r}")
    try:
        return datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise FeedError(f"{field}: 无法解析时刻 {text!r}") from error


def format_time(moment):
    if moment is None:
        return None
    if moment.tzinfo is None:
        raise FeedError("输出时刻必须带时区")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def shift(moment, minutes):
    return moment + timedelta(minutes=minutes)


def intersect(a, b):
    """两个半开区间的交集；没有交集时返回 None。"""
    if a is None or b is None:
        return None
    start = max(a[0], b[0])
    end = min(a[1], b[1])
    return (start, end) if start < end else None


def merge(spans):
    """合并重叠或相接的区间，返回升序且互不相交的列表。"""
    ordered = sorted(span for span in spans if span is not None)
    result = []
    for span in ordered:
        if result and span[0] <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], span[1]))
        else:
            result.append(span)
    return result


def subtract(base, cuts):
    """从一组区间中挖掉 ``cuts``，返回剩余区间。"""
    remaining = merge(base)
    for cut in merge(cuts):
        updated = []
        for span in remaining:
            if cut[1] <= span[0] or cut[0] >= span[1]:
                updated.append(span)
                continue
            if span[0] < cut[0]:
                updated.append((span[0], cut[0]))
            if cut[1] < span[1]:
                updated.append((cut[1], span[1]))
        remaining = updated
    return remaining


def intersect_all(lists):
    """多组区间的公共交集。"""
    if not lists:
        return []
    current = merge(lists[0])
    for spans in lists[1:]:
        candidates = []
        for left in current:
            for right in merge(spans):
                overlap = intersect(left, right)
                if overlap:
                    candidates.append(overlap)
        current = merge(candidates)
        if not current:
            break
    return current


def union(lists):
    return merge([span for spans in lists for span in spans])


def describe(span):
    if span is None:
        return None
    return {"from": format_time(span[0]), "until": format_time(span[1])}


def parse_span(payload, field):
    if not isinstance(payload, dict):
        raise FeedError(f"{field}: 应为包含 from/until 的对象")
    try:
        start, end = payload["from"], payload["until"]
    except KeyError as error:
        raise FeedError(f"{field}: 缺少 {error.args[0]}") from error
    span = (parse_time(start, f"{field}.from"), parse_time(end, f"{field}.until"))
    if span[0] >= span[1]:
        raise FeedError(f"{field}: from 必须早于 until")
    return span
