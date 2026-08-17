"""Tests for the Snakes of Honour wipe analyser.

Weighted towards the conclusions that would be embarrassing to get wrong in
front of a raid: cascade attribution, the absorb-uptime rule, and the parser's
field alignment. A test that cannot fail is worthless, so each of these was
confirmed to go red when the thing it guards was broken.

Run:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wipeanalyser.analysis import (
    alive_at,
    alive_time,
    avoidable_table,
    damage_windows,
    shared_bursts,
)
from wipeanalyser.session import Session
from wipeanalyser.config import load_config
from wipeanalyser.deaths import (
    SIG_LOOSE_BOSS,
    SIG_TANK_MITIGATION,
    analyse_deaths,
    verdict,
)
from wipeanalyser.findings import (
    Finding,
    MetricError,
    assert_metric_valid,
    rank_findings,
    RANK_ROOT_CAUSE,
    RANK_THROUGHPUT,
)
from wipeanalyser.logparse import parse_line, split_fields
from wipeanalyser.pulls import (
    AuraRecord,
    CastRecord,
    DamageRecord,
    DeathRecord,
    HealRecord,
    Pull,
    PullSegmenter,
)
from wipeanalyser.roles import TANK, detect_roles
from wipeanalyser.tail import LogTailer

SAMPLES = Path(__file__).resolve().parent.parent / "samples"

# A real line from the guild's log, kept verbatim as the parser's fixture.
REAL_SPELL_DAMAGE = (
    '8/11/2026 19:57:10.9381  SPELL_DAMAGE,Player-4454-06028C34,'
    '"Defendra-MirageRaceway-EU",0x40514,0x80000000,'
    'Creature-0-4459-1136-13865-73349-00017B6EAC,"Tormented Initiate",0xa48,'
    '0x80000000,23922,"Shield Slam",0x1,'
    'Creature-0-4459-1136-13865-73349-00017B6EAC,0000000000000000,4008335,'
    '4075320,0,0,0,0,0,-1,0,0,0,1377.46,400.90,557,4.7124,91,66985,97974,-1,1,'
    '0,0,0,nil,nil,nil,ST'
)

REAL_SWING_DAMAGE = (
    '8/11/2026 19:57:11.1701  SWING_DAMAGE,Player-4454-06028C34,'
    '"Defendra-MirageRaceway-EU",0x40514,0x80000000,'
    'Creature-0-4459-1136-13865-73349-00017B6EAC,"Tormented Initiate",0xa48,'
    '0x80000000,Player-4454-06028C34,0000000000000000,100,100,59781,29,74565,'
    '0,0,1,403,1000,0,1376.88,403.89,557,4.8937,553,16013,24469,-1,1,0,0,0,'
    'nil,1,nil'
)


class TestParser(unittest.TestCase):
    def test_quoted_commas_do_not_shift_columns(self):
        fields = split_fields('A,"has, a comma",C')
        self.assertEqual(fields, ["A", "has, a comma", "C"])

    def test_spell_damage_amount_is_the_real_amount(self):
        """maxHP - currentHP must equal the damage, which is what proves the
        amount column is column 29 and not the unmitigated base in column 30."""
        ev = parse_line(REAL_SPELL_DAMAGE)
        self.assertEqual(ev.event, "SPELL_DAMAGE")
        self.assertEqual(ev.spell_id, 23922)
        self.assertEqual(ev.spell_name, "Shield Slam")
        self.assertEqual(ev.amount, 66985)
        current, maximum = ev.hp_values
        self.assertEqual(maximum - current, ev.amount)

    def test_swing_damage_has_no_spell_prefix(self):
        ev = parse_line(REAL_SWING_DAMAGE)
        self.assertEqual(ev.spell_id, 0)
        self.assertEqual(ev.spell_name, "Melee")
        # Payload is indexed from the end, so the missing spell prefix and the
        # absent ST/AOE tag must not shift the amount.
        self.assertEqual(ev.amount, 16013)

    def test_hp_fraction_is_normalised_for_both_unit_types(self):
        """Players log percent, creatures log absolute. Only the ratio ports."""
        creature = parse_line(REAL_SPELL_DAMAGE)
        self.assertAlmostEqual(creature.hp_fraction, 4008335 / 4075320, places=6)
        player = parse_line(REAL_SWING_DAMAGE)
        self.assertAlmostEqual(player.hp_fraction, 1.0, places=6)

    def test_hostility_is_read_from_flags_not_guid_type(self):
        """Pets are Creature GUIDs on the raid's side."""
        boss = parse_line(REAL_SPELL_DAMAGE)
        self.assertFalse(boss.src_hostile)   # a player cast it
        self.assertTrue(boss.dest_hostile)   # at a boss add

    def test_damage_still_parses_without_advanced_logging(self):
        """Advanced Combat Logging can be off -- it was, on 13/08.

        WoW keeps the same field count and zero-fills the advanced block. The
        damage amount survives only because the payload is indexed from the END
        of the line; health correctly becomes unknown rather than zero.
        """
        line = (
            '8/13/2026 21:04:30.7232  SPELL_DAMAGE,Player-4454-055AFC7B,'
            '"Andwhatnow-MirageRaceway-EU",0x514,0x80000000,'
            'Vehicle-0-4445-1136-4063-71543-00007E0D5C,"Immerseus",0x10a48,'
            '0x80000000,30451,"Arcane Blast",0x40,'
            '0000000000000000,0000000000000000,0,0,0,0,0,0,0,-1,0,0,0,'
            '0.00,0.00,557,0.0000,0,101631,101631,-1,64,0,0,0,nil,nil,nil,ST'
        )
        ev = parse_line(line)
        self.assertEqual(ev.spell_name, "Arcane Blast")
        self.assertEqual(ev.amount, 101631)
        self.assertIsNone(ev.hp_fraction, "no health data without advanced logging")

    def test_position_reads_real_coordinates(self):
        ev = parse_line(REAL_SPELL_DAMAGE)
        self.assertEqual(ev.position, (1377.46, 400.90))

    def test_position_is_refused_on_events_with_an_unmodelled_payload(self):
        """Position sits at the end of the advanced block, so reading it on an
        event whose trailing payload is not modelled returns whatever happens
        to be at those offsets. SPELL_ENERGIZE produced (562.0, 0.0) sitting
        beside a real arena at (1226, -5052)."""
        line = (
            '8/2/2026 21:00:00.000  SPELL_ENERGIZE,Player-1,"A-Realm",0x512,'
            '0x80000000,Player-1,"A-Realm",0x512,0x80000000,29842,"Second Wind",'
            '0x1,Player-1,0000000000000000,100,100,0,0,0,0,0,1,60,100,0,'
            '1226.70,-5052.60,557,1.5,553,20,0,1,0'
        )
        ev = parse_line(line)
        self.assertEqual(ev.event, "SPELL_ENERGIZE")
        self.assertIsNone(
            ev.position, "must not guess coordinates from an unmodelled payload"
        )

    def test_malformed_line_returns_none(self):
        self.assertIsNone(parse_line("not a log line"))
        self.assertIsNone(parse_line(""))


