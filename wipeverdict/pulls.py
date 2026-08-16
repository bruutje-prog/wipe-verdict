"""Segment a combat-log stream into pulls (attempts) on ENCOUNTER_START/END.

A `Pull` does not retain raw Event objects. A 25-player progression night is
hundreds of megabytes and holding ~40 strings per line would cost more memory
than the machine can spare mid-raid. Instead events are converted to compact
records as they stream past, which is also what makes the live path viable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Iterator, Optional

from .logparse import (
    DAMAGE_EVENTS,
    HEAL_EVENTS,
    Event,
    is_pet,
    is_player,
    iter_events,
)

#: Seconds between retained position samples per unit.
POSITION_SAMPLE_S = 0.5

DIFFICULTY_NAMES = {
    1: "Normal",
    2: "Heroic",
    3: "10 Normal",
    4: "25 Normal",
    5: "10 Heroic",
    6: "25 Heroic",
    7: "LFR",
    14: "Normal",
    15: "Heroic",
    16: "Mythic",
    17: "LFR",
}


@dataclass(slots=True)
class DamageRecord:
    t: float                 # seconds since pull start
    dest_guid: str
    dest_name: str
    src_guid: str
    src_name: str
    spell_id: int
    spell_name: str
    amount: int
    absorbed: int
    overkill: int
    periodic: bool
    hp_after: Optional[float]   # victim health fraction, when this event reports it


@dataclass(slots=True)
class HealRecord:
    t: float
    dest_guid: str
    src_guid: str
    src_name: str
    spell_id: int
    spell_name: str
    effective: int


@dataclass(slots=True)
class AbsorbRecord:
    """Damage prevented by a shield.

    Absorbs are NOT logged as SPELL_HEAL. Discarding them makes a discipline
    priest look like they healed nothing, because most of their output is
    Power Word: Shield and Divine Aegis.
    """

    t: float
    absorber_guid: str
    absorber_name: str
    victim_guid: str
    spell_id: int
    spell_name: str
    amount: int


@dataclass(slots=True)
class CastRecord:
    t: float
    src_guid: str
    src_name: str
    dest_guid: str
    spell_id: int
    spell_name: str
    started: bool            # True for SPELL_CAST_START, False for _SUCCESS
    hostile: bool = False    # an enemy cast, not one of the raid's own pets


@dataclass(slots=True)
class AuraRecord:
    t: float
    dest_guid: str
    dest_name: str
    src_guid: str
    spell_id: int
    spell_name: str
    aura_type: str           # BUFF / DEBUFF
    applied: bool


@dataclass(slots=True)
class DeathRecord:
    t: float
    guid: str
    name: str


@dataclass(slots=True)
class InterruptRecord:
    t: float
    src_guid: str
    src_name: str
    dest_guid: str
    extra_spell_id: int
    extra_spell_name: str


@dataclass(slots=True)
class DispelRecord:
    t: float
    src_guid: str
    src_name: str
    dest_guid: str
    extra_spell_id: int
    extra_spell_name: str


@dataclass(slots=True)
class Pull:
    """One attempt at an encounter."""

    encounter_id: int
    boss: str
    difficulty_id: int
    group_size: int
    start: datetime
    end: Optional[datetime] = None
    success: bool = False
    #: index of this attempt on this boss within the session, 1-based
    attempt: int = 0

    damage_taken: list[DamageRecord] = field(default_factory=list)
    heals: list[HealRecord] = field(default_factory=list)
    absorbs: list[AbsorbRecord] = field(default_factory=list)
    casts: list[CastRecord] = field(default_factory=list)
    auras: list[AuraRecord] = field(default_factory=list)
    deaths: list[DeathRecord] = field(default_factory=list)
    interrupts: list[InterruptRecord] = field(default_factory=list)
    dispels: list[DispelRecord] = field(default_factory=list)
    resurrects: list[DeathRecord] = field(default_factory=list)

    #: player GUID -> display name (short, realm stripped)
    players: dict[str, str] = field(default_factory=dict)
    #: enemy name -> [lowest health fraction seen, largest max-health seen]
    enemy_hp: dict[str, list[float]] = field(default_factory=dict)
    #: damage dealt by each player, for throughput checks only
    damage_done: dict[str, int] = field(default_factory=dict)
    #: player GUID -> set of spell ids cast, used for spec detection
    cast_ids: dict[str, set[int]] = field(default_factory=dict)
    #: unit GUID -> [(t, x, y)], downsampled. Enough to reconstruct where
    #: everyone was standing at the moment of a death.
    positions: dict[str, list[tuple[float, float, float]]] = field(
        default_factory=dict
    )
    #: GUID -> display name for non-player units that have positions
    unit_names: dict[str, str] = field(default_factory=dict)

    @property
    def difficulty(self) -> str:
        return DIFFICULTY_NAMES.get(self.difficulty_id, f"diff{self.difficulty_id}")

    @property
    def duration(self) -> float:
        if self.end is None:
            return 0.0
        return (self.end - self.start).total_seconds()

    @property
    def is_wipe(self) -> bool:
        return not self.success

    @property
    def label(self) -> str:
        return f"{self.boss} ({self.difficulty}) pull {self.attempt}"

    def boss_names(self) -> list[str]:
        """Enemies big enough to be the encounter itself, not its adds.

        Adds die, so tracking the lowest health of *any* enemy reports 0% on
        every wipe. A boss is taken to be an enemy whose maximum health is
        within half of the largest seen, which keeps both halves of a two-boss
        encounter (Dark Shaman) and drops slimes and totems.
        """
        if not self.enemy_hp:
            return []
        biggest = max(v[1] for v in self.enemy_hp.values())
        if biggest <= 0:
            return []
        return sorted(n for n, v in self.enemy_hp.items() if v[1] >= 0.5 * biggest)

    def best_boss_percent(self) -> Optional[float]:
        """Lowest boss health reached, as a percentage -- the 'we got it to 1.1%' number.

        With two bosses the honest figure is the one furthest from dead, since
        the encounter is not over until both are.
        """
        names = self.boss_names()
        if not names:
            return None
        return max(self.enemy_hp[n][0] for n in names) * 100.0

    def offset(self, ts: datetime) -> float:
        return (ts - self.start).total_seconds()

    def fmt(self, t: float) -> str:
        return f"{int(t) // 60}:{int(t) % 60:02d}"


#: Events whose fields are NOT the standard src/dest prefix. Treating these as
#: prefixed events corrupts the roster.
_NO_PREFIX_EVENTS = frozenset(
    {
        "COMBATANT_INFO",
        "ENCOUNTER_START",
        "ENCOUNTER_END",
        "ZONE_CHANGE",
        "MAP_CHANGE",
        "COMBAT_LOG_VERSION",
        "WORLD_MARKER_PLACED",
        "WORLD_MARKER_REMOVED",
    }
)


def short_name(name: str) -> str:
    """`Bruutjeh-MirageRaceway-EU` -> `Bruutjeh`."""
    return name.split("-", 1)[0] if name else name


def _register(pull: "Pull", guid: str, raw_name: str) -> None:
    """Record a player's display name, rejecting placeholders."""
    if not raw_name or raw_name == "nil" or raw_name.isdigit():
        return
    pull.players.setdefault(guid, short_name(raw_name))


