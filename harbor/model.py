"""项目状态：航次预报、排期方案、协助承诺与实际回执。

本项目只保存“自己登记过的内容”。泊位窗口、协助可用区间和现场事件来自
交换资料；服务读取时把两者合并，并在输出中标明来源，避免把预计时间误当
实际回执。

两类事实严格分开：

* 泊位位置：由 ``all_fast`` / ``lines_clear`` 实际记录推进；
* 协助组：由对端协助回话（confirmed/released/dispatched/on_board/completed）推进。

因此“拖轮结束”不代表“船已离泊”，“解缆”也不代表“协助组空闲”。
"""

from dataclasses import dataclass, field

from .feeds import QuayEvent, Vessel, Window
from .timeutil import FeedError, describe, format_time, parse_span, parse_time

PLAN_STATES = ("candidate", "confirmed", "withdrawn", "superseded")
ASSIST_KINDS = ("pilot", "tug")

# Booking.state 的取值。peer_state 保存对端最近一次明确回话。
BOOKING_STATES = (
    "booked",      # 本项目已登记，未取得或未使用对端答复
    "active",      # 对端回话 dispatched / on_board
    "cancelling",  # 已登记撤销，等待原机构确认
    "released",    # 对端确认释放，可给别的船
    "completed",   # 对端确认该动作结束
    "refused",     # 撤销被拒，仍由原动作占用
)
PEER_FREE_STATES = ("released", "completed")
PEER_HELD_STATES = ("dispatched", "on_board")


def _span_json(span):
    return describe(span)


def _span_load(payload, field_name="span"):
    if payload is None:
        return None
    return parse_span(payload, field_name)


@dataclass
class Forecast:
    """船代登记的到达预报，可被继续更新。"""

    beam_m: int
    draft_dm: int
    eta: object
    work_minutes: int
    latest_exit: object
    assist_required: tuple

    @classmethod
    def from_vessel(cls, vessel):
        return cls(
            beam_m=vessel.beam_m,
            draft_dm=vessel.draft_dm,
            eta=vessel.eta,
            work_minutes=vessel.work_minutes,
            latest_exit=vessel.latest_exit,
            assist_required=tuple(vessel.assist_required),
        )

    def to_vessel(self, call):
        return Vessel(
            call=call,
            beam_m=self.beam_m,
            draft_dm=self.draft_dm,
            eta=self.eta,
            work_minutes=self.work_minutes,
            latest_exit=self.latest_exit,
            assist_required=tuple(self.assist_required),
        )

    def validate(self, call):
        if self.work_minutes <= 0:
            raise FeedError(f"vessels: {call} 的 work_minutes 必须为正")
        if self.latest_exit <= self.eta:
            raise FeedError(f"vessels: {call} 的 latest_exit 必须晚于 eta")
        unknown = set(self.assist_required) - set(ASSIST_KINDS)
        if unknown:
            raise FeedError(f"vessels: {call} 需要未知协助 {sorted(unknown)}")
        if len(set(self.assist_required)) != len(self.assist_required):
            raise FeedError(f"vessels: {call} 的 assist_required 重复")

    def to_json(self):
        return {
            "beam_m": self.beam_m,
            "draft_dm": self.draft_dm,
            "eta": format_time(self.eta),
            "work_minutes": self.work_minutes,
            "latest_exit": format_time(self.latest_exit),
            "assist_required": list(self.assist_required),
        }

    @classmethod
    def from_json(cls, payload):
        if not isinstance(payload, dict):
            raise FeedError("forecast 应为对象")
        return cls(
            beam_m=payload["beam_m"],
            draft_dm=payload["draft_dm"],
            eta=parse_time(payload["eta"], "forecast.eta"),
            work_minutes=payload["work_minutes"],
            latest_exit=parse_time(payload["latest_exit"], "forecast.latest_exit"),
            assist_required=tuple(payload["assist_required"]),
        )


