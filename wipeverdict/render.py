"""Text rendering of a pull report, for the terminal and for tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .session import PullReport


def render_report(report: "PullReport", width: int = 78) -> str:
    p = report.pull
    lines: list[str] = []
    rule = "=" * width

    result = "KILL" if p.success else "WIPE"
    pct = p.best_boss_percent()
    pct_s = f" at {pct:.1f}%" if pct is not None and not p.success else ""
    lines.append(rule)
    lines.append(
        f"{p.boss} ({p.difficulty}) - pull {p.attempt} - "
        f"{p.fmt(p.duration)} - {result}{pct_s}"
    )
    lines.append(rule)

    lines.append("")
    lines.append(report.verdict.headline(p.fmt))

    if report.findings:
        lines.append("")
        lines.append("WHAT TO CHANGE BEFORE THE NEXT PULL")
        lines.append("-" * width)
        for i, f in enumerate(report.findings, 1):
            tag = f"[{f.rank_name}]"
            lines.append(f"{i}. {f.action}  {tag}")
            for e in f.evidence:
                lines.append(f"      - {e}")
            if f.method:
                lines.append(f"      method: {f.method}")
            if f.rejected:
                lines.append(f"      not measured by: {f.rejected}")
            if f.config_ref:
                lines.append(f"      config: {f.config_ref}")
            lines.append("")

    if report.notes:
        lines.append("NOTES ON THE CONFIG (not raid actions)")
        lines.append("-" * width)
        for n in report.notes:
            lines.append(f"   {n.action}")
            for e in n.evidence:
                lines.append(f"      - {e}")
        lines.append("")

    if report.delta:
        lines.append("VERSUS BEST PREVIOUS ATTEMPT")
        lines.append("-" * width)
        lines.append(f"compared to {report.delta.compared_to}")
        for line in report.delta.lines():
            lines.append(f"   {line}")
        lines.append("")

    deaths = report.verdict.deaths
    if deaths:
        lines.append("DEATHS")
        lines.append("-" * width)
        lines.append(
            f"{len(deaths)} total, {report.verdict.cascade_count} cascade, "
            f"{len(report.verdict.blameable)} carrying information"
        )
        for d in deaths[:12]:
            tag = "cascade" if d.is_cascade else d.signature
            lines.append(
                f"   {p.fmt(d.t):>5}  {d.name:<14} {d.role:<7} {tag:<16} "
                f"{d.killer} ({d.killer_source})"
            )
        if len(deaths) > 12:
            lines.append(f"   ... and {len(deaths) - 12} more")
        lines.append("")

    if report.avoidable:
        lines.append("AVOIDABLE DAMAGE (hits are the leading indicator)")
        lines.append("-" * width)
        lines.append(
            f"{'player':<14} {'role':<7} {'mechanic':<20} {'n':>4} "
            f"{'damage':>12}  counted by"
        )
        for r in report.avoidable[:12]:
            lines.append(
                f"{r.player:<14} {r.role:<7} {r.mechanic:<20} {r.count:>4} "
                f"{r.damage:>12,}  {r.counted_by}"
            )
        lines.append("")

    return "\n".join(lines)
