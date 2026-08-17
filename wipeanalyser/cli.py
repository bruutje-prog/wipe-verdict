"""Command line entry point.

    python -m wipeanalyser report <logfile> [--encounter 1606] [--pull 2]
    python -m wipeanalyser pulls  <logfile>
    python -m wipeanalyser live   [--log <path>] [--port 8765]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import load_config
from .pulls import read_pulls
from .render import render_report
from .session import Session


def cmd_pulls(args: argparse.Namespace) -> int:
    pulls = read_pulls(args.logfile, encounter_id=args.encounter)
    # So the boss percentage here matches what `report` shows.
    cfg = load_config(args.config)
    for p in pulls:
        boss = cfg.boss(p.encounter_id)
        if boss and boss.boss_units:
            p.configured_bosses = list(boss.boss_units)
    print(f"{len(pulls)} pulls\n")
    for i, p in enumerate(pulls, 1):
        pct = p.best_boss_percent()
        pct_s = f"{pct:5.1f}%" if pct is not None else "     -"
        print(
            f"{i:>3}  {p.boss:<26} {p.difficulty:<11} {p.fmt(p.duration):>6} "
            f"{'KILL' if p.success else 'wipe':<5} {len(p.deaths):>3} deaths  {pct_s}"
        )
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    pulls = read_pulls(args.logfile, encounter_id=args.encounter)
    if not pulls:
        print("no pulls found")
        return 1
    session = Session(load_config(args.config))
    reports = [session.add(p) for p in pulls]

    chosen = reports
    if args.pull:
        if args.pull < 1 or args.pull > len(reports):
            print(f"pull {args.pull} out of range (1-{len(reports)})")
            return 1
        chosen = [reports[args.pull - 1]]
    elif args.last:
        chosen = reports[-1:]

    for r in chosen:
        if r.pull.duration < args.min_duration:
            continue
        print(render_report(r))
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    from .server import serve

    serve(log_path=args.log, port=args.port, config_dir=args.config)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="wipeanalyser")
    ap.add_argument(
        "--config", default=None, help="config directory (default: ./config)"
    )
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("pulls", help="list pulls in a log file")
    p.add_argument("logfile")
    p.add_argument("--encounter", type=int, default=None)
    p.set_defaults(func=cmd_pulls)

    p = sub.add_parser("report", help="full wipe verdict for pulls in a log file")
    p.add_argument("logfile")
    p.add_argument("--encounter", type=int, default=None)
    p.add_argument("--pull", type=int, default=None, help="1-based pull index")
    p.add_argument("--last", action="store_true", help="only the final pull")
    p.add_argument(
        "--min-duration", type=float, default=30.0,
        help="skip resets and mis-pulls shorter than this",
    )
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("live", help="tail the live log and serve the dashboard")
    p.add_argument(
        "--log", default=None,
        help="path to WoWCombatLog.txt (auto-detected if omitted)",
    )
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(func=cmd_live)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
