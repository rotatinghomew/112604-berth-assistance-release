"""靠离泊协调服务的行为测试。

这些测试直接构造交换资料和项目状态，重点覆盖业务判断而不是格式。
"""

import unittest

from harbor import schedule
from harbor.feeds import Feeds, AssistSlot, QuayEvent, ShiftUpdate, Vessel, Window
from harbor.model import Project
from harbor.peer import AssistReply, PeerUnavailable
from harbor.service import HarborService
from harbor.timeutil import parse_time


def t(text):
    return parse_time(text)


def vessel(call="V1", beam=24, draft=74, eta="2026-11-27T01:30:00Z", work=55,
           latest="2026-11-27T05:20:00Z", kinds=("pilot", "tug")):
    return Vessel(
        call=call,
        beam_m=beam,
        draft_dm=draft,
        eta=t(eta),
        work_minutes=work,
        latest_exit=t(latest),
        assist_required=tuple(kinds),
    )


def forecast_payload(v=None):
    v = v or vessel()
    return {
        "beam_m": v.beam_m,
        "draft_dm": v.draft_dm,
        "eta": v.eta.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "work_minutes": v.work_minutes,
        "latest_exit": v.latest_exit.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "assist_required": list(v.assist_required),
    }


def window(berth="B1", revision=1, start="2026-11-27T01:00:00Z",
           until="2026-11-27T05:00:00Z", draft=100, beam=40):
    return Window(berth=berth, revision=revision, start=t(start), until=t(until),
                  draft_dm=draft, beam_m=beam)


def assist(group="P1", agency="pilot-A", kind="pilot",
           start="2026-11-27T01:00:00Z", until="2026-11-27T05:00:00Z", draft=100):
    return AssistSlot(group=group, agency=agency, kind=kind, start=t(start),
                      until=t(until), max_draft_dm=draft)


def feeds_of(windows=None, assists=None, events=None, vessels=None, shifts=None):
    return Feeds(
        vessels={v.call: v for v in (vessels or [])},
        windows=list(windows or []),
        assists=list(assists or []),
        quay_events=list(events or []),
        shift_updates=list(shifts or []),
    )


def quay_event(event, call, berth, kind, at):
    return QuayEvent(event=event, call=call, berth=berth, kind=kind, at=t(at))


class ConfirmPeer:
    """按引用回话的对端；ref 形如 ``P1-V1`` 或 ``T2-V1-depart``。"""

    def __init__(self, states=None, cancel_states=None):
        self.states = states or {}
        self.cancel_states = cancel_states or {}
        self.fetched = []
        self.cancelled = []

    def _group(self, ref):
        head = ref.split("-")[0]
        action = "departure" if ref.endswith("-depart") else "approach"
        call = ref.split("-")[1]
        return call, action, head

    def fetch(self, ref):
        self.fetched.append(ref)
        call, action, group = self._group(ref)
        if ref in self.states:
            state = self.states[ref]
        else:
            state = "confirmed"
        if isinstance(state, PeerUnavailable):
            raise state
        return AssistReply(ref=ref, call=call, group=group, action=action, state=state)

    def cancel(self, ref):
        self.cancelled.append(ref)
        call, action, group = self._group(ref)
        state = self.cancel_states.get(ref, "released")
        if isinstance(state, PeerUnavailable):
            raise state
        return AssistReply(ref=ref, call=call, group=group, action=action, state=state)