class TestSegmentation(unittest.TestCase):
    def test_encounter_start_end_produces_a_pull(self):
        seg = PullSegmenter()
        start = (
            '7/30/2026 20:00:00.000  ENCOUNTER_START,1606,'
            '"Kor\'kron Dark Shaman",6,25,1136,19'
        )
        end = (
            '7/30/2026 20:05:00.000  ENCOUNTER_END,1606,'
            '"Kor\'kron Dark Shaman",6,25,0'
        )
        self.assertIsNone(seg.feed(parse_line(start)))
        pull = seg.feed(parse_line(end))
        self.assertIsNotNone(pull)
        self.assertEqual(pull.encounter_id, 1606)
        self.assertEqual(pull.difficulty, "25 Heroic")
        self.assertEqual(pull.group_size, 25)
        self.assertEqual(pull.duration, 300.0)
        self.assertFalse(pull.success)

    def test_health_is_not_attributed_to_a_unit_it_does_not_belong_to(self):
        """The advanced info unit is sometimes neither source nor destination.

        Falling back to the source's name in that case files a third party's
        health under the boss. It reported a 1:16 Thok wipe as reaching 6.5%
        when the raid actually got him to 61.8% -- a wrong answer to the single
        most-quoted question a raid leader asks.
        """
        adv = (
            "Creature-OTHER,0000000000000000,10,1000,"      # 1% health, NOT the boss
            "0,0,0,0,0,-1,0,0,0,1.0,2.0,557,0.0,93"
        )
        payload = "500,600,-1,1,0,0,0,nil,nil,nil,ST"
        line = (
            '8/2/2026 21:33:30.548  SPELL_PERIODIC_DAMAGE,'
            'Creature-BOSS,"Thok the Bloodthirsty",0xa48,0x80000000,'
            'Player-1,"Victim-Realm",0x514,0x80000000,'
            f'1234,"Bite",0x1,{adv},{payload}'
        )
        ev = parse_line(line)
        self.assertEqual(ev.info_guid, "Creature-OTHER")
        self.assertAlmostEqual(ev.hp_fraction, 0.01)

        seg = PullSegmenter()
        seg.feed(parse_line(
            '8/2/2026 21:33:00.000  ENCOUNTER_START,1599,"Thok",6,25,1136,19'
        ))
        seg.feed(ev)
        self.assertNotIn(
            "Thok the Bloodthirsty", seg.current.enemy_hp,
            "health of a third-party unit must not be filed under the boss",
        )

    def test_combatant_info_does_not_register_players_as_faction(self):
        """COMBATANT_INFO leads GUID,faction -- it looks exactly like a prefix
        and registered every raider under the name "0"."""
        seg = PullSegmenter()
        seg.feed(parse_line(
            '7/30/2026 20:00:00.000  ENCOUNTER_START,1606,"X",6,25,1136,19'
        ))
        seg.feed(parse_line(
            '7/30/2026 20:00:01.000  COMBATANT_INFO,Player-4454-0001,0,1200,900'
        ))
        self.assertEqual(seg.current.players, {})


def _pull(duration: float = 300.0) -> Pull:
    return Pull(
        encounter_id=1606,
        boss="Kor'kron Dark Shaman",
        difficulty_id=6,
        group_size=25,
        start=datetime(2026, 7, 30, 20, 0, 0),
        end=datetime(2026, 7, 30, 20, 0, 0) + timedelta(seconds=duration),
    )


def _melee(t: float, victim: str, amount: int = 500_000, hp=None) -> DamageRecord:
    return DamageRecord(
        t=t, dest_guid=victim, dest_name=victim, src_guid="Creature-1",
        src_name="Boss", spell_id=0, spell_name="Melee", amount=amount,
        absorbed=0, overkill=1, periodic=False, hp_after=hp,
    )


def _mechanic(t: float, victim: str, spell_id: int = 144017) -> DamageRecord:
    return DamageRecord(
        t=t, dest_guid=victim, dest_name=victim, src_guid="Creature-1",
        src_name="Boss", spell_id=spell_id, spell_name="Toxic Storm",
        amount=400_000, absorbed=0, overkill=1, periodic=False, hp_after=0.9,
    )


