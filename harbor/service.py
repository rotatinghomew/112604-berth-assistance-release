"""靠离泊协作与作业窗口交接服务。

同一项目接口下登记：

* 船代：到达预报、接受/拒绝候选方案；
* 港调：计算候选、确认、撤回、改期、登记码头回执与咨询；
* 码头/协助机构：查询候选与确认窗口、登记实际事件。

设计要点：

1. 候选与确认分开。机构没有明确答复时只能是候选，查询里能看出谁还在决定。
2. 泊位位置与协助组是两套事实。``lines_clear`` 才释放泊位；协助组由对端
   released/completed 释放。``dispatched``/``on_board`` 的预约不能按“改写
   计划”当作空闲。
3. 改期只有在靠、离两个动作的资源都能兑现时才确认；失败时输出仍有效的旧承诺。
"""

from datetime import datetime, timezone

from . import schedule
from .facts import call_actuals, expected_clear, occupancies
from .feeds import FeedError, QuayEvent, ShiftUpdate, Window
from .model import (
    ActionPlan,
    Booking,
    Call,
    ClearConfirmation,
    Forecast,
    Plan,
    Project,
    assist_slot_json,
)
from .peer import PeerUnavailable
from .timeutil import describe, format_time, parse_span, parse_time

class NotFound(KeyError):
    pass


class Conflict(RuntimeError):
    pass


# 诊断方案的优先级：越能说明“为什么排不出来”越优先。
_ISSUE_RANK = {
    "no_berth": 0,
    "berth_window": 1,
    "assist_unavailable": 2,
    "no_approach": 3,
    "awaiting_clear": 4,
    "no_departure": 5,
    "work_overrun": 6,
    "call_completed": 7,
}


def _issue_rank(issues):
    return max((_ISSUE_RANK.get(issue.get("kind"), 0) for issue in issues), default=0)


def _empty_draft(berth, revision, issue):
    from .schedule import Draft

    draft = Draft(berth=berth, revision=revision)
    draft.issues.append(issue)
    return draft


class Outcome:
    """一次操作的可见结果。"""

    def __init__(self, ok, plan=None, pending=None, issues=None, kept=None, released=None, note=None):
        self.ok = ok
        self.plan = plan
        self.pending = pending or []
        self.issues = issues or []
        self.kept = kept or []
        self.released = released or []
        self.note = note

    def to_json(self):
        return {
            "ok": self.ok,
            "plan": self.plan,
            "pending": list(self.pending),
            "issues": list(self.issues),
            "kept": list(self.kept),
            "released": list(self.released),
            "note": self.note,
        }


