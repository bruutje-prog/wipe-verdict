"""Audit the mechanic config against the logs it claims to describe.

Of the first six ability tooltips checked by hand, THREE overturned a config
entry -- Detonate!, Toxic Mist and Falling Ash were each being reported as
avoidable when they are not, and each was flagging most of the raid every
pull. The entries that have not been checked came from the same inference, so
they deserve the same suspicion.

This walks every configured mechanic, prints the evidence behind it, and says
where the numbers disagree with the classification. It cannot decide anything
-- only someone who knows the fight can -- but it turns "is this whole file
right?" into a short list of specific questions.
"""

from __future__ import annotations

import statistics as st
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from .config import BossConfig, COUNT_APPLICATIONS
from .logparse import is_player
from .pulls import Pull
from .roles import TANK, detect_roles

#: A mechanic hitting at least this share of the raid, every pull, is more
#: likely unavoidable than universally misplayed.
UNAVOIDABLE_SHARE = 0.8

#: Tank share this many times the tanks' share of the raid means the mechanic
#: follows the threat target.
TANK_FOLLOW_FACTOR = 3.0

#: Median gap between hits on one player below this suggests ticking damage
#: being counted once per tick.
TICK_GAP_S = 3.0

#: 90th/10th percentile damage above this suggests two components under one id.
BIMODAL_RATIO = 2.5


@dataclass(slots=True)
class MechanicEvidence:
    spell_id: int
    name: str
    category: str            # avoidable | tank_buster | shared | soak
    settings: str            # the config options currently applied
    hits: int = 0
    victims: int = 0
    #: what the config actually counts, after amount_at_least / min_gap_s /
    #: exempt_roles. Raw and counted diverging is the point of showing both:
    #: "160 hits on 24 of 24" reads alarming until you see the threshold
    #: already reduces it to nothing.
    counted_hits: int = 0
    counted_victims: int = 0
    raid: int = 0
    tank_hits: int = 0
    tanks: int = 0
    median_damage: int = 0
    p10: int = 0
    p90: int = 0
    max_damage: int = 0
    median_gap: Optional[float] = None
    pulls_seen: int = 0
    concerns: list[str] = field(default_factory=list)

    @property
    def share(self) -> float:
        """Share of the raid the config actually counts as having failed."""
        return self.counted_victims / self.raid if self.raid else 0.0

    @property
    def raw_share(self) -> float:
        return self.victims / self.raid if self.raid else 0.0

    @property
    def filtered(self) -> bool:
        return self.counted_hits != self.hits

    @property
    def tank_share(self) -> float:
        return self.tank_hits / self.hits if self.hits else 0.0

    @property
    def ok(self) -> bool:
        return not self.concerns