class PlannerTest(unittest.TestCase):
    def setUp(self):
        self.feeds = feeds_of(
            windows=[window()],
            assists=[assist(), assist("T1", "tug-A", "tug")],
        )

    def test_complete_schedule_respects_eta_work_and_latest_exit(self):
        planner = schedule.Planner(self.feeds, Project("t"))
        draft = planner.plan("V1", vessel(), "B1", 1)
        self.assertTrue(draft.complete, draft.issues)
        self.assertEqual(draft.approach.maneuver[0], t("2026-11-27T01:30:00Z"))
        work_end = schedule.shift(draft.approach.maneuver[1], 55)
        self.assertGreaterEqual(draft.departure.maneuver[1], work_end)
        self.assertLessEqual(draft.departure.maneuver[1], t("2026-11-27T05:20:00Z"))

    def test_window_requires_draft_and_beam(self):
        feeds = feeds_of(windows=[window(draft=70)], assists=[assist(), assist("T1", "tug-A", "tug")])
        draft = schedule.Planner(feeds, Project("t")).plan("V1", vessel(), "B1", 1)
        self.assertFalse(draft.complete)
        self.assertEqual(draft.issues[0]["kind"], "berth_window")

    def test_berth_window_is_half_open(self):
        # 窗口在 05:00 结束，离泊动作必须在 05:00 前完整结束。
        feeds = feeds_of(
            windows=[window(until="2026-11-27T03:00:00Z")],
            assists=[assist(until="2026-11-27T03:00:00Z"), assist("T1", "tug-A", "tug", until="2026-11-27T03:00:00Z")],
        )
        draft = schedule.Planner(feeds, Project("t")).plan("V1", vessel(), "B1", 1)
        if draft.departure is not None:
            self.assertLessEqual(draft.departure.maneuver[1], t("2026-11-27T03:00:00Z"))

    def test_draft_limited_assist_group_not_used(self):
        feeds = feeds_of(
            windows=[window()],
            assists=[assist(draft=70), assist("T1", "tug-A", "tug", draft=100)],
        )
        draft = schedule.Planner(feeds, Project("t")).plan("V1", vessel(draft=90), "B1", 1)
        self.assertFalse(draft.complete)
        self.assertEqual(draft.issues[0]["kind"], "assist_unavailable")

    def test_actual_occupancy_blocks_berth_until_lines_clear(self):
        feeds = feeds_of(
            windows=[window()],
            assists=[assist(), assist("T1", "tug-A", "tug")],
            events=[quay_event("E1", "PREV", "B1", "all_fast", "2026-11-27T00:30:00Z")],
        )
        draft = schedule.Planner(feeds, Project("t")).plan("V1", vessel(), "B1", 1)
        self.assertFalse(draft.complete)
        self.assertEqual(draft.issues[0]["kind"], "awaiting_clear")
        self.assertEqual(draft.issues[0]["call"], "PREV")

    def test_open_occupancy_can_be_cleared_by_wharf_confirmation(self):
        feeds = feeds_of(
            windows=[window()],
            assists=[assist(), assist("T1", "tug-A", "tug")],
            events=[quay_event("E1", "PREV", "B1", "all_fast", "2026-11-27T00:30:00Z")],
        )
        service = HarborService(feeds, peer=ConfirmPeer())
        service.record_clear_confirmation("B1", "PREV", at="2026-11-27T01:00:00Z")
        draft = schedule.Planner(feeds, service.project).plan("V1", vessel(), "B1", 1)
        self.assertTrue(draft.complete, draft.issues)

    def test_shift_update_is_not_actual_release(self):
        feeds = feeds_of(
            windows=[window()],
            assists=[assist(), assist("T1", "tug-A", "tug")],
            events=[quay_event("E1", "PREV", "B1", "all_fast", "2026-11-27T00:30:00Z")],
            shifts=[ShiftUpdate(update="U1", berth="B1", new_revision=2,
                                announced_at=t("2026-11-27T00:50:00Z"),
                                reported_clear=t("2026-11-27T01:00:00Z"))],
        )
        planner = schedule.Planner(feeds, Project("t"))
        draft = planner.plan("V1", vessel(), "B1", 1)
        self.assertFalse(draft.complete)
        self.assertEqual(draft.issues[0]["kind"], "awaiting_clear")

    def test_late_eta_produces_advance_candidate(self):
        # 船 01:10 才到；只有 01:00 提前进港，才赶得上完整作业与离泊窗口。
        feeds = feeds_of(
            windows=[window(start="2026-11-27T01:00:00Z", until="2026-11-27T03:00:00Z")],
            assists=[assist(until="2026-11-27T03:00:00Z"),
                     assist("T1", "tug-A", "tug", until="2026-11-27T03:00:00Z")],
        )
        draft = schedule.Planner(feeds, Project("t")).plan(
            "V1", vessel(eta="2026-11-27T01:10:00Z"), "B1", 1
        )
        self.assertTrue(draft.complete, draft.issues)
        self.assertTrue(draft.advance)
        self.assertLess(draft.approach.maneuver[0], t("2026-11-27T01:10:00Z"))

    def test_on_time_candidate_preferred_when_both_exist(self):
        # 同样窗口里，船按 ETA 到港也能完成时，不应要求提前。
        feeds = feeds_of(
            windows=[window(start="2026-11-27T01:00:00Z", until="2026-11-27T05:00:00Z")],
            assists=[assist(), assist("T1", "tug-A", "tug")],
        )
        draft = schedule.Planner(feeds, Project("t")).plan(
            "V1", vessel(eta="2026-11-27T01:10:00Z"), "B1", 1
        )
        self.assertTrue(draft.complete)
        self.assertFalse(draft.advance)
        self.assertEqual(draft.approach.maneuver[0], t("2026-11-27T01:10:00Z"))

    def test_manual_span_must_respect_work_duration(self):
        service = HarborService(self.feeds)
        service.register_forecast("V1", forecast_payload())
        plan_id = list(service.project.calls["V1"].plans)[-1]
        outcome = service.propose_manual(
            "V1",
            {
                "berth": "B1",
                "revision": 1,
                "approach": {"from": "2026-11-27T02:00:00Z", "until": "2026-11-27T02:30:00Z"},
                "departure": {"from": "2026-11-27T02:40:00Z", "until": "2026-11-27T03:10:00Z"},
            },
        )
        self.assertFalse(outcome.ok)
        kinds = [issue["kind"] for issue in outcome.issues]
        self.assertIn("manual_too_early", kinds)
        self.assertNotEqual(plan_id, None)

    def test_group_can_be_reused_in_different_windows(self):
        feeds = feeds_of(
            windows=[window()],
            assists=[assist(), assist("T1", "tug-A", "tug")],
        )
        service = HarborService(feeds, peer=ConfirmPeer())
        service.register_forecast("V1", forecast_payload())
        pid = list(service.project.calls["V1"].plans)[-1]
        self.assertTrue(service.confirm(pid).ok)
        bookings = service.project.bookings_of("V1", pid)
        groups = {b.group for b in bookings}
        # 同一组在靠和离两个窗口各有一条承诺，但两个窗口不重叠。
        self.assertEqual(len(bookings), 4)
        for group in groups:
            spans = sorted(b.span for b in bookings if b.group == group)
            for earlier, later in zip(spans, spans[1:]):
                self.assertLessEqual(earlier[1], later[0])