@dataclass
class ActionPlan:
    """一个动作（靠泊或离泊）的候选与协助安排。"""

    action: str
    berth_window: tuple
    candidate: list
    maneuver: tuple
    assists: list
    requires_clear: str = None

    def group_names(self):
        return [item["group"] for item in self.assists]

    def to_json(self):
        return {
            "action": self.action,
            "berth_window": _span_json(self.berth_window),
            "candidate": [_span_json(span) for span in self.candidate],
            "maneuver": _span_json(self.maneuver),
            "assists": [
                {
                    "kind": item["kind"],
                    "group": item["group"],
                    "agency": item["agency"],
                    "ref": item.get("ref"),
                    "span": _span_json(item["span"]),
                }
                for item in self.assists
            ],
            "requires_clear": self.requires_clear,
        }

    @classmethod
    def from_json(cls, payload):
        return cls(
            action=payload["action"],
            berth_window=_span_load(payload["berth_window"], "berth_window"),
            candidate=[_span_load(span, "candidate") for span in payload["candidate"]],
            maneuver=_span_load(payload["maneuver"], "maneuver"),
            assists=[
                {
                    "kind": item["kind"],
                    "group": item["group"],
                    "agency": item["agency"],
                    "ref": item.get("ref"),
                    "span": _span_load(item["span"], "assist.span"),
                }
                for item in payload["assists"]
            ],
            requires_clear=payload.get("requires_clear"),
        )


@dataclass
class Plan:
    id: str
    call: str
    berth: str
    revision: int
    state: str
    created_seq: int
    approach: ActionPlan = None
    departure: ActionPlan = None
    issues: list = field(default_factory=list)
    note: str = None
    replaces: str = None
    replaced_by: str = None
    manual: bool = False
    complete: bool = False
    accepted: bool = False
    stale: bool = False

    def actions(self):
        return [item for item in (self.approach, self.departure) if item is not None]

    def groups(self):
        result = {}
        for action in self.actions():
            for item in action.assists:
                result.setdefault(item["group"], []).append((action.action, item["span"]))
        return result

    def to_json(self):
        return {
            "id": self.id,
            "call": self.call,
            "berth": self.berth,
            "revision": self.revision,
            "state": self.state,
            "created_seq": self.created_seq,
            "approach": self.approach.to_json() if self.approach else None,
            "departure": self.departure.to_json() if self.departure else None,
            "issues": list(self.issues),
            "note": self.note,
            "replaces": self.replaces,
            "replaced_by": self.replaced_by,
            "manual": self.manual,
            "complete": self.complete,
            "accepted": self.accepted,
            "stale": self.stale,
        }

    @classmethod
    def from_json(cls, payload):
        return cls(
            id=payload["id"],
            call=payload["call"],
            berth=payload["berth"],
            revision=payload["revision"],
            state=payload["state"],
            created_seq=payload["created_seq"],
            approach=ActionPlan.from_json(payload["approach"]) if payload.get("approach") else None,
            departure=ActionPlan.from_json(payload["departure"]) if payload.get("departure") else None,
            issues=list(payload.get("issues", [])),
            note=payload.get("note"),
            replaces=payload.get("replaces"),
            replaced_by=payload.get("replaced_by"),
            manual=payload.get("manual", False),
            complete=payload.get("complete", False),
            accepted=payload.get("accepted", False),
            stale=payload.get("stale", False),
        )


