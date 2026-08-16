"""The output type: a conclusion that can be argued with.

Every hard-won lesson in the brief was found by a raider challenging a
conclusion, not by the analysis catching itself. So a finding is not a string.
It carries the numbers it was derived from, the metric used, and -- where a
naive alternative exists -- why that alternative was rejected. A raider who
knows their spec can then see the working and correct the config.

The absorb-uptime rule is enforced here rather than documented, because a rule
that lives in a comment is a rule that gets re-broken. It was re-broken three
times in manual review before a raider caught it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Rank classes, highest impact first. Straight from the brief's weighting.
# ---------------------------------------------------------------------------

RANK_ROOT_CAUSE = 1       # directly caused this wipe
RANK_REPEATED = 2         # same mechanic, multiple players, multiple pulls
RANK_EARLY_DEATH = 3      # deaths before the phase the raid usually loses in
RANK_COOLDOWN = 4         # unused defensives / raid cooldowns in damage windows
RANK_THROUGHPUT = 5       # only when the pull was a damage or healing check

#: Not a raid action at all -- a note to whoever maintains the config. Ranked
#: last so tool hygiene can never crowd out something that lost the pull.
RANK_CONFIG = 6

RANK_NAMES = {
    RANK_ROOT_CAUSE: "root cause",
    RANK_REPEATED: "raid-wide pattern",
    RANK_EARLY_DEATH: "early death",
    RANK_COOLDOWN: "cooldown usage",
    RANK_THROUGHPUT: "throughput",
    RANK_CONFIG: "config check",
}


class MetricError(RuntimeError):
    """Raised when a finding uses a metric that is invalid for its subject."""


# ---------------------------------------------------------------------------
# Absorb shields end when CONSUMED, not when they expire.
#
# A tank under heavy melee will always show low Guard uptime no matter how well
# it is played -- low uptime can mean the absorb is doing its job. These are
# measured by cast rate against cooldown, never by uptime.
#
# Keyed by lowercased spell name so a config typo cannot slip past.
# ---------------------------------------------------------------------------

CONSUMED_ABSORBS = {
    "guard",
    "power word: shield",
    "blood shield",
    "divine aegis",
    "sacred shield",
    "shield barrier",
    "ice barrier",
    "anti-magic shell",
    "spirit shell",
    "illuminated healing",
    "savage defense",
}

#: Metrics that measure "how much of the fight was this active".
UPTIME_METRICS = {"uptime", "uptime_pct", "buff_uptime", "aura_uptime"}


def assert_metric_valid(subject: str, metric: str) -> None:
    """Refuse an uptime metric on a shield that ends when it is consumed.

    Raises MetricError so the failure is loud in tests and in development,
    rather than a quietly wrong number on a dashboard at 10:23pm.
    """
    if metric.lower() in UPTIME_METRICS and subject.lower() in CONSUMED_ABSORBS:
        raise MetricError(
            f"{subject!r} is an absorb that ends when consumed; uptime is not a "
            f"valid measure of it. Use cast rate against cooldown instead."
        )


@dataclass(slots=True)
class Finding:
    """One ranked, contestable recommendation."""

    rank_class: int
    #: ordering within a rank class; larger is more important
    score: float
    #: phrased as an action, because a list of observations is not a list of fixes
    action: str
    #: the underlying numbers, one string per fact
    evidence: list[str] = field(default_factory=list)
    #: the metric this conclusion rests on
    method: str = ""
    #: the naive alternative that was rejected, and why
    rejected: Optional[str] = None
    #: "individual" | "raid-wide" | "encounter"
    scope: str = "individual"
    players: list[str] = field(default_factory=list)
    #: which config key supplied the mechanic assumption, so it can be corrected
    config_ref: Optional[str] = None
    #: subject the metric is applied to, checked against the absorb rule
    subject: Optional[str] = None

    def __post_init__(self) -> None:
        if self.subject and self.method:
            assert_metric_valid(self.subject, self.method)

    @property
    def rank_name(self) -> str:
        return RANK_NAMES.get(self.rank_class, "other")

    def as_dict(self) -> dict:
        return {
            "rank_class": self.rank_class,
            "rank_name": self.rank_name,
            "score": self.score,
            "action": self.action,
            "evidence": self.evidence,
            "method": self.method,
            "rejected": self.rejected,
            "scope": self.scope,
            "players": self.players,
            "config_ref": self.config_ref,
        }

    def as_text(self, indent: str = "") -> str:
        lines = [f"{indent}{self.action}"]
        for e in self.evidence:
            lines.append(f"{indent}    - {e}")
        if self.method:
            lines.append(f"{indent}    method: {self.method}")
        if self.rejected:
            lines.append(f"{indent}    not measured by: {self.rejected}")
        if self.config_ref:
            lines.append(f"{indent}    config: {self.config_ref}")
        return "\n".join(lines)


def rank_findings(findings: list[Finding], cap: int = 5) -> list[Finding]:
    """Order by impact and cap the list.

    A ranked list of twenty is the same as no list, so the cap is part of the
    product, not a display detail.
    """
    ordered = sorted(findings, key=lambda f: (f.rank_class, -f.score))
    return ordered[:cap]