class ConfirmationTest(unittest.TestCase):
    def setUp(self):
        self.feeds = feeds_of(
            windows=[window()],
            assists=[assist(), assist("T1", "tug-A", "tug")],
        )

    def make_service(self, peer):
        service = HarborService(self.feeds, peer=peer)
        service.register_forecast("V1", forecast_payload())
        self.pid = list(service.project.calls["V1"].plans)[-1]
        return service

    def test_confirm_requires_every_institution(self):
        peer = ConfirmPeer(states={"T1-V1": "released"})
        service = self.make_service(peer)
        outcome = service.confirm(self.pid)
        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.pending)
        self.assertEqual(service.project.calls["V1"].plans[self.pid].state, "candidate")
        self.assertIsNone(service.project.calls["V1"].active)
        # 未回话的预约必须能在查询里看出来是候选。
        report = service.call_report("V1")
        self.assertEqual(report["candidate"][0]["id"], self.pid)
        self.assertEqual(report["confirmed"], [])

    def test_unreachable_peer_keeps_plan_as_candidate(self):
        peer = ConfirmPeer(states={"P1-V1": PeerUnavailable("P1-V1", "连接断开")})
        service = self.make_service(peer)
        outcome = service.confirm(self.pid)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.pending[0]["group"], "P1")
        self.assertEqual(service.pending_decisions()[0]["group"], "P1")

    def test_confirm_waits_for_all_then_activates(self):
        peer = ConfirmPeer(states={"P1-V1": "confirmed", "T1-V1": "confirmed"},
                           )
        service = self.make_service(peer)
        outcome = service.confirm(self.pid)
        self.assertTrue(outcome.ok, outcome.pending)
        self.assertEqual(service.project.calls["V1"].active, self.pid)
        self.assertEqual(service.call_report("V1")["confirmed"][0]["id"], self.pid)

    def test_second_confirm_is_rejected(self):
        service = self.make_service(ConfirmPeer())
        self.assertTrue(service.confirm(self.pid).ok)
        other = service.propose("V1", berth="B1", revision=1)
        second = service.confirm(other.plan["id"])
        self.assertFalse(second.ok)
        self.assertEqual(second.issues[0]["kind"], "already_confirmed")

    def test_confirm_revalidates_after_window_change(self):
        service = self.make_service(ConfirmPeer())
        # 码头把窗口压到靠泊时段之后，原方案失效。
        service.set_window_revision(
            "B1",
            2,
            {"windows": [{"from": "2026-11-27T04:00:00Z", "until": "2026-11-27T04:30:00Z"}]},
        )
        record = service.project.calls["V1"]
        record.revision = 2
        outcome = service.propose("V1", berth="B1", revision=2)
        confirmed = service.confirm(outcome.plan["id"])
        self.assertFalse(confirmed.ok)
        self.assertNotEqual(service.project.calls["V1"].plans[outcome.plan["id"]].state, "confirmed")
        self.assertFalse(
            any(plan.state == "confirmed" for plan in record.plans.values())
        )

    def test_window_change_marks_confirmed_plan_stale(self):
        service = self.make_service(ConfirmPeer())
        self.assertTrue(service.confirm(self.pid).ok)
        service.set_window_revision(
            "B1",
            2,
            {"windows": [{"from": "2026-11-27T01:00:00Z", "until": "2026-11-27T05:00:00Z"}]},
        )
        plan = service.project.calls["V1"].plans[self.pid]
        self.assertTrue(plan.stale)
        self.assertIn("修订 2", plan.note)

    def test_manual_time_survives_confirm_revalidation(self):
        service = self.make_service(ConfirmPeer())
        proposed = service.propose_manual(
            "V1",
            {
                "berth": "B1",
                "revision": 1,
                "approach": {"from": "2026-11-27T02:00:00Z", "until": "2026-11-27T02:30:00Z"},
                "departure": {"from": "2026-11-27T03:30:00Z", "until": "2026-11-27T04:00:00Z"},
            },
        )
        self.assertTrue(proposed.ok, proposed.issues)
        confirmed = service.confirm(proposed.plan["id"])
        self.assertTrue(confirmed.ok, confirmed.issues)
        plan = service.project.calls["V1"].plans[proposed.plan["id"]]
        self.assertEqual(plan.approach.maneuver[0], t("2026-11-27T02:00:00Z"))


