"""Empirically determine the combat-log field layout for THIS client build.

The brief warns that field ordering differs between retail and MoP Classic and
between patches. So we do not trust documentation: we read a real log and report
what each column actually contains, with evidence.

Usage:  python tools/probe_layout.py <logfile> [max_lines]
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict


def split_fields(payload: str) -> list[str]:
    """Split a combat-log payload on commas, respecting double-quoted strings."""
    out: list[str] = []
    buf: list[str] = []
    in_quotes = False
    for ch in payload:
        if ch == '"':
            in_quotes = not in_quotes
            continue
        if ch == "," and not in_quotes:
            out.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    out.append("".join(buf))
    return out


def classify(v: str) -> str:
    if v == "nil":
        return "nil"
    if v in ("0", "1") :
        return "bool01"
    if v.startswith("0x"):
        return "hex"
    if v.startswith(("Player-", "Creature-", "Pet-", "Vehicle-", "GameObject-")):
        return "guid"
    if v == "0000000000000000":
        return "nullguid"
    try:
        f = float(v)
        return "float" if "." in v else ("int_neg" if f < 0 else "int")
    except ValueError:
        return "str"


def main() -> int:
    path = sys.argv[1]
    max_lines = int(sys.argv[2]) if len(sys.argv) > 2 else 400_000

    # event -> field index -> Counter of value kinds
    shapes: dict[str, dict[int, Counter]] = defaultdict(lambda: defaultdict(Counter))
    examples: dict[str, list[list[str]]] = defaultdict(list)
    widths: dict[str, Counter] = defaultdict(Counter)

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            if i >= max_lines:
                break
            # "8/11/2026 19:56:46.6141  EVENT,a,b,c"
            sep = line.find("  ")
            if sep < 0:
                continue
            payload = line[sep + 2 :].rstrip("\n")
            if not payload:
                continue
            fields = split_fields(payload)
            event = fields[0]
            widths[event][len(fields)] += 1
            for idx, val in enumerate(fields[1:], start=0):
                shapes[event][idx][classify(val)] += 1
            if len(examples[event]) < 3:
                examples[event].append(fields[1:])

    interesting = [
        "SPELL_DAMAGE",
        "SWING_DAMAGE",
        "SPELL_PERIODIC_DAMAGE",
        "RANGE_DAMAGE",
        "SPELL_HEAL",
        "SPELL_ABSORBED",
        "UNIT_DIED",
        "SPELL_CAST_SUCCESS",
        "SPELL_AURA_APPLIED",
        "SPELL_INTERRUPT",
        "SPELL_DISPEL",
        "ENCOUNTER_START",
        "ENCOUNTER_END",
        "SPELL_MISSED",
        "SWING_MISSED",
    ]

    print("=" * 78)
    print("EVENT FREQUENCY AND FIELD WIDTHS")
    print("=" * 78)
    for event, w in sorted(widths.items(), key=lambda kv: -sum(kv[1].values()))[:30]:
        total = sum(w.values())
        widths_desc = ", ".join(f"{n} fields x{c}" for n, c in w.most_common(4))
        print(f"{event:<28} {total:>8}  ({widths_desc})")

    for event in interesting:
        if event not in shapes:
            continue
        print()
        print("=" * 78)
        print(f"{event}   widths={dict(widths[event].most_common(3))}")
        print("=" * 78)
        for idx in sorted(shapes[event]):
            kinds = shapes[event][idx].most_common(3)
            kind_desc = " ".join(f"{k}:{c}" for k, c in kinds)
            samples = [ex[idx] for ex in examples[event] if idx < len(ex)]
            sample_desc = " | ".join(s[:34] for s in samples[:2])
            print(f"  [{idx:>2}] {kind_desc:<34} e.g. {sample_desc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