class TestCascade(unittest.TestCase):
    """The feature that must never blame the wrong person."""

    def _tank_then_melee_deaths(self) -> Pull:
        p = _pull()
        p.players = {"Tank": "Tank", "Healer": "Healer", "Mage": "Mage"}
        # Tank identity: takes the boss melee and presses a tank ability.
        for i in range(20):
            p.damage_taken.append(_melee(float(i * 5), "Tank", 100_000, hp=0.6))
        p.casts.append(CastRecord(
            t=1.0, src_guid="Tank", src_name="Tank", dest_guid="Tank",
            spell_id=1, spell_name="Shield Slam", started=False,
        ))
        p.heals.append(HealRecord(
            t=1.0, dest_guid="Healer", src_guid="Healer", src_name="Healer",
            spell_id=2, spell_name="Chain Heal", effective=900_000,
        ))
        p.casts.append(CastRecord(
            t=1.0, src_guid="Healer", src_name="Healer", dest_guid="Healer",
            spell_id=2, spell_name="Chain Heal", started=False,
        ))
        return p

    def test_deaths_after_a_tank_death_are_cascade(self):
        p = self._tank_then_melee_deaths()
        p.damage_taken.append(_melee(100.0, "Tank", 900_000, hp=0.5))
        p.deaths.append(DeathRecord(t=100.0, guid="Tank", name="Tank"))
        # Killed by melee from full health five seconds later: a loose boss.
        p.damage_taken.append(_melee(105.0, "Healer", 900_000, hp=1.0))
        p.damage_taken.append(_melee(105.5, "Healer", 900_000, hp=0.0))
        p.deaths.append(DeathRecord(t=106.0, guid="Healer", name="Healer"))

        roles = detect_roles(p)
        self.assertEqual(roles["Tank"].role, TANK)

        result = analyse_deaths(p, roles)
        healer_death = next(d for d in result if d.name == "Healer")
        self.assertTrue(healer_death.is_cascade)
        self.assertEqual(healer_death.signature, SIG_LOOSE_BOSS)
        self.assertFalse(healer_death.blameable)
        self.assertIn("tank Tank died", healer_death.cascade_reason)

    def test_a_mechanic_death_after_a_tank_death_is_NOT_cascade(self):
        """Standing in something is your own doing even if a tank just died."""
        p = self._tank_then_melee_deaths()
        p.damage_taken.append(_melee(100.0, "Tank", 900_000, hp=0.5))
        p.deaths.append(DeathRecord(t=100.0, guid="Tank", name="Tank"))
        p.damage_taken.append(_mechanic(104.0, "Mage"))
        p.deaths.append(DeathRecord(t=105.0, guid="Mage", name="Mage"))

        result = analyse_deaths(p, detect_roles(p))
        mage = next(d for d in result if d.name == "Mage")
        self.assertFalse(mage.is_cascade)
        self.assertTrue(mage.blameable)

    def test_a_tank_dying_to_melee_is_a_mitigation_gap_not_cascade(self):
        p = self._tank_then_melee_deaths()
        p.damage_taken.append(_melee(100.0, "Tank", 900_000, hp=0.9))
        p.deaths.append(DeathRecord(t=100.5, guid="Tank", name="Tank"))
        result = analyse_deaths(p, detect_roles(p))
        tank = next(d for d in result if d.name == "Tank")
        self.assertEqual(tank.signature, SIG_TANK_MITIGATION)
        self.assertFalse(tank.is_cascade)
        self.assertTrue(tank.blameable)

    def test_killing_blow_is_inferred_and_labelled_as_inferred(self):
        p = self._tank_then_melee_deaths()
        p.damage_taken.append(_mechanic(50.0, "Mage"))
        p.deaths.append(DeathRecord(t=50.5, guid="Mage", name="Mage"))
        mage = next(d for d in analyse_deaths(p, detect_roles(p)) if d.name == "Mage")
        self.assertEqual(mage.killer, "Toxic Storm")
        self.assertTrue(
            any("inferred" in e for e in mage.evidence),
            "the inference must be stated, not hidden",
        )

    def test_root_cause_skips_a_death_with_no_identifiable_killer(self):
        """A Spoils pull opened with a death 2s in that no damage explained.
        Naming it as the root cause produces "root cause: unknown"."""
        p = self._tank_then_melee_deaths()
        # Death with nothing to attribute it to.
        p.deaths.append(DeathRecord(t=2.0, guid="Ghost", name="Ghost"))
        p.players["Ghost"] = "Ghost"
        # A later death that CAN be explained.
        p.damage_taken.append(_mechanic(50.0, "Mage"))
        p.deaths.append(DeathRecord(t=50.5, guid="Mage", name="Mage"))

        v = verdict(p)
        self.assertIsNotNone(v.root)
        self.assertEqual(v.root.name, "Mage")
        self.assertIn("Toxic Storm", v.headline(p.fmt))

    def test_kill_headline_does_not_claim_a_root_cause(self):
        p = self._tank_then_melee_deaths()
        p.success = True
        p.damage_taken.append(_mechanic(50.0, "Mage"))
        p.deaths.append(DeathRecord(t=50.5, guid="Mage", name="Mage"))
        text = verdict(p).headline(p.fmt)
        self.assertIn("Killed", text)
        self.assertNotIn("Root cause", text)


