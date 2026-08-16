"""Turn a pull report into three to five things to actually change.

Ranked by estimated impact on killing the boss, not by size of the number. A
ranked list of twenty is the same as no list, so the cap is enforced.

Every finding states the number behind it and the metric used. Where a naive
alternative exists and was rejected, it says which and why -- that is what lets
a raider who knows their spec catch the tool being wrong, which is how all of
the lessons in this codebase were found in the first place.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from .deaths import (
    SIG_LOOSE_BOSS,
    SIG_MECHANIC,
    SIG_REPRIEVE,
    SIG_TANK_MITIGATION,
)
from .findings import (
    RANK_CONFIG,
    RANK_COOLDOWN,
    RANK_EARLY_DEATH,
    RANK_REPEATED,
    RANK_ROOT_CAUSE,
    RANK_THROUGHPUT,
    Finding,
    rank_findings,
)
from .roles import TANK

if TYPE_CHECKING:  # pragma: no cover
    from .session import PullReport, Session


def build_findings(
    report: "PullReport", session: "Session"
) -> tuple[list[Finding], list[Finding]]:
    """Return (actions for the raid, notes for whoever maintains the config).

    They are separated rather than ranked together because they have different
    audiences. A config note competing for one of five slots either crowds out
    something that lost the pull, or never appears at all.
    """
    out: list[Finding] = []
    out.extend(_root_cause(report))
    out.extend(_repeated(report))
    out.extend(_early_deaths(report, session))
    out.extend(_cooldowns(report, session))
    out.extend(_interrupts_and_dispels(report))
    out.extend(_throughput(report))

    notes = [f for f in out if f.rank_class == RANK_CONFIG]
    actions = [f for f in out if f.rank_class != RANK_CONFIG]
    return rank_findings(actions, cap=5), notes


# ---------------------------------------------------------------------------

def _root_cause(report: "PullReport") -> list[Finding]:
    root = report.verdict.root
    if root is None:
        return []
    pull = report.pull
    at = pull.fmt(root.t)
    cascade = report.verdict.cascade_count
    total_after = len([d for d in report.verdict.deaths if d.order > root.order])

    # Whether the killing blow is something the player could have avoided is a
    # config question, not a guess from the event type. Telling somebody to
    # stop taking damage the config itself lists as unavoidable -- Thok's
    # Deafening Screech hits all 25 raiders thousands of times a pull -- is
    # advice nobody can act on, and it discredits the rest of the list.
    boss = report.boss
    spell_id = root.killing_blow.spell_id if root.killing_blow else -1
    mechanic = boss.is_avoidable(spell_id) if boss else None
    dodgeable = mechanic is not None and not mechanic.exempt(root.role)
    known_boss = boss is not None
    extra_evidence: list[str] = []

    if root.signature == SIG_TANK_MITIGATION:
        action = (
            f"Cover {root.name} on {root.killer} - the tank died to it at {at} "
            f"and the raid lost the pull from there."
        )
    elif root.signature == SIG_LOOSE_BOSS:
        action = (
            f"Re-check the tank swap: {root.name} was killed by melee at {at} "
            f"while not tanking."
        )
    elif dodgeable:
        action = (
            f"Stop {root.name} taking {root.killer} - it was the first death, "
            f"at {at}."
        )
        extra_evidence.append(
            f"{mechanic.name} is configured as avoidable for this boss"
        )
    elif mechanic is not None:
        action = (
            f"Keep {root.name} alive through {root.killer} at {at} - their role "
            f"is exempt from it, so this is a healing or cooldown problem."
        )
        extra_evidence.append(
            f"{mechanic.name} is exempt for {root.role}s in config: they cannot "
            f"avoid it, so it is not a positioning error"
        )
    elif known_boss:
        action = (
            f"{root.name} died to {root.killer} at {at} - it is not avoidable "
            f"damage, so the fix is healing or a cooldown, not positioning."
        )
        extra_evidence.append(
            f"{root.killer} is not in this boss's avoidable list, so nobody is "
            f"being told to move out of it; add it to bosses.yaml if it can "
            f"in fact be dodged"
        )
    else:
        action = f"First death was {root.name} at {at} to {root.killer}."
        extra_evidence.append(
            "no mechanics configured for this boss, so avoidability is unknown"
        )

    evidence = list(root.evidence) + extra_evidence
    if total_after:
        evidence.append(
            f"{cascade} of the {total_after} later deaths were cascade and are "
            f"not attributed to anyone"
        )

    return [
        Finding(
            rank_class=RANK_ROOT_CAUSE,
            score=100.0,
            action=action,
            evidence=evidence,
            method="first non-reprieve death, with the killing blow inferred from "
                   "the last damage event before UNIT_DIED",
            rejected="a flat list of deaths, which counts cascade deaths as "
                     "independent mistakes",
            scope="encounter",
            players=[root.name],
        )
    ]


def _repeated(report: "PullReport") -> list[Finding]:
    out: list[Finding] = []
    raid_size = max(len(report.pull.players), 1)
    suspect: list[str] = []

    for rep in report.repeated:
        if not rep.raid_wide:
            continue

        # If practically everyone is hit in every pull, the likeliest
        # explanation is that the mechanic is not avoidable and the config is
        # wrong -- not that 24 raiders simultaneously cannot move.
        if len(rep.players) / raid_size >= 0.8:
            suspect.append(f"{rep.mechanic} ({len(rep.players)}/{raid_size})")
            continue

        if len(out) >= 2:
            continue
        out.append(
            Finding(
                rank_class=RANK_REPEATED,
                score=float(len(rep.players) * 10 + rep.total_hits),
                action=(
                    f"Re-assign positioning for {rep.mechanic} - {len(rep.players)} "
                    f"players have been hit across {rep.pulls_seen} pulls tonight."
                ),
                evidence=[
                    f"{rep.total_hits} hits total on {', '.join(rep.players[:6])}"
                    + (" and others" if len(rep.players) > 6 else ""),
                    "several players over several pulls: an assignment problem, "
                    "not individual error",
                ],
                method="avoidable hits per player per pull, counted from the "
                       "mechanics config",
                rejected="blaming the individuals hit, which does not fix a "
                         "raid-wide positioning failure",
                scope="raid-wide",
                players=rep.players,
                config_ref=f"bosses.yaml -> avoidable -> {rep.mechanic}",
            )
        )

    # One config note, ranked last. Three separate ones would crowd out the
    # things that actually lost the pull, which is the failure mode the whole
    # cap exists to prevent.
    if suspect:
        out.append(
            Finding(
                rank_class=RANK_CONFIG,
                score=float(len(suspect)),
                action=(
                    "Config check: "
                    + ", ".join(suspect)
                    + " hit almost the whole raid every pull, which usually means "
                    "unavoidable damage rather than a positioning failure."
                ),
                evidence=[
                    "listed as avoidable in bosses.yaml; if that is wrong, remove "
                    "them or these will crowd out real findings every night",
                    "not addressed to the raid - nobody can act on this during a pull",
                ],
                method="share of the raid hit, per mechanic, across tonight's pulls",
                scope="raid-wide",
                config_ref="bosses.yaml -> avoidable",
            )
        )
    return out


def _early_deaths(report: "PullReport", session: "Session") -> list[Finding]:
    """Deaths before the point the raid usually reaches."""
    previous = [
        r for r in session.previous(report.pull)
        if r.pull.difficulty_id == report.pull.difficulty_id and r.pull.duration > 20
    ]
    if not previous:
        return []
    typical = sorted(r.pull.duration for r in previous)
    usual_loss = typical[len(typical) // 2]

    early = [
        d for d in report.verdict.deaths
        if d.blameable and d.t < usual_loss * 0.5
    ]
    if len(early) < 2:
        return []
    names = sorted({d.name for d in early})
    return [
        Finding(
            rank_class=RANK_EARLY_DEATH,
            score=float(len(early)),
            action=(
                f"{len(early)} avoidable deaths landed in the first half of the "
                f"pull - hold cooldowns and positioning through the opening."
            ),
            evidence=[
                f"deaths at {', '.join(report.pull.fmt(d.t) for d in early[:5])}",
                f"the raid usually loses this fight around "
                f"{report.pull.fmt(usual_loss)}",
                "cascade deaths excluded",
            ],
            method="blameable deaths before half the median pull length tonight",
            rejected="raw death count, which is dominated by the wipe itself",
            scope="raid-wide",
            players=names,
        )
    ]


def _cooldowns(report: "PullReport", session: "Session") -> list[Finding]:
    out: list[Finding] = []

    uncovered = [w for w in report.windows if not w.covered]
    if uncovered:
        w = max(uncovered, key=lambda x: x.damage)
        late = ""
        if w.late_by is not None:
            late = f" The next one came {w.late_by:.0f}s after the window opened."
        out.append(
            Finding(
                rank_class=RANK_COOLDOWN,
                score=float(w.damage) / 1_000_000.0,
                action=(
                    f"Put a raid cooldown on the damage spike at "
                    f"{report.pull.fmt(w.start)}.{late}"
                ),
                evidence=[
                    f"{w.damage:,} raid damage taken in "
                    f"{report.pull.fmt(w.start)}-{report.pull.fmt(w.end)}",
                    f"{len([x for x in report.windows if x.covered])} of "
                    f"{len(report.windows)} damage windows were covered",
                ],
                method="damage windows derived from the pull's own raid-damage "
                       "peaks, matched against raid cooldown casts",
                rejected="counting cooldown casts, which cannot tell a cooldown "
                         "used on time from one used late",
                scope="raid-wide",
                config_ref="mechanics.yaml -> raid_cooldowns",
            )
        )

    # Absorbs: cast rate against cooldown ONLY.
    for a in report.absorbs:
        if a.available <= 0 or a.alive_s < 60:
            continue
        if a.casts >= a.available * 0.7:
            continue
        out.append(
            Finding(
                rank_class=RANK_COOLDOWN,
                score=(a.available - a.casts),
                action=(
                    f"{a.player} can press {a.ability} more often - "
                    f"{a.rate_per_min:.1f}/min against {a.available_per_min:.1f} "
                    f"available."
                ),
                evidence=[
                    f"{a.casts} casts in {a.alive_s:.0f}s alive, "
                    f"about {a.available:.0f} were available",
                    "measured against alive time, not fight duration",
                ],
                method="cast rate against cooldown",
                rejected=(
                    "absorb uptime - Guard, Power Word: Shield and Blood Shield "
                    "end when they are consumed, so a tank under heavy melee "
                    "shows low uptime however well they play"
                ),
                scope="individual",
                players=[a.player],
                subject=a.ability,
                config_ref="mechanics.yaml -> consumed_absorbs",
            )
        )
        break  # one absorb item is enough; the cap is precious

    # A tank that died with nothing up is a cooldown finding, not a skill one.
    for d in report.verdict.deaths:
        if d.role != TANK or not d.blameable:
            continue
        if d.externals or d.personals:
            continue
        out.append(
            Finding(
                rank_class=RANK_COOLDOWN,
                score=50.0,
                action=(
                    f"Assign an external to {d.name} - they died at "
                    f"{report.pull.fmt(d.t)} with nothing up."
                ),
                evidence=[
                    f"killed by {d.killer} for "
                    f"{(d.killing_blow.amount + d.killing_blow.absorbed):,}"
                    if d.killing_blow else "killing blow not recorded",
                    "no external and no personal defensive in the 20s before dying",
                ],
                method="defensive casts on the victim in the window before death",
                scope="individual",
                players=[d.name],
            )
        )
        break
    return out


def _interrupts_and_dispels(report: "PullReport") -> list[Finding]:
    out: list[Finding] = []
    if report.interrupts:
        missed = report.interrupts
        spell = missed[0].spell_name
        unverified = any(not m.verified for m in missed)
        evidence = [
            f"{len(missed)} {spell} casts completed",
            f"first at {report.pull.fmt(missed[0].t)}",
        ]
        if missed[0].available:
            evidence.append(
                f"off cooldown at the time: {', '.join(missed[0].available[:4])}"
            )
        evidence.append(
            "availability means alive and off cooldown; the log does not record "
            "whether they were in range"
        )
        if unverified:
            evidence.append(
                "this cast is marked unverified in config - confirm it can "
                "actually be interrupted before acting on it"
            )
        # An unverified interruptible is a guess about the encounter, so it must
        # not outrank findings derived from what actually happened.
        weight = 1.0 if not unverified else 0.3
        out.append(
            Finding(
                rank_class=RANK_COOLDOWN,
                score=float(len(missed)) * weight,
                action=f"Set an interrupt rotation for {spell}.",
                evidence=evidence,
                method="interruptible casts that completed with no SPELL_INTERRUPT "
                       "within 1.5s",
                scope="raid-wide",
                config_ref="bosses.yaml -> interruptible",
            )
        )

    if report.dispels:
        by_spell: dict[str, list] = defaultdict(list)
        for m in report.dispels:
            by_spell[m.spell_name].append(m)
        name, items = max(by_spell.items(), key=lambda kv: len(kv[1]))
        out.append(
            Finding(
                rank_class=RANK_COOLDOWN,
                score=float(len(items)),
                action=f"Cover {name} dispels - {len(items)} ran their full duration.",
                evidence=[
                    f"longest was {items[0].duration:.0f}s on {items[0].victim}",
                    "this debuff is treated as dispellable because the raid "
                    "dispelled it elsewhere tonight",
                ],
                method="debuff applications that expired without a SPELL_DISPEL",
                scope="raid-wide",
            )
        )
    return out


def _throughput(report: "PullReport") -> list[Finding]:
    """Only when the pull was a damage or healing check, never after a wipe."""
    if not report.is_damage_check():
        return []
    pct = report.pull.best_boss_percent()
    return [
        Finding(
            rank_class=RANK_THROUGHPUT,
            score=1.0,
            action=(
                f"This was a damage check, not a survival failure - the boss "
                f"finished at {pct:.1f}% with few deaths."
            ),
            evidence=[
                f"{len(report.verdict.blameable)} blameable deaths in "
                f"{report.pull.fmt(report.pull.duration)}",
                "throughput is ranked last deliberately; it only applies when "
                "the raid survived and still did not finish the boss",
            ],
            method="boss health at wipe against blameable death count",
            scope="raid-wide",
        )
    ]