class PullSegmenter:
    """Feed events in; get completed Pulls out.

    Works identically on a static file and on a live tail, which is what lets
    milestones 1-5 be developed offline and reused unchanged by milestone 6.
    """

    def __init__(self, track_enemy_hp: bool = True) -> None:
        self.current: Optional[Pull] = None
        self.completed: list[Pull] = []
        self.track_enemy_hp = track_enemy_hp
        self._attempts: dict[tuple[int, int], int] = {}

    # -- ingestion ------------------------------------------------------
    def feed(self, ev: Event) -> Optional[Pull]:
        """Consume one event. Returns a Pull when one has just ended."""
        if ev.event == "ENCOUNTER_START":
            self._start(ev)
            return None
        if ev.event == "ENCOUNTER_END":
            return self._end(ev)
        if self.current is not None:
            self._record(ev)
        return None

    def _start(self, ev: Event) -> None:
        try:
            encounter_id = int(ev.fields[0])
            difficulty_id = int(ev.fields[2])
            group_size = int(ev.fields[3])
        except (ValueError, IndexError):
            return
        key = (encounter_id, difficulty_id)
        self._attempts[key] = self._attempts.get(key, 0) + 1
        self.current = Pull(
            encounter_id=encounter_id,
            boss=ev.fields[1],
            difficulty_id=difficulty_id,
            group_size=group_size,
            start=ev.ts,
            attempt=self._attempts[key],
        )

    def _end(self, ev: Event) -> Optional[Pull]:
        pull = self.current
        if pull is None:
            return None
        pull.end = ev.ts
        try:
            pull.success = ev.fields[4] == "1"
        except IndexError:
            pull.success = False
        self.current = None
        self.completed.append(pull)
        return pull

    # -- per-event extraction -------------------------------------------
    def _record(self, ev: Event) -> None:
        pull = self.current
        assert pull is not None
        etype = ev.event

        # Track enemy health from any event that reports it, before filtering.
        if self.track_enemy_hp and ev.info_guid and not is_player(ev.info_guid):
            values = ev.hp_values
            if values is not None:
                current, maximum = values
                # The advanced info unit is sometimes the source and sometimes
                # the destination -- and sometimes NEITHER. Falling back to the
                # source's name in that third case files another unit's health
                # under the boss, which reported a 1:16 Thok wipe as reaching
                # 6.5% when it actually reached 61.8%. If it cannot be
                # attributed, it is not recorded.
                if ev.info_guid == ev.dest_guid:
                    name = ev.dest_name
                elif ev.info_guid == ev.src_guid:
                    name = ev.src_name
                else:
                    name = ""
                if name and name != "nil":
                    frac = max(0.0, min(1.0, current / maximum))
                    slot = pull.enemy_hp.get(name)
                    if slot is None:
                        pull.enemy_hp[name] = [frac, maximum]
                    else:
                        if frac < slot[0]:
                            slot[0] = frac
                        if maximum > slot[1]:
                            slot[1] = maximum

        # COMBATANT_INFO leads with a player GUID followed by FACTION, not a
        # name, so it looks exactly like a normal prefix and silently registers
        # every player in the raid as "0".
        if etype in _NO_PREFIX_EVENTS:
            return

        src_is_player = is_player(ev.src_guid)
        dest_is_player = is_player(ev.dest_guid)
        if src_is_player:
            _register(pull, ev.src_guid, ev.src_name)
        if dest_is_player:
            _register(pull, ev.dest_guid, ev.dest_name)

        t = pull.offset(ev.ts)

        # Position history, downsampled hard. Every event carries coordinates,
        # and keeping them all would cost more memory than the whole rest of
        # the pull; one sample every POSITION_SAMPLE_S per unit is plenty to
        # answer "where was everyone standing when this happened".
        info = ev.info_guid
        if info and info != "0000000000000000":
            spot = ev.position
            if spot is not None:
                track = pull.positions.get(info)
                if track is None:
                    pull.positions[info] = [(t, spot[0], spot[1])]
                    if not is_player(info):
                        name = (
                            ev.dest_name if info == ev.dest_guid else ev.src_name
                        )
                        if name and name != "nil":
                            pull.unit_names[info] = name
                elif t - track[-1][0] >= POSITION_SAMPLE_S:
                    track.append((t, spot[0], spot[1]))

        # SWING_DAMAGE_LANDED duplicates SWING_DAMAGE. Counting both doubles
        # every melee hit -- the same trap as Warcraft Logs counting channel
        # ticks as casts.
        if etype in DAMAGE_EVENTS:
            if dest_is_player:
                pull.damage_taken.append(
                    DamageRecord(
                        t=t,
                        dest_guid=ev.dest_guid,
                        dest_name=short_name(ev.dest_name),
                        src_guid=ev.src_guid,
                        src_name=ev.src_name,
                        spell_id=ev.spell_id,
                        spell_name=ev.spell_name,
                        amount=ev.amount,
                        absorbed=ev.absorbed,
                        overkill=ev.overkill,
                        periodic=etype == "SPELL_PERIODIC_DAMAGE",
                        hp_after=(
                            ev.hp_fraction
                            if ev.info_guid == ev.dest_guid
                            else None
                        ),
                    )
                )
            elif src_is_player or is_pet(ev.src_guid):
                pull.damage_done[ev.src_guid] = (
                    pull.damage_done.get(ev.src_guid, 0) + ev.amount
                )
            return

        if etype in HEAL_EVENTS:
            if dest_is_player:
                eff = ev.effective_heal
                if eff > 0:
                    pull.heals.append(
                        HealRecord(
                            t=t,
                            dest_guid=ev.dest_guid,
                            src_guid=ev.src_guid,
                            src_name=short_name(ev.src_name),
                            spell_id=ev.spell_id,
                            spell_name=ev.spell_name,
                            effective=eff,
                        )
                    )
            return

        if etype == "SPELL_ABSORBED":
            # Two variants exist: with and without the attacking spell's
            # prefix. The absorber block is a fixed distance from the END, so
            # index from there and both variants parse identically.
            f = ev.fields
            if len(f) >= 9 and is_player(f[-9]):
                try:
                    amount = int(f[-2])
                    spell_id = int(f[-5])
                except ValueError:
                    return
                _register(pull, f[-9], f[-8])
                pull.absorbs.append(
                    AbsorbRecord(
                        t=t,
                        absorber_guid=f[-9],
                        absorber_name=short_name(f[-8]),
                        victim_guid=ev.dest_guid,
                        spell_id=spell_id,
                        spell_name=f[-4],
                        amount=amount,
                    )
                )
            return

        if etype == "UNIT_DIED":
            if dest_is_player:
                pull.deaths.append(
                    DeathRecord(t=t, guid=ev.dest_guid, name=short_name(ev.dest_name))
                )
            return

        if etype in ("SPELL_CAST_SUCCESS", "SPELL_CAST_START"):
            started = etype == "SPELL_CAST_START"
            pull.casts.append(
                CastRecord(
                    t=t,
                    src_guid=ev.src_guid,
                    src_name=short_name(ev.src_name),
                    dest_guid=ev.dest_guid,
                    spell_id=ev.spell_id,
                    spell_name=ev.spell_name,
                    started=started,
                    hostile=ev.src_hostile,
                )
            )
            if src_is_player and not started:
                pull.cast_ids.setdefault(ev.src_guid, set()).add(ev.spell_id)
            return

        if etype in (
            "SPELL_AURA_APPLIED",
            "SPELL_AURA_REMOVED",
            "SPELL_AURA_REFRESH",
        ):
            aura_type = ev.fields[11] if len(ev.fields) > 11 else "BUFF"
            pull.auras.append(
                AuraRecord(
                    t=t,
                    dest_guid=ev.dest_guid,
                    dest_name=short_name(ev.dest_name),
                    src_guid=ev.src_guid,
                    spell_id=ev.spell_id,
                    spell_name=ev.spell_name,
                    aura_type=aura_type,
                    applied=etype != "SPELL_AURA_REMOVED",
                )
            )
            return

        if etype == "SPELL_INTERRUPT":
            extra_id, extra_name = _extra_spell(ev)
            pull.interrupts.append(
                InterruptRecord(
                    t=t,
                    src_guid=ev.src_guid,
                    src_name=short_name(ev.src_name),
                    dest_guid=ev.dest_guid,
                    extra_spell_id=extra_id,
                    extra_spell_name=extra_name,
                )
            )
            return

        if etype == "SPELL_DISPEL":
            extra_id, extra_name = _extra_spell(ev)
            pull.dispels.append(
                DispelRecord(
                    t=t,
                    src_guid=ev.src_guid,
                    src_name=short_name(ev.src_name),
                    dest_guid=ev.dest_guid,
                    extra_spell_id=extra_id,
                    extra_spell_name=extra_name,
                )
            )
            return

        if etype == "SPELL_RESURRECT":
            pull.resurrects.append(
                DeathRecord(t=t, guid=ev.dest_guid, name=short_name(ev.dest_name))
            )


