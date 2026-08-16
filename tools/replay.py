"""Replay a recorded log into a file as if the game were writing it.

This is how the live path gets tested without waiting for a raid night. It
deliberately writes in BURSTS and splits some writes mid-line, because that is
what WoW actually does and it is the case that breaks naive tailers.

Usage:  python tools/replay.py <source-log> <target-file> [--chunk 4000] [--delay 0.05]
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("target")
    ap.add_argument("--chunk", type=int, default=4000, help="lines per burst")
    ap.add_argument("--delay", type=float, default=0.05, help="seconds between bursts")
    ap.add_argument("--truncate", action="store_true", help="start the target empty")
    args = ap.parse_args()

    target = Path(args.target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if args.truncate or not target.exists():
        target.write_text("", encoding="utf-8")

    written = 0
    buf: list[str] = []
    with open(args.source, "r", encoding="utf-8", errors="replace") as src:
        for line in src:
            buf.append(line)
            if len(buf) < args.chunk:
                continue
            blob = "".join(buf)
            buf.clear()
            # Split the burst mid-line, the way a buffered write lands.
            cut = len(blob) // 2
            with open(target, "a", encoding="utf-8") as out:
                out.write(blob[:cut])
                out.flush()
            time.sleep(args.delay / 2)
            with open(target, "a", encoding="utf-8") as out:
                out.write(blob[cut:])
                out.flush()
            written += args.chunk
            print(f"  {written:,} lines", end="\r", flush=True)
            time.sleep(args.delay / 2)

    if buf:
        with open(target, "a", encoding="utf-8") as out:
            out.write("".join(buf))
    print(f"\nreplayed {written + len(buf):,} lines into {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
