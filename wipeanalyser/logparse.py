"""Combat log tokenizer for MoP Classic (COMBAT_LOG_VERSION 9, advanced logging).

Layout was established empirically against real Siege of Orgrimmar logs from this
guild rather than from documentation -- see tools/probe_layout.py and
docs/log_format.md. The important structural decision:

    prefix (fixed, from the START)  |  advanced block (VARIABLE)  |  payload (fixed, from the END)

Blizzard adds fields to the advanced block between patches. Indexing the payload
from the end means a patch that grows the advanced block does not silently shift
the damage amount into the "resisted" column, which is the failure mode that
produces confident wrong answers.

Two facts that cost real confusion and are encoded here:

* Players log health as a PERCENTAGE (88/100); creatures log it in absolute
  points (1962590350/1962616500). Only the RATIO is portable, so only the ratio
  is exposed (`hp_fraction`).
* The advanced "info" unit is sometimes the source and sometimes the
  destination. Never assume -- compare `info_guid` against src/dest yourself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Iterator, Optional

# --------------------------------------------------------------------------
# Field-count constants, all verified against real logs.
# --------------------------------------------------------------------------

#: src GUID/name/flags/raidflags + dest GUID/name/flags/raidflags
PREFIX_LEN = 8

#: spellId, spellName, spellSchool
SPELL_PREFIX_LEN = 3

#: amount, base, overkill, school, resisted, blocked, absorbed, critical,
#: glancing, crushing
DAMAGE_SUFFIX_LEN = 10

#: amount, base, overhealing, absorbed, critical
HEAL_SUFFIX_LEN = 5

#: Trailing single-target / area-of-effect marker present on SPELL_* damage but
#: not on SWING_DAMAGE.
_TAIL_TAGS = ("ST", "AOE")

#: Unit-flag reaction bits. 0x514 (a raider) has FRIENDLY set; 0xa48 (a boss)
#: has HOSTILE set.
REACTION_FRIENDLY = 0x10
REACTION_NEUTRAL = 0x20
REACTION_HOSTILE = 0x40


def _hex(fields: list[str], idx: int) -> int:
    try:
        return int(fields[idx], 16)
    except (ValueError, IndexError):
        return 0


_SWING_EVENTS = frozenset(
    {"SWING_DAMAGE", "SWING_DAMAGE_LANDED", "SWING_MISSED"}
)
_ENVIRONMENTAL_EVENTS = frozenset({"ENVIRONMENTAL_DAMAGE"})

DAMAGE_EVENTS = frozenset(
    {
        "SPELL_DAMAGE",
        "SPELL_PERIODIC_DAMAGE",
        "SPELL_BUILDING_DAMAGE",
        "RANGE_DAMAGE",
        "SWING_DAMAGE",
        "ENVIRONMENTAL_DAMAGE",
        "DAMAGE_SPLIT",
    }
)

HEAL_EVENTS = frozenset({"SPELL_HEAL", "SPELL_PERIODIC_HEAL"})

#: Events carrying an advanced-info block. SWING_DAMAGE_LANDED is a duplicate of
#: SWING_DAMAGE used for melee-swing bookkeeping; counting both double-counts
#: damage, so callers should ignore _LANDED.
#: Events whose payload length this module models, so indexing the advanced
#: block from the end is safe. SPELL_CAST_SUCCESS has no payload at all.
POSITION_EVENTS = DAMAGE_EVENTS | HEAL_EVENTS | {"SPELL_CAST_SUCCESS"}

ADVANCED_EVENTS = DAMAGE_EVENTS | HEAL_EVENTS | {
    "SPELL_CAST_SUCCESS",
    "SPELL_ENERGIZE",
    "SPELL_PERIODIC_ENERGIZE",
    "SWING_DAMAGE_LANDED",
}


def split_fields(payload: str) -> list[str]:
    """Split a combat-log payload on commas, respecting double-quoted strings.

    Quotes are stripped. Spell names legitimately contain commas, so a plain
    ``str.split(',')`` silently shifts every later column on those lines.
    """
    if '"' not in payload:
        return payload.split(",")

    out: list[str] = []
    buf: list[str] = []
    in_quotes = False
    for ch in payload:
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == "," and not in_quotes:
            out.append("".join(buf))
            buf.clear()
        else:
            buf.append(ch)
    out.append("".join(buf))
    return out


def parse_timestamp(stamp: str, year_hint: Optional[int] = None) -> datetime:
    """Parse ``8/11/2026 19:56:46.6141``.

    The sub-second part has four digits, which ``%f`` accepts. Some builds omit
    the year, hence ``year_hint``.
    """
    date_part, time_part = stamp.split(" ", 1)
    bits = date_part.split("/")
    if len(bits) == 3:
        month, day, year = int(bits[0]), int(bits[1]), int(bits[2])
        if year < 100:
            year += 2000
    else:
        month, day = int(bits[0]), int(bits[1])
        year = year_hint or datetime.now().year

    hms, _, frac = time_part.partition(".")
    hour, minute, second = (int(x) for x in hms.split(":"))
    micro = int((frac + "000000")[:6]) if frac else 0
    return datetime(year, month, day, hour, minute, second, micro)


@dataclass(slots=True)
class Event:
    """One parsed combat-log line.

    Only the columns the analyses actually use are promoted to attributes; the
    raw field list stays available for anything else.
    """

    ts: datetime
    event: str
    fields: list[str]

    # ---- prefix -------------------------------------------------------
    @property
    def src_guid(self) -> str:
        return self.fields[0] if len(self.fields) > 0 else ""

    @property
    def src_name(self) -> str:
        return self.fields[1] if len(self.fields) > 1 else ""

    @property
    def dest_guid(self) -> str:
        return self.fields[4] if len(self.fields) > 4 else ""

    @property
    def dest_name(self) -> str:
        return self.fields[5] if len(self.fields) > 5 else ""

    @property
    def src_flags(self) -> int:
        return _hex(self.fields, 2)

    @property
    def dest_flags(self) -> int:
        return _hex(self.fields, 6)

    @property
    def src_hostile(self) -> bool:
        """Whether the source is an enemy.

        `not is_player()` is NOT the same thing: Mirror Images, Water
        Elementals and warlock pets are Creature GUIDs on the raid's side, and
        counting their casts as boss casts fills the interruptible list with
        the raid's own pets.
        """
        return bool(self.src_flags & REACTION_HOSTILE)

    @property
    def dest_hostile(self) -> bool:
        return bool(self.dest_flags & REACTION_HOSTILE)

    # ---- spell --------------------------------------------------------
    @property
    def has_spell_prefix(self) -> bool:
        return not (
            self.event in _SWING_EVENTS or self.event in _ENVIRONMENTAL_EVENTS
        )

    @property
    def spell_id(self) -> int:
        if not self.has_spell_prefix:
            return 0
        try:
            return int(self.fields[PREFIX_LEN])
        except (ValueError, IndexError):
            return 0

    @property
    def spell_name(self) -> str:
        if not self.has_spell_prefix:
            return "Melee"
        try:
            return self.fields[PREFIX_LEN + 1]
        except IndexError:
            return ""

    # ---- variable middle ----------------------------------------------
    def _body(self) -> list[str]:
        """Fields between the prefix (incl. spell prefix) and the payload."""
        start = PREFIX_LEN + (SPELL_PREFIX_LEN if self.has_spell_prefix else 0)
        end = len(self.fields)
        if self.fields and self.fields[-1] in _TAIL_TAGS:
            end -= 1
        if self.event in DAMAGE_EVENTS or self.event == "SWING_DAMAGE_LANDED":
            end -= DAMAGE_SUFFIX_LEN
        elif self.event in HEAL_EVENTS:
            end -= HEAL_SUFFIX_LEN
        return self.fields[start:end] if end > start else []

    def _suffix(self) -> list[str]:
        end = len(self.fields)
        if self.fields and self.fields[-1] in _TAIL_TAGS:
            end -= 1
        if self.event in DAMAGE_EVENTS or self.event == "SWING_DAMAGE_LANDED":
            return self.fields[end - DAMAGE_SUFFIX_LEN : end]
        if self.event in HEAL_EVENTS:
            return self.fields[end - HEAL_SUFFIX_LEN : end]
        return []

    @property
    def info_guid(self) -> str:
        """GUID the advanced block describes -- may be the source OR the dest."""
        body = self._body()
        return body[0] if body else ""

    @property
    def position(self) -> Optional[tuple[float, float]]:
        """World (x, y) of `info_guid`, or None.

        Present only with Advanced Combat Logging. With it off the columns are
        zero-filled, and 0,0 is not a real arena coordinate, so it is rejected.

        Restricted to events whose trailing payload length is known. Position
        sits at the END of the advanced block, so on an event with an unmodelled
        payload -- SPELL_ENERGIZE, for one -- the same offsets read whatever
        happens to be there and produce coordinates like (562.0, 0.0) sitting
        next to a real arena at (1226, -5052).
        """
        if self.event not in POSITION_EVENTS:
            return None
        body = self._body()
        if len(body) < 6:
            return None
        try:
            x = float(body[-5])
            y = float(body[-4])
        except ValueError:
            return None
        if x == 0.0 and y == 0.0:
            return None
        return x, y

    @property
    def hp_values(self) -> Optional[tuple[float, float]]:
        """Raw (current, max) health of `info_guid`.

        Units differ by unit type -- players report a percentage (88/100),
        creatures report absolute points. Only compare like with like; the
        absolute maximum is still useful for telling a boss from an add.
        """
        body = self._body()
        if len(body) < 4:
            return None
        try:
            current = float(body[2])
            maximum = float(body[3])
        except ValueError:
            return None
        if maximum <= 0:
            return None
        return current, maximum

    @property
    def hp_fraction(self) -> Optional[float]:
        """Health of `info_guid` as 0.0-1.0, or None.

        Players report percent and creatures report absolute points, so the
        ratio is the only value that means the same thing for both.
        """
        values = self.hp_values
        if values is None:
            return None
        current, maximum = values
        return max(0.0, min(1.0, current / maximum))

    # ---- payload -------------------------------------------------------
    @property
    def amount(self) -> int:
        suffix = self._suffix()
        if not suffix:
            return 0
        try:
            return int(suffix[0])
        except ValueError:
            return 0

    @property
    def overkill(self) -> int:
        """Damage beyond the target's remaining health; -1 when not a killing blow."""
        suffix = self._suffix()
        if len(suffix) < 3 or self.event not in DAMAGE_EVENTS:
            return -1
        try:
            return int(suffix[2])
        except ValueError:
            return -1

    @property
    def absorbed(self) -> int:
        suffix = self._suffix()
        idx = 6 if self.event in DAMAGE_EVENTS else 3
        if len(suffix) <= idx:
            return 0
        try:
            return int(suffix[idx])
        except ValueError:
            return 0

    @property
    def overhealing(self) -> int:
        suffix = self._suffix()
        if self.event not in HEAL_EVENTS or len(suffix) < 3:
            return 0
        try:
            return int(suffix[2])
        except ValueError:
            return 0

    @property
    def effective_heal(self) -> int:
        return max(0, self.amount - self.overhealing)


def is_player(guid: str) -> bool:
    return guid.startswith("Player-")


def is_pet(guid: str) -> bool:
    return guid.startswith(("Pet-", "Vehicle-"))


def iter_events(
    lines: Iterable[str], year_hint: Optional[int] = None
) -> Iterator[Event]:
    """Parse an iterable of raw log lines into Events, skipping malformed ones."""
    for line in lines:
        ev = parse_line(line, year_hint)
        if ev is not None:
            yield ev


def parse_line(line: str, year_hint: Optional[int] = None) -> Optional[Event]:
    """Parse one raw line, or return None if it is not a usable event."""
    sep = line.find("  ")
    if sep < 0:
        return None
    stamp = line[:sep]
    payload = line[sep + 2 :].rstrip("\r\n")
    if not payload:
        return None
    try:
        ts = parse_timestamp(stamp, year_hint)
    except (ValueError, IndexError):
        return None
    fields = split_fields(payload)
    return Event(ts=ts, event=fields[0], fields=fields[1:])
