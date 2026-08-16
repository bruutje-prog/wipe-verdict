"""Milestone 1 validation: list every pull in a log file.

Usage:  python tools/list_pulls.py <logfile> [encounter_id]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wipeverdict.pulls import read_pulls  # noqa: E402


def main() -> int:
    path = sys.argv[1]
    encounter_id = int(sys.argv[2]) if len(sys.argv) > 2 else None

    t0 = time.time()
    pulls = read_pulls(path, encounter_id=encounter_id)
    elapsed = time.time() - t0

    print(f"{len(pulls)} pulls in {elapsed:.1f}s\n")
    header = (
        f"{'#':>3}  {'boss':<26} {'diff':<11} {'dur':>6} {'result':<7} "
        f"{'deaths':>6} {'low%':>6}  players"
    )
    print(header)
    print("-" * len(header))
    for i, p in enumerate(pulls, 1):
        low = p.best_boss_percent()
        low_s = f"{low:.1f}" if low is not None else "-"
        result = "KILL" if p.success else "wipe"
        print(
            f"{i:>3}  {p.boss:<26} {p.difficulty:<11} "
            f"{p.fmt(p.duration):>6} {result:<7} {len(p.deaths):>6} {low_s:>6}  "
            f"{len(p.players)}"
        )

    total_kills = sum(1 for p in pulls if p.success)
    print(f"\n{len(pulls)} pulls, {total_kills} kills, {len(pulls)-total_kills} wipes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
