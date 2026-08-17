"""A raid session: the pulls so far tonight, and what they say together.

Two things only exist at session level. The pull-over-pull delta, which answers
"we got to 1.1%, what was different"; and repeated-failure detection, which is
what separates an individual mistake from a raid-wide assignment problem. The
brief is explicit that the second must be labelled as such -- the same mechanic
hitting several players across several pulls is not several people being bad.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from .analysis import (
    AbsorbUsage,
    AvoidableRow,
    DamageWindow,
    MissedDispel,
    MissedInterrupt,
    SharedBurst,
    SoakReport,
    absorb_usage,
    avoidable_table,
    cooldown_coverage,
    damage_windows,
    dispellable_ids,
    missed_dispels,
    missed_interrupts,
    shared_bursts,
    soak_report,
)
from .config import BossConfig, Config, load_config
from .deaths import WipeVerdict, verdict
from .findings import Finding
from .pulls import Pull
from .roles import PlayerRole, detect_roles


@dataclass(slots=True)
class PullDelta:
    """How this pull compared with the best previous attempt tonight."""

    compared_to: str
    boss_pct: Optional[float]
    boss_pct_before: Optional[float]
    duration: float
    duration_before: float
    deaths: int
    deaths_before: int
    avoidable_hits: int
    avoidable_hits_before: int
    first_death: Optional[float]
    first_death_before: Optional[float]
    #: hits counted only for mechanics that occurred in BOTH pulls
    avoidable_shared: int = 0
    avoidable_shared_before: int = 0
    #: mechanics that happened in only one of the two pulls
    only_now: list[str] = field(default_factory=list)
    only_before: list[str] = field(default_factory=list)

    @property
    def progressed(self) -> Optional[bool]:
        if self.boss_pct is None or self.boss_pct_before is None:
            return None
        return self.boss_pct < self.boss_pct_before

    def lines(self) -> list[str]:
        out: list[str] = []
        if self.boss_pct is not None and self.boss_pct_before is not None:
            arrow = "better" if self.boss_pct < self.boss_pct_before else "worse"
            out.append(
                f"boss reached {self.boss_pct:.1f}% vs {self.boss_pct_before:.1f}% "
                f"({arrow})"
            )
        out.append(
            f"lasted {self.duration:.0f}s vs {self.duration_before:.0f}s"
        )
        out.append(f"{self.deaths} deaths vs {self.deaths_before}")
        # Only compare mechanics that occurred in both pulls. Thok's breath
        # depends on which captive he drinks, so a raw hit total compares two
        # different fights and reports a difference that nobody caused.
        if self.only_now or self.only_before:
            out.append(
                f"{self.avoidable_shared} avoidable hits vs "
                f"{self.avoidable_shared_before}, counting only mechanics that "
                f"occurred in both pulls"
            )
            if self.only_now:
                out.append(
                    f"only this pull saw: {', '.join(self.only_now[:4])}"
                )
            if self.only_before:
                out.append(
                    f"only the earlier pull saw: {', '.join(self.only_before[:4])}"
                )
        else:
            out.append(
                f"{self.avoidable_hits} avoidable hits vs "
                f"{self.avoidable_hits_before}"
            )
        if self.first_death is not None and self.first_death_before is not None:
            out.append(
                f"first death at {self.first_death:.0f}s vs "
                f"{self.first_death_before:.0f}s"
            )
        return out


@dataclass(slots=True)
class RepeatedFailure:
    mechanic: str
    spell_id: int
    pulls_seen: int
    players: list[str]
    total_hits: int
    #: everyone who raided in the pulls this covers.
    #:
    #: `players` accumulates across several pulls, so with roster swaps it can
    #: exceed the size of any single one -- six Blackfuse pulls produced
    #: "29/25". Comparing an accumulated count against one pull's raid size
    #: compares two different populations, so the denominator has to accumulate
    #: over exactly the same pulls.
    roster: int = 0

    @property
    def share(self) -> float:
        return len(self.players) / self.roster if self.roster else 0.0

    @property
    def raid_wide(self) -> bool:
        """Several players, several pulls: an assignment problem, not individuals."""
        return len(self.players) >= 3 and self.pulls_seen >= 2


@dataclass(slots=True)
class PullReport:
    pull: Pull
    roles: dict[str, PlayerRole]
    verdict: WipeVerdict
    avoidable: list[AvoidableRow] = field(default_factory=list)
    interrupts: list[MissedInterrupt] = field(default_factory=list)
    dispels: list[MissedDispel] = field(default_factory=list)
    windows: list[DamageWindow] = field(default_factory=list)
    absorbs: list[AbsorbUsage] = field(default_factory=list)
    shared: list[SharedBurst] = field(default_factory=list)
    soaks: list[SoakReport] = field(default_factory=list)
    delta: Optional[PullDelta] = None
    repeated: list[RepeatedFailure] = field(default_factory=list)
    #: the boss's mechanic config, so findings can check what is actually
    #: avoidable before telling somebody to stop standing in it
    boss: Optional[BossConfig] = None
    findings: list[Finding] = field(default_factory=list)
    #: config-hygiene observations, addressed to the config owner not the raid
    notes: list[Finding] = field(default_factory=list)

    @property
    def avoidable_hits(self) -> int:
        return sum(r.count for r in self.avoidable)

    def is_damage_check(self) -> bool:
        """Whether the pull failed as a damage/healing check rather than survival.

        Throughput advice is ranked last and only applies here, because telling
        a raid to press harder after a survival failure kills them faster.
        """
        if not self.pull.is_wipe:
            return False
        pct = self.pull.best_boss_percent()
        if pct is None:
            return False
        return pct > 20.0 and len(self.verdict.blameable) <= 2


class Session:
    """Everything seen tonight, keyed by boss."""

    def __init__(self, config: Optional[Config] = None) -> None:
        self.config = config or load_config()
        self.pulls: list[Pull] = []
        self.reports: list[PullReport] = []
        self._by_boss: dict[int, list[PullReport]] = defaultdict(list)

    # -- queries ---------------------------------------------------------
    def previous(self, pull: Pull) -> list[PullReport]:
        return [r for r in self._by_boss.get(pull.encounter_id, []) if r.pull is not pull]

    def best_previous(self, pull: Pull) -> Optional[PullReport]:
        """The attempt to compare against: closest to a kill, else the longest.

        Only attempts at the same difficulty and raid size count. Blending a 10
        and a 25 comparison produces a number that means nothing.
        """
        candidates = [
            r for r in self.previous(pull)
            if r.pull.difficulty_id == pull.difficulty_id
            and r.pull.group_size == pull.group_size
            and r.pull.duration > 20
        ]
        if not candidates:
            return None
        with_pct = [r for r in candidates if r.pull.best_boss_percent() is not None]
        if with_pct:
            return min(with_pct, key=lambda r: r.pull.best_boss_percent())
        return max(candidates, key=lambda r: r.pull.duration)

    # -- ingestion -------------------------------------------------------
    def add(self, pull: Pull) -> PullReport:
        """Analyse a finished pull in the context of the session so far."""
        cfg = self.config
        boss = cfg.boss(pull.encounter_id)
        roles = detect_roles(pull)

        report = PullReport(
            pull=pull,
            roles=roles,
            verdict=verdict(pull, roles),
            avoidable=avoidable_table(pull, boss, roles),
            interrupts=missed_interrupts(pull, boss, cfg, roles),
            absorbs=absorb_usage(pull, cfg, roles),
            shared=shared_bursts(pull, boss),
            soaks=soak_report(pull, boss),
            boss=boss,
        )
        report.windows = cooldown_coverage(pull, cfg, damage_windows(pull))

        known = dispellable_ids(self.pulls + [pull])
        report.dispels = missed_dispels(pull, known)

        report.delta = self._delta(report)
        report.repeated = self._repeated(report)

        # Imported late: recommend depends on this module's types.
        from .recommend import build_findings

        report.findings, report.notes = build_findings(report, self)

        self.pulls.append(pull)
        self.reports.append(report)
        self._by_boss[pull.encounter_id].append(report)
        return report

    # -- internals -------------------------------------------------------
    def _delta(self, report: PullReport) -> Optional[PullDelta]:
        best = self.best_previous(report.pull)
        if best is None:
            return None
        p, q = report.pull, best.pull

        now_by_mech: dict[str, int] = defaultdict(int)
        for row in report.avoidable:
            now_by_mech[row.mechanic] += row.count
        before_by_mech: dict[str, int] = defaultdict(int)
        for row in best.avoidable:
            before_by_mech[row.mechanic] += row.count
        shared = set(now_by_mech) & set(before_by_mech)

        return PullDelta(
            avoidable_shared=sum(now_by_mech[m] for m in shared),
            avoidable_shared_before=sum(before_by_mech[m] for m in shared),
            only_now=sorted(set(now_by_mech) - set(before_by_mech)),
            only_before=sorted(set(before_by_mech) - set(now_by_mech)),
            compared_to=q.label,
            boss_pct=p.best_boss_percent(),
            boss_pct_before=q.best_boss_percent(),
            duration=p.duration,
            duration_before=q.duration,
            deaths=len(p.deaths),
            deaths_before=len(q.deaths),
            avoidable_hits=report.avoidable_hits,
            avoidable_hits_before=best.avoidable_hits,
            first_death=min((d.t for d in p.deaths), default=None),
            first_death_before=min((d.t for d in q.deaths), default=None),
        )

    def _repeated(self, report: PullReport) -> list[RepeatedFailure]:
        history = self.previous(report.pull) + [report]
        history = [
            r for r in history
            if r.pull.difficulty_id == report.pull.difficulty_id
        ]
        by_mech: dict[int, dict] = defaultdict(
            lambda: {
                "name": "", "pulls": set(), "players": set(), "hits": 0,
                "roster": set(),
            }
        )
        for r in history:
            for spell_id in {row.spell_id for row in r.avoidable}:
                # Everyone who raided in a pull this mechanic occurred in --
                # the population the hit count is actually drawn from.
                by_mech[spell_id]["roster"].update(r.pull.players.values())
            for row in r.avoidable:
                slot = by_mech[row.spell_id]
                slot["name"] = row.mechanic
                slot["pulls"].add(id(r.pull))
                slot["players"].add(row.player)
                slot["hits"] += row.count

        out = [
            RepeatedFailure(
                mechanic=slot["name"],
                spell_id=spell_id,
                pulls_seen=len(slot["pulls"]),
                players=sorted(slot["players"]),
                total_hits=slot["hits"],
                roster=len(slot["roster"]),
            )
            for spell_id, slot in by_mech.items()
        ]
        out.sort(key=lambda r: (-len(r.players), -r.total_hits))
        return out