class CancellationTest(unittest.TestCase):
    def setUp(self):
        self.feeds = feeds_of(
            windows=[window()],
            assists=[assist(), assist("T1", "tug-A", "tug")],
        )

    def confirmed(self, peer):
        service = HarborService(self.feeds, peer=peer)
        service.register_forecast("V1", forecast_payload())
        pid = list(service.project.calls["V1"].plans)[-1]
        self.assertTrue(service.confirm(pid).ok)
        return service, pid

    def test_cancel_needs_original_institution_release(self):
        peer = ConfirmPeer(cancel_states={"P1-V1": "on_board", "T1-V1": "on_board"})
        service, pid = self.confirmed(peer)
        outcome = service.cancel(pid)
        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.pending)
        # 未确认释放前，值班组不能被别的船使用。
        self.assertFalse(service.group_report("P1")["free"])
        self.assertTrue(all(row["still_holds"] for row in service.group_report("P1")["commitments"]))

    def test_released_booking_frees_group(self):
        peer = ConfirmPeer(cancel_states={"P1-V1": "released", "T1-V1": "released"})
        service, pid = self.confirmed(peer)
        outcome = service.cancel(pid)
        self.assertTrue(outcome.ok, outcome.pending)
        self.assertTrue(service.group_report("P1")["free"])
        booking = next(
            b for b in service.project.bookings_of("V1", pid) if b.external_ref == "P1-V1"
        )
        self.assertEqual(booking.state, "released")

    def test_held_booking_can_continue_on_new_window(self):
        peer = ConfirmPeer(cancel_states={"P1-V1": "on_board", "T1-V1": "on_board"})
        service, pid = self.confirmed(peer)
        outcome = service.cancel(pid, continue_booking=True)
        self.assertTrue(outcome.ok, outcome.pending)
        self.assertTrue(outcome.kept)
        # 同意继续服务不等于空闲。
        self.assertTrue(all(row["still_holds"] for row in outcome.kept))
        self.assertFalse(service.group_report("P1")["free"])