@dataclass
class Booking:
    """一个动作对一个真实值班组的承诺。

    与 ``state`` 分开保存 ``peer_state``：本地撤销成功不代表对端值班组已空闲，
    只有对端明确 released/completed 才能把组给别的船。
    """

    ref: str
    call: str
    group: str
    agency: str
    kind: str
    action: str
    span: tuple
    plan_id: str = None
    state: str = "booked"
    peer_state: str = None
    unresolved: str = None
    external: bool = False
    continuation_agreed: bool = False
    note: str = None
    peer_ref: str = None

    @property
    def external_ref(self):
        """对端登记用的引用；同一航次同一动作同一组只有一个。"""
        return self.peer_ref or self.ref

    @property
    def blocking(self):
        """是否仍占用值班组。

        ``released``/``completed`` 是对端的明确放行；其余本地状态一律按占用处理，
        包括已经登记撤销但尚未收到答复的情况。
        """
        if self.peer_state in PEER_FREE_STATES:
            return False
        if self.state in ("released", "completed"):
            return False
        return True

    @property
    def free(self):
        return not self.blocking

    @property
    def held(self):
        """动作已开始（已出航或已登轮），不能按预约直接替换。"""
        return self.peer_state in PEER_HELD_STATES and self.blocking

    def to_json(self):
        return {
            "ref": self.ref,
            "call": self.call,
            "group": self.group,
            "agency": self.agency,
            "kind": self.kind,
            "action": self.action,
            "span": _span_json(self.span),
            "plan_id": self.plan_id,
            "state": self.state,
            "peer_state": self.peer_state,
            "unresolved": self.unresolved,
            "external": self.external,
            "continuation_agreed": self.continuation_agreed,
            "note": self.note,
            "peer_ref": self.peer_ref,
            "blocking": self.blocking,
            "held": self.held,
        }

    @classmethod
    def from_json(cls, payload):
        return cls(
            ref=payload["ref"],
            call=payload["call"],
            group=payload["group"],
            agency=payload["agency"],
            kind=payload["kind"],
            action=payload["action"],
            span=_span_load(payload["span"], "booking.span"),
            plan_id=payload.get("plan_id"),
            state=payload.get("state", "booked"),
            peer_state=payload.get("peer_state"),
            unresolved=payload.get("unresolved"),
            external=payload.get("external", False),
            continuation_agreed=payload.get("continuation_agreed", False),
            note=payload.get("note"),
            peer_ref=payload.get("peer_ref"),
        )


@dataclass
class Call:
    call: str
    forecast: Forecast
    berth: str = None
    revision: int = None
    plans: dict = field(default_factory=dict)
    active: str = None
    notes: list = field(default_factory=list)

    def active_plan(self):
        return self.plans.get(self.active) if self.active else None

    def latest_candidate(self):
        rows = [p for p in self.plans.values() if p.state == "candidate"]
        return max(rows, key=lambda p: p.created_seq) if rows else None

    def to_json(self):
        return {
            "call": self.call,
            "forecast": self.forecast.to_json(),
            "berth": self.berth,
            "revision": self.revision,
            "plans": {key: plan.to_json() for key, plan in self.plans.items()},
            "active": self.active,
            "notes": list(self.notes),
        }

    @classmethod
    def from_json(cls, payload):
        return cls(
            call=payload["call"],
            forecast=Forecast.from_json(payload["forecast"]),
            berth=payload.get("berth"),
            revision=payload.get("revision"),
            plans={key: Plan.from_json(plan) for key, plan in payload.get("plans", {}).items()},
            active=payload.get("active"),
            notes=list(payload.get("notes", [])),
        )


@dataclass
class ClearConfirmation:
    """码头明确确认某泊位上的前船已腾让。不是实际解缆记录，但可解除软占用。"""

    berth: str
    call: str
    at: object
    note: str = None

    def to_json(self):
        return {"berth": self.berth, "call": self.call, "at": format_time(self.at), "note": self.note}

    @classmethod
    def from_json(cls, payload):
        return cls(
            berth=payload["berth"],
            call=payload["call"],
            at=parse_time(payload["at"], "clear_confirmation.at"),
            note=payload.get("note"),
        )