def _extra_spell(ev: Event) -> tuple[int, str]:
    """The interrupted/dispelled spell, which follows the casting spell."""
    base = 8 + 3
    try:
        return int(ev.fields[base]), ev.fields[base + 1]
    except (ValueError, IndexError):
        return 0, ""


def read_pulls(
    path: str,
    encounter_id: Optional[int] = None,
    progress_every: int = 0,
) -> list[Pull]:
    """Parse a whole log file into pulls.

    `encounter_id` restricts work to one boss, which matters when the file is
    half a gigabyte.
    """
    seg = PullSegmenter()
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for n, line in enumerate(fh, 1):
            if progress_every and n % progress_every == 0:
                print(f"  ...{n:,} lines", flush=True)
            # Cheap pre-filter: skip whole lines when we are between pulls and
            # the line is not an encounter boundary.
            if seg.current is None and "ENCOUNTER_START" not in line:
                continue
            ev = _parse(line)
            if ev is None:
                continue
            if (
                encounter_id is not None
                and ev.event == "ENCOUNTER_START"
                and _int0(ev.fields) != encounter_id
            ):
                # Not the boss we care about: consume its events cheaply.
                seg.current = None
                continue
            seg.feed(ev)
    return seg.completed


def _int0(fields: list[str]) -> int:
    try:
        return int(fields[0])
    except (ValueError, IndexError):
        return -1


def _parse(line: str) -> Optional[Event]:
    from .logparse import parse_line

    return parse_line(line)


def iter_pulls_from_events(events: Iterable[Event]) -> Iterator[Pull]:
    seg = PullSegmenter()
    for ev in events:
        pull = seg.feed(ev)
        if pull is not None:
            yield pull
