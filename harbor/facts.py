"""从实际回执推导泊位占用。

泊位是否空出只看 ``lines_clear`` 的实际时点；``shift_updates`` 的预计腾出
时间只用于提醒，不参与可用性判断，对应“预计不替代实际离泊记录”。
"""

from dataclasses import dataclass


@dataclass
class CallActuals:
    call: str
    berth: str = None
    all_fast: object = None
    lines_clear: object = None

    @property
    def moored(self):
        return self.all_fast is not None and self.lines_clear is None

    @property
    def departed(self):
        return self.all_fast is not None and self.lines_clear is not None

    @property
    def status(self):
        if self.lines_clear is not None:
            return "departed"
        if self.all_fast is not None:
            return "moored"
        return "not_arrived"


@dataclass(frozen=True)
class Occupancy:
    berth: str
    call: str
    start: object
    end: object = None

    @property
    def open(self):
        return self.end is None


def call_actuals(project, feeds):
    """按航次汇总实际系缆与解缆，返回 ``{call: CallActuals}``。

    事件按时间排序。``lines_clear`` 只结束同一泊位上的系泊；泊位不符的回执
    不能关闭占用，避免把“离开泊位”和“协助结束”混为一谈。
    """
    events = sorted(
        list(feeds.quay_events) + list(project.quay_events),
        key=lambda item: (item.at, item.event),
    )
    result = {}
    for event in events:
        record = result.setdefault(event.call, CallActuals(call=event.call))
        if event.kind == "all_fast":
            record.berth = event.berth
            record.all_fast = event.at
            record.lines_clear = None
        elif event.kind == "lines_clear":
            if record.all_fast is None or record.lines_clear is not None:
                continue
            if record.berth is not None and event.berth != record.berth:
                continue
            record.lines_clear = event.at
    return result


def occupancies(project, feeds):
    """按泊位列出实际占用区间；未解缆的占用 ``end=None``。"""
    by_berth = {}
    for call, actual in call_actuals(project, feeds).items():
        if actual.all_fast is None:
            continue
        by_berth.setdefault(actual.berth, []).append(
            Occupancy(
                berth=actual.berth,
                call=call,
                start=actual.all_fast,
                end=actual.lines_clear,
            )
        )
    for rows in by_berth.values():
        rows.sort(key=lambda row: row.start)
    return by_berth


def expected_clear(project, feeds, berth):
    """码头公布的最新预计腾出，仅用于提示；不改变实际占用。"""
    rows = [item for item in (list(feeds.shift_updates) + list(project.shift_updates)) if item.berth == berth]
    if not rows:
        return None
    return max(rows, key=lambda item: (item.announced_at, item.update))
