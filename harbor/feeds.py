"""读取并校验项目约定的五类交换资料。

资料格式以 ``samples/formats.json`` 为准。读取时除了字段存在与类型，
还会把同一对象上的时间关系检查清楚，避免把自相矛盾的窗口带进排期。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from .timeutil import FeedError, format_time, parse_time

ASSIST_KINDS = ("pilot", "tug")
ASSIST_STATES = ("confirmed", "released", "dispatched", "on_board", "completed")
ACTIONS = ("approach", "departure")
QUAY_KINDS = ("all_fast", "lines_clear")

KIND_TYPE = {
    "string": str,
    "int": int,
    "bool": bool,
    "list": list,
    "time": str,
    "date": str,
}


@dataclass(frozen=True)
class Vessel:
    call: str
    beam_m: int
    draft_dm: int
    eta: object
    work_minutes: int
    latest_exit: object
    assist_required: tuple

    @property
    def assist_set(self):
        return frozenset(self.assist_required)


@dataclass(frozen=True)
class Window:
    berth: str
    revision: int
    start: object
    until: object
    draft_dm: int
    beam_m: int

    @property
    def span(self):
        return (self.start, self.until)

    def fits(self, vessel):
        return vessel.draft_dm <= self.draft_dm and vessel.beam_m <= self.beam_m


@dataclass(frozen=True)
class AssistSlot:
    group: str
    agency: str
    kind: str
    start: object
    until: object
    max_draft_dm: int

    @property
    def span(self):
        return (self.start, self.until)

    def accepts(self, vessel):
        return self.kind in vessel.assist_set and vessel.draft_dm <= self.max_draft_dm


@dataclass(frozen=True)
class QuayEvent:
    event: str
    call: str
    berth: str
    kind: str
    at: object


@dataclass(frozen=True)
class ShiftUpdate:
    update: str
    berth: str
    new_revision: int
    announced_at: object
    reported_clear: object


@dataclass
class Feeds:
    vessels: dict = field(default_factory=dict)
    windows: list = field(default_factory=list)
    assists: list = field(default_factory=list)
    quay_events: list = field(default_factory=list)
    shift_updates: list = field(default_factory=list)


def _require(record, spec, name, index):
    if not isinstance(record, dict):
        raise FeedError(f"{name} 第 {index + 1} 行应为对象")
    missing = set(spec["fields"]) - set(record)
    extra = set(record) - set(spec["fields"])
    if missing:
        raise FeedError(f"{name} 第 {index + 1} 行缺少字段 {sorted(missing)}")
    if extra:
        raise FeedError(f"{name} 第 {index + 1} 行出现未约定字段 {sorted(extra)}")
    for field_name, kind in spec["fields"].items():
        expected = KIND_TYPE[kind]
        value = record[field_name]
        if kind == "time":
            parse_time(value, f"{name}.{field_name}")
        elif type(value) is not expected:
            raise FeedError(
                f"{name} 第 {index + 1} 行 {field_name} 应为 {kind}，实际为 {type(value).__name__}"
            )
    key = spec["key"]
    if any(record[field_name] == "" for field_name in key):
        raise FeedError(f"{name} 第 {index + 1} 行记录键为空：{key}")


def _rows(path, schema, name):
    if name not in schema["feeds"]:
        raise FeedError(f"未约定的资料：{name}")
    spec = schema["feeds"][name]
    text = Path(path).read_text(encoding="utf-8")
    rows = []
    for index, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as error:
            raise FeedError(f"{name} 第 {index + 1} 行不是有效 JSON") from error
        _require(record, spec, name, index)
        rows.append(record)
    keys = [tuple(row[field_name] for field_name in spec["key"]) for row in rows]
    if len(keys) != len(set(keys)):
        raise FeedError(f"{name} 记录键重复：{spec['key']}")
    return rows


def load_schema(directory):
    return json.loads((Path(directory) / "formats.json").read_text(encoding="utf-8"))


def load(directory):
    """读取一个目录下的全部交换资料，返回 ``Feeds``。"""
    directory = Path(directory)
    schema = load_schema(directory)
    feeds = Feeds()

    for row in _rows(directory / "vessels.jsonl", schema, "vessels"):
        vessel = Vessel(
            call=row["call"],
            beam_m=row["beam_m"],
            draft_dm=row["draft_dm"],
            eta=parse_time(row["eta"], "vessels.eta"),
            work_minutes=row["work_minutes"],
            latest_exit=parse_time(row["latest_exit"], "vessels.latest_exit"),
            assist_required=tuple(row["assist_required"]),
        )
        _check_vessel(vessel)
        feeds.vessels[vessel.call] = vessel

    for row in _rows(directory / "windows.jsonl", schema, "windows"):
        window = Window(
            berth=row["berth"],
            revision=row["revision"],
            start=parse_time(row["from"], "windows.from"),
            until=parse_time(row["until"], "windows.until"),
            draft_dm=row["draft_dm"],
            beam_m=row["beam_m"],
        )
        if window.start >= window.until:
            raise FeedError(f"windows: {window.berth} 修订 {window.revision} 的 from 不早于 until")
        feeds.windows.append(window)

    for row in _rows(directory / "assist.jsonl", schema, "assist"):
        slot = AssistSlot(
            group=row["group"],
            agency=row["agency"],
            kind=row["kind"],
            start=parse_time(row["from"], "assist.from"),
            until=parse_time(row["until"], "assist.until"),
            max_draft_dm=row["max_draft_dm"],
        )
        if slot.kind not in ASSIST_KINDS:
            raise FeedError(f"assist: {slot.group} 的 kind 应为 {ASSIST_KINDS}")
        if slot.start >= slot.until:
            raise FeedError(f"assist: {slot.group} 的 from 不早于 until")
        feeds.assists.append(slot)

    for row in _rows(directory / "quay_events.jsonl", schema, "quay_events"):
        event = QuayEvent(
            event=row["event"],
            call=row["call"],
            berth=row["berth"],
            kind=row["kind"],
            at=parse_time(row["at"], "quay_events.at"),
        )
        if event.kind not in QUAY_KINDS:
            raise FeedError(f"quay_events: {event.event} 的 kind 应为 {QUAY_KINDS}")
        feeds.quay_events.append(event)

    for row in _rows(directory / "shift_updates.jsonl", schema, "shift_updates"):
        update = ShiftUpdate(
            update=row["update"],
            berth=row["berth"],
            new_revision=row["new_revision"],
            announced_at=parse_time(row["announced_at"], "shift_updates.announced_at"),
            reported_clear=parse_time(row["reported_clear"], "shift_updates.reported_clear"),
        )
        feeds.shift_updates.append(update)

    _check_duplicate_windows(feeds)
    return feeds


def _check_vessel(vessel):
    unknown = set(vessel.assist_required) - set(ASSIST_KINDS)
    if unknown:
        raise FeedError(f"vessels: {vessel.call} 需要未知协助 {sorted(unknown)}")
    if len(set(vessel.assist_required)) != len(vessel.assist_required):
        raise FeedError(f"vessels: {vessel.call} 的 assist_required 重复")
    if vessel.work_minutes <= 0:
        raise FeedError(f"vessels: {vessel.call} 的 work_minutes 必须为正")
    if vessel.latest_exit <= vessel.eta:
        raise FeedError(f"vessels: {vessel.call} 的 latest_exit 必须晚于 eta")


def _check_duplicate_windows(feeds):
    # 同一泊位同一修订内，两个窗口若重叠，则“可用时段”本身有歧义。
    by_key = {}
    for window in feeds.windows:
        by_key.setdefault((window.berth, window.revision), []).append(window)
    for (berth, revision), group in by_key.items():
        group.sort(key=lambda w: w.start)
        for earlier, later in zip(group, group[1:]):
            if later.start < earlier.until:
                raise FeedError(
                    f"windows: {berth} 修订 {revision} 的窗口 {format_time(earlier.start)} "
                    f"与 {format_time(later.start)} 重叠"
                )