class HarborService:
    def __init__(self, feeds, project=None, peer=None):
        self.feeds = feeds
        self.project = project or Project(name="harbor")
        self.peer = peer

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    def snapshot(self):
        return self.project.to_json()

    @classmethod
    def restore(cls, feeds, payload, peer=None):
        return cls(feeds, Project.from_json(payload), peer)

    # ------------------------------------------------------------------
    # 船代：预报
    # ------------------------------------------------------------------

    def register_forecast(self, call, payload):
        forecast = Forecast.from_json(payload)
        forecast.validate(call)
        vessel = forecast.to_vessel(call)
        record = self.project.calls.get(call)
        if record is None:
            record = Call(call=call, forecast=forecast)
            self.project.calls[call] = record
            fresh = True
        else:
            record.forecast = forecast
            fresh = False
        if record.berth is None:
            record.berth, record.revision = self._auto_berth(call, vessel)
        self.project.record("forecast", {"call": call, **forecast.to_json()})
        plan = self._build_plan(call, vessel, record.berth, record.revision)
        outcome = self._outcome_for(plan)
        if fresh:
            outcome.note = "已登记预报；如泊位或时间不合适，可协商换泊或改时"
        return outcome

    def update_forecast(self, call, payload):
        """船代继续更新预报；已确认方案会被重新评估为候选。"""
        record = self._call(call)
        was_confirmed = [
            plan.id for plan in record.plans.values() if plan.state == "confirmed"
        ]
        outcome = self.register_forecast(call, payload)
        if was_confirmed:
            for plan_id in was_confirmed:
                plan = record.plans[plan_id]
                plan.state = "candidate"
                plan.note = "因预报更新转为候选，需重新确认"
            record.active = None
            outcome.note = (
                "预报已更新；原确认方案转为候选，需重新确认"
            )
            self.project.record("forecast-invalidated", {"call": call, "plans": was_confirmed})
        # 已实际系泊的船，作业时长变化会改变可开始装车时间。
        return outcome

    def _auto_berth(self, call, vessel):
        best = None
        for berth in self.project.berths(self.feeds):
            revision = self.project.latest_revision(self.feeds, berth)
            draft = schedule.Planner(self.feeds, self.project).plan(
                call, vessel, berth, revision
            )
            if draft.complete:
                return berth, revision
            if best is None:
                if draft.approach is not None:
                    best = (berth, revision)
                elif any(
                    issue.get("kind") in ("awaiting_clear", "no_departure")
                    for issue in draft.issues
                ):
                    best = (berth, revision)
        return best or (None, None)

    def accept_plan(self, plan_id):
        """船代接受候选方案。

        接受只是项目内的确认，不代替协助机构的答复；仍需港调执行 confirm。
        """
        plan = self._plan(plan_id)
        if plan.state in ("withdrawn", "superseded"):
            return Outcome(
                ok=False,
                plan=self.view_plan(plan.id),
                issues=[{"kind": "plan_inactive", "detail": f"方案已处于 {plan.state}"}],
            )
        if plan.issues:
            return Outcome(
                ok=False,
                plan=self.view_plan(plan.id),
                issues=list(plan.issues),
                note="方案不完整，暂不能接受",
            )
        plan.accepted = True
        self.project.record("accept", {"plan": plan.id})
        return Outcome(ok=True, plan=self.view_plan(plan.id), note="船代已接受，等待港调向机构确认")

    def record_inquiry(self, call, question, institutions=None, plan_id=None):
        """登记港调向协助机构或码头的咨询，便于之后对账。"""
        self._call(call)
        entry = {
            "call": call,
            "plan_id": plan_id,
            "question": question,
            "institutions": list(institutions or []),
        }
        self.project.record("inquiry", entry)
        return {
            "registered": True,
            "inquiry": entry,
            "pending": [row for row in self.pending_decisions() if row["call"] == call],
        }

    # ------------------------------------------------------------------
    # 港调：候选与人工调整
    # ------------------------------------------------------------------

    def propose(self, call, berth=None, revision=None):
        record = self._call(call)
        vessel = record.forecast.to_vessel(call)
        berth = berth or record.berth
        if berth is None:
            raise NotFound("没有可用的泊位")
        revision = revision if revision is not None else self.project.latest_revision(
            self.feeds, berth
        )
        if revision is None:
            raise NotFound(f"泊位 {berth} 没有窗口")
        plan = self._build_plan(call, vessel, berth, revision)
        return self._outcome_for(plan)

    def propose_manual(self, call, payload):
        """人工指定靠/离时段，仍受窗口、占用、作业时长和协助区间约束。"""
        record = self._call(call)
        vessel = record.forecast.to_vessel(call)
        berth = payload.get("berth") or record.berth
        revision = payload.get("revision")
        if revision is None:
            revision = self.project.latest_revision(self.feeds, berth)
        draft = schedule.Planner(self.feeds, self.project).plan(
            call,
            vessel,
            berth,
            revision,
            approach_span=self._optional_span(payload, "approach"),
            departure_span=self._optional_span(payload, "departure"),
        )
        plan = self._store_draft(call, berth, revision, draft, manual=True)
        return self._outcome_for(plan)

    def _optional_span(self, payload, key):
        if not payload.get(key):
            return None
        return parse_span(payload[key], key)

    def _build_plan(self, call, vessel, berth, revision):
        if berth is None:
            return self._diagnostic_plan(call, vessel)
        draft = schedule.Planner(self.feeds, self.project).plan(
            call, vessel, berth, revision
        )
        return self._store_draft(call, berth, revision, draft, manual=False)

    def _diagnostic_plan(self, call, vessel):
        """没有合适泊位时，仍给出一次带原因的候选，说明缺什么。"""
        chosen = None
        for berth in self.project.berths(self.feeds):
            revision = self.project.latest_revision(self.feeds, berth)
            if revision is None:
                continue
            draft = schedule.Planner(self.feeds, self.project).plan(
                call, vessel, berth, revision
            )
            if any(issue.get("kind") == "call_completed" for issue in draft.issues):
                return self._store_draft(call, berth, revision, draft, manual=False)
            if chosen is None or _issue_rank(draft.issues) > _issue_rank(chosen[2]):
                chosen = (berth, revision, draft.issues, draft)
        if chosen is not None:
            berth, revision, _, draft = chosen
            return self._store_draft(call, berth, revision, draft, manual=False)
        # 连泊位都没有：给出空方案并说明需要码头先发布窗口。
        return self._store_draft(
            call,
            None,
            None,
            _empty_draft(
                None,
                None,
                {
                    "kind": "no_berth",
                    "detail": "还没有码头公布的泊位窗口，需先取得窗口再排期",
                },
            ),
            manual=False,
        )

    def _store_draft(self, call, berth, revision, draft, manual):
        record = self._call(call)
        plan_id = f"{call}-{self.project.sequence + 1:03d}"
        plan = Plan(
            id=plan_id,
            call=call,
            berth=berth,
            revision=revision,
            state="candidate",
            created_seq=self.project.next_seq(),
            approach=(
                self._action_plan(
                    draft.approach,
                    [c.maneuver for c in draft.approach_candidates] or None,
                )
                if draft.approach
                else None
            ),
            departure=(
                self._action_plan(
                    draft.departure,
                    [c.maneuver for c in draft.departure_candidates] or None,
                )
                if draft.departure
                else None
            ),
            issues=list(draft.issues),
            manual=manual,
            complete=bool(draft.complete and not draft.issues),
        )
        notes = []
        if draft.advance:
            notes.append("船舶需提前到锚地等待，泊位腾出前不得占位")
        if draft.already_moored:
            notes.append("船舶已实际系泊，只需安排离泊")
        if draft.requires_clear:
            notes.append(f"需码头确认 {draft.requires_clear} 已腾让")
        plan.note = "；".join(notes) or None
        record.plans[plan_id] = plan
        if berth is not None:
            record.berth = berth
            record.revision = revision
        return plan

    def _action_plan(self, candidate, alternatives=None):
        return ActionPlan(
            action=candidate.action,
            berth_window=candidate.berth_window,
            candidate=alternatives or [candidate.maneuver],
            maneuver=candidate.maneuver,
            assists=[
                {
                    "kind": item["kind"],
                    "group": item["group"],
                    "agency": item["agency"],
                    "ref": None,
                    "span": item["span"],
                }
                for item in candidate.assists
            ],
            requires_clear=candidate.requires_clear,
        )

    def _outcome_for(self, plan):
        outcome = Outcome(ok=not plan.issues, plan=self.view_plan(plan.id))
        outcome.issues = list(plan.issues)
        if plan.issues:
            outcome.note = "方案未完整，见 issues；可协商换泊或改时"
        return outcome

    # ------------------------------------------------------------------
    # 港调：确认
    # ------------------------------------------------------------------

    def confirm(self, plan_id):
        plan = self._plan(plan_id)
        record = self._call(plan.call)
        previous = record.active_plan()
        if previous is not None and previous.state == "confirmed":
            return Outcome(
                ok=False,
                plan=self.view_plan(plan.id),
                issues=[
                    {
                        "kind": "already_confirmed",
                        "detail": f"{plan.call} 已有确认方案 {previous.id}；改时请使用 reschedule",
                    }
                ],
                note="未发出新的确认",
            )

        vessel = record.forecast.to_vessel(plan.call)
        if not plan.complete:
            return Outcome(
                ok=False,
                plan=self.view_plan(plan.id),
                issues=list(plan.issues) or [
                    {"kind": "incomplete_plan", "detail": "方案缺少靠泊或离泊动作，不能确认"}
                ],
                note="方案不完整，未向机构发出确认",
            )
        issues = self._verify_plan(plan, vessel)
        if issues:
            plan.issues = issues
            return Outcome(
                ok=False,
                plan=self.view_plan(plan.id),
                issues=issues,
                note="方案已失效，未向机构发出确认",
            )

        pending = self._request_confirmation(plan, record)
        if pending:
            plan.state = "candidate"
            self.project.record("confirm-pending", {"plan": plan.id, "pending": pending})
            return Outcome(
                ok=False,
                plan=self.view_plan(plan.id),
                pending=pending,
                note="以上机构仍未明确答复；方案保留为候选，可继续使用",
            )

        self._activate(plan, record)
        return Outcome(ok=True, plan=self.view_plan(plan.id))

    def _request_confirmation(self, plan, record):
        pending = []
        planner = schedule.Planner(self.feeds, self.project)
        for action in plan.actions():
            for item in action.assists:
                # 同一值班组同一时刻只能承诺一个动作；不同窗口可以复用。
                busy = planner.group_busy(item["group"], exclude_plan=plan.id)
                if any(
                    span[0] < item["span"][1] and item["span"][0] < span[1]
                    for span in busy
                ):
                    pending.append(
                        {
                            "ref": None,
                            "group": item["group"],
                            "agency": item["agency"],
                            "action": action.action,
                            "peer_state": None,
                            "detail": (
                                f"{item['group']} 在同一时段已承诺 "
                                f"{self._group_owner(item['group'], item['span'], plan.id)}，"
                                "需等该动作结束或改用其它窗口"
                            ),
                        }
                    )
                    continue
                existing = self._existing_booking(
                    plan.call, plan.id, action.action, item["group"]
                )
                if existing is not None and existing.blocking:
                    item["ref"] = existing.ref
                    if existing.state == "booked" and existing.peer_state is None:
                        pending.append(self._pending_booking(existing, "尚未回话"))
                    continue
                ref = self._ref_for(plan, action.action, item["group"])
                item["ref"] = ref
                peer_ref = self._peer_ref(plan.call, action.action, item["group"])
                try:
                    reply = self.peer.fetch(peer_ref) if self.peer else None
                except PeerUnavailable as error:
                    booking = Booking(
                        ref=ref,
                        call=plan.call,
                        group=item["group"],
                        agency=item["agency"],
                        kind=item["kind"],
                        action=action.action,
                        span=item["span"],
                        plan_id=plan.id,
                        state="booked",
                        unresolved=error.reason,
                        peer_ref=peer_ref,
                    )
                    self.project.bookings[ref] = booking
                    pending.append(self._pending_booking(booking, error.reason))
                    continue
                booking = Booking(
                    ref=ref,
                    call=plan.call,
                    group=item["group"],
                    agency=item["agency"],
                    kind=item["kind"],
                    action=action.action,
                    span=item["span"],
                    plan_id=plan.id,
                    state="booked",
                    peer_state=reply.state if reply else None,
                    peer_ref=peer_ref,
                )
                self.project.bookings[ref] = booking
                if reply is None:
                    pending.append(self._pending_booking(booking, "尚未回话"))
                elif reply.state == "confirmed":
                    booking.state = "booked"
                elif reply.state in ("dispatched", "on_board"):
                    # 值班组已出航或已登轮。即使引用是本航次，也不能把“已在执行”
                    # 当成对新窗口的承诺；要么等对方结束，要么另行同意继续服务。
                    booking.state = "active"
                    pending.append(
                        self._pending_booking(
                            booking,
                            f"回话 {reply.state}，值班组已在执行；需等对方确认结束"
                            "或同意继续服务新窗口",
                        )
                    )
                else:
                    pending.append(
                        self._pending_booking(
                            booking, f"回话 {reply.state}，该值班组不能承接本窗口"
                        )
                    )
        return pending

    def _pending_booking(self, booking, detail):
        return {
            "ref": booking.ref,
            "peer_ref": booking.external_ref,
            "group": booking.group,
            "agency": booking.agency,
            "action": booking.action,
            "peer_state": booking.peer_state,
            "detail": f"{booking.group}：{detail}",
        }

    def _ref_for(self, plan, action, group):
        """内部键：每个方案、动作、组各一条记录。"""
        return f"{plan.id}-{action}-{group}"

    def _peer_ref(self, call, action, group):
        """对端引用沿用录制格式：``<组>-<航次>`` 与 ``<组>-<航次>-depart``。

        同一航次同一动作同一组在对端只有一个引用；改期换窗口时沿用该引用，
        先撤销旧承诺，再为新窗口重新取得答复。
        """
        suffix = "" if action == "approach" else "-depart"
        return f"{group}-{call}{suffix}"

    def _existing_booking(self, call, plan_id, action, group):
        for booking in self.project.bookings_of(call, plan_id):
            if booking.action == action and booking.group == group:
                return booking
        return None

    def _group_owner(self, group, span, plan_id):
        for booking in self.project.bookings.values():
            if booking.group != group or booking.plan_id == plan_id:
                continue
            if not booking.blocking:
                continue
            if booking.span[0] < span[1] and span[0] < booking.span[1]:
                return f"{booking.call} 的 {booking.action}"
        return None

    def _activate(self, plan, record):
        plan.state = "confirmed"
        plan.issues = []
        previous = record.active_plan()
        if previous is not None and previous.id != plan.id:
            previous.state = "superseded"
            previous.replaced_by = plan.id
        record.active = plan.id
        self.project.record("confirm", {"plan": plan.id})

    # ------------------------------------------------------------------
    # 港调：撤回
    # ------------------------------------------------------------------

    def cancel(self, plan_id, reason=None, continue_booking=False):
        """撤回方案或单个承诺。

        ``continue_booking=True`` 只用于已出航/已登轮的预约：记录对方同意
        继续服务新窗口，但不会把它当成空闲。
        """
        plan = self._plan(plan_id)
        record = self._call(plan.call)
        pending, kept, released = [], [], []
        for booking in self.project.bookings_of(plan.call, plan.id):
            if booking.free:
                released.append(booking.ref)
                continue
            if booking.held:
                if continue_booking:
                    booking.continuation_agreed = True
                    booking.note = "原动作已开始，对方同意继续服务新窗口"
                    kept.append(self._booking_view(booking))
                else:
                    pending.append(
                        {
                            "ref": booking.ref,
                            "group": booking.group,
                            "action": booking.action,
                            "peer_state": booking.peer_state,
                            "detail": (
                                f"{booking.group} 已出航或已登轮，需等对方确认结束"
                                "或同意继续服务新窗口"
                            ),
                        }
                    )
                continue
            outcome = self._cancel_booking(booking, continue_booking=continue_booking)
            if outcome == "released":
                released.append(booking.ref)
            elif outcome == "kept":
                kept.append(self._booking_view(booking))
            else:
                pending.append(outcome)

        if pending:
            plan.state = "candidate"
            if record.active == plan.id:
                record.active = None
            self.project.record("cancel-pending", {"plan": plan.id, "pending": pending})
            return Outcome(
                ok=False,
                plan=self.view_plan(plan.id),
                pending=pending,
                kept=kept,
                note="撤回未全部完成；所列承诺仍有效",
            )

        plan.state = "withdrawn"
        if reason:
            plan.note = reason
        if record.active == plan.id:
            record.active = None
        self.project.record("cancel", {"plan": plan.id, "released": released})
        return Outcome(ok=True, plan=self.view_plan(plan.id), kept=kept)

    def _cancel_booking(self, booking, continue_booking=False):
        try:
            reply = self.peer.cancel(booking.external_ref) if self.peer else None
        except PeerUnavailable as error:
            booking.state = "cancelling"
            booking.unresolved = error.reason
            return self._pending_booking(booking, error.reason)
        booking.peer_state = reply.state if reply else None
        if reply is None:
            booking.state = "cancelling"
            return self._pending_booking(booking, "尚未确认撤销")
        if reply.state in ("released", "completed"):
            booking.state = "released"
            booking.note = "原机构确认释放"
            booking.unresolved = None
            return "released"
        if continue_booking and reply.state in ("dispatched", "on_board"):
            # 原机构回话说明该值班组已经在为原动作执行；船代同意继续等待，
            # 由对方结束或另行同意服务新窗口，届时仍占用值班组，不当作空闲。
            booking.state = "active"
            booking.continuation_agreed = True
            booking.note = "原动作已开始，对方将继续服务而不按撤销处理"
            return "kept"
        booking.state = "refused"
        booking.note = f"原机构回话 {reply.state}，仍由原动作占用"
        return self._pending_booking(booking, f"回话 {reply.state}，不能按撤销处理")

    # ------------------------------------------------------------------
    # 港调：改期
    # ------------------------------------------------------------------

    def reschedule(self, plan_id, payload=None):
        """改期。

        先为新靠/离时间重新计算并取得协助；两个动作都兑现才切换。任何机构
        未明确答复或旧承诺未释放时，新方案只保留候选，并列出仍有效的旧承诺。
        """
        payload = payload or {}
        old = self._plan(plan_id)
        record = self._call(old.call)
        vessel = record.forecast.to_vessel(old.call)
        berth = payload.get("berth") or old.berth
        revision = payload.get("revision")
        if revision is None:
            revision = self.project.latest_revision(self.feeds, berth)
        draft = schedule.Planner(self.feeds, self.project).plan(
            old.call,
            vessel,
            berth,
            revision,
            exclude_plan=old.id,
            approach_span=self._optional_span(payload, "approach"),
            departure_span=self._optional_span(payload, "departure"),
        )
        new_plan = self._store_draft(old.call, berth, revision, draft, manual=bool(payload))
        new_plan.replaces = old.id

        if not draft.complete or draft.issues:
            return Outcome(
                ok=False,
                plan=self.view_plan(new_plan.id),
                issues=list(draft.issues),
                kept=self._kept_bookings(old.call, old.id),
                note="改期未能兑现全部资源；旧承诺仍按下表有效",
            )

        released, pending = self._release_old(old, new_plan)
        if pending:
            new_plan.state = "candidate"
            self.project.record(
                "reschedule-pending",
                {"old": old.id, "new": new_plan.id, "pending": pending},
            )
            return Outcome(
                ok=False,
                plan=self.view_plan(new_plan.id),
                pending=pending,
                kept=self._kept_bookings(old.call, old.id),
                note="旧承诺尚由原机构占用，新方案仅作候选",
            )

        pending = self._request_confirmation(new_plan, record)
        if pending:
            new_plan.state = "candidate"
            # 旧承诺中已释放的部分不能再执行；仍被机构占用的部分保留下来，
            # 由 kept 列出，便于港调决定继续协商还是恢复原窗口。
            if released:
                old.state = "candidate"
                old.stale = True
                old.note = "改期时部分旧承诺已释放，原方案需重新确认"
                if record.active == old.id:
                    record.active = None
            self.project.record(
                "reschedule-pending",
                {"old": old.id, "new": new_plan.id, "pending": pending, "released": released},
            )
            return Outcome(
                ok=False,
                plan=self.view_plan(new_plan.id),
                pending=pending,
                kept=self._kept_bookings(old.call, old.id),
                released=released,
                note=(
                    "新方案尚未全部兑现。旧承诺已按下表释放；kept 中的承诺仍有效，"
                    "不能确认改期"
                ),
            )

        old.state = "superseded"
        old.replaced_by = new_plan.id
        record.active = new_plan.id
        new_plan.state = "confirmed"
        self.project.record(
            "reschedule", {"old": old.id, "new": new_plan.id, "released": released}
        )
        return Outcome(ok=True, plan=self.view_plan(new_plan.id))

    def _release_old(self, old, new_plan):
        released, pending = [], []
        for booking in self.project.bookings_of(old.call, old.id):
            if booking.free:
                continue
            reuse = self._reuse_booking(booking, new_plan)
            if reuse is not None:
                booking.plan_id = new_plan.id
                continue
            if booking.held:
                pending.append(
                    {
                        "ref": booking.ref,
                        "group": booking.group,
                        "action": booking.action,
                        "peer_state": booking.peer_state,
                        "detail": f"{booking.group} 已出航或已登轮，改期须对方同意",
                    }
                )
                continue
            outcome = self._cancel_booking(booking)
            if outcome == "released":
                released.append(booking.ref)
            else:
                pending.append(outcome)
        return released, pending

    def _reuse_booking(self, booking, new_plan):
        for action in new_plan.actions():
            if action.action != booking.action:
                continue
            for item in action.assists:
                if item["group"] == booking.group and item["span"] == booking.span:
                    return item
        return None

    # ------------------------------------------------------------------
    # 码头：实际回执与窗口修订
    # ------------------------------------------------------------------

    def record_quay_event(self, payload):
        kind = payload.get("kind")
        if kind not in ("all_fast", "lines_clear"):
            raise FeedError("quay_events: kind 应为 all_fast 或 lines_clear")
        event = QuayEvent(
            event=payload["event"],
            call=payload["call"],
            berth=payload["berth"],
            kind=kind,
            at=parse_time(payload["at"], "quay_events.at"),
        )
        if self._find_event(event.event):
            raise Conflict(f"回执 {event.event} 已登记")
        self.project.quay_events.append(event)
        self.project.record(
            "quay-event",
            {
                "event": event.event,
                "call": event.call,
                "berth": event.berth,
                "kind": event.kind,
                "at": format_time(event.at),
            },
        )
        self._refresh_after_actual(event)
        return self.call_report(event.call)

    def _refresh_after_actual(self, event):
        """实际事件改变事实后，把受影响方案标为需要复核，不自动改承诺。"""
        affected = []
        for call in self.project.calls.values():
            for plan in call.plans.values():
                if plan.state != "confirmed":
                    continue
                if plan.call == event.call or plan.berth == event.berth:
                    plan.stale = True
                    plan.note = "实际回执已更新，需重新核对"
                    affected.append(plan.id)
        if affected:
            self.project.record("stale-plans", {"plans": affected, "event": event.event})

    def record_shift_update(self, payload):
        update = ShiftUpdate(
            update=payload["update"],
            berth=payload["berth"],
            new_revision=payload["new_revision"],
            announced_at=parse_time(payload["announced_at"], "shift_updates.announced_at"),
            reported_clear=parse_time(payload["reported_clear"], "shift_updates.reported_clear"),
        )
        self.project.shift_updates.append(update)
        self.project.record(
            "shift-update",
            {
                "update": update.update,
                "berth": update.berth,
                "new_revision": update.new_revision,
                "reported_clear": format_time(update.reported_clear),
            },
        )
        return self.berth_report(update.berth)

    def record_clear_confirmation(self, berth, call, at=None, note=None):
        """码头确认前船让出泊位。解除软占用，但不生成 ``lines_clear``。"""
        moment = parse_time(at, "at") if at else datetime.now(timezone.utc)
        self.project.clear_confirmations.append(
            ClearConfirmation(berth=berth, call=call, at=moment, note=note)
        )
        self.project.record(
            "clear", {"berth": berth, "call": call, "at": format_time(moment), "note": note}
        )
        return self.berth_report(berth)

    def set_window_revision(self, berth, revision, payload):
        """码头发布压缩或新增的泊位窗口修订。"""
        windows = []
        for item in payload.get("windows", []):
            span = parse_span(item, "window")
            windows.append(
                Window(
                    berth=berth,
                    revision=revision,
                    start=span[0],
                    until=span[1],
                    draft_dm=item.get("draft_dm", 100),
                    beam_m=item.get("beam_m", 40),
                )
            )
        self.project.window_revisions.setdefault(berth, {})[revision] = windows
        self.project.record(
            "window-revision",
            {
                "berth": berth,
                "revision": revision,
                "windows": [
                    {"from": format_time(w.start), "until": format_time(w.until)} for w in windows
                ],
            },
        )
        self._stale_plans_for_berth(berth, f"泊位窗口已发布修订 {revision}")
        return self.berth_report(berth)

    def _stale_plans_for_berth(self, berth, reason):
        affected = []
        for call in self.project.calls.values():
            for plan in call.plans.values():
                if plan.state == "confirmed" and plan.berth == berth:
                    plan.stale = True
                    plan.note = reason
                    affected.append(plan.id)
        if affected:
            self.project.record("stale-plans", {"plans": affected, "reason": reason})

    # ------------------------------------------------------------------
    # 协助机构：回话处理
    # ------------------------------------------------------------------

    def apply_peer_reply(self, ref, state):
        booking = self.project.bookings.get(ref)
        if booking is None:
            raise NotFound(ref)
        booking.peer_state = state
        if state in ("released", "completed"):
            booking.state = state
            booking.unresolved = None
        elif state in ("dispatched", "on_board"):
            booking.state = "active"
            booking.unresolved = None
        self.project.record("peer", {"ref": ref, "state": state})
        return self.call_report(booking.call)

    def poll(self, ref):
        """向原机构重问一次；无应答时保留原判断。"""
        booking = self.project.bookings.get(ref)
        if booking is None:
            raise NotFound(ref)
        try:
            reply = self.peer.fetch(booking.external_ref)
        except PeerUnavailable as error:
            return {
                "ref": ref,
                "peer_ref": booking.external_ref,
                "pending": True,
                "detail": error.reason,
            }
        self.apply_peer_reply(ref, reply.state)
        return {
            "ref": ref,
            "pending": False,
            "peer_state": reply.state,
            "state": booking.state,
        }

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def pending_decisions(self):
        """仍未得到明确回话、或已有回话但值班组仍被原动作占着的承诺。

        后者（``dispatched``/``on_board``）不算“凭空答应”，要等对方结束或
        另行同意继续服务，所以仍会出现在待办里。
        """
        rows = []
        for booking in self.project.bookings.values():
            if not booking.blocking:
                continue
            unclear = (
                booking.state in ("cancelling", "refused")
                or booking.peer_state is None
                or booking.state == "active"
            )
            if not unclear:
                continue
            rows.append(
                {
                    "ref": booking.ref,
                    "group": booking.group,
                    "agency": booking.agency,
                    "call": booking.call,
                    "action": booking.action,
                    "peer_state": booking.peer_state,
                    "state": booking.state,
                    "detail": booking.unresolved
                    or (
                        "值班组仍在执行原动作，需等对方结束或同意继续服务"
                        if booking.state == "active"
                        else "等待原机构明确回话"
                    ),
                }
            )
        return rows

    def call_report(self, call):
        record = self._call(call)
        actual = call_actuals(self.project, self.feeds).get(call)
        plans = [self.view_plan(plan_id) for plan_id in record.plans]
        active = record.active_plan()
        return {
            "call": call,
            "forecast": record.forecast.to_json(),
            "berth": record.berth,
            "revision": record.revision,
            "actual": self._actual_view(actual),
            "candidate": [p for p in plans if p["state"] == "candidate"],
            "confirmed": [p for p in plans if p["state"] == "confirmed"],
            "manual": [
                p for p in plans if p["manual"] and p["state"] in ("candidate", "confirmed")
            ],
            "superseded": [p for p in plans if p["state"] == "superseded"],
            "withdrawn": [p for p in plans if p["state"] == "withdrawn"],
            "active": active.id if active else None,
            "berthing": self._berthing_view(call, record, actual),
            "loading_point": self._loading_point(call, record, actual),
            "pending": [row for row in self.pending_decisions() if row["call"] == call],
            "bookings": [
                self._booking_view(booking) for booking in self.project.bookings_of(call)
            ],
            "notes": list(record.notes),
        }

    def berth_report(self, berth):
        actuals = call_actuals(self.project, self.feeds)
        occupants = occupancies(self.project, self.feeds).get(berth, [])
        actual_rows = [
            {
                "call": occupancy.call,
                "all_fast": format_time(occupancy.start),
                "lines_clear": format_time(occupancy.end),
                "open": occupancy.open,
                "loading_point": (
                    self._loading_point(
                        occupancy.call,
                        self.project.calls.get(occupancy.call),
                        actuals.get(occupancy.call),
                    )
                    if occupancy.open
                    else None
                ),
            }
            for occupancy in occupants
        ]
        revisions = self.project.revisions_for(self.feeds, berth)
        latest = revisions[-1] if revisions else None
        expected = expected_clear(self.project, self.feeds, berth)
        open_occupant = next((row for row in occupants if row.open), None)
        return {
            "berth": berth,
            "latest_revision": latest,
            "free": open_occupant is None,
            "occupied_by": open_occupant.call if open_occupant else None,
            "revisions": {
                str(revision): [
                    {
                        "from": format_time(window.start),
                        "until": format_time(window.until),
                        "draft_dm": window.draft_dm,
                        "beam_m": window.beam_m,
                    }
                    for window in self.project.windows_for(self.feeds, berth, revision)
                ]
                for revision in revisions
            },
            "actual": actual_rows,
            "expected_clear": (
                {
                    "update": expected.update,
                    "announced_at": format_time(expected.announced_at),
                    "reported_clear": format_time(expected.reported_clear),
                    "is_actual": False,
                    "note": "预计腾出不替代实际解缆记录",
                }
                if expected
                else None
            ),
            "plans": [
                plan_view
                for call in self.project.calls.values()
                for plan_view in (
                    self.view_plan(plan_id) for plan_id in call.plans
                    if call.plans[plan_id].berth == berth
                )
                if plan_view["state"] in ("candidate", "confirmed")
            ],
        }

    def group_report(self, group):
        slots = [slot for slot in self.feeds.assists if slot.group == group]
        busy = [
            self._booking_view(booking)
            for booking in self.project.bookings.values()
            if booking.group == group and booking.blocking
        ]
        return {
            "group": group,
            "availability": [assist_slot_json(slot) for slot in slots],
            "commitments": sorted(busy, key=lambda row: row["span"]["from"]),
            "free": not busy,
            "pending": [row for row in self.pending_decisions() if row["group"] == group],
        }

    def query(self, call=None, berth=None, group=None):
        return {
            "call": self.call_report(call) if call else None,
            "berth": self.berth_report(berth) if berth else None,
            "group": self.group_report(group) if group else None,
        }

    def view_plan(self, plan_id):
        plan = self._plan(plan_id)
        payload = plan.to_json()
        bookings = self.project.bookings_of(plan.call, plan.id)
        payload["bookings"] = [self._booking_view(booking) for booking in bookings]
        payload["pending"] = [
            row
            for row in self.pending_decisions()
            if row["ref"] in {booking.ref for booking in bookings}
        ]
        payload["status"] = self._plan_status(plan, bookings)
        return payload

    def _plan_status(self, plan, bookings):
        if plan.state == "confirmed":
            return "confirmed_stale" if plan.stale else "confirmed"
        if plan.state in ("withdrawn", "superseded"):
            return plan.state
        if any(booking.blocking for booking in bookings):
            return "awaiting_institutions"
        return "candidate"

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _verify_plan(self, plan, vessel):
        """确认前用最新资料复核方案自己的靠/离时段是否仍然可执行。

        不要求它等于重新计算出的最优时段：人工改时或协商换泊后的时段只要
        仍在窗口、占用、作业时长和协助区间之内，就应保持有效。资料变化导致
        原时段不再可执行时才失效。
        """
        planner = schedule.Planner(self.feeds, self.project)
        approach_span = plan.approach.maneuver if plan.approach else None
        departure_span = plan.departure.maneuver if plan.departure else None
        draft = planner.plan(
            plan.call,
            vessel,
            plan.berth,
            plan.revision,
            exclude_plan=plan.id,
            approach_span=approach_span,
            departure_span=departure_span,
        )
        if plan.approach is not None and draft.approach is None:
            return [
                {
                    "kind": "approach_no_longer_feasible",
                    "detail": "靠泊时间已不在可用窗口、泊位占用或协助区间内",
                }
            ] + list(draft.issues)
        if draft.issues:
            return list(draft.issues)
        return []

    def _kept_bookings(self, call, plan_id):
        return [
            self._booking_view(booking)
            for booking in self.project.bookings_of(call, plan_id)
            if booking.blocking
        ]

    def _berthing_view(self, call, record, actual):
        """码头侧要看的三件事：实际靠妥、可开始装车、是否已无合适离泊窗口。"""
        plan = record.active_plan() if record is not None else None
        if actual is None or actual.all_fast is None:
            return {
                "status": "not_arrived",
                "all_fast": None,
                "loading_from": None,
                "departure": None,
                "loss_of_departure": None,
            }
        loading_from = schedule.shift(actual.all_fast, record.forecast.work_minutes)
        departure = plan.departure.maneuver if plan is not None and plan.departure else None
        loss = None
        if actual.lines_clear is None:
            if departure is None:
                loss = {
                    "kind": "no_departure_window",
                    "detail": "已实际系泊，但尚无合适的离泊窗口",
                }
            elif departure[0] < loading_from:
                loss = {
                    "kind": "loading_overruns_departure",
                    "detail": "完整作业时长结束时已晚于离泊窗口开始",
                }
        return {
            "status": actual.status,
            "all_fast": format_time(actual.all_fast),
            "loading_from": format_time(loading_from),
            "departure": describe(departure),
            "loss_of_departure": loss,
        }

    def _loading_point(self, call, record, actual):
        """下一次可以交给装车组的时间。"""
        if record is None:
            return {"call": call, "status": "unknown", "note": "没有登记预报"}
        if actual is not None and actual.moored:
            end = schedule.shift(actual.all_fast, record.forecast.work_minutes)
            departure = self._departure_span(call)
            return {
                "call": call,
                "status": "loading",
                "from": format_time(end),
                "until": departure["from"] if departure else None,
                "berth": actual.berth,
                "note": "已实际系泊；开始装车时间按实际系缆加作业时长",
                "departure_window": departure,
            }
        plan = record.active_plan()
        if plan is not None and plan.approach is not None:
            end = schedule.shift(plan.approach.maneuver[1], record.forecast.work_minutes)
            return {
                "call": call,
                "status": "planned",
                "from": format_time(end),
                "until": describe(plan.departure.maneuver) if plan.departure else None,
                "berth": plan.berth,
                "note": "按确认方案的靠妥时间推算，仍以 all_fast 实际回执为准",
            }
        return {"call": call, "status": "unknown", "note": "尚无确认方案或实际系缆回执"}

    def _departure_span(self, call):
        record = self.project.calls.get(call)
        if record is None:
            return None
        plan = record.active_plan()
        if plan is not None and plan.departure is not None:
            return describe(plan.departure.maneuver)
        return None

    def _actual_view(self, actual):
        if actual is None:
            return {"status": "not_arrived", "berth": None}
        return {
            "status": actual.status,
            "berth": actual.berth,
            "all_fast": format_time(actual.all_fast),
            "lines_clear": format_time(actual.lines_clear),
        }

    def _booking_view(self, booking):
        payload = booking.to_json()
        payload["still_holds"] = booking.blocking
        if booking.held:
            payload["next_step"] = "等待原机构确认结束或同意继续服务新窗口"
        elif booking.state == "cancelling":
            payload["next_step"] = "等待原机构确认撤销"
        return payload

    def _call(self, call):
        record = self.project.calls.get(call)
        if record is None:
            raise NotFound(call)
        return record

    def _plan(self, plan_id):
        for call in self.project.calls.values():
            if plan_id in call.plans:
                return call.plans[plan_id]
        raise NotFound(plan_id)

    def _find_event(self, event_id):
        for event in self.project.quay_events:
            if event.event == event_id:
                return event
        for event in self.feeds.quay_events:
            if event.event == event_id:
                return event
        return None
