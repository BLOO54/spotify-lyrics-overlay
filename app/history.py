"""A short history of the important pop-ups (AI sync results, model downloads, saved timing),
so you can see them later in Settings → Notifications. Kept in notifications.json (last 100)."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)
MAX_ENTRIES = 100


class NotificationLog:
    def __init__(self, path: Path | None):
        self.path = path
        self.entries: list[dict] = []
        if path and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.entries = [e for e in data if isinstance(e, dict) and "text" in e][-MAX_ENTRIES:]
            except Exception:
                log.exception("could not read the notification history")

    def add(self, text: str, kind: str = "info", when: datetime | None = None) -> None:
        when = when or datetime.now()
        self.entries.append({"time": when.isoformat(timespec="seconds"), "text": text, "kind": kind})
        self.entries = self.entries[-MAX_ENTRIES:]
        self._save()

    def clear(self) -> None:
        self.entries = []
        self._save()

    def newest_first(self) -> list[dict]:
        return list(reversed(self.entries))

    def _save(self) -> None:
        if not self.path:
            return
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.entries, indent=1, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception:
            log.exception("could not save the notification history")


def when_text(iso: str, now: datetime | None = None) -> str:
    """'Today 7:52 PM', 'Yesterday 9:03 AM' or 'Nov 3, 7:52 PM'."""
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    now = now or datetime.now()
    clock = t.strftime("%I:%M %p").lstrip("0")
    days = (now.date() - t.date()).days
    if days == 0:
        return f"Today {clock}"
    if days == 1:
        return f"Yesterday {clock}"
    return f"{t.strftime('%b')} {t.day}, {clock}"