class RescheduleTest(unittest.TestCase):
    def setUp(self):
        self.feeds = feeds_of(
            windows=[
                window(start="2026-11-27T01:00:00Z", until="2026-11-27T02:30:00Z"),
                window(start="2026-11-27T03:00:00Z", until="2026-11-27T05:00:00Z"),
            ],
            assists=[assist(), assist("T1", "tug-A", "tug")],
        )

    def confirmed(self, peer, payload=None):
        service = HarborService(self.feeds, peer=peer)
        service.register_forecast("V1", forecast_payload(payload))
        pid = list(service.project.calls["V1"].plans)[-1]
        outcome = service.confirm(pid)
        self.assertTrue(outcome.ok, outcome.pending)
        return service, pid

    def test_reschedule_succeeds_only_when_both_actions_granted(self):
        peer = ConfirmPeer(
            cancel_states={"P1-V1": "released", "T1-V1": "released",
                           "P1-V1-depart": "released", "T1-V1-depart": "released"},
        )
        service, pid = self.confirmed(peer)
        outcome = service.reschedule(
            pid,
            {
                "berth": "B1",
                "revision": 1,
                "approach": {"from": "2026-11-27T03:00:00Z", "until": "2026-11-27T03:30:00Z"},
                "departure": {"from": "2026-11-27T04:30:00Z", "until": "2026-11-27T05:00:00Z"},
            },
        )
        self.assertTrue(outcome.ok, (outcome.issues, outcome.pending))
        new_id = outcome.plan["id"]
        self.assertEqual(service.project.calls["V1"].active, new_id)
        self.assertEqual(service.project.calls["V1"].plans[pid].state, "superseded")

    def test_reschedule_failure_reports_kept_commitments(self):
        peer = ConfirmPeer(
            cancel_states={"P1-V1": "on_board", "T1-V1": "on_board",
                           "P1-V1-depart": "on_board", "T1-V1-depart": "on_board"},
        )
        service, pid = self.confirmed(peer)
        outcome = service.reschedule(
            pid,
            {
                "berth": "B1",
                "revision": 1,
                "approach": {"from": "2026-11-27T03:00:00Z", "until": "2026-11-27T03:30:00Z"},
                "departure": {"from": "2026-11-27T04:30:00Z", "until": "2026-11-27T05:00:00Z"},
            },
        )
        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.kept)
        self.assertTrue(all(item["still_holds"] for item in outcome.kept))
        self.assertEqual(service.project.calls["V1"].active, pid)

    def test_reschedule_without_peer_confirmation_stays_candidate(self):
        peer = ConfirmPeer(
            cancel_states={"P1-V1": "released", "T1-V1": "released",
                           "P1-V1-depart": "released", "T1-V1-depart": "released"},
        )
        service, pid = self.confirmed(peer)
        # 新窗口的拖轮回话不明确：撤销已成功，但对方没有答应新窗口。
        peer.states["T1-V1"] = "released"
        outcome = service.reschedule(
            pid,
            {
                "berth": "B1",
                "revision": 1,
                "approach": {"from": "2026-11-27T03:00:00Z", "until": "2026-11-27T03:30:00Z"},
                "departure": {"from": "2026-11-27T04:30:00Z", "until": "2026-11-27T05:00:00Z"},
            },
        )
        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.pending)
        self.assertTrue(outcome.released)
        # 旧承诺已全部释放，无法用它继续执行；新方案仍缺拖轮答复，只能留作候选。
        self.assertEqual(service.project.calls["V1"].plans[pid].state, "candidate")
        self.assertIsNone(service.project.calls["V1"].active)
        self.assertFalse(service.project.calls["V1"].plans[outcome.plan["id"]].state == "confirmed")


