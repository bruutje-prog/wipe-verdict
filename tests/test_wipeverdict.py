"""Tests for Wipe Verdict.

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

from wipeverdict.analysis import alive_time, avoidable_table, damage_windows
from wipeverdict.config import load_config
from wipeverdict.deaths import (
    SIG_LOOSE_BOSS,
    SIG_TANK_MITIGATION,
    analyse_deaths,
    verdict,
)
from wipeverdict.findings import (
    Finding,
    MetricError,
    assert_metric_valid,
    rank_findings,
    RANK_ROOT_CAUSE,
    RANK_THROUGHPUT,
)
from wipeverdict.logparse import parse_line, split_fields
from wipeverdict.pulls import (
    AuraRecord,
    CastRecord,
    DamageRecord,
    DeathRecord,
    HealRecord,
    Pull,
    PullSegmenter,
)
from wipeverdict.roles import TANK, detect_roles
from wipeverdict.tail import LogTailer

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
        p.players = {"A": "A"}
        # Toxic Mist (144089) is configured count: applications.
        for i in range(100):
            p.damage_taken.append(
                DamageRecord(
                    t=float(i), dest_guid="A", dest_name="A",
                    src_guid="Creature-1", src_name="Boss", spell_id=144089,
                    spell_name="Toxic Mist", amount=1000, absorbed=0,
                    overkill=-1, periodic=True, hp_after=None,
                )
            )
        p.auras.append(AuraRecord(
            t=0.0, dest_guid="A", dest_name="A", src_guid="Creature-1",
            spell_id=144089, spell_name="Toxic Mist", aura_type="DEBUFF",
            applied=True,
        ))
        rows = avoidable_table(p, boss, detect_roles(p))
        mist = next(r for r in rows if r.spell_id == 144089)
        self.assertEqual(mist.count, 1, "ticks must not be counted as hits")
        self.assertEqual(mist.counted_by, "applications")
        self.assertEqual(mist.damage, 100_000, "damage still totals every tick")


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
        from wipeverdict.session import PullDelta

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
        from wipeverdict.session import PullDelta

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
        from wipeverdict.pulls import read_pulls

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
        tanks = sum(1 for r in roles.values() if r.role == "tank")
        healers = sum(1 for r in roles.values() if r.role == "healer")
        self.assertEqual(tanks, 2)
        self.assertGreaterEqual(healers, 4, "disc priests heal mostly via absorbs")
        self.assertLessEqual(healers, 7)

    def test_cascade_deaths_are_excluded_from_blame(self):
        v = verdict(self.pulls[1])
        self.assertGreater(v.cascade_count, 0)
        self.assertLess(len(v.blameable), len(v.deaths))


if __name__ == "__main__":
    unittest.main(verbosity=2)
