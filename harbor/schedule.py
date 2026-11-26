"""排期计算：把泊位窗口、实际占用和协助可用区间合成候选动作。

两个动作：

* ``approach``：引航登轮 + 拖轮协助，船靠妥，随后 ``all_fast``；
* ``departure``：拖轮协助 + 引航，船离泊，随后 ``lines_clear``。

约束：

* 动作必须完整落在码头公布的某个泊位窗口内（左闭右开），且船宽、吃水适配；
* 泊位实际被占用时不可接船；``lines_clear`` 之后才真正空出；
* 靠妥后要留出完整作业时长，离泊不得早于 ``all_fast + work_minutes``，
  且不晚于最迟离开时间；
* 值班组同一时刻只能服务一个动作，但不同窗口可复用同一组。

本模块只产出候选，不判断机构是否已确认；确认由 ``service`` 在收到对端
回话后写入。
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .timeutil import describe, intersect, intersect_all, merge, shift, subtract, union

MANEUVER_MINUTES = 30
MAX_CANDIDATES = 6


def horizon(feeds, *moments):
    """把尚未结束的占用截断到业务视界内，避免开放区间参与无限运算。"""
    latest = [moment for moment in moments if moment is not None]
    latest += [window.until for window in feeds.windows]
    latest += [slot.until for slot in feeds.assists]
    base = max(latest) if latest else datetime.now(timezone.utc)
    return base + timedelta(days=1)


@dataclass
class ActionCandidate:
    action: str
    berth_window: tuple
    maneuver: tuple
    assists: list
    advance: bool = False
    requires_clear: str = None
    manual: bool = False

    @property
    def groups(self):
        return {item["group"] for item in self.assists}

    def to_json(self):
        return {
            "action": self.action,
            "berth_window": describe(self.berth_window),
            "maneuver": describe(self.maneuver),
            "assists": [
                {
                    "kind": item["kind"],
                    "group": item["group"],
                    "agency": item["agency"],
                    "span": describe(item["span"]),
                }
                for item in self.assists
            ],
            "advance": self.advance,
            "requires_clear": self.requires_clear,
            "manual": self.manual,
        }


@dataclass
class Draft:
    berth: str
    revision: int
    approach: ActionCandidate = None
    departure: ActionCandidate = None
    approach_candidates: list = field(default_factory=list)
    departure_candidates: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    advance: bool = False
    requires_clear: str = None
    already_moored: bool = False
    already_departed: bool = False

    @property
    def complete(self):
        return self.departure is not None and (
            self.approach is not None or self.already_moored
        )


class Planner:
    """在给定交换资料与项目状态上做纯计算。"""

    def __init__(self, feeds, project):
        self.feeds = feeds
        self.project = project

    # ---------- 占用与可用 ----------

    def berth_blocks(self, berth, vessel_call, exclude_plan=None):
        """泊位不可用的时段：实际系泊占用，以及别的已确认方案。"""
        limit = horizon(self.feeds)
        blocks = []
        for occupant in occupancies(self.feeds, self.project).get(berth, []):
            if occupant.call == vessel_call:
                continue
            if occupant.end is not None:
                blocks.append((occupant.start, occupant.end))
                continue
            confirmation = self.project.clear_confirmed(berth, occupant.call)
            if confirmation is not None:
                blocks.append((occupant.start, min(confirmation.at, limit)))
            else:
                blocks.append((occupant.start, limit))
        for plan in self._confirmed_plans():
            if plan.berth != berth or plan.id == exclude_plan:
                continue
            if plan.call == vessel_call:
                continue
            if plan.approach is None:
                # 已系泊船的泊位占用来自实际 all_fast，不在方案里重复造块。
                continue
            if plan.departure is None:
                continue
            blocks.append((plan.approach.maneuver[0], plan.departure.maneuver[1]))
        return merge(blocks)

    def group_busy(self, group, exclude_plan=None):
        spans = []
        for booking in self.project.bookings.values():
            if booking.group != group or not booking.blocking:
                continue
            if exclude_plan is not None and booking.plan_id == exclude_plan:
                continue
            spans.append(booking.span)
        return merge(spans)

    def assist_windows(self, kind, vessel, exclude_plan=None):
        """某类协助对给定船可用的时段，按值班组分别扣掉本项目已承诺的占用。"""
        per_group = {}
        for slot in self.feeds.assists:
            if slot.kind != kind or not slot.accepts(vessel):
                continue
            free = subtract([slot.span], self.group_busy(slot.group, exclude_plan))
            if free:
                entry = per_group.setdefault(group := slot.group, {"agency": slot.agency, "spans": []})
                entry["spans"].extend(free)
        for entry in per_group.values():
            entry["spans"] = merge(entry["spans"])
        return per_group

    def _confirmed_plans(self):
        result = []
        for call in self.project.calls.values():
            for plan in call.plans.values():
                if plan.state == "confirmed" and plan.complete:
                    result.append(plan)
        return result

    # ---------- 计算 ----------

    def plan(
        self,
        vessel_call,
        vessel,
        berth,
        revision,
        exclude_plan=None,
        approach_span=None,
        departure_span=None,
    ):
        draft = Draft(berth=berth, revision=revision)
        windows = [
            window
            for window in self.project.windows_for(self.feeds, berth, revision)
            if window.fits(vessel)
        ]
        if not windows:
            draft.issues.append(
                {
                    "kind": "berth_window",
                    "detail": f"{berth} 修订 {revision} 没有同时满足船宽与吃水的窗口",
                }
            )
            return draft

        if self._actual_of(vessel_call) is not None and self._actual_of(vessel_call).moored:
            draft.already_moored = True
            self._plan_departure_only(
                draft, vessel, windows, exclude_plan, departure_span
            )
            return draft

        actual = self._actual_of(vessel_call)
        if actual is not None and actual.departed:
            draft.already_departed = True
            draft.issues.append(
                {
                    "kind": "call_completed",
                    "detail": "该航次已实际解缆离泊，如需再次挂靠请另立航次",
                    "lines_clear": describe((actual.lines_clear, actual.lines_clear)),
                }
            )
            return draft

        # 泊位仍被别船实际占用且没有码头确认腾让时，无法判断何时空出；
        # 已系泊的船不能当成空泊位，只能等实际解缆或明确让泊位。
        occupant = self._awaiting_clear(berth, vessel_call)
        if occupant is not None:
            draft.requires_clear = occupant
            draft.issues.append(
                {
                    "kind": "awaiting_clear",
                    "detail": f"{berth} 仍由 {occupant} 实际占用，且没有码头确认腾让",
                    "call": occupant,
                }
            )
            return draft

        blocks = self.berth_blocks(berth, vessel_call, exclude_plan)
        limit = horizon(self.feeds, vessel.latest_exit)

        if approach_span is not None:
            manual = self._candidate_at(
                "approach", vessel, approach_span, windows, blocks, exclude_plan,
                manual=True,
            )
            if manual is not None:
                draft.approach = manual
                draft.approach_candidates = [manual]
                draft.advance = manual.maneuver[0] < vessel.eta
                draft.requires_clear = manual.requires_clear
            else:
                draft.issues.append(
                    {
                        "kind": "manual_infeasible",
                        "detail": "人工指定的靠泊时间不在可用窗口或协助组区间内",
                        "span": describe(approach_span),
                    }
                )
                return draft
        else:
            approach_runs = self._approach_runs(
                vessel, windows, blocks, exclude_plan
            )
            if not approach_runs:
                draft.issues.append(self._no_approach_issue(berth, blocks, windows, vessel))
                draft.requires_clear = self._awaiting_clear(berth, vessel_call)
                return draft
            on_time = [run for run in approach_runs if not run.advance]
            ordered = on_time + [run for run in approach_runs if run.advance]
            draft.approach_candidates = ordered[:MAX_CANDIDATES]
            chosen = None
            departure = None
            departure_candidates = []
            for candidate in ordered:
                work_end = shift(candidate.maneuver[1], vessel.work_minutes)
                if work_end > vessel.latest_exit:
                    continue
                option, options = self._choose_departure(
                    vessel, windows, work_end, blocks, exclude_plan
                )
                if option is not None:
                    chosen, departure, departure_candidates = candidate, option, options
                    break
                if chosen is None:
                    chosen = candidate
                    departure_candidates = options
            if chosen is None:
                draft.issues.append(self._no_approach_issue(berth, blocks, windows, vessel))
                draft.requires_clear = self._awaiting_clear(berth, vessel_call)
                return draft
            draft.approach = chosen
            draft.advance = chosen.advance
            draft.requires_clear = chosen.requires_clear
            draft.departure = departure
            draft.departure_candidates = departure_candidates[:MAX_CANDIDATES]
            if departure is None:
                draft.issues.append(
                    {
                        "kind": "no_departure",
                        "detail": (
                            "靠妥并完成作业后，没有同时满足泊位窗口、最迟离开时间和"
                            "协助组可用区间的离泊窗口"
                        ),
                    }
                )
                draft.issues.extend(
                    self._diagnose_departure(vessel, shift(chosen.maneuver[1], vessel.work_minutes))
                )
            return draft

        if departure_span is not None:
            manual = self._candidate_at(
                "departure", vessel, departure_span, windows, blocks, exclude_plan,
                manual=True,
            )
            if manual is None:
                draft.issues.append(
                    {
                        "kind": "manual_infeasible",
                        "detail": "人工指定的离泊时间不在可用窗口或协助组区间内",
                        "span": describe(departure_span),
                    }
                )
                return draft
            work_end = shift(draft.approach.maneuver[1], vessel.work_minutes)
            if manual.maneuver[0] < work_end:
                draft.issues.append(
                    {
                        "kind": "manual_too_early",
                        "detail": "人工指定的离泊时间早于靠妥后完整作业时长",
                        "span": describe(departure_span),
                        "earliest": describe((work_end, work_end)),
                    }
                )
                return draft
            draft.departure = manual
            draft.departure_candidates = [manual]
            if manual.maneuver[1] > vessel.latest_exit:
                draft.issues.append(
                    {
                        "kind": "late_exit",
                        "detail": "人工指定的离泊结束晚于最迟离开时间",
                        "span": describe(departure_span),
                    }
                )
                return draft
            return draft

        work_end = shift(draft.approach.maneuver[1], vessel.work_minutes)
        departure, departure_candidates = self._choose_departure(
            vessel, windows, work_end, blocks, exclude_plan
        )
        draft.departure = departure
        draft.departure_candidates = departure_candidates[:MAX_CANDIDATES]
        if departure is None:
            draft.issues.append(
                {
                    "kind": "no_departure",
                    "detail": (
                        "靠妥并完成作业后，没有同时满足泊位窗口、最迟离开时间和"
                        "协助组可用区间的离泊窗口"
                    ),
                }
            )
            draft.issues.extend(self._diagnose_departure(vessel, work_end))
        return draft

    def _plan_departure_only(self, draft, vessel, windows, exclude_plan, departure_span):
        actual = self._actual_of(vessel.call)
        earliest = shift(actual.all_fast, vessel.work_minutes)
        blocks = self.berth_blocks(draft.berth, vessel.call, exclude_plan)
        if departure_span is not None:
            manual = self._candidate_at(
                "departure", vessel, departure_span, windows, blocks, exclude_plan,
                manual=True,
            )
            if manual is None:
                draft.issues.append(
                    {"kind": "no_departure", "detail": "人工指定的离泊时间不可执行"}
                )
            elif manual.maneuver[0] < earliest:
                draft.issues.append(
                    {
                        "kind": "work_incomplete",
                        "detail": "实际系泊时间加作业时长尚未留足",
                        "earliest": describe((earliest, earliest)),
                    }
                )
            else:
                draft.departure = manual
                draft.departure_candidates = [manual]
            return
        departure, candidates = self._choose_departure(
            vessel, windows, earliest, blocks, exclude_plan
        )
        draft.departure = departure
        draft.departure_candidates = candidates[:MAX_CANDIDATES]
        if departure is None:
            draft.issues.append(
                {
                    "kind": "no_departure",
                    "detail": "实际系泊后没有合适的离泊窗口",
                    "earliest_departure": describe((earliest, earliest)),
                }
            )

    def _actual_of(self, call):
        return actuals(self.feeds, self.project).get(call)

    def _approach_runs(self, vessel, windows, blocks, exclude_plan):
        runs = []
        for window in windows:
            free = subtract([window.span], blocks)
            per_kind, missing = self._assist_options(vessel, exclude_plan)
            if missing:
                continue
            feasible = intersect_all(
                [free]
                + [
                    union([entry["spans"] for entry in per_kind[kind].values()])
                    for kind in per_kind
                ]
            )
            for span in feasible:
                starts = [span[0]]
                # 船代按 ETA 到港时，优先给出不要求提前的时段；提前时段另列。
                if span[0] < vessel.eta < span[1]:
                    starts.append(vessel.eta)
                for start in starts:
                    maneuver = (start, shift(start, MANEUVER_MINUTES))
                    if maneuver[1] > span[1]:
                        continue
                    if shift(maneuver[1], vessel.work_minutes) > vessel.latest_exit:
                        continue
                    assigned = self._assign_groups(per_kind, maneuver)
                    if assigned is None:
                        continue
                    runs.append(
                        ActionCandidate(
                            action="approach",
                            berth_window=window.span,
                            maneuver=maneuver,
                            assists=assigned,
                            advance=maneuver[0] < vessel.eta,
                            requires_clear=self._awaiting_clear_for_berth(window.berth),
                        )
                    )
            if len(runs) >= MAX_CANDIDATES * 2:
                break
        runs.sort(key=lambda run: (run.advance, run.maneuver[0]))
        return runs

    def _assist_options(self, vessel, exclude_plan):
        per_kind = {}
        missing = []
        for kind in vessel.assist_required:
            options = self.assist_windows(kind, vessel, exclude_plan)
            if not options:
                missing.append(kind)
            else:
                per_kind[kind] = options
        return per_kind, missing

    def _choose_departure(self, vessel, windows, earliest, blocks, exclude_plan):
        candidates = []
        per_kind, missing = self._assist_options(vessel, exclude_plan)
        if missing:
            return None, []
        for window in windows:
            if window.until <= earliest:
                continue
            start = max(window.start, earliest)
            free = subtract([(start, window.until)], blocks)
            feasible = intersect_all(
                [free]
                + [
                    union([entry["spans"] for entry in per_kind[kind].values()])
                    for kind in per_kind
                ]
            )
            for span in feasible:
                maneuver = (span[0], shift(span[0], MANEUVER_MINUTES))
                if span[1] < maneuver[1]:
                    continue
                if maneuver[1] > vessel.latest_exit:
                    continue
                assigned = self._assign_groups(per_kind, maneuver)
                if assigned is None:
                    continue
                candidates.append(
                    ActionCandidate(
                        action="departure",
                        berth_window=window.span,
                        maneuver=maneuver,
                        assists=assigned,
                    )
                )
        candidates.sort(key=lambda run: run.maneuver[0])
        return (candidates[0] if candidates else None), candidates

    def _candidate_at(self, action, vessel, span, windows, blocks, exclude_plan, manual):
        per_kind, missing = self._assist_options(vessel, exclude_plan)
        if missing:
            return None
        containing = [
            window
            for window in windows
            if window.start <= span[0] and span[1] <= window.until
        ]
        if not containing:
            return None
        if any(intersect(span, block) for block in blocks):
            return None
        assigned = self._assign_groups(per_kind, span)
        if assigned is None:
            return None
        return ActionCandidate(
            action=action,
            berth_window=containing[0].span,
            maneuver=span,
            assists=assigned,
            advance=action == "approach" and span[0] < vessel.eta,
            requires_clear=self._awaiting_clear_for_berth(containing[0].berth)
            if action == "approach"
            else None,
            manual=manual,
        )

    def _assign_groups(self, per_kind, maneuver):
        """为每类协助挑一个能完整覆盖动作的值班组；同一动作内不重复用组。"""
        used = set()
        chosen = []
        for kind, options in sorted(per_kind.items()):
            pick = None
            for group, entry in sorted(options.items()):
                if group in used:
                    continue
                if any(
                    span[0] <= maneuver[0] and maneuver[1] <= span[1]
                    for span in entry["spans"]
                ):
                    pick = (group, entry["agency"])
                    break
            if pick is None:
                return None
            group, agency = pick
            used.add(group)
            chosen.append({"kind": kind, "group": group, "agency": agency, "span": maneuver})
        return chosen

    def _awaiting_clear_for_berth(self, berth):
        for occupancy in occupancies(self.feeds, self.project).get(berth, []):
            if occupancy.end is not None:
                continue
            if self.project.clear_confirmed(berth, occupancy.call) is None:
                return occupancy.call
        return None

    def _awaiting_clear(self, berth, vessel_call):
        for occupancy in occupancies(self.feeds, self.project).get(berth, []):
            if occupancy.call == vessel_call or occupancy.end is not None:
                continue
            if self.project.clear_confirmed(berth, occupancy.call) is None:
                return occupancy.call
        return None

    def _no_approach_issue(self, berth, blocks, windows, vessel):
        occupant = self._awaiting_clear(berth, None)
        if occupant is not None:
            return {
                "kind": "awaiting_clear",
                "detail": f"{berth} 仍由 {occupant} 实际占用，且没有码头确认腾让",
                "call": occupant,
            }
        missing = [
            kind
            for kind in vessel.assist_required
            if not self.assist_windows(kind, vessel)
        ]
        if missing:
            return {
                "kind": "assist_unavailable",
                "detail": f"没有适配该船的协助值班组：{sorted(missing)}",
                "kinds": missing,
            }
        return {
            "kind": "no_approach",
            "detail": "泊位窗口、实际占用与协助可用区间没有交集，需协商换泊或改时",
            "blocked": [describe(span) for span in blocks],
        }

    def _diagnose_departure(self, vessel, work_end):
        if work_end > vessel.latest_exit:
            return [
                {
                    "kind": "work_overrun",
                    "detail": "靠妥时间加作业时长已晚于最迟离开时间",
                }
            ]
        return []


def occupancies(feeds, project):
    from .facts import occupancies as build

    return build(project, feeds)


def actuals(feeds, project):
    from .facts import call_actuals

    return call_actuals(project, feeds)