class TestAbsorbRule(unittest.TestCase):
    """Absorbs end when consumed. Uptime is never a valid measure of them."""

    def test_uptime_on_a_consumed_absorb_is_refused(self):
        with self.assertRaises(MetricError):
            assert_metric_valid("Guard", "uptime")
        with self.assertRaises(MetricError):
            assert_metric_valid("Power Word: Shield", "uptime_pct")

    def test_cast_rate_on_a_consumed_absorb_is_allowed(self):
        assert_metric_valid("Guard", "cast rate against cooldown")

    def test_a_finding_cannot_be_constructed_with_the_bad_metric(self):
        with self.assertRaises(MetricError):
            Finding(
                rank_class=1, score=1.0, action="Guard uptime is low",
                method="uptime", subject="Guard",
            )

    def test_uptime_is_still_fine_for_things_that_are_not_absorbs(self):
        assert_metric_valid("Rend", "uptime")


class TestRatesAndWindows(unittest.TestCase):
    def test_alive_time_excludes_time_spent_dead(self):
        p = _pull(300.0)
        p.deaths.append(DeathRecord(t=100.0, guid="A", name="A"))
        self.assertAlmostEqual(alive_time(p, "A"), 100.0)

    def test_alive_time_resumes_after_a_resurrect(self):
        p = _pull(300.0)
        p.deaths.append(DeathRecord(t=100.0, guid="A", name="A"))
        p.resurrects.append(DeathRecord(t=150.0, guid="A", name="A"))
        self.assertAlmostEqual(alive_time(p, "A"), 250.0)

    def test_alive_time_is_full_duration_when_never_dead(self):
        self.assertAlmostEqual(alive_time(_pull(300.0), "A"), 300.0)

    def test_damage_windows_find_the_spike(self):
        p = _pull(120.0)
        for t in range(0, 120):
            p.damage_taken.append(_melee(float(t), "A", 1_000))
        for _ in range(50):
            p.damage_taken.append(_melee(60.0, "A", 500_000))
        windows = damage_windows(p)
        self.assertTrue(windows)
        self.assertTrue(any(w.start <= 60.0 < w.end for w in windows))


class TestAvoidableCounting(unittest.TestCase):
    def test_ticking_mechanics_are_counted_by_application_not_by_tick(self):
        """A pool that ticks 100 times is one mistake, not 100."""
        cfg = load_config()
        boss = cfg.boss(1606)
        self.assertIsNotNone(boss, "Dark Shaman must be configured")

        p = _pull()
        # A real player GUID: application counting is restricted to players, so
        # a placeholder id would be filtered out along with the boss's adds.
        p.players = {"Player-4454-0001": "A"}
        # Foulness (144066) is configured count: applications.
        for i in range(100):
            p.damage_taken.append(
                DamageRecord(
                    t=float(i), dest_guid="Player-4454-0001", dest_name="A",
                    src_guid="Creature-1", src_name="Boss", spell_id=144066,
                    spell_name="Foulness", amount=1000, absorbed=0,
                    overkill=-1, periodic=True, hp_after=None,
                )
            )
        p.auras.append(AuraRecord(
            t=0.0, dest_guid="Player-4454-0001", dest_name="A",
            src_guid="Creature-1", spell_id=144066, spell_name="Foulness",
            aura_type="DEBUFF", applied=True,
        ))
        rows = avoidable_table(p, boss, detect_roles(p))
        mist = next(r for r in rows if r.spell_id == 144066)
        self.assertEqual(mist.count, 1, "ticks must not be counted as hits")
        self.assertEqual(mist.counted_by, "applications")
        self.assertEqual(mist.damage, 100_000, "damage still totals every tick")


    def test_a_role_that_cannot_avoid_a_mechanic_is_not_counted(self):
        """Freezing Breath is a frontal. The tank holding the boss eats it by
        definition, so counting it against them blames them for tanking."""
        cfg = load_config()
        boss = cfg.boss(1599)
        self.assertIsNotNone(boss, "Thok must be configured")
        self.assertIn("tank", boss.avoidable[143773].exempt_roles)

        p = _pull(300.0)
        p.players = {"Tank": "Tank", "Mage": "Mage"}
        # Make the tank unambiguous: sustained boss melee plus a tank ability.
        for i in range(40):
            p.damage_taken.append(_melee(float(i * 5), "Tank", 100_000, hp=0.7))
        p.casts.append(CastRecord(
            t=1.0, src_guid="Tank", src_name="Tank", dest_guid="Tank",
            spell_id=1, spell_name="Shield Slam", started=False,
        ))
        for who in ("Tank", "Mage"):
            for i in range(5):
                p.damage_taken.append(
                    DamageRecord(
                        t=float(100 + i), dest_guid=who, dest_name=who,
                        src_guid="Creature-1", src_name="Thok", spell_id=143773,
                        spell_name="Freezing Breath", amount=300_000,
                        absorbed=0, overkill=-1, periodic=False, hp_after=0.5,
                    )
                )

        roles = detect_roles(p)
        self.assertEqual(roles["Tank"].role, TANK)
        rows = avoidable_table(p, boss, roles)
        hit = {r.player for r in rows if r.spell_id == 143773}
        self.assertIn("Mage", hit)
        self.assertNotIn("Tank", hit, "the tank cannot avoid a frontal breath")