class ActualsTest(unittest.TestCase):
    def setUp(self):
        self.feeds = feeds_of(
            windows=[window(start="2026-11-27T00:00:00Z", until="2026-11-27T06:00:00Z")],
            assists=[assist(start="2026-11-27T00:00:00Z", until="2026-11-27T06:00:00Z"),
                     assist("T1", "tug-A", "tug", start="2026-11-27T00:00:00Z", until="2026-11-27T06:00:00Z")],
        )

    def test_moored_vessel_is_not_an_empty_berth(self):
        service = HarborService(self.feeds, peer=ConfirmPeer())
        service.register_forecast("V1", forecast_payload())
        pid = list(service.project.calls["V1"].plans)[-1]
        service.confirm(pid)
        report = service.record_quay_event(
            {"event": "A1", "call": "V1", "berth": "B1", "kind": "all_fast", "at": "2026-11-27T01:40:00Z"}
        )
        self.assertEqual(report["actual"]["status"], "moored")
        self.assertEqual(report["loading_point"]["from"], "2026-11-27T02:35:00Z")
        # 另一条船不能用这个泊位。
        service.register_forecast("V2", forecast_payload(vessel(call="V2", beam=24, draft=74,
                                                               eta="2026-11-27T02:00:00Z")))
        other = service.project.calls["V2"]
        plan = other.plans[list(other.plans)[-1]]
        self.assertIn(
            "awaiting_clear",
            [issue.get("kind") for issue in plan.issues],
        )

    def test_loading_point_uses_actual_all_fast(self):
        service = HarborService(self.feeds, peer=ConfirmPeer())
        service.register_forecast("V1", forecast_payload())
        report = service.record_quay_event(
            {"event": "A1", "call": "V1", "berth": "B1", "kind": "all_fast", "at": "2026-11-27T02:10:00Z"}
        )
        self.assertEqual(report["loading_point"]["from"], "2026-11-27T03:05:00Z")

    def test_departure_and_tug_completion_are_separate(self):
        peer = ConfirmPeer()
        service = HarborService(self.feeds, peer=peer)
        service.register_forecast("V1", forecast_payload())
        pid = list(service.project.calls["V1"].plans)[-1]
        service.confirm(pid)
        # 解缆只释放泊位位置。
        service.record_quay_event(
            {"event": "A1", "call": "V1", "berth": "B1", "kind": "all_fast", "at": "2026-11-27T01:40:00Z"}
        )
        service.record_quay_event(
            {"event": "A2", "call": "V1", "berth": "B1", "kind": "lines_clear", "at": "2026-11-27T03:20:00Z"}
        )
        booking = service.project.bookings_of("V1", pid)[0]
        # 拖轮/引航尚未回结束，组仍占用。
        self.assertTrue(booking.blocking)
        service.apply_peer_reply(booking.ref, "completed")
        self.assertFalse(booking.blocking)
        berth = service.berth_report("B1")
        self.assertTrue(all(not row["open"] for row in berth["actual"]))

    def test_vessel_without_departure_window_is_flagged(self):
        feeds = feeds_of(
            windows=[window(start="2026-11-27T00:00:00Z", until="2026-11-27T02:00:00Z")],
            assists=[assist(start="2026-11-27T00:00:00Z", until="2026-11-27T02:00:00Z"),
                     assist("T1", "tug-A", "tug", start="2026-11-27T00:00:00Z", until="2026-11-27T02:00:00Z")],
        )
        service = HarborService(feeds, peer=ConfirmPeer())
        service.register_forecast("V1", forecast_payload())
        report = service.record_quay_event(
            {"event": "A1", "call": "V1", "berth": "B1", "kind": "all_fast", "at": "2026-11-27T01:00:00Z"}
        )
        self.assertEqual(report["berthing"]["loss_of_departure"]["kind"], "no_departure_window")


