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

    # spell_id -> (name, victims, hits, total damage, max single hit)
    stats: dict[int, dict] = defaultdict(
        lambda: {"name": "", "victims": set(), "hits": 0, "dmg": 0, "max": 0}
    )
    for p in pulls:
        for d in p.damage_taken:
            if is_player(d.src_guid):
                continue  # ignore player-on-player (reflects, etc.)
            s = stats[d.spell_id]
            s["name"] = d.spell_name
            s["victims"].add(d.dest_guid)
            s["hits"] += 1
            s["dmg"] += d.amount + d.absorbed
            s["max"] = max(s["max"], d.amount + d.absorbed)

    rows = sorted(stats.items(), key=lambda kv: -kv[1]["dmg"])

    print("## enemy damage sources, by total damage to the raid")
    print(f"{'spell_id':>9}  {'name':<32} {'victims':>7} {'hits':>7} "
          f"{'total':>13} {'max hit':>10}  guess")
    for spell_id, s in rows[:34]:
        victims = len(s["victims"])
        avg_hits_per_victim = s["hits"] / max(1, victims)
        if spell_id == 0:
            guess = "melee"
        elif victims <= 3 and s["max"] > 150_000:
            guess = "TANK BUSTER"
        elif victims >= 6:
            guess = "avoidable?" if avg_hits_per_victim < 12 else "raid-wide dot"
        else:
            guess = ""
        print(
            f"{spell_id:>9}  {s['name'][:32]:<32} {victims:>7} {s['hits']:>7} "
            f"{s['dmg']:>13,} {s['max']:>10,}  {guess}"
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
    seen: dict[int, tuple[str, int]] = {}
    for p in pulls:
        for i in p.interrupts:
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