class TestSharedDamage(unittest.TestCase):
    """Unavoidable damage that is split between everyone it hits."""

    def _pull_with_bursts(self, plan):
        """plan: list of (t, n_soakers, damage_each)."""
        p = _pull(300.0)
        # Blood Rage belongs to Malkorok; the default fixture is Dark Shaman,
        # and the session resolves the boss config by encounter id.
        p.encounter_id = 1595
        p.boss = "Malkorok"
        p.players = {f"P{i}": f"P{i}" for i in range(25)}
        for t, n, each in plan:
            for i in range(n):
                p.damage_taken.append(
                    DamageRecord(
                        t=t, dest_guid=f"P{i}", dest_name=f"P{i}",
                        src_guid="Creature-1", src_name="Malkorok",
                        spell_id=142890, spell_name="Blood Rage", amount=each,
                        absorbed=0, overkill=-1, periodic=False, hp_after=0.8,
                    )
                )
        return p

    def test_alive_at_discounts_the_dead(self):
        p = _pull(300.0)
        p.players = {f"P{i}": f"P{i}" for i in range(25)}
        p.deaths.append(DeathRecord(t=100.0, guid="P1", name="P1"))
        p.deaths.append(DeathRecord(t=110.0, guid="P2", name="P2"))
        self.assertEqual(alive_at(p, 50.0), 25)
        self.assertEqual(alive_at(p, 105.0), 24)
        self.assertEqual(alive_at(p, 120.0), 23)

    def test_under_soaked_cast_is_reported(self):
        cfg = load_config()
        boss = cfg.boss(1595)
        self.assertIsNotNone(boss, "Malkorok must be configured")
        self.assertIn(142890, boss.shared, "Blood Rage must be shared damage")

        # Three well-shared casts and one soaked by a handful, which hurts more.
        p = self._pull_with_bursts([
            (30.0, 24, 200_000),
            (60.0, 23, 205_000),
            (90.0, 24, 198_000),
            (120.0, 8, 620_000),
        ])
        bursts = shared_bursts(p, boss)
        self.assertEqual(len(bursts), 4)
        worst = min(bursts, key=lambda b: b.share)
        self.assertEqual(worst.participants, 8)
        self.assertAlmostEqual(worst.share, 8 / 25)

        session = Session(cfg)
        report = session.add(p)
        blood = [f for f in report.findings if "Blood Rage" in f.action]
        self.assertTrue(blood, "an under-soaked cast must produce a finding")
        self.assertIn("8 of 25", blood[0].action)
        self.assertTrue(
            any("correlate" in (blood[0].rejected or "") for _ in [0]),
            "the confound must be stated",
        )

    def test_well_shared_casts_produce_no_finding(self):
        cfg = load_config()
        p = self._pull_with_bursts([
            (30.0, 24, 200_000), (60.0, 23, 205_000), (90.0, 24, 198_000),
        ])
        report = Session(cfg).add(p)
        self.assertFalse(
            [f for f in report.findings if "Blood Rage" in f.action],
            "a raid that soaks properly must not be told to soak",
        )

    def test_fewer_soakers_that_cost_nothing_are_not_reported(self):
        """A stray fragment with few soakers and LOW damage each is not an
        under-soak: nothing was made worse, so there is nothing to fix."""
        cfg = load_config()
        p = self._pull_with_bursts([
            (30.0, 24, 200_000), (60.0, 23, 205_000), (90.0, 24, 198_000),
            (120.0, 2, 40_000),
        ])
        report = Session(cfg).add(p)
        self.assertFalse(
            [f for f in report.findings if "Blood Rage" in f.action]
        )

    def test_soaker_counts_late_in_a_wipe_are_ignored(self):
        """With most of the raid dead the count says nothing about play."""
        cfg = load_config()
        boss = cfg.boss(1595)
        p = self._pull_with_bursts([
            (30.0, 24, 200_000), (60.0, 23, 205_000), (90.0, 24, 198_000),
            (200.0, 4, 900_000),
        ])
        for i in range(3, 22):        # 19 dead before the last cast
            p.deaths.append(DeathRecord(t=150.0, guid=f"P{i}", name=f"P{i}"))
        late = [b for b in shared_bursts(p, boss) if b.t == 200.0][0]
        self.assertLess(late.alive, 15)
        report = Session(cfg).add(p)
        self.assertFalse(
            [f for f in report.findings if "Blood Rage" in f.action],
            "must not blame the survivors of a wipe for not soaking",
        )


class TestSoaks(unittest.TestCase):
    """Standing in it is the job. The failure is nobody standing in it."""

    def _soak_pull(self, soaks, fails):
        """soaks: [(t, [guids])]  fails: [(t, n_players)]"""
        p = _pull(300.0)
        p.encounter_id = 1595
        p.boss = "Malkorok"
        p.players = {f"P{i}": f"P{i}" for i in range(25)}
        def hit(t, guid, spell, amount):
            p.damage_taken.append(
                DamageRecord(
                    t=t, dest_guid=guid, dest_name=guid, src_guid="Creature-1",
                    src_name="Malkorok", spell_id=spell,
                    spell_name="Imploding Energy", amount=amount, absorbed=0,
                    overkill=-1, periodic=False, hp_after=0.7,
                )
            )
        for t, guids in soaks:
            for g in guids:
                hit(t, g, 142986, 585_000)
        for t, n in fails:
            for i in range(n):
                hit(t, f"P{i}", 142987, 688_500)
        return p

    def test_soak_damage_is_never_counted_as_avoidable(self):
        """Counting it blames the people doing the job."""
        cfg = load_config()
        boss = cfg.boss(1595)
        self.assertNotIn(142986, boss.avoidable, "soak damage must not be avoidable")
        self.assertNotIn(142987, boss.avoidable, "raid punishment must not be avoidable")
        self.assertIn(142986, boss.soaks)

        p = self._soak_pull([(30.0, ["P1", "P2"])], [])
        rows = avoidable_table(p, boss, detect_roles(p))
        self.assertEqual(rows, [], "soaking must produce no avoidable rows")

    def test_missed_soak_is_reported_and_soakers_are_credited(self):
        cfg = load_config()
        p = self._soak_pull(
            [(30.0, ["P1", "P2"]), (60.0, ["P1", "P3"])],
            [(90.0, 24)],
        )
        report = Session(cfg).add(p)
        s = report.soaks[0]
        self.assertEqual(s.soaked, 2)
        self.assertEqual(s.missed, 1)
        self.assertEqual(s.soakers.get("P1"), 2)

        f = [x for x in report.findings if "unsoaked" in x.action]
        self.assertTrue(f, "a missed soak must be reported")
        joined = " ".join(f[0].evidence)
        self.assertIn("P1", joined, "soakers must be credited")
        self.assertIn("not a mistake", joined)

    def test_all_soaked_produces_no_finding(self):
        cfg = load_config()
        p = self._soak_pull([(30.0, ["P1"]), (60.0, ["P2"])], [])
        report = Session(cfg).add(p)
        self.assertFalse([x for x in report.findings if "unsoaked" in x.action])


