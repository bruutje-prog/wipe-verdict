"""Local dashboard. Runs on the raiding machine, reads a local file, uploads nothing.

The verdict has to be on screen within a few seconds of a wipe, because the
window in which anyone will read it is the gap before the next pull. So the
tailer runs in a background thread and the page polls a small JSON endpoint
rather than re-rendering anything expensive.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, render_template

from .config import load_config
from .logparse import parse_line
from .pulls import PullSegmenter
from .session import PullReport, Session
from .tail import LogTailer, find_log, read_existing

POLL_SECONDS = 1.0

#: How often to look for a newer session log appearing beside the current one.
LOG_RECHECK_SECONDS = 20.0


def _report_json(report: PullReport, pull_id: int = -1) -> dict:
    p = report.pull
    v = report.verdict
    return {
        "id": pull_id,
        "boss": p.boss,
        "difficulty": p.difficulty,
        "attempt": p.attempt,
        "duration": p.fmt(p.duration),
        "success": p.success,
        "boss_percent": p.best_boss_percent(),
        "headline": v.headline(p.fmt),
        "deaths_total": len(v.deaths),
        "deaths_cascade": v.cascade_count,
        "deaths_blameable": len(v.blameable),
        "findings": [f.as_dict() for f in report.findings],
        "notes": [f.as_dict() for f in report.notes],
        "delta": (
            {
                "compared_to": report.delta.compared_to,
                "lines": report.delta.lines(),
                "progressed": report.delta.progressed,
            }
            if report.delta
            else None
        ),
        "deaths": [
            {
                "at": p.fmt(d.t),
                "name": d.name,
                "role": d.role,
                "tag": "cascade" if d.is_cascade else d.signature,
                "killer": d.killer,
                "source": d.killer_source,
                "cascade": d.is_cascade,
            }
            for d in v.deaths[:20]
        ],
        "avoidable": [
            {
                "player": r.player,
                "role": r.role,
                "mechanic": r.mechanic,
                "count": r.count,
                "damage": r.damage,
                "counted_by": r.counted_by,
            }
            for r in report.avoidable[:15]
        ],
    }


class Monitor:
    """Tails the log and keeps the latest analysed pull available."""

    def __init__(
        self,
        log_path: Optional[Path] = None,
        config_dir: Optional[str] = None,
        catch_up: bool = True,
    ) -> None:
        self.log_path = Path(log_path) if log_path else find_log()
        #: An explicit path is never second-guessed; an auto-detected one is
        #: re-resolved while we wait, because the file may not exist yet.
        self._explicit = log_path is not None
        self.session = Session(load_config(config_dir))
        self.segmenter = PullSegmenter()
        self.catch_up = catch_up
        self._lock = threading.Lock()
        self._latest: Optional[PullReport] = None
        self._status = "starting"
        self._lines_seen = 0
        #: None until a COMBAT_LOG_VERSION header has been seen
        self._advanced: Optional[bool] = None
        self._thread: Optional[threading.Thread] = None

    # -- state ----------------------------------------------------------
    def state(self) -> dict:
        with self._lock:
            latest = self._latest
            status = self._status
            lines = self._lines_seen
            advanced = self._advanced
            reports = self.session.reports
            latest_id = len(reports) - 1
            # The index is the pull's identity, so the dashboard can ask for an
            # earlier attempt by id rather than by guessing from a label.
            history = [
                {
                    "id": i,
                    "boss": r.pull.boss,
                    "attempt": r.pull.attempt,
                    "duration": r.pull.fmt(r.pull.duration),
                    "success": r.pull.success,
                    "percent": r.pull.best_boss_percent(),
                    "deaths": len(r.pull.deaths),
                }
                for i, r in enumerate(reports)
            ][-14:]
        return {
            "status": status,
            "log_path": str(self.log_path) if self.log_path else None,
            "lines_seen": lines,
            "advanced_logging": advanced,
            "in_pull": self.segmenter.current is not None,
            "current_boss": (
                self.segmenter.current.boss if self.segmenter.current else None
            ),
            "history": list(reversed(history)),
            "latest": _report_json(latest, latest_id) if latest else None,
        }

    def report_json(self, pull_id: int) -> Optional[dict]:
        """An earlier attempt, by index, for revisiting it mid-raid."""
        with self._lock:
            reports = list(self.session.reports)
        if 0 <= pull_id < len(reports):
            return _report_json(reports[pull_id], pull_id)
        return None

    # -- ingestion ------------------------------------------------------
    def _consume(self, line: str) -> None:
        ev = parse_line(line)
        if ev is None:
            return
        # Advanced Combat Logging is a game setting that can be off. Without it
        # there are no health values, so boss percentage and "died from full
        # health" are impossible. That is worth saying loudly on the dashboard
        # rather than silently reporting a blank column all night.
        if ev.event == "COMBAT_LOG_VERSION":
            f = ev.fields
            if "ADVANCED_LOG_ENABLED" in f:
                idx = f.index("ADVANCED_LOG_ENABLED")
                if idx + 1 < len(f):
                    with self._lock:
                        self._advanced = f[idx + 1] == "1"
            return
        finished = self.segmenter.feed(ev)
        if finished is None:
            return
        # Ignore resets and mis-pulls; they have no story.
        if finished.duration < 20:
            return
        report = self.session.add(finished)
        with self._lock:
            self._latest = report

    def _wait_for_log(self) -> None:  # pragma: no cover - timing dependent
        """Block until the log exists.

        The tool is started BEFORE the raid, and WoW does not create
        WoWCombatLog.txt until logging is turned on. Giving up at startup meant
        sitting dead all night on the one workflow that matters.
        """
        announced = False
        while self.log_path is None or not self.log_path.exists():
            if not announced:
                with self._lock:
                    self._status = (
                        "waiting for WoWCombatLog.txt - turn logging on with "
                        "/combatlog in game"
                    )
                announced = True
            if not self._explicit:
                found = find_log()
                if found is not None:
                    self.log_path = found
                    break
            time.sleep(2.0)

    def run(self) -> None:  # pragma: no cover - needs a live file
        self._wait_for_log()

        if self.catch_up:
            with self._lock:
                self._status = "reading tonight's log so far..."
            count = 0
            for line in read_existing(self.log_path):
                self._consume(line)
                count += 1
            with self._lock:
                self._lines_seen = count

        tailer = LogTailer(self.log_path, from_start=False)
        tailer.poll()  # position at end
        with self._lock:
            self._status = "watching"

        since_check = 0.0
        while True:
            lines = tailer.poll()
            if lines:
                for line in lines:
                    self._consume(line)
                with self._lock:
                    self._lines_seen += len(lines)

            # This client names each session's log after its start time, so a
            # client restart mid-raid produces a NEW file and leaves the old one
            # frozen. Without this the dashboard would sit on a dead file for
            # the rest of the night looking perfectly healthy.
            since_check += POLL_SECONDS
            if not self._explicit and since_check >= LOG_RECHECK_SECONDS:
                since_check = 0.0
                newest = find_log()
                if newest is not None and newest != self.log_path:
                    with self._lock:
                        self._status = f"switched to a newer log: {newest.name}"
                    self.log_path = newest
                    tailer = LogTailer(newest, from_start=True)
            time.sleep(POLL_SECONDS)

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()


def create_app(monitor: Monitor) -> Flask:
    app = Flask(__name__)

    @app.route("/")
    def index():
        return render_template("dashboard.html")

    @app.route("/api/state")
    def state():
        return jsonify(monitor.state())

    @app.route("/api/pull/<int:pull_id>")
    def pull(pull_id: int):
        data = monitor.report_json(pull_id)
        if data is None:
            return jsonify({"error": "no such pull"}), 404
        return jsonify(data)

    return app


def serve(
    log_path: Optional[str] = None,
    port: int = 8765,
    config_dir: Optional[str] = None,
) -> None:  # pragma: no cover
    monitor = Monitor(Path(log_path) if log_path else None, config_dir)
    if monitor.log_path is None:
        print("No WoWCombatLog.txt yet - waiting for it.")
        print("Turn logging on in game with /combatlog (or use --log <path>).")
    else:
        print(f"Watching {monitor.log_path}")
    print(f"Dashboard: http://127.0.0.1:{port}")
    monitor.start()
    app = create_app(monitor)
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
