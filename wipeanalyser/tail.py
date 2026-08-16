"""Follow WoWCombatLog.txt while the game is writing it.

Three things make this harder than `tail -f`:

* The game writes in BURSTS, not continuously, and a read can land in the
  middle of a line. Emitting a half-line silently corrupts a pull, so partial
  tails are held back until their newline arrives.
* The file is ROTATED between sessions -- the Warcraft Logs uploader moves it
  into warcraftlogsarchive and the game starts a fresh one. The tailer has to
  notice it is now reading a different file and start again.
* WoW keeps the file open. On Windows the handle must be opened in a way that
  does not fight the game for it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator, Optional

#: Log DIRECTORIES, most likely flavour first.
DEFAULT_LOG_DIRS = [
    r"C:\Program Files (x86)\World of Warcraft\_classic_\Logs",
    r"C:\Program Files (x86)\World of Warcraft\_retail_\Logs",
    r"C:\Program Files (x86)\World of Warcraft\_anniversary_\Logs",
    r"D:\World of Warcraft\_classic_\Logs",
]

#: The log is NOT always called WoWCombatLog.txt. This client writes a
#: timestamped name per session -- WoWCombatLog-081626_195253.txt -- so looking
#: for the bare filename finds nothing on a night that is actively logging.
#: `Archive-*` files live in a subdirectory and are excluded by not recursing.
LOG_GLOB = "WoWCombatLog*.txt"


def find_log(dirs: Optional[list[str]] = None) -> Optional[Path]:
    """Locate the live combat log: newest matching file across known dirs."""
    candidates: list[Path] = []
    for d in dirs or DEFAULT_LOG_DIRS:
        directory = Path(d)
        if not directory.is_dir():
            continue
        candidates.extend(
            p for p in directory.glob(LOG_GLOB) if p.is_file()
        )
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _signature(path: Path) -> Optional[tuple]:
    """Identity of the file behind a path, for detecting rotation."""
    try:
        st = path.stat()
    except OSError:
        return None
    # st_ino is meaningful on Windows for NTFS in modern Python; ctime is a
    # good enough tiebreak when it is not.
    return (st.st_ino, st.st_ctime)


class LogTailer:
    """Yield complete lines appended to a log file."""

    def __init__(self, path: Path | str, from_start: bool = False) -> None:
        self.path = Path(path)
        self.from_start = from_start
        self._pos = 0
        self._sig: Optional[tuple] = None
        self._remainder = ""
        self._opened = False

    def _open_fresh(self, at_end: bool) -> None:
        self._sig = _signature(self.path)
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        self._pos = size if at_end else 0
        self._remainder = ""
        self._opened = True

    def poll(self) -> list[str]:
        """Return complete lines appended since the last call."""
        if not self.path.exists():
            self._opened = False
            return []

        if not self._opened:
            self._open_fresh(at_end=not self.from_start)
            if not self.from_start:
                return []

        sig = _signature(self.path)
        try:
            size = self.path.stat().st_size
        except OSError:
            return []

        # Rotated (different file) or truncated (same name, fresh session).
        if sig != self._sig or size < self._pos:
            self._open_fresh(at_end=False)

        if size <= self._pos:
            return []

        try:
            with open(
                self.path, "r", encoding="utf-8", errors="replace", newline=""
            ) as fh:
                fh.seek(self._pos)
                chunk = fh.read()
                self._pos = fh.tell()
        except OSError:
            return []

        if not chunk:
            return []

        data = self._remainder + chunk
        # A burst can end mid-line. Hold the tail back until its newline lands.
        if data.endswith("\n"):
            self._remainder = ""
            lines = data.splitlines()
        else:
            lines = data.splitlines()
            self._remainder = lines.pop() if lines else data
        return [ln for ln in lines if ln]

    def follow(self, poll_s: float = 1.0) -> Iterator[str]:  # pragma: no cover
        import time

        while True:
            for line in self.poll():
                yield line
            time.sleep(poll_s)


def read_existing(path: Path | str) -> Iterator[str]:
    """Stream a whole file, for catching up on a session already in progress."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            yield line.rstrip("\n")