class TestAvoidableTargets(unittest.TestCase):
    def test_auras_on_enemies_are_not_counted_as_players(self):
        """Ground fire ticks on the boss's own adds too.

        Counting those application events listed four Automated Shredders in
        the avoidable table as though they were raiders, which is how the
        impossible "29 of 25 players" reached the config note.
        """
        cfg = load_config()
        boss = cfg.boss(1606)
        p = _pull()
        p.players = {"Player-1": "Raider"}
        for guid in ("Player-1", "Creature-0-4448-1136-7803-71591-000002267A"):
            p.auras.append(AuraRecord(
                t=10.0, dest_guid=guid, dest_name=guid, src_guid="Creature-9",
                spell_id=144066, spell_name="Foulness", aura_type="DEBUFF",
                applied=True,
            ))
        rows = avoidable_table(p, boss, detect_roles(p))
        self.assertEqual(len(rows), 1, "only the raider counts")
        self.assertEqual(rows[0].player, "Raider")
        self.assertTrue(
            all("Creature-" not in r.player for r in rows),
            "no creature may appear in the avoidable table",
        )


class TestAmountThreshold(unittest.TestCase):
    """One spell id can carry an avoidable and an unavoidable component."""

    def _pull_with_hits(self, amounts):
        p = _pull(300.0)
        p.players = {"A": "A"}
        for i, amt in enumerate(amounts):
            p.damage_taken.append(
                DamageRecord(
                    t=float(i * 5), dest_guid="A", dest_name="A",
                    src_guid="Creature-1", src_name="Garrosh", spell_id=144017,
                    spell_name="Toxic Storm", amount=amt, absorbed=0,
                    overkill=-1, periodic=False, hp_after=0.8,
                )
            )
        return p

    def test_threshold_keeps_only_the_bigger_component(self):
        from wipeanalyser.config import Mechanic

        cfg = load_config()
        boss = cfg.boss(1606)
        p = self._pull_with_hits([180_000, 190_000, 320_000, 360_000])
        roles = detect_roles(p)

        # Without a threshold every hit counts.
        rows = avoidable_table(p, boss, roles)
        self.assertEqual(next(r.count for r in rows if r.spell_id == 144017), 4)

        # With one, only the hits above it do.
        boss.avoidable[144017] = Mechanic(
            spell_id=144017, name="Toxic Storm", amount_at_least=280_000
        )
        rows = avoidable_table(p, boss, roles)
        row = next(r for r in rows if r.spell_id == 144017)
        self.assertEqual(row.count, 2)
        self.assertEqual(row.damage, 680_000, "damage must exclude filtered hits")


class TestFollowUp(unittest.TestCase):
    """Did the advice from the last pull change anything."""

    def _pull_with(self, hits_per_player: int, duration: float = 300.0):
        p = _pull(duration)
        p.players = {f"Player-{i}": f"P{i}" for i in range(25)}
        t = 10.0
        for i in range(4):                    # 4 of 25, so not a config note
            for _ in range(hits_per_player):
                p.damage_taken.append(
                    DamageRecord(
                        t=t, dest_guid=f"Player-{i}", dest_name=f"P{i}",
                        src_guid="Creature-1", src_name="Boss",
                        spell_id=144334, spell_name="Iron Tomb",
                        amount=300_000, absorbed=0, overkill=-1,
                        periodic=False, hp_after=0.6,
                    )
                )
                t += 7.0
        return p

    def _follow_line(self, report):
        for f in report.findings:
            for e in f.evidence:
                if "raised after the last pull" in e:
                    return e
        return None

    # A repeated-failure finding needs TWO pulls before it is raised at all,
    # and the follow-up needs it raised in the pull before this one -- so the
    # earliest it can appear is the third pull. That matches the real logs,
    # where Death From Above was raised on Blackfuse pull 3 and followed up on
    # pulls 4 and 5.

    def test_improvement_is_reported_against_the_previous_pull(self):
        s = Session(load_config())
        s.add(self._pull_with(4))
        s.add(self._pull_with(4))          # finding raised here
        third = s.add(self._pull_with(1))  # followed up here
        line = self._follow_line(third)
        self.assertIsNotNone(line, "a repeated mechanic must be followed up")
        self.assertIn("better", line)

    def test_a_mechanic_that_stopped_entirely_is_reported(self):
        s = Session(load_config())
        s.add(self._pull_with(4))
        s.add(self._pull_with(4))
        clean = _pull(300.0)
        clean.players = {f"Player-{i}": f"P{i}" for i in range(25)}
        third = s.add(clean)
        line = self._follow_line(third)
        self.assertIsNotNone(line)
        self.assertIn("did not happen at all", line)

    def test_no_follow_up_before_the_finding_has_been_raised(self):
        s = Session(load_config())
        first = s.add(self._pull_with(4))
        self.assertIsNone(self._follow_line(first))
        second = s.add(self._pull_with(4))
        self.assertIsNone(
            self._follow_line(second),
            "the finding is only raised on this pull, so there is nothing to "
            "follow up yet",
        )

    def test_rate_is_per_minute_so_pull_length_does_not_decide_it(self):
        """A pull lasting twice as long collects twice the hits without
        anybody playing worse."""
        s = Session(load_config())
        s.add(self._pull_with(3, duration=300.0))
        s.add(self._pull_with(3, duration=300.0))
        # Same rate, double the length: twice the raw hits, no real change.
        third = s.add(self._pull_with(6, duration=600.0))
        line = self._follow_line(third)
        self.assertIsNotNone(line)
        self.assertIn("no real change", line)


