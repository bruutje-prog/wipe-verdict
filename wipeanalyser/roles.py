"""Role and spec detection, per pull.

Players swap spec between fights, so role is derived from what someone actually
did in *this* pull rather than from a roster. Never blend two specs into one
average -- that is one of the brief's hard-won lessons, and it starts here.

Detection is deliberately output-driven first (what did this player produce and
absorb) and signature-ability second. A pure ability list misfires on hybrids:
a feral druid casts Healing Touch, and a resto druid casts Mangle.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from .logparse import is_player

if TYPE_CHECKING:  # pragma: no cover
    from .pulls import Pull

TANK = "tank"
HEALER = "healer"
DPS = "dps"

#: Abilities only a tanking spec presses. Used as corroboration, not proof.
TANK_SIGNATURES = {
    "guard",                 # Brewmaster monk
    "elusive brew",
    "dizzying haze",
    "death strike",          # Blood death knight
    "bone shield",
    "dancing rune weapon",
    "dark command",
    "vengeance",
    "shield slam",           # Protection warrior
    "shield barrier",
    "shield block",
    "demoralizing shout",
    "avenger's shield",      # Protection paladin
    "shield of the righteous",
    "sacred shield",
    "savage defense",        # Guardian druid
    "frenzied regeneration",
    "mangle",
}

#: Abilities that only a healing spec presses (excluding hybrid filler heals
#: like Healing Touch, which every druid has).
HEALER_SIGNATURES = {
    "chain heal",            # Restoration shaman
    "healing tide totem",
    "riptide",
    "earth shield",
    "healing rain",
    "circle of healing",     # Holy priest
    "divine hymn",
    "holy word: sanctuary",
    "lightwell",
    "prayer of healing",
    "penance",               # Discipline priest
    "power word: barrier",
    "archangel",
    "wild growth",           # Restoration druid
    "swiftmend",
    "tranquility",
    "efflorescence",
    "holy radiance",         # Holy paladin
    "light of dawn",
    "beacon of light",
    "uplift",                # Mistweaver monk
    "soothing mist",
    "renewing mist",
}


@dataclass(slots=True)
class PlayerRole:
    guid: str
    name: str
    role: str
    #: 0.0-1.0 confidence, so a caller can decline to blame on a weak call
    confidence: float
    #: the numbers behind the call, for "show your working"
    evidence: list[str]
    melee_taken: int = 0
    healing_done: int = 0
    damage_done: int = 0


def detect_roles(pull: "Pull") -> dict[str, PlayerRole]:
    """Classify every player in the pull as tank, healer or dps."""
    melee_taken: dict[str, int] = defaultdict(int)
    melee_hits: dict[str, int] = defaultdict(int)
    melee_first: dict[str, float] = {}
    melee_last: dict[str, float] = {}
    for d in pull.damage_taken:
        # Boss melee is the tank signal: only the threat target eats it.
        if d.spell_id == 0 and not is_player(d.src_guid):
            melee_taken[d.dest_guid] += d.amount + d.absorbed
            melee_hits[d.dest_guid] += 1
            melee_first.setdefault(d.dest_guid, d.t)
            melee_last[d.dest_guid] = d.t

    healing_done: dict[str, int] = defaultdict(int)
    for h in pull.heals:
        if is_player(h.src_guid):
            healing_done[h.src_guid] += h.effective
    # Absorbs are healing output. A discipline priest whose absorbs are ignored
    # looks like a dps who occasionally casts Penance.
    absorbed_done: dict[str, int] = defaultdict(int)
    for a in pull.absorbs:
        absorbed_done[a.absorber_guid] += a.amount
        healing_done[a.absorber_guid] += a.amount

    cast_names: dict[str, set[str]] = defaultdict(set)
    for c in pull.casts:
        if is_player(c.src_guid) and not c.started:
            cast_names[c.src_guid].add(c.spell_name.lower())

    max_melee = max(melee_taken.values(), default=0)

    roles: dict[str, PlayerRole] = {}
    for guid, name in pull.players.items():
        names = cast_names.get(guid, set())
        melee = melee_taken.get(guid, 0)
        healed = healing_done.get(guid, 0)
        absorbed = absorbed_done.get(guid, 0)
        damage = pull.damage_done.get(guid, 0)

        tank_hits = names & TANK_SIGNATURES
        heal_hits = names & HEALER_SIGNATURES

        evidence: list[str] = []
        role = DPS
        confidence = 0.5

        # A tank is the unit the boss is hitting. That is a positional fact, not
        # a spec guess, so it outranks ability lists.
        #
        # But melee TOTAL alone is not enough. When a boss gets loose it kills
        # several people with melee in a few seconds, and those victims can
        # out-total a tank on damage taken. Misreading one of them as a tank
        # would disqualify them from cascade detection and blame them for the
        # wipe they were a casualty of. A real tank is hit repeatedly, spread
        # across the pull.
        melee_share = melee / max_melee if max_melee > 0 else 0.0
        span = melee_last.get(guid, 0.0) - melee_first.get(guid, 0.0)
        hits = melee_hits.get(guid, 0)
        sustained = hits >= 10 and span >= 0.15 * max(pull.duration, 1.0)

        if len(tank_hits) >= 2:
            # Several tanking abilities means a tanking SPEC, and a tanking
            # spec is not played as dps in a raid. This has to outrank melee
            # share: an off-tank's share of the boss's attention swings with
            # the swap order, and Nodory -- a protection paladin pressing
            # Shield of the Righteous, Avenger's Shield and Sacred Shield in
            # all nine pulls of a night -- flipped to "dps" on the one pull
            # where that share landed at 24.6% instead of 25%. Role decides who
            # is exempt from blame, so it must not hinge on half a percent.
            role, confidence = TANK, 0.9
            evidence.append(
                f"casts {len(tank_hits)} tanking abilities: "
                f"{', '.join(sorted(tank_hits)[:3])}"
            )
            evidence.append(
                f"took {melee:,} boss melee ({melee_share:.0%} of the top tank)"
            )
        elif melee_share >= 0.25 and tank_hits:
            role, confidence = TANK, 0.95
            evidence.append(f"took {melee:,} boss melee ({melee_share:.0%} of top tank)")
            evidence.append(f"tank abilities: {', '.join(sorted(tank_hits)[:3])}")
        elif melee_share >= 0.5 and sustained:
            role, confidence = TANK, 0.7
            evidence.append(
                f"took {melee:,} boss melee over {hits} hits spanning {span:.0f}s"
            )
            evidence.append("no tank-signature ability seen")
        elif heal_hits and healed > damage:
            role, confidence = HEALER, 0.9
            share = f" ({absorbed:,} of it absorbs)" if absorbed else ""
            evidence.append(f"healed {healed:,}{share} vs {damage:,} damage done")
            evidence.append(f"healer abilities: {', '.join(sorted(heal_hits)[:3])}")
        else:
            # Hybrids muddy ability lists: an elemental shaman drops Healing
            # Tide and a feral druid casts Healing Touch. Output decides.
            evidence.append(f"dealt {damage:,} damage, healed {healed:,}")
            if heal_hits and healed > 0:
                evidence.append(
                    "casts healer abilities but deals more damage than healing "
                    "- counted as dps"
                )

        roles[guid] = PlayerRole(
            guid=guid,
            name=name,
            role=role,
            confidence=confidence,
            evidence=evidence,
            melee_taken=melee,
            healing_done=healed,
            damage_done=damage,
        )
    return roles


def tanks(roles: dict[str, PlayerRole]) -> list[PlayerRole]:
    return [r for r in roles.values() if r.role == TANK]


def role_of(roles: dict[str, PlayerRole], guid: str) -> str:
    r = roles.get(guid)
    return r.role if r else DPS


def summarise(roles: dict[str, PlayerRole]) -> str:
    counts: dict[str, int] = defaultdict(int)
    for r in roles.values():
        counts[r.role] += 1
    return (
        f"{counts[TANK]} tanks, {counts[HEALER]} healers, {counts[DPS]} dps"
    )