@dataclass
class Project:
    name: str
    sequence: int = 0
    calls: dict = field(default_factory=dict)
    bookings: dict = field(default_factory=dict)
    quay_events: list = field(default_factory=list)
    shift_updates: list = field(default_factory=list)
    clear_confirmations: list = field(default_factory=list)
    window_revisions: dict = field(default_factory=dict)  # berth -> revision -> [Window]
    log: list = field(default_factory=list)

    def next_seq(self):
        self.sequence += 1
        return self.sequence

    def record(self, op, payload):
        seq = self.next_seq()
        self.log.append({"seq": seq, "op": op, "payload": payload})
        return seq

    def clear_confirmed(self, berth, call):
        rows = [c for c in self.clear_confirmations if c.berth == berth and c.call == call]
        return max(rows, key=lambda item: item.at) if rows else None

    def bookings_of(self, call, plan_id=None):
        rows = [booking for booking in self.bookings.values() if booking.call == call]
        if plan_id is not None:
            rows = [booking for booking in rows if booking.plan_id == plan_id]
        return sorted(rows, key=lambda booking: (booking.span[0], booking.ref))

    def all_quay_events(self, feeds):
        return sorted(
            list(feeds.quay_events) + list(self.quay_events),
            key=lambda item: (item.at, item.event),
        )

    def all_shift_updates(self, feeds):
        return list(feeds.shift_updates) + list(self.shift_updates)

    def windows_for(self, feeds, berth, revision):
        if berth in self.window_revisions and revision in self.window_revisions[berth]:
            return list(self.window_revisions[berth][revision])
        return [w for w in feeds.windows if w.berth == berth and w.revision == revision]

    def revisions_for(self, feeds, berth):
        revisions = {w.revision for w in feeds.windows if w.berth == berth}
        revisions.update(self.window_revisions.get(berth, {}).keys())
        return sorted(revisions)

    def latest_revision(self, feeds, berth):
        revisions = self.revisions_for(feeds, berth)
        return revisions[-1] if revisions else None

    def berths(self, feeds):
        names = {w.berth for w in feeds.windows}
        names.update(self.window_revisions)
        return sorted(names)

    def to_json(self):
        return {
            "project": self.name,
            "sequence": self.sequence,
            "calls": {key: call.to_json() for key, call in self.calls.items()},
            "bookings": {key: booking.to_json() for key, booking in self.bookings.items()},
            "quay_events": [
                {
                    "event": event.event,
                    "call": event.call,
                    "berth": event.berth,
                    "kind": event.kind,
                    "at": format_time(event.at),
                }
                for event in self.quay_events
            ],
            "shift_updates": [
                {
                    "update": item.update,
                    "berth": item.berth,
                    "new_revision": item.new_revision,
                    "announced_at": format_time(item.announced_at),
                    "reported_clear": format_time(item.reported_clear),
                }
                for item in self.shift_updates
            ],
            "clear_confirmations": [item.to_json() for item in self.clear_confirmations],
            "window_revisions": {
                berth: {
                    str(revision): [
                        {
                            "berth": window.berth,
                            "revision": window.revision,
                            "from": format_time(window.start),
                            "until": format_time(window.until),
                            "draft_dm": window.draft_dm,
                            "beam_m": window.beam_m,
                        }
                        for window in windows
                    ]
                    for revision, windows in revisions.items()
                }
                for berth, revisions in self.window_revisions.items()
            },
            "log": list(self.log),
        }

    @classmethod
    def from_json(cls, payload):
        from .feeds import ShiftUpdate

        window_revisions = {}
        for berth, revisions in payload.get("window_revisions", {}).items():
            window_revisions[berth] = {
                int(revision): [
                    Window(
                        berth=item["berth"],
                        revision=item["revision"],
                        start=parse_time(item["from"], "window.from"),
                        until=parse_time(item["until"], "window.until"),
                        draft_dm=item["draft_dm"],
                        beam_m=item["beam_m"],
                    )
                    for item in windows
                ]
                for revision, windows in revisions.items()
            }
        return cls(
            name=payload["project"],
            sequence=payload["sequence"],
            calls={key: Call.from_json(value) for key, value in payload.get("calls", {}).items()},
            bookings={
                key: Booking.from_json(value) for key, value in payload.get("bookings", {}).items()
            },
            quay_events=[
                QuayEvent(
                    event=item["event"],
                    call=item["call"],
                    berth=item["berth"],
                    kind=item["kind"],
                    at=parse_time(item["at"], "quay_event.at"),
                )
                for item in payload.get("quay_events", [])
            ],
            shift_updates=[
                ShiftUpdate(
                    update=item["update"],
                    berth=item["berth"],
                    new_revision=item["new_revision"],
                    announced_at=parse_time(item["announced_at"], "shift_update.announced_at"),
                    reported_clear=parse_time(item["reported_clear"], "shift_update.reported_clear"),
                )
                for item in payload.get("shift_updates", [])
            ],
            clear_confirmations=[
                ClearConfirmation.from_json(item)
                for item in payload.get("clear_confirmations", [])
            ],
            window_revisions=window_revisions,
            log=list(payload.get("log", [])),
        )


def assist_slot_json(slot):
    return {
        "group": slot.group,
        "agency": slot.agency,
        "kind": slot.kind,
        "from": format_time(slot.start),
        "until": format_time(slot.until),
        "max_draft_dm": slot.max_draft_dm,
    }