class TestSpecDetection(unittest.TestCase):
    def test_a_distinctive_kit_identifies_a_spec(self):
        from wipeanalyser.roles import detect_spec

        sigs = load_config().spec_signatures
        spec, why = detect_spec(
            {"shield slam", "shield block", "devastate"}, sigs
        )
        self.assertEqual(spec, "protection warrior")
        self.assertIn("shield slam", why)

    def test_a_tie_declines_to_guess(self):
        """An unknown spec is honest; a wrong one silently splits a player
        into two people who are then compared against each other."""
        from wipeanalyser.roles import detect_spec

        sigs = load_config().spec_signatures
        # One signature ability from each of two specs.
        spec, _ = detect_spec({"chain heal", "lava burst"}, sigs)
        self.assertEqual(spec, "")

    def test_nothing_distinctive_gives_no_spec(self):
        from wipeanalyser.roles import detect_spec

        spec, _ = detect_spec({"auto attack", "healthstone"},
                              load_config().spec_signatures)
        self.assertEqual(spec, "")


class TestShareText(unittest.TestCase):
    def _report(self):
        cfg = load_config()
        p = _pull()
        p.players = {"Player-1": "Raider"}
        p.damage_taken.append(_mechanic(50.0, "Player-1"))
        p.deaths.append(DeathRecord(t=50.5, guid="Player-1", name="Raider"))
        return Session(cfg).add(p)

    def test_summary_names_the_boss_and_the_verdict(self):
        from wipeanalyser.share import verdict_text

        text = verdict_text(self._report())
        self.assertIn("Kor'kron Dark Shaman", text)
        self.assertIn("Raider", text)

    def test_summary_fits_in_a_discord_message(self):
        """A paste that Discord truncates loses the end of the list, which is
        where the least important items are -- so trim deliberately instead."""
        from wipeanalyser.share import DISCORD_LIMIT, verdict_text

        text = verdict_text(self._report())
        self.assertLessEqual(len(text), DISCORD_LIMIT)

    def test_a_tight_limit_keeps_the_headline(self):
        from wipeanalyser.share import verdict_text

        text = verdict_text(self._report(), limit=120)
        self.assertLessEqual(len(text), 120)
        self.assertIn("Kor'kron Dark Shaman", text)


class TestConfig(unittest.TestCase):
    def test_both_progression_bosses_are_configured(self):
        cfg = load_config()
        self.assertIn(1606, cfg.bosses)   # Kor'kron Dark Shaman
        self.assertIn(1601, cfg.bosses)   # Siegecrafter Blackfuse

    def test_config_extends_the_absorb_guard(self):
        load_config()
        with self.assertRaises(MetricError):
            assert_metric_valid("Sacred Shield", "uptime")

    def test_non_stacking_pairs_are_refused(self):
        cfg = load_config()
        rule = cfg.stacking_warning(
            ["Celestial Alignment", "Incarnation: Chosen of Elune"]
        )
        self.assertIsNotNone(rule, "CA + Incarnation must never be recommended")
        self.assertIn("Sequence", rule.reason)

    def test_confounded_metric_has_a_substitute(self):
        cfg = load_config()
        self.assertEqual(
            cfg.substitute_for("Blood Shield uptime"), "Death Strike cast rate"
        )


class TestDelta(unittest.TestCase):
    def test_only_mechanics_present_in_both_pulls_are_compared(self):
        """Thok's breath depends on which captive he drinks, so two pulls can
        contain different mechanics. Comparing raw totals then reports a
        difference nobody caused."""
        from wipeanalyser.session import PullDelta

        d = PullDelta(
            compared_to="pull 7", boss_pct=0.0, boss_pct_before=11.5,
            duration=383, duration_before=292, deaths=8, deaths_before=28,
            avoidable_hits=156, avoidable_hits_before=0,
            first_death=56, first_death_before=101,
            avoidable_shared=0, avoidable_shared_before=0,
            only_now=["Freezing Breath", "Tail Lash"], only_before=[],
        )
        text = " | ".join(d.lines())
        self.assertIn("occurred in both pulls", text)
        self.assertIn("Freezing Breath", text)
        self.assertNotIn("156 avoidable hits vs 0", text)

    def test_identical_mechanics_compare_directly(self):
        from wipeanalyser.session import PullDelta

        d = PullDelta(
            compared_to="pull 2", boss_pct=5.0, boss_pct_before=9.0,
            duration=300, duration_before=280, deaths=8, deaths_before=12,
            avoidable_hits=14, avoidable_hits_before=22,
            first_death=56, first_death_before=40,
            avoidable_shared=14, avoidable_shared_before=22,
            only_now=[], only_before=[],
        )
        text = " | ".join(d.lines())
        self.assertIn("14 avoidable hits vs 22", text)
        self.assertNotIn("occurred in both pulls", text)