class QueryTest(unittest.TestCase):
    def setUp(self):
        self.feeds = feeds_of(
            windows=[window()],
            assists=[assist(), assist("T1", "tug-A", "tug")],
        )

    def test_queries_by_call_berth_and_group(self):
        peer = ConfirmPeer(states={"T1-V1": "dispatched"})
        service = HarborService(self.feeds, peer=peer)
        service.register_forecast("V1", forecast_payload())
        pid = list(service.project.calls["V1"].plans)[-1]
        service.confirm(pid)
        result = service.query(call="V1", berth="B1", group="T1")
        self.assertEqual(result["call"]["candidate"][0]["id"], pid)
        self.assertEqual(result["berth"]["latest_revision"], 1)
        self.assertEqual(result["group"]["group"], "T1")
        self.assertTrue(result["group"]["pending"])

    def test_record_inquiry_and_accept(self):
        service = HarborService(self.feeds, peer=ConfirmPeer())
        service.register_forecast("V1", forecast_payload())
        pid = list(service.project.calls["V1"].plans)[-1]
        accepted = service.accept_plan(pid)
        self.assertTrue(accepted.ok)
        answer = service.record_inquiry("V1", "拖轮能否提前 20 分钟", ["tug-A"])
        self.assertTrue(answer["registered"])
        self.assertTrue(any(item["op"] == "inquiry" for item in service.project.log))


class GroupExclusivityTest(unittest.TestCase):
    def test_same_group_cannot_serve_two_calls_at_once(self):
        # 两个泊位都能接船，但 P1/T1 只有一组，同一时段不能同时服务两条船。
        feeds = feeds_of(
            windows=[window(berth="B1"), window(berth="B2")],
            assists=[assist(), assist("T1", "tug-A", "tug")],
        )
        service = HarborService(feeds, peer=ConfirmPeer())
        service.register_forecast("V1", forecast_payload(vessel()))
        service.register_forecast("V2", forecast_payload(vessel(call="V2")))
        first = list(service.project.calls["V1"].plans)[-1]
        second = service.propose("V2", berth="B2", revision=1).plan["id"]
        self.assertTrue(service.confirm(first).ok)
        outcome = service.confirm(second)
        self.assertFalse(outcome.ok)
        self.assertEqual(service.project.calls["V2"].plans[second].state, "candidate")
        # 值班组仍只被一份确认承诺占用：重叠时段内至多一条。
        spans = [
            booking.span
            for booking in service.project.bookings.values()
            if booking.group == "P1" and booking.blocking
        ]
        for earlier, later in zip(sorted(spans), sorted(spans)[1:]):
            self.assertLessEqual(earlier[1], later[0])

    def test_group_can_serve_later_window_after_release(self):
        feeds = feeds_of(
            windows=[
                window(start="2026-11-27T01:00:00Z", until="2026-11-27T03:00:00Z"),
                window(start="2026-11-27T04:00:00Z", until="2026-11-27T05:00:00Z"),
            ],
            assists=[assist(), assist("T1", "tug-A", "tug")],
        )
        peer = ConfirmPeer()
        service = HarborService(feeds, peer=peer)
        service.register_forecast("V1", forecast_payload(
            vessel(eta="2026-11-27T01:00:00Z", work=20, latest="2026-11-27T03:00:00Z")
        ))
        pid = list(service.project.calls["V1"].plans)[-1]
        self.assertTrue(service.confirm(pid).ok)
        # 释放较早窗口后，另一条船可以使用同一值班组的晚窗口。
        for booking in service.project.bookings_of("V1", pid):
            service.apply_peer_reply(booking.ref, "completed")
        service.register_forecast("V2", forecast_payload(
            vessel(call="V2", eta="2026-11-27T04:00:00Z", work=20,
                   latest="2026-11-27T05:00:00Z")
        ))
        second = list(service.project.calls["V2"].plans)[-1]
        self.assertTrue(service.confirm(second).ok)


