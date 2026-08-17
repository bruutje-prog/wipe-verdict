"""Render a verdict as text somebody can paste into Discord.

A verdict that lives only on one person's second monitor has an audience of
one. The raid leader has to SAY it, in the sixty seconds before the next pull,
and the raid mostly reads Discord.

So this is deliberately short. The dashboard can afford evidence lines and
method notes; a paste into raid chat cannot, and a wall of text gets skimmed
past exactly like the ranked-list-of-twenty the cap exists to prevent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .session import PullReport

#: Discord renders a 2000-character message and truncates beyond it.
DISCORD_LIMIT = 1900


def verdict_text(report: "PullReport", limit: int = DISCORD_LIMIT) -> str:
    """A short, pasteable summary of one pull."""
    p = report.pull
    v = report.verdict

    result = "KILL" if p.success else "WIPE"
    pct = p.best_boss_percent()
    if not p.success and pct is not None:
        result += f" at {pct:.1f}%"

    lines = [f"**{p.boss}** ({p.difficulty}) pull {p.attempt} - {p.fmt(p.duration)} - {result}"]
    lines.append(v.headline(p.fmt))

    if report.findings:
        lines.append("")
        for i, f in enumerate(report.findings, 1):
            lines.append(f"{i}. {f.action}")

    if report.delta:
        lines.append("")
        lines.append(f"vs {report.delta.compared_to}:")
        for line in report.delta.lines()[:3]:
            lines.append(f"  - {line}")

    if v.deaths:
        lines.append("")
        lines.append(
            f"{len(v.deaths)} deaths, {v.cascade_count} of them cascade "
            f"(not attributed to anyone)"
        )

    text = "\n".join(lines)
    if len(text) <= limit:
        return text

    # Trim from the least important end rather than cutting mid-sentence.
    while len(lines) > 3 and len("\n".join(lines)) > limit:
        lines.pop()
    text = "\n".join(lines)
    return text if len(text) <= limit else text[: limit - 1] + "…"