class TestRanking(unittest.TestCase):
    def test_output_is_capped_and_root_cause_comes_first(self):
        items = [
            Finding(rank_class=RANK_THROUGHPUT, score=99.0, action="dps harder"),
            Finding(rank_class=RANK_ROOT_CAUSE, score=1.0, action="root"),
        ] + [
            Finding(rank_class=3, score=float(i), action=f"x{i}") for i in range(10)
        ]
        ranked = rank_findings(items, cap=5)
        self.assertEqual(len(ranked), 5)
        self.assertEqual(ranked[0].action, "root")
        self.assertNotIn("dps harder", [f.action for f in ranked])


class TestTailer(unittest.TestCase):
    def test_partial_lines_are_held_until_complete(self):
        """The game writes in bursts; half a line must never be parsed."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "WoWCombatLog.txt"
            path.write_text("", encoding="utf-8")
            tailer = LogTailer(path, from_start=True)
            tailer.poll()

            with open(path, "a", encoding="utf-8") as fh:
                fh.write("complete line one\npartial li")
            self.assertEqual(tailer.poll(), ["complete line one"])

            with open(path, "a", encoding="utf-8") as fh:
                fh.write("ne two\n")
            self.assertEqual(tailer.poll(), ["partial line two"])

    def test_rotation_restarts_from_the_beginning(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "WoWCombatLog.txt"
            path.write_text("old session\n", encoding="utf-8")
            tailer = LogTailer(path, from_start=True)
            self.assertEqual(tailer.poll(), ["old session"])

            # The uploader archives the file and the game starts a new one.
            path.unlink()
            path.write_text("new session\n", encoding="utf-8")
            self.assertEqual(tailer.poll(), ["new session"])

    def test_finds_a_timestamped_log(self):
        """The live log is NOT called WoWCombatLog.txt.

        This client writes one file per session named after its start time --
        WoWCombatLog-081626_195253.txt. Looking for the bare filename found
        nothing on a night that was actively logging.
        """
        from wipeanalyser.tail import find_log

        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "WoWCombatLog-081326_205908.txt").write_text("old\n", encoding="utf-8")
            newer = d / "WoWCombatLog-081626_195253.txt"
            newer.write_text("new\n", encoding="utf-8")
            import os
            import time as _t

            now = _t.time()
            os.utime(d / "WoWCombatLog-081326_205908.txt", (now - 500, now - 500))
            os.utime(newer, (now, now))

            found = find_log([str(d)])
            self.assertEqual(found, newer, "must pick the newest session log")

    def test_archive_subdirectory_is_not_picked_up(self):
        """Uploaded logs are moved into warcraftlogsarchive/ and are dead."""
        from wipeanalyser.tail import find_log

        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "warcraftlogsarchive").mkdir()
            (d / "warcraftlogsarchive" / "WoWCombatLog-010126_000000.txt").write_text(
                "archived\n", encoding="utf-8"
            )
            self.assertIsNone(find_log([str(d)]))

    def test_no_output_when_nothing_appended(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "WoWCombatLog.txt"
            path.write_text("a\n", encoding="utf-8")
            tailer = LogTailer(path, from_start=True)
            tailer.poll()
            self.assertEqual(tailer.poll(), [])


@unittest.skipUnless(
    (SAMPLES / "darkshaman_0730_h25.txt").exists(), "sample log not present"
)
class TestAgainstRealLog(unittest.TestCase):
    """Integration against the guild's own log."""

    @classmethod
    def setUpClass(cls):
        from wipeanalyser.pulls import read_pulls

        cls.pulls = read_pulls(str(SAMPLES / "darkshaman_0730_h25.txt"))

    def test_pull_count_and_result(self):
        self.assertEqual(len(self.pulls), 3)
        self.assertEqual([p.success for p in self.pulls], [False, False, True])

    def test_boss_units_are_the_two_shaman_not_their_wolves(self):
        p = self.pulls[1]
        self.assertEqual(
            p.boss_names(), ["Earthbreaker Haromm", "Wavebinder Kardris"]
        )

    def test_wipe_percentage_is_not_zero(self):
        """Adds dying dragged this to 0% before boss detection existed."""
        pct = self.pulls[1].best_boss_percent()
        self.assertIsNotNone(pct)
        self.assertGreater(pct, 1.0)

    def test_raid_composition_is_plausible(self):
        roles = detect_roles(self.pulls[1])
        tanks = sorted(r.name for r in roles.values() if r.role == "tank")
        healers = sum(1 for r in roles.values() if r.role == "healer")
        # Dark Shaman is a two-boss encounter, so three tanks is normal. An
        # earlier version of this test demanded exactly two and was encoding a
        # threshold bug that dropped a protection paladin to "dps".
        self.assertIn(len(tanks), (2, 3), f"implausible tank count: {tanks}")
        self.assertIn("Alaraya", tanks)
        self.assertIn("Cytrina", tanks)
        self.assertGreaterEqual(healers, 4, "disc priests heal mostly via absorbs")
        self.assertLessEqual(healers, 7)

    def test_a_players_role_does_not_flip_between_pulls(self):
        """Role decides who is exempt from blame, so it cannot depend on how
        much of the boss's attention someone happened to get in one pull."""
        from collections import defaultdict

        seen = defaultdict(set)
        for p in self.pulls:
            if p.duration < 60:
                continue
            for r in detect_roles(p).values():
                seen[r.name].add(r.role)
        flipped = {n: sorted(v) for n, v in seen.items() if len(v) > 1}
        self.assertEqual(flipped, {}, f"role changed between pulls: {flipped}")

    def test_cascade_deaths_are_excluded_from_blame(self):
        v = verdict(self.pulls[1])
        self.assertGreater(v.cascade_count, 0)
        self.assertLess(len(v.blameable), len(v.deaths))


if __name__ == "__main__":
    unittest.main(verbosity=2)