class StalePlanTest(unittest.TestCase):
    def test_actual_event_marks_confirmed_plan_stale(self):
        feeds = feeds_of(
            windows=[window()],
            assists=[assist(), assist("T1", "tug-A", "tug")],
        )
        service = HarborService(feeds, peer=ConfirmPeer())
        service.register_forecast("V1", forecast_payload())
        pid = list(service.project.calls["V1"].plans)[-1]
        service.confirm(pid)
        service.record_quay_event(
            {"event": "A1", "call": "V1", "berth": "B1", "kind": "all_fast", "at": "2026-11-27T01:40:00Z"}
        )
        self.assertTrue(service.project.calls["V1"].plans[pid].stale)

    def test_forecast_update_returns_plan_to_candidate(self):
        feeds = feeds_of(
            windows=[window()],
            assists=[assist(), assist("T1", "tug-A", "tug")],
        )
        service = HarborService(feeds, peer=ConfirmPeer())
        service.register_forecast("V1", forecast_payload())
        pid = list(service.project.calls["V1"].plans)[-1]
        self.assertTrue(service.confirm(pid).ok)
        outcome = service.update_forecast("V1", forecast_payload(vessel(work=120)))
        self.assertEqual(service.project.calls["V1"].plans[pid].state, "candidate")
        self.assertIsNone(service.project.calls["V1"].active)
        self.assertIn("重新确认", outcome.note)




class PeerIntegrationTest(unittest.TestCase):
    """用项目里录制的对端回放，走一遍候选 -> 确认 -> 回话 -> 回执。"""

    def setUp(self):
        from pathlib import Path
        from peer_replay import serve_recording
        from threading import Thread
        from harbor.peer import PeerClient

        recording = Path(__file__).resolve().parents[1] / "samples" / "peer.json"
        self.server = serve_recording(recording, 0)
        self.thread = Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()
        self.client = PeerClient(f"http://127.0.0.1:{self.server.server_port}")

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def test_replayed_states_drive_booking_state(self):
        from harbor.peer import PeerUnavailable

        first = self.client.fetch("P1-Q41")
        self.assertTrue(first.holds_group)
        second = self.client.fetch("P1-Q41")
        self.assertTrue(second.finished)
        # 第三项之后重复最后一项，仍是 completed。
        self.assertEqual(self.client.fetch("P1-Q41").state, "completed")
        with self.assertRaises(PeerUnavailable):
            self.client.fetch("P1-Q41-missing")
        self.assertEqual(self.client.cancel("P1-Q41").state, "on_board")


class SnapshotTest(unittest.TestCase):
    def test_round_trip_preserves_commitments(self):
        feeds = feeds_of(
            windows=[window()],
            assists=[assist(), assist("T1", "tug-A", "tug")],
        )
        service = HarborService(feeds, peer=ConfirmPeer())
        service.register_forecast("V1", forecast_payload())
        pid = list(service.project.calls["V1"].plans)[-1]
        self.assertTrue(service.confirm(pid).ok)
        restored = HarborService.restore(feeds, service.snapshot(), peer=ConfirmPeer())
        original = service.project.calls["V1"]
        copy = restored.project.calls["V1"]
        self.assertEqual(copy.active, pid)
        self.assertEqual(copy.plans[pid].approach.maneuver, original.plans[pid].approach.maneuver)
        self.assertEqual(
            [b.ref for b in restored.project.bookings_of("V1", pid)],
            [b.ref for b in service.project.bookings_of("V1", pid)],
        )
        self.assertEqual(restored.group_report("P1")["commitments"][0]["still_holds"], True)

    def test_unknown_call_and_plan_raise(self):
        service = HarborService(feeds_of(), peer=ConfirmPeer())
        with self.assertRaises(KeyError):
            service.call_report("NOPE")
        with self.assertRaises(KeyError):
            service.confirm("NOPE-001")




if __name__ == "__main__":
    unittest.main()