def review_boss(pulls: list[Pull], boss: BossConfig) -> list[MechanicEvidence]:
    """Gather evidence for every configured mechanic on this boss."""
    catalogue: dict[int, tuple[str, str]] = {}
    for sid, m in boss.avoidable.items():
        bits = [f"count: {m.count}"]
        if m.exempt_roles:
            bits.append(f"exempt_roles: {list(m.exempt_roles)}")
        if m.amount_at_least:
            bits.append(f"amount_at_least: {m.amount_at_least:,}")
        if m.min_gap_s:
            bits.append(f"min_gap_s: {m.min_gap_s:g}")
        catalogue[sid] = ("avoidable", ", ".join(bits))
    for sid, t in boss.tank_busters.items():
        catalogue[sid] = (
            "tank_buster", f"expect_cooldown: {str(t.expect_cooldown).lower()}"
        )
    for sid, s in boss.shared.items():
        catalogue[sid] = ("shared", f"expect_share: {s.expect_share:g}")
    for sid, k in boss.soaks.items():
        catalogue[sid] = ("soak", f"fail_spell_id: {k.fail_spell_id}")

    amounts: dict[int, list[int]] = defaultdict(list)
    victims: dict[int, set[str]] = defaultdict(set)
    counted: dict[int, int] = defaultdict(int)
    counted_victims: dict[int, set[str]] = defaultdict(set)
    tank_hits: dict[int, int] = defaultdict(int)
    seen_in: dict[int, set[int]] = defaultdict(set)
    gaps: dict[int, list[float]] = defaultdict(list)
    raid: set[str] = set()
    tanks: set[str] = set()

    for idx, pull in enumerate(pulls):
        roles = detect_roles(pull)
        raid.update(pull.players)
        tanks.update(g for g, r in roles.items() if r.role == TANK)
        last: dict[tuple[str, int], float] = {}
        last_counted: dict[tuple[str, int], float] = {}
        for d in pull.damage_taken:
            if d.spell_id not in catalogue or is_player(d.src_guid):
                continue
            sid = d.spell_id
            amounts[sid].append(d.amount + d.absorbed)
            victims[sid].add(d.dest_guid)
            seen_in[sid].add(idx)
            role = roles[d.dest_guid].role if d.dest_guid in roles else "dps"
            if role == TANK:
                tank_hits[sid] += 1
            key = (d.dest_guid, sid)
            if key in last:
                gaps[sid].append(d.t - last[key])
            last[key] = d.t

            # Mirror what avoidable_table would actually count, so the review
            # never raises an alarm the config already answers.
            mech = boss.avoidable.get(sid)
            if mech is None:
                continue
            if mech.exempt(role):
                continue
            if mech.amount_at_least and (d.amount + d.absorbed) < mech.amount_at_least:
                continue
            if mech.min_gap_s:
                prev = last_counted.get(key)
                if prev is not None and d.t - prev < mech.min_gap_s:
                    continue
                last_counted[key] = d.t
            if not mech.by_applications:
                counted[sid] += 1
                counted_victims[sid].add(d.dest_guid)

    # An applications-counted mechanic is NOT counted from damage events, so
    # counting them here reports the tick total as though it were the figure
    # the table uses. Superheated read as 575 when the table counts 212, and
    # that gap is what made a ticking mechanic look unhandled when it was
    # already handled.
    for pull in pulls:
        for a in pull.auras:
            mech = boss.avoidable.get(a.spell_id)
            if mech is None or not mech.by_applications or not a.applied:
                continue
            if not is_player(a.dest_guid):
                continue
            counted[a.spell_id] += 1
            counted_victims[a.spell_id].add(a.dest_guid)

    out: list[MechanicEvidence] = []
    for sid, (category, settings) in sorted(
        catalogue.items(), key=lambda kv: kv[1][0]
    ):
        amts = sorted(amounts.get(sid, []))
        ev = MechanicEvidence(
            spell_id=sid,
            name=(
                boss.avoidable[sid].name if category == "avoidable"
                else boss.tank_busters[sid].name if category == "tank_buster"
                else boss.shared[sid].name if category == "shared"
                else boss.soaks[sid].name
            ),
            category=category,
            settings=settings,
            hits=len(amts),
            victims=len(victims.get(sid, ())),
            counted_hits=(
                counted.get(sid, 0) if category == "avoidable" else len(amts)
            ),
            counted_victims=(
                len(counted_victims.get(sid, ()))
                if category == "avoidable"
                else len(victims.get(sid, ()))
            ),
            raid=len(raid),
            tank_hits=tank_hits.get(sid, 0),
            tanks=len(tanks),
            pulls_seen=len(seen_in.get(sid, ())),
        )
        if amts:
            ev.median_damage = int(st.median(amts))
            ev.p10 = amts[int(len(amts) * 0.1)]
            ev.p90 = amts[int(len(amts) * 0.9)]
            ev.max_damage = amts[-1]
        g = gaps.get(sid)
        if g:
            ev.median_gap = round(st.median(g), 1)
        _flag(ev, boss)
        out.append(ev)
    return out


