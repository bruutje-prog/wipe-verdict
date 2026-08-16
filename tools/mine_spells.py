"""Seed the boss config from a real log rather than from a spell database.

The brief requires spell IDs to be read from this client build's own logs, so
that a config entry cannot silently refer to an ID that does not match. This
prints candidate entries for:

  * avoidable    - enemy spells that hit many players (area mechanics)
  * tank_busters - enemy spells that hit very few players very hard
  * interruptible- enemy casts the raid actually interrupted at least once
  * player roles - the abilities each player cast, for role detection

Usage:  python tools/mine_spells.py <logfile> [encounter_id]
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wipeverdict.logparse import is_player  # noqa: E402
from wipeverdict.roles import detect_roles  # noqa: E402
from wipeverdict.pulls import read_pulls  # noqa: E402


def main() -> int:
    path = sys.argv[1]
    encounter_id = int(sys.argv[2]) if len(sys.argv) > 2 else None
    pulls = read_pulls(path, encounter_id=encounter_id)
    pulls = [p for p in pulls if p.duration > 30]
    if not pulls:
        print("no pulls of usable length")
        return 1

    print(f"# mined from {len(pulls)} pulls of {pulls[0].boss} "
          f"({pulls[0].difficulty}), encounter_id {pulls[0].encounter_id}\n")

    # spell_id -> stats, including WHO takes it. Role share is what tells a
    # frontal breath (unavoidable for the tank holding the boss) apart from a
    # ground effect anyone can step out of.
    stats: dict[int, dict] = defaultdict(
        lambda: {
            "name": "", "victims": set(), "hits": 0, "dmg": 0, "max": 0,
            "tank_hits": 0, "tank_victims": set(),
        }
    )
    raid = set()
    n_tanks = 0
    for p in pulls:
        roles = detect_roles(p)
        raid.update(p.players)
        n_tanks = max(n_tanks, sum(1 for r in roles.values() if r.role == "tank"))
        for d in p.damage_taken:
            if is_player(d.src_guid):
                continue  # ignore player-on-player (reflects, etc.)
            s = stats[d.spell_id]
            s["name"] = d.spell_name
            s["victims"].add(d.dest_guid)
            s["hits"] += 1
            s["dmg"] += d.amount + d.absorbed
            s["max"] = max(s["max"], d.amount + d.absorbed)
            if d.dest_guid in roles and roles[d.dest_guid].role == "tank":
                s["tank_hits"] += 1
                s["tank_victims"].add(d.dest_guid)

    raid_size = max(len(raid), 1)
    rows = sorted(stats.items(), key=lambda kv: -kv[1]["dmg"])

    print(f"## enemy damage sources ({raid_size} raiders, {n_tanks} tanks)")
    print(f"{'spell_id':>9}  {'name':<30} {'victims':>7} {'hits':>7} "
          f"{'total':>13} {'max hit':>10} {'tank%':>6}  suggestion")
    for spell_id, s in rows[:34]:
        victims = len(s["victims"])
        share = victims / raid_size
        per_victim = s["hits"] / max(1, victims)
        tank_share = s["tank_hits"] / max(1, s["hits"])
        # A tank is roughly n_tanks/raid_size of the raid. Taking a lot more
        # than that share of a mechanic means it follows whoever is tanking.
        expected_tank = (n_tanks / raid_size) if n_tanks else 0.08

        if spell_id == 0:
            guess = "melee"
        elif victims <= 3 and s["max"] > 150_000:
            guess = "TANK BUSTER -> tank_busters"
        elif share >= 0.8 and per_victim >= 12:
            guess = "unavoidable raid damage -> OMIT"
        elif tank_share >= max(0.35, expected_tank * 3):
            guess = "avoidable BUT exempt_roles:[tank]"
        elif share >= 0.25:
            guess = "avoidable"
        else:
            guess = ""
        print(
            f"{spell_id:>9}  {s['name'][:30]:<30} {victims:>7} {s['hits']:>7} "
            f"{s['dmg']:>13,} {s['max']:>10,} {tank_share*100:5.0f}%  {guess}"
        )

    print("\n## hostile casts with a cast time (interruptible candidates)")
    started: dict[int, list] = defaultdict(lambda: ["", set(), 0])
    for p in pulls:
        for c in p.casts:
            if c.hostile and c.started:
                slot = started[c.spell_id]
                slot[0] = c.spell_name
                slot[1].add(c.src_name)
                slot[2] += 1
    for spell_id, (name, casters, count) in sorted(
        started.items(), key=lambda kv: -kv[1][2]
    )[:12]:
        who = ", ".join(sorted(casters)[:2])
        print(f"{spell_id:>9}  {name:<32} x{count:<5} by {who}")

    print("\n## enemy casts that were interrupted at least once (interruptible)")
    # Only interrupts performed BY the raid. Bosses interrupt players too --
    # Thok's Deafening Screech fills this list with the raid's own Chain Heals
    # if the source is not checked.
    seen: dict[int, tuple[str, int]] = {}
    for p in pulls:
        for i in p.interrupts:
            if not is_player(i.src_guid):
                continue
            name, count = seen.get(i.extra_spell_id, (i.extra_spell_name, 0))
            seen[i.extra_spell_id] = (name, count + 1)
    for spell_id, (name, count) in sorted(seen.items(), key=lambda kv: -kv[1][1]):
        print(f"{spell_id:>9}  {name:<32} interrupted {count}x")

    print("\n## dispels performed (dispellable debuffs)")
    seen_d: dict[int, tuple[str, int]] = {}
    for p in pulls:
        for d in p.dispels:
            name, count = seen_d.get(d.extra_spell_id, (d.extra_spell_name, 0))
            seen_d[d.extra_spell_id] = (name, count + 1)
    for spell_id, (name, count) in sorted(seen_d.items(), key=lambda kv: -kv[1][1]):
        print(f"{spell_id:>9}  {name:<32} dispelled {count}x")

    print("\n## per-player cast profile (for role/spec detection)")
    p = max(pulls, key=lambda x: x.duration)
    by_player: dict[str, set[str]] = defaultdict(set)
    for c in p.casts:
        if is_player(c.src_guid) and not c.started:
            by_player[c.src_name].add(c.spell_name)
    melee_taken: dict[str, int] = defaultdict(int)
    for d in p.damage_taken:
        if d.spell_id == 0 and not is_player(d.src_guid):
            melee_taken[d.dest_name] += d.amount + d.absorbed
    for name in sorted(by_player, key=lambda n: -melee_taken.get(n, 0)):
        top = sorted(by_player[name])[:9]
        print(f"  {name:<16} melee_taken={melee_taken.get(name,0):>10,}  {', '.join(top)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
