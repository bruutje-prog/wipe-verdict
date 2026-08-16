"""Death reconstruction and cascade classification.

The brief is emphatic and it is right: a flat list of deaths produces badly
wrong conclusions, because deaths cascade. When a tank dies the boss reaches
whoever is next on threat, and the people it then kills were not making a
mistake -- they were standing where the fight told them to stand.

This module exists to make sure the tool never says otherwise. Cascade deaths
are identified, labelled, and excluded from blame. Every classification carries
the evidence that produced it so it can be argued with.

UNIT_DIED carries no killing blow in this log format, so the killing blow is
inferred from the last damage event landing on the victim before death. That
inference is stated in the output rather than hidden.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from .logparse import is_player
from .roles import DPS, HEALER, TANK, PlayerRole, detect_roles

if TYPE_CHECKING:  # pragma: no cover
    from .pulls import DamageRecord, Pull

#: A tank death lets the boss loose. Deaths inside this window afterwards are
#: consequence rather than cause.
CASCADE_WINDOW_S = 15.0

#: How recently damage/healing counts as "leading up to" a death.
LOOKBACK_S = 6.0

#: Fallback when nothing landed in the normal window.
WIDE_LOOKBACK_S = 25.0

#: At or above this health fraction, a death is "from full", which combined with
#: a single melee killing blow is the loose-boss signature.
FULL_HEALTH = 0.85

#: Once this share of the raid is down, the pull is already lost and further
#: deaths carry no information about what went wrong.
COLLAPSE_SHARE = 0.4

#: Talents that convert a death into a reprieve. Dying "to" these means the
#: talent WORKED and bought the raid time -- reporting it as a death is wrong.
REPRIEVE_SPELLS = {
    "cauterize",
    "ardent defender",
    "guardian spirit",
    "purgatory",
    "shroud of purgatory",
    "spirit of redemption",
}

#: Cooldowns another player casts ON the victim.
EXTERNAL_COOLDOWNS = {
    "pain suppression",
    "guardian spirit",
    "hand of sacrifice",
    "hand of protection",
    "life cocoon",
    "ironbark",
    "vigilance",
    "safeguard",
    "spirit link totem",
}

#: Cooldowns the victim casts on themselves.
PERSONAL_DEFENSIVES = {
    "shield wall", "last stand", "die by the sword", "enraged regeneration",
    "icebound fortitude", "anti-magic shell", "vampiric blood", "dancing rune weapon",
    "fortifying brew", "guard", "elusive brew", "diffuse magic", "dampen harm",
    "zen meditation", "survival instincts", "barkskin", "frenzied regeneration",
    "divine protection", "divine shield", "ardent defender", "shield of the righteous",
    "shield block", "shield barrier", "deterrence", "cloak of shadows", "evasion",
    "feint", "ice block", "alter time", "unending resolve", "dark bargain",
    "astral shift", "desperate prayer", "dispersion", "fade",
}

SIG_LOOSE_BOSS = "loose_boss"
SIG_TANK_MITIGATION = "tank_mitigation"
SIG_MECHANIC = "mechanic"
SIG_ATTRITION = "attrition"
SIG_REPRIEVE = "reprieve"


@dataclass(slots=True)
class DeathAnalysis:
    t: float
    guid: str
    name: str
    role: str
    order: int                      # 1 = first death of the pull

    killing_blow: Optional["DamageRecord"] = None
    hp_before: Optional[float] = None
    hp_before_at: Optional[float] = None
    damage_last: int = 0
    healing_last: int = 0
    externals: list[str] = field(default_factory=list)
    personals: list[str] = field(default_factory=list)

    is_cascade: bool = False
    cascade_reason: str = ""
    signature: str = SIG_ATTRITION
    evidence: list[str] = field(default_factory=list)

    @property
    def killer(self) -> str:
        if self.killing_blow is None:
            return "unknown"
        return self.killing_blow.spell_name or "Melee"

    @property
    def killer_source(self) -> str:
        """Who dealt the killing blow.

        Ground effects and totems log a `nil` source, which is not a name and
        must not be printed as one.
        """
        if self.killing_blow is None:
            return "unknown"
        src = self.killing_blow.src_name
        return src if src and src != "nil" else "an environmental effect"

    @property
    def blameable(self) -> bool:
        """Whether this death says anything about what to change."""
        return not self.is_cascade and self.signature != SIG_REPRIEVE


def _index_by_dest(records) -> dict[str, list]:
    out: dict[str, list] = defaultdict(list)
    for r in records:
        out[r.dest_guid].append(r)
    return out


def analyse_deaths(
    pull: "Pull", roles: Optional[dict[str, PlayerRole]] = None
) -> list[DeathAnalysis]:
    """Reconstruct every death in the pull and classify cascades."""
    if roles is None:
        roles = detect_roles(pull)

    dmg_by_victim = _index_by_dest(pull.damage_taken)
    heal_by_victim = _index_by_dest(pull.heals)

    casts_by_target: dict[str, list] = defaultdict(list)
    casts_by_self: dict[str, list] = defaultdict(list)
    for c in pull.casts:
        if c.started or not is_player(c.src_guid):
            continue
        if c.dest_guid and c.dest_guid != c.src_guid:
            casts_by_target[c.dest_guid].append(c)
        else:
            casts_by_self[c.src_guid].append(c)

    deaths = sorted(pull.deaths, key=lambda d: d.t)
    raid_size = max(len(pull.players), pull.group_size or 1)

    out: list[DeathAnalysis] = []
    tank_deaths: list[tuple[float, str]] = []

    for order, death in enumerate(deaths, start=1):
        role = roles[death.guid].role if death.guid in roles else DPS
        da = DeathAnalysis(
            t=death.t, guid=death.guid, name=death.name, role=role, order=order
        )

        records = dmg_by_victim.get(death.guid, ())
        window = [
            d for d in records
            if death.t - LOOKBACK_S <= d.t <= death.t + 0.5
        ]
        if not window:
            # Nothing in the normal window: the victim may have been finished by
            # a slow tick or died just outside it. Widen rather than report an
            # unknown killer, which tells the raid leader nothing.
            window = [
                d for d in records
                if death.t - WIDE_LOOKBACK_S <= d.t <= death.t + 0.5
            ]
        window.sort(key=lambda d: d.t)

        # UNIT_DIED does not name the killer, so infer it: the last damage to
        # land on the victim. Overkill corroborates -- a real killing blow
        # usually reports overkill >= 0.
        if window:
            with_overkill = [d for d in window if d.overkill >= 0]
            da.killing_blow = (with_overkill or window)[-1]
            da.damage_last = sum(d.amount + d.absorbed for d in window)

        # Health immediately before the killing blow: the most recent health
        # report on this victim strictly before the fatal event.
        kb_t = da.killing_blow.t if da.killing_blow else death.t
        for d in reversed(window):
            if d.t < kb_t and d.hp_after is not None:
                da.hp_before = d.hp_after
                da.hp_before_at = d.t
                break

        da.healing_last = sum(
            h.effective
            for h in heal_by_victim.get(death.guid, ())
            if death.t - LOOKBACK_S <= h.t <= death.t
        )
        da.externals = sorted({
            c.spell_name for c in casts_by_target.get(death.guid, ())
            if death.t - LOOKBACK_S <= c.t <= death.t
            and c.spell_name.lower() in EXTERNAL_COOLDOWNS
        })
        da.personals = sorted({
            c.spell_name for c in casts_by_self.get(death.guid, ())
            if death.t - 20.0 <= c.t <= death.t
            and c.spell_name.lower() in PERSONAL_DEFENSIVES
        })

        _classify(da, tank_deaths, order, raid_size, roles)

        if role == TANK:
            tank_deaths.append((death.t, death.name))
        out.append(da)

    return out


def _classify(
    da: DeathAnalysis,
    tank_deaths: list[tuple[float, str]],
    order: int,
    raid_size: int,
    roles: dict[str, PlayerRole],
) -> None:
    kb = da.killing_blow
    melee = kb is not None and kb.spell_id == 0
    from_full = da.hp_before is not None and da.hp_before >= FULL_HEALTH
    unhealed = da.healing_last == 0

    if kb is not None and kb.spell_name.lower() in REPRIEVE_SPELLS:
        da.signature = SIG_REPRIEVE
        da.evidence.append(
            f"killing blow was {kb.spell_name}, a talent that prevents a death "
            f"rather than causing one - the reprieve was not converted into a save"
        )
        return

    recent_tank_death = next(
        ((t, n) for t, n in reversed(tank_deaths) if 0 < da.t - t <= CASCADE_WINDOW_S),
        None,
    )

    # The raid is already lost; later deaths carry no information.
    if order > max(2, raid_size * COLLAPSE_SHARE):
        da.is_cascade = True
        da.cascade_reason = (
            f"death #{order} of the pull - the raid had already collapsed"
        )
        da.evidence.append(da.cascade_reason)
        return

    if recent_tank_death is not None and da.role != TANK:
        gap = da.t - recent_tank_death[0]
        # A loose boss kills with melee. Someone standing in a ground effect
        # died of their own accord even if a tank happened to die first, so a
        # non-melee killing blow is NOT excused.
        if melee:
            da.is_cascade = True
            da.signature = SIG_LOOSE_BOSS
            da.cascade_reason = (
                f"tank {recent_tank_death[1]} died {gap:.0f}s earlier; "
                f"killed by melee, so the boss was loose"
            )
            da.evidence.append(da.cascade_reason)
            da.evidence.append(
                "consequence of the tank death, not a positioning error - not counted "
                "against this player"
            )
            return
        da.evidence.append(
            f"tank {recent_tank_death[1]} died {gap:.0f}s earlier, but the killing "
            f"blow was {kb.spell_name if kb else 'unknown'} rather than melee, "
            f"so this is not cascade"
        )

    if da.role == TANK and melee:
        # Tanks are the exception: melee is the intended damage, so a fatal one
        # is a mitigation gap rather than a loose boss.
        da.signature = SIG_TANK_MITIGATION
        da.evidence.append(
            f"tank killed by melee - the intended target, so this indicates a "
            f"mitigation gap rather than a loose boss"
        )
    elif melee and from_full and unhealed:
        da.signature = SIG_LOOSE_BOSS
        da.evidence.append(
            f"single melee hit from {da.hp_before:.0%} health with no healing "
            f"received in {LOOKBACK_S:.0f}s - a loose boss, not a positioning error"
        )
    elif kb is not None and not melee:
        da.signature = SIG_MECHANIC
    else:
        da.signature = SIG_ATTRITION

    if kb is not None:
        hp_txt = (
            f"at {da.hp_before:.0%} health" if da.hp_before is not None
            else "health before the hit not reported"
        )
        da.evidence.append(
            f"killing blow {kb.spell_name or 'Melee'} from {da.killer_source} "
            f"for {kb.amount + kb.absorbed:,} ({hp_txt})"
        )
        da.evidence.append("killing blow inferred from last damage before UNIT_DIED")
    da.evidence.append(
        f"took {da.damage_last:,} and received {da.healing_last:,} healing in the "
        f"{LOOKBACK_S:.0f}s before dying"
    )
    if da.externals:
        da.evidence.append(f"externals used: {', '.join(da.externals)}")
    elif da.role == TANK:
        da.evidence.append("no external cooldown used on this tank")
    if da.personals:
        da.evidence.append(f"personal cooldowns: {', '.join(da.personals)}")
    elif da.role == TANK:
        da.evidence.append("no personal defensive in the 20s before dying")


@dataclass(slots=True)
class WipeVerdict:
    """The headline answer: what killed this pull."""

    wiped: bool
    wipe_at: float
    root: Optional[DeathAnalysis]
    deaths: list[DeathAnalysis]

    @property
    def cascade_count(self) -> int:
        return sum(1 for d in self.deaths if d.is_cascade)

    @property
    def blameable(self) -> list[DeathAnalysis]:
        return [d for d in self.deaths if d.blameable]

    def headline(self, fmt) -> str:
        if not self.deaths:
            return "No deaths." if not self.wiped else "Wipe with no deaths recorded."
        root = self.root
        if root is None:
            return f"Wipe at {fmt(self.wipe_at)}."
        if not self.wiped:
            # Nothing "caused" a kill. Reporting a root cause for a successful
            # pull reads as a failure and undersells the attempt.
            return (
                f"Killed at {fmt(self.wipe_at)} with {len(self.deaths)} deaths "
                f"({self.cascade_count} cascade). First death: {root.name} to "
                f"{root.killer} at {fmt(root.t)}."
            )
        bits = [f"Wipe at {fmt(self.wipe_at)}."]
        bits.append(
            f"Root cause: {root.name} died to {root.killer} at {fmt(root.t)}"
        )
        if root.role == TANK and not root.externals:
            bits[-1] += " with no external cooldown used"
        bits[-1] += "."
        after = [d for d in self.deaths if d.order > root.order]
        if after:
            casc = sum(1 for d in after if d.is_cascade)
            bits.append(
                f"{casc} of the following {len(after)} deaths were cascade."
            )
        return " ".join(bits)


def verdict(pull: "Pull", roles: Optional[dict[str, PlayerRole]] = None) -> WipeVerdict:
    deaths = analyse_deaths(pull, roles)
    root = None
    # The first death is usually the story -- but not if it was a reprieve.
    for d in deaths:
        if d.signature != SIG_REPRIEVE:
            root = d
            break
    wipe_at = max((d.t for d in deaths), default=pull.duration)
    return WipeVerdict(
        wiped=pull.is_wipe,
        wipe_at=pull.duration if pull.is_wipe else wipe_at,
        root=root,
        deaths=deaths,
    )