def _flag(ev: MechanicEvidence, boss: BossConfig) -> None:
    """Where the numbers disagree with how the mechanic is classified."""
    if ev.hits == 0:
        ev.concerns.append(
            "never seen in these logs - the id may be wrong, or the mechanic "
            "may only appear on a difficulty or phase not sampled here"
        )
        return

    expected_tank = (ev.tanks / ev.raid) if ev.raid and ev.tanks else 0.1
    mech = boss.avoidable.get(ev.spell_id)

    if ev.category == "avoidable":
        if ev.share >= UNAVOIDABLE_SHARE and ev.pulls_seen >= 2:
            ev.concerns.append(
                f"hits {ev.victims} of {ev.raid} raiders across {ev.pulls_seen} "
                f"pulls - is it actually avoidable, or unavoidable raid damage?"
            )
        if (
            ev.tank_share >= max(0.35, expected_tank * TANK_FOLLOW_FACTOR)
            and mech is not None
            and not mech.exempt_roles
        ):
            ev.concerns.append(
                f"{ev.tank_share:.0%} of hits land on tanks against "
                f"{expected_tank:.0%} expected - does it follow the threat "
                f"target? consider exempt_roles: [tank]"
            )
        if (
            mech is not None
            and mech.count != COUNT_APPLICATIONS
            and not mech.min_gap_s
            and ev.median_gap is not None
            and ev.median_gap < TICK_GAP_S
        ):
            ev.concerns.append(
                f"hits repeat on the same player every {ev.median_gap}s - this "
                f"ticks, so the count measures time spent rather than mistakes "
                f"made; consider min_gap_s"
            )
        if (
            mech is not None
            and not mech.amount_at_least
            # amount_at_least filters DAMAGE events, and an applications-counted
            # mechanic takes its count from auras -- so suggesting it here would
            # change the damage total and not one thing about the count. A
            # suggestion that does not do what it says is worse than silence.
            and not mech.by_applications
            and ev.p10 > 0
            and ev.p90 / ev.p10 >= BIMODAL_RATIO
        ):
            ev.concerns.append(
                f"damage spreads from {ev.p10:,} to {ev.p90:,} (10th-90th) - "
                f"two components under one id? consider amount_at_least"
            )
    elif ev.category == "tank_buster":
        if ev.victims > max(3, ev.tanks + 1) and ev.tank_share < 0.5:
            ev.concerns.append(
                f"hits {ev.victims} players and only {ev.tank_share:.0%} of "
                f"hits are on tanks - this may not be a tank mechanic"
            )


def render_review(pulls: list[Pull], boss: BossConfig, width: int = 78) -> str:
    rows = review_boss(pulls, boss)
    lines = ["=" * width]
    lines.append(f"CONFIG REVIEW - {boss.display_name}")
    lines.append(
        f"{len(pulls)} pulls"
        + (f" - seeded from {boss.seeded_from}" if boss.seeded_from else "")
    )
    lines.append("=" * width)
    flagged = [r for r in rows if r.concerns]
    lines.append(
        f"{len(rows)} configured mechanics, {len(flagged)} worth a second look"
    )
    lines.append("")

    for r in sorted(rows, key=lambda x: (x.ok, x.category, -x.hits)):
        mark = "  " if r.ok else "->"
        lines.append(f"{mark} {r.name} ({r.spell_id})  [{r.category}]")
        lines.append(f"     config: {r.settings}")
        if r.hits:
            lines.append(
                f"     {r.hits} hits on {r.victims}/{r.raid} raiders over "
                f"{r.pulls_seen} pulls, {r.tank_share:.0%} on tanks"
            )
            if r.filtered:
                lines.append(
                    f"     the config counts {r.counted_hits} of those, on "
                    f"{r.counted_victims}/{r.raid} raiders"
                )
            gap = (
                f", damage ticks every {r.median_gap}s" if r.median_gap else ""
            )
            lines.append(
                f"     damage median {r.median_damage:,}, "
                f"10th-90th {r.p10:,}-{r.p90:,}, max {r.max_damage:,}{gap}"
            )
        for c in r.concerns:
            lines.append(f"     ? {c}")
        lines.append("")

    if not flagged:
        lines.append("Nothing disagrees with the numbers.")
    else:
        lines.append(
            "A '?' is a question, not a verdict - the logs cannot tell an "
            "unavoidable mechanic"
        )
        lines.append(
            "from one the raid simply always fails. Only someone who knows the "
            "fight can."
        )
    return "\n".join(lines)
