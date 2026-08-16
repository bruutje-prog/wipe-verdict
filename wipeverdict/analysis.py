"""Per-pull analysis: avoidable damage, interrupts, dispels, cooldown coverage.

Two rules run through all of it.

Rates are computed against ALIVE time, not fight duration. A player dead for
40% of a pull shows suppressed everything, and comparing that to a player who
survived produces a confident wrong answer.

Absorb shields are never measured by uptime. They end when consumed, so a tank
under heavy melee shows low uptime no matter how well they play. Cast rate
against cooldown is the only valid measure, and findings.assert_metric_valid
enforces it.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from .config import Config, BossConfig
from .logparse import is_player
from .roles import HEALER, TANK, PlayerRole

if TYPE_CHECKING:  # pragma: no cover
    from .pulls import Pull

#: Bucket size when looking for spikes of raid-wide damage.
WINDOW_BUCKET_S = 8.0

#: A raid cooldown cast more than this far after a damage window opened was
#: late rather than planned.
COOLDOWN_LATE_S = 3.0


# ---------------------------------------------------------------------------
# Alive time
# ---------------------------------------------------------------------------

def alive_time(pull: "Pull", guid: str) -> float:
    """Seconds this player spent alive, for honest rate denominators."""
    duration = pull.duration or 0.0
    deaths = sorted(d.t for d in pull.deaths if d.guid == guid)
    if not deaths:
        return duration
    rez = sorted(r.t for r in pull.resurrects if r.guid == guid)
    alive = 0.0
    cursor = 0.0
    dead_from: Optional[float] = None
    for t in deaths:
        if dead_from is None:
            alive += max(0.0, t - cursor)
            dead_from = t
            back = next((r for r in rez if r > t), None)
            if back is None:
                return alive
            cursor = back
            dead_from = None
    alive += max(0.0, duration - cursor)
    return max(0.0, min(alive, duration))


# ---------------------------------------------------------------------------
# Where everyone was standing
# ---------------------------------------------------------------------------

#: How far from the requested moment a position sample may be and still count.
SNAPSHOT_WINDOW_S = 4.0


@dataclass(slots=True)
class Marker:
    name: str
    kind: str          # tank | healer | dps | victim | enemy
    x: float
    y: float
    stale: float       # seconds between the sample and the moment asked for


def position_snapshot(
    pull: "Pull",
    t: float,
    roles: dict[str, PlayerRole],
    victim_guid: Optional[str] = None,
    window: float = SNAPSHOT_WINDOW_S,
) -> list[Marker]:
    """Where every unit was at time `t`.

    Positions are only in the log with Advanced Combat Logging on, so this
    returns nothing without it rather than drawing a misleading empty arena.
    Each marker carries how stale its sample is, because a position from three
    seconds ago is not evidence of where somebody stood when they died.
    """
    bosses = set(pull.boss_names())
    out: list[Marker] = []
    for guid, track in pull.positions.items():
        best: Optional[tuple[float, float, float]] = None
        for sample in track:
            gap = abs(sample[0] - t)
            if gap <= window and (best is None or gap < abs(best[0] - t)):
                best = sample
        if best is None:
            continue

        if guid in pull.players:
            kind = "victim" if guid == victim_guid else (
                roles[guid].role if guid in roles else "dps"
            )
            name = pull.players[guid]
        else:
            name = pull.unit_names.get(guid, "")
            # Only the encounter itself; adds and pets would bury the raid.
            if name not in bosses:
                continue
            kind = "enemy"
        out.append(
            Marker(name=name, kind=kind, x=best[1], y=best[2], stale=abs(best[0] - t))
        )
    return out


# ---------------------------------------------------------------------------
# Avoidable damage
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class AvoidableRow:
    player: str
    guid: str
    role: str
    mechanic: str
    spell_id: int
    count: int
    damage: int
    counted_by: str
    deaths_to_it: int = 0


def avoidable_table(
    pull: "Pull", boss: Optional[BossConfig], roles: dict[str, PlayerRole]
) -> list[AvoidableRow]:
    """Per player, per mechanic: how many hits and how much damage.

    Hits are the leading indicator -- they identify who is about to die before
    they do, which is the whole point of measuring them rather than deaths.
    """
    if boss is None:
        return []

    hits: dict[tuple[str, int], int] = defaultdict(int)
    damage: dict[tuple[str, int], int] = defaultdict(int)
    for d in pull.damage_taken:
        mech = boss.is_avoidable(d.spell_id)
        if mech is None:
            continue
        key = (d.dest_guid, d.spell_id)
        damage[key] += d.amount + d.absorbed
        if not mech.by_applications:
            hits[key] += 1

    # Ticking mechanics are counted by application. Counting ticks would let one
    # pool outweigh every real mistake in the pull.
    for a in pull.auras:
        mech = boss.is_avoidable(a.spell_id)
        if mech is None or not mech.by_applications or not a.applied:
            continue
        hits[(a.dest_guid, a.spell_id)] += 1

    deaths_to: dict[tuple[str, int], int] = defaultdict(int)
    for death in pull.deaths:
        window = [
            d for d in pull.damage_taken
            if d.dest_guid == death.guid and death.t - 2.0 <= d.t <= death.t + 0.5
        ]
        for d in window[-1:]:
            if boss.is_avoidable(d.spell_id):
                deaths_to[(death.guid, d.spell_id)] += 1

    rows: list[AvoidableRow] = []
    for (guid, spell_id), count in hits.items():
        mech = boss.avoidable[spell_id]
        role = roles[guid].role if guid in roles else "dps"
        # Some mechanics cannot be avoided by the role that has to stand there.
        # Counting a frontal breath against the tank holding the boss blames
        # someone for doing their job.
        if mech.exempt(role):
            continue
        rows.append(
            AvoidableRow(
                player=pull.players.get(guid, guid),
                guid=guid,
                role=role,
                mechanic=mech.name,
                spell_id=spell_id,
                count=count,
                damage=damage.get((guid, spell_id), 0),
                counted_by=mech.count,
                deaths_to_it=deaths_to.get((guid, spell_id), 0),
            )
        )
    rows.sort(key=lambda r: (-r.count, -r.damage))
    return rows


# ---------------------------------------------------------------------------
# Interrupts
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class MissedInterrupt:
    t: float
    spell_id: int
    spell_name: str
    caster: str
    verified: bool
    available: list[str] = field(default_factory=list)


def missed_interrupts(
    pull: "Pull", boss: Optional[BossConfig], cfg: Config,
    roles: dict[str, PlayerRole],
) -> list[MissedInterrupt]:
    """Interruptible casts that completed, and who could have stopped them.

    "Available" means alive and off cooldown. The log does not record whether a
    player was in RANGE, so that caveat travels with every finding rather than
    being quietly dropped.
    """
    if boss is None or not boss.interruptible:
        return []

    interrupted_at = [(i.t, i.extra_spell_id) for i in pull.interrupts]

    # Last use of each player's interrupt, to judge availability.
    kickers: dict[str, tuple[str, float]] = {}
    last_used: dict[str, float] = {}
    for c in pull.casts:
        if c.started or not is_player(c.src_guid):
            continue
        ability = cfg.interrupt_abilities.get(c.spell_name.lower())
        if ability is None:
            continue
        kickers[c.src_guid] = (ability.name, ability.cooldown_s)
        last_used[c.src_guid] = max(last_used.get(c.src_guid, -999.0), c.t)

    deaths_by_guid: dict[str, float] = {}
    for d in pull.deaths:
        deaths_by_guid.setdefault(d.guid, d.t)

    out: list[MissedInterrupt] = []
    for c in pull.casts:
        if c.started or not c.hostile:
            continue
        spec = boss.interruptible.get(c.spell_id)
        if spec is None:
            continue
        # A cast that finished within a second of an interrupt on the same
        # spell was stopped, not missed.
        if any(
            abs(c.t - t) <= 1.5 and sid == c.spell_id for t, sid in interrupted_at
        ):
            continue
        available = []
        for guid, (ability, cooldown) in kickers.items():
            died = deaths_by_guid.get(guid)
            if died is not None and died < c.t:
                continue
            used = last_used.get(guid, -999.0)
            if c.t - used >= cooldown:
                available.append(f"{pull.players.get(guid, guid)} ({ability})")
        out.append(
            MissedInterrupt(
                t=c.t,
                spell_id=c.spell_id,
                spell_name=c.spell_name,
                caster=c.src_name,
                verified=spec.verified,
                available=sorted(available)[:5],
            )
        )
    return out


# ---------------------------------------------------------------------------
# Dispels
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class MissedDispel:
    spell_id: int
    spell_name: str
    victim: str
    applied_at: float
    duration: float


def dispellable_ids(pulls: list["Pull"]) -> dict[int, str]:
    """Debuffs this raid has actually dispelled, so the set is evidence-based.

    Deriving it from behaviour avoids maintaining a list of every dispellable
    debuff in the instance, and it cannot claim something is dispellable when
    this raid composition has no way to remove it.
    """
    out: dict[int, str] = {}
    for p in pulls:
        for d in p.dispels:
            if d.extra_spell_id:
                out[d.extra_spell_id] = d.extra_spell_name
    return out


def missed_dispels(
    pull: "Pull", known: dict[int, str], min_duration: float = 4.0
) -> list[MissedDispel]:
    """Dispellable debuffs that ran their full duration on a player."""
    dispelled = {(d.dest_guid, d.extra_spell_id, round(d.t, 1)) for d in pull.dispels}
    dispel_times: dict[tuple[str, int], list[float]] = defaultdict(list)
    for d in pull.dispels:
        dispel_times[(d.dest_guid, d.extra_spell_id)].append(d.t)

    opened: dict[tuple[str, int], float] = {}
    out: list[MissedDispel] = []
    for a in pull.auras:
        if a.spell_id not in known or a.aura_type != "DEBUFF":
            continue
        key = (a.dest_guid, a.spell_id)
        if a.applied:
            opened.setdefault(key, a.t)
            continue
        start = opened.pop(key, None)
        if start is None:
            continue
        duration = a.t - start
        if duration < min_duration:
            continue
        # Removed by a dispel rather than by expiring?
        if any(abs(t - a.t) <= 1.0 for t in dispel_times.get(key, ())):
            continue
        out.append(
            MissedDispel(
                spell_id=a.spell_id,
                spell_name=known[a.spell_id],
                victim=pull.players.get(a.dest_guid, a.dest_name),
                applied_at=start,
                duration=duration,
            )
        )
    out.sort(key=lambda m: -m.duration)
    return out


# ---------------------------------------------------------------------------
# Damage windows and cooldown coverage
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class DamageWindow:
    start: float
    end: float
    damage: int
    covered_by: list[str] = field(default_factory=list)
    late_by: Optional[float] = None

    @property
    def covered(self) -> bool:
        return bool(self.covered_by)


def damage_windows(pull: "Pull", top_n: int = 4) -> list[DamageWindow]:
    """Spikes of raid-wide damage, derived from the pull itself.

    Deriving windows from the log means a new boss needs no scripted timers in
    config to get useful cooldown analysis.
    """
    if pull.duration <= 0:
        return []
    buckets: dict[int, int] = defaultdict(int)
    for d in pull.damage_taken:
        buckets[int(d.t // WINDOW_BUCKET_S)] += d.amount + d.absorbed
    if not buckets:
        return []

    ordered = sorted(buckets.values())
    median = ordered[len(ordered) // 2]
    threshold = max(median * 1.8, 1)

    spikes = sorted(
        (b for b, v in buckets.items() if v >= threshold),
        key=lambda b: -buckets[b],
    )[:top_n]

    windows = [
        DamageWindow(
            start=b * WINDOW_BUCKET_S,
            end=(b + 1) * WINDOW_BUCKET_S,
            damage=buckets[b],
        )
        for b in sorted(spikes)
    ]
    return windows


def cooldown_coverage(
    pull: "Pull", cfg: Config, windows: list[DamageWindow]
) -> list[DamageWindow]:
    """Did raid cooldowns land inside the damage windows, or after them?"""
    casts = [
        c for c in pull.casts
        if not c.started
        and is_player(c.src_guid)
        and c.spell_name.lower() in cfg.raid_cooldowns
    ]
    for w in windows:
        inside = [
            c for c in casts
            if w.start - COOLDOWN_LATE_S <= c.t <= w.end
        ]
        if inside:
            w.covered_by = sorted({f"{c.spell_name} ({c.src_name})" for c in inside})
            first = min(c.t for c in inside)
            w.late_by = max(0.0, first - w.start)
        else:
            after = [c for c in casts if w.end < c.t <= w.end + 10.0]
            if after:
                w.late_by = min(c.t for c in after) - w.start
    return windows


# ---------------------------------------------------------------------------
# Absorbs -- cast rate only, never uptime
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class AbsorbUsage:
    player: str
    guid: str
    ability: str
    casts: int
    available: float
    alive_s: float
    note: str = ""

    @property
    def rate_per_min(self) -> float:
        return self.casts / (self.alive_s / 60.0) if self.alive_s > 0 else 0.0

    @property
    def available_per_min(self) -> float:
        return self.available / (self.alive_s / 60.0) if self.alive_s > 0 else 0.0


def absorb_usage(
    pull: "Pull", cfg: Config, roles: dict[str, PlayerRole]
) -> list[AbsorbUsage]:
    """Cast rate against cooldown for shields that end when consumed."""
    out: list[AbsorbUsage] = []
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for c in pull.casts:
        if c.started or not is_player(c.src_guid):
            continue
        if c.spell_name.lower() in cfg.consumed_absorbs:
            counts[(c.src_guid, c.spell_name)] += 1

    for (guid, ability), n in counts.items():
        definition = cfg.consumed_absorbs[ability.lower()]
        alive = alive_time(pull, guid)
        if definition.cooldown_s <= 0:
            # No real cooldown, so "casts vs available" is meaningless. Say so
            # rather than inventing a denominator.
            out.append(
                AbsorbUsage(
                    player=pull.players.get(guid, guid),
                    guid=guid,
                    ability=ability,
                    casts=n,
                    available=0.0,
                    alive_s=alive,
                    note=definition.note or "no fixed cooldown; rate shown without a target",
                )
            )
            continue
        out.append(
            AbsorbUsage(
                player=pull.players.get(guid, guid),
                guid=guid,
                ability=ability,
                casts=n,
                available=alive / definition.cooldown_s,
                alive_s=alive,
                note=definition.note,
            )
        )
    out.sort(key=lambda a: a.rate_per_min)
    return out
