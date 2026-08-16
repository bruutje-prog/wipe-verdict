"""Load the YAML mechanic configuration.

Mechanic assumptions live in config rather than code so that a raider who knows
a spec can correct the tool without a developer. The loader's job is therefore
to be forgiving about what it accepts and loud about what it cannot understand.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .findings import CONSUMED_ABSORBS

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

COUNT_HITS = "hits"
COUNT_APPLICATIONS = "applications"


@dataclass(slots=True)
class Mechanic:
    spell_id: int
    name: str
    count: str = COUNT_HITS
    note: str = ""
    #: roles that cannot avoid this and must not be counted as having failed.
    #: A frontal breath is unavoidable for whoever is holding the boss.
    exempt_roles: tuple[str, ...] = ()
    #: Ignore hits below this damage. Some abilities put an avoidable component
    #: and an unavoidable one under ONE spell id -- Garrosh's Annihilate is a
    #: frontal cone plus a raid-wide pulse -- and the log gives no other way to
    #: tell them apart. A threshold lets whoever knows the fight separate them
    #: instead of the tool guessing.
    amount_at_least: int = 0

    @property
    def by_applications(self) -> bool:
        return self.count == COUNT_APPLICATIONS

    def exempt(self, role: str) -> bool:
        return role in self.exempt_roles


@dataclass(slots=True)
class SharedMechanic:
    """Damage split between everyone it hits.

    Unavoidable, so it does not belong in the avoidable table -- but not
    nothing either: the fewer players share it, the harder each one is hit.
    The actionable number is how many soaked it, not whether anyone was hit.
    """

    spell_id: int
    name: str
    #: soakers below this share of the raid is worth reporting
    expect_share: float = 0.7
    note: str = ""


@dataclass(slots=True)
class SoakMechanic:
    """Something a player must deliberately stand in.

    Taking the soak damage is CORRECT PLAY, so it must never appear in the
    avoidable table -- that penalises the people doing the job. The failure is
    a separate spell that punishes the whole raid, and it is the one worth
    counting: one missed tear, twenty-five people hit.
    """

    spell_id: int          # damage to the soaker
    name: str
    fail_spell_id: int     # raid-wide damage when nobody soaked
    #: tears opened per cast, when known; only used for reporting
    per_cast: int = 0
    note: str = ""


@dataclass(slots=True)
class TankBuster:
    spell_id: int
    name: str
    expect_cooldown: bool = False


@dataclass(slots=True)
class Interruptible:
    spell_id: int
    name: str
    verified: bool = False


@dataclass(slots=True)
class BossConfig:
    key: str
    encounter_id: int
    display_name: str
    boss_units: list[str] = field(default_factory=list)
    avoidable: dict[int, Mechanic] = field(default_factory=dict)
    shared: dict[int, SharedMechanic] = field(default_factory=dict)
    soaks: dict[int, SoakMechanic] = field(default_factory=dict)
    tank_busters: dict[int, TankBuster] = field(default_factory=dict)
    interruptible: dict[int, Interruptible] = field(default_factory=dict)
    seeded_from: str = ""

    def is_avoidable(self, spell_id: int) -> Optional[Mechanic]:
        return self.avoidable.get(spell_id)


@dataclass(slots=True)
class CooldownDef:
    name: str
    cooldown_s: float
    spec: str = ""
    note: str = ""


@dataclass(slots=True)
class NonStacking:
    spec: str
    abilities: list[str]
    reason: str


@dataclass(slots=True)
class Config:
    bosses: dict[int, BossConfig] = field(default_factory=dict)
    raid_cooldowns: dict[str, CooldownDef] = field(default_factory=dict)
    consumed_absorbs: dict[str, CooldownDef] = field(default_factory=dict)
    interrupt_abilities: dict[str, CooldownDef] = field(default_factory=dict)
    non_stacking: list[NonStacking] = field(default_factory=list)
    refreshed_by: dict[str, str] = field(default_factory=dict)
    reprieve_talents: dict[str, float] = field(default_factory=dict)
    confounded: list[dict] = field(default_factory=list)

    def boss(self, encounter_id: int) -> Optional[BossConfig]:
        return self.bosses.get(encounter_id)

    def substitute_for(self, metric: str) -> Optional[str]:
        """A metric that is not downstream of incoming damage."""
        for row in self.confounded:
            if row.get("metric", "").lower() == metric.lower():
                return row.get("prefer")
        return None

    def stacking_warning(self, abilities: list[str]) -> Optional[NonStacking]:
        """Refuse to recommend pairing cooldowns that do not stack."""
        lowered = {a.lower() for a in abilities}
        for rule in self.non_stacking:
            if len({a.lower() for a in rule.abilities} & lowered) >= 2:
                return rule
        return None


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_config(config_dir: Optional[Path | str] = None) -> Config:
    """Read config/bosses.yaml and config/mechanics.yaml."""
    base = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
    cfg = Config()

    bosses_path = base / "bosses.yaml"
    if bosses_path.exists():
        raw = yaml.safe_load(bosses_path.read_text(encoding="utf-8")) or {}
        for key, body in (raw.get("bosses") or {}).items():
            encounter_id = _as_int(body.get("encounter_id"), -1)
            if encounter_id < 0:
                raise ValueError(f"boss {key!r} has no usable encounter_id")
            boss = BossConfig(
                key=key,
                encounter_id=encounter_id,
                display_name=body.get("display_name", key),
                boss_units=list(body.get("boss_units") or []),
                seeded_from=body.get("seeded_from", ""),
            )
            for row in body.get("avoidable") or []:
                m = Mechanic(
                    spell_id=_as_int(row.get("spell_id")),
                    name=row.get("name", "?"),
                    count=row.get("count", COUNT_HITS),
                    note=row.get("note", ""),
                    exempt_roles=tuple(row.get("exempt_roles") or ()),
                    amount_at_least=_as_int(row.get("amount_at_least"), 0),
                )
                if m.count not in (COUNT_HITS, COUNT_APPLICATIONS):
                    raise ValueError(
                        f"{key}/{m.name}: count must be "
                        f"{COUNT_HITS!r} or {COUNT_APPLICATIONS!r}, got {m.count!r}"
                    )
                boss.avoidable[m.spell_id] = m
            for row in body.get("shared_damage") or []:
                sm = SharedMechanic(
                    spell_id=_as_int(row.get("spell_id")),
                    name=row.get("name", "?"),
                    expect_share=float(row.get("expect_share", 0.7)),
                    note=row.get("note", ""),
                )
                boss.shared[sm.spell_id] = sm
            for row in body.get("soaks") or []:
                sk = SoakMechanic(
                    spell_id=_as_int(row.get("spell_id")),
                    name=row.get("name", "?"),
                    fail_spell_id=_as_int(row.get("fail_spell_id")),
                    per_cast=_as_int(row.get("per_cast"), 0),
                    note=row.get("note", ""),
                )
                if not sk.fail_spell_id:
                    raise ValueError(
                        f"{key}/{sk.name}: a soak needs fail_spell_id, the spell "
                        f"that punishes the raid when nobody stands in it"
                    )
                boss.soaks[sk.spell_id] = sk
            for row in body.get("tank_busters") or []:
                tb = TankBuster(
                    spell_id=_as_int(row.get("spell_id")),
                    name=row.get("name", "?"),
                    expect_cooldown=bool(row.get("expect_cooldown", False)),
                )
                boss.tank_busters[tb.spell_id] = tb
            for row in body.get("interruptible") or []:
                it = Interruptible(
                    spell_id=_as_int(row.get("spell_id")),
                    name=row.get("name", "?"),
                    verified=bool(row.get("verified", False)),
                )
                boss.interruptible[it.spell_id] = it
            cfg.bosses[boss.encounter_id] = boss

    mech_path = base / "mechanics.yaml"
    if mech_path.exists():
        raw = yaml.safe_load(mech_path.read_text(encoding="utf-8")) or {}
        for row in raw.get("raid_cooldowns") or []:
            cd = CooldownDef(
                name=row["name"],
                cooldown_s=float(row.get("cooldown_s", 180)),
                spec=row.get("spec", ""),
            )
            cfg.raid_cooldowns[cd.name.lower()] = cd
        for row in raw.get("consumed_absorbs") or []:
            cd = CooldownDef(
                name=row["name"],
                cooldown_s=float(row.get("cooldown_s", 0)),
                spec=row.get("spec", ""),
                note=row.get("note", ""),
            )
            cfg.consumed_absorbs[cd.name.lower()] = cd
            # Extend the runtime guard so a config addition is protected too.
            CONSUMED_ABSORBS.add(cd.name.lower())
        for row in raw.get("interrupt_abilities") or []:
            cd = CooldownDef(
                name=row["name"],
                cooldown_s=float(row.get("cooldown_s", 15)),
                spec=row.get("spec", ""),
            )
            cfg.interrupt_abilities[cd.name.lower()] = cd
        for row in raw.get("non_stacking") or []:
            cfg.non_stacking.append(
                NonStacking(
                    spec=row.get("spec", ""),
                    abilities=list(row.get("abilities") or []),
                    reason=row.get("reason", ""),
                )
            )
        for row in raw.get("refreshed_by_other_spells") or []:
            cfg.refreshed_by[row["ability"].lower()] = row.get("refreshed_by", "")
        for row in raw.get("reprieve_talents") or []:
            cfg.reprieve_talents[row["name"].lower()] = float(row.get("grace_s", 6))
        cfg.confounded = list(raw.get("confounded_metrics") or [])

    return cfg
