"""Metadatos de una grabación (session.json) para poder reprocesarla o recuperarla tras un corte."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

MIC_FILE = "mic.wav"
SYSTEM_FILE = "meeting.wav"
TRANSCRIPT_FILE = "transcript.md"
SESSION_FILE = "session.json"


class Session:
    def __init__(self, directory: Path, data: dict):
        self.dir = Path(directory)
        self.data = data

    @classmethod
    def create(cls, recordings_dir: Path, started: datetime) -> "Session":
        base = started.strftime("%Y-%m-%d_%H%M")
        d = recordings_dir / base
        i = 2
        while d.exists():
            d = recordings_dir / f"{base}_{i}"
            i += 1
        d.mkdir(parents=True)
        s = cls(d, {
            "version": 1,
            "started": started.isoformat(timespec="seconds"),
            "stopped": None,
            "status": "recording",  # recording | stopped | interrupted
            "pid": os.getpid(),
            "channels": {},
            "events": [],
            "transcription": None,
        })
        s.save()
        return s

    @classmethod
    def load(cls, directory: Path) -> "Session":
        directory = Path(directory)
        f = directory / SESSION_FILE
        if f.exists():
            return cls(directory, json.loads(f.read_text()))
        # Carpeta sin session.json (p.ej. copiada a mano): inferir lo mínimo.
        started = datetime.fromtimestamp((directory / MIC_FILE).stat().st_mtime) if (directory / MIC_FILE).exists() else datetime.now()
        return cls(directory, {"version": 1, "started": started.isoformat(timespec="seconds"),
                               "status": "stopped", "channels": {}, "events": []})

    @property
    def started(self) -> datetime:
        return datetime.fromisoformat(self.data["started"])

    def add_event(self, **event) -> None:
        self.data.setdefault("events", []).append(event)

    def save(self) -> None:
        tmp = self.dir / (SESSION_FILE + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False) + "\n")
        tmp.replace(self.dir / SESSION_FILE)

    @property
    def mic_path(self) -> Path:
        return self.dir / MIC_FILE

    @property
    def system_path(self) -> Path:
        return self.dir / SYSTEM_FILE

    @property
    def transcript_path(self) -> Path:
        return self.dir / TRANSCRIPT_FILE


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def find_interrupted(recordings_dir: Path) -> list[Session]:
    """Sesiones que quedaron en estado 'recording' porque la app se cerró de golpe."""
    out = []
    if not recordings_dir.exists():
        return out
    for f in sorted(recordings_dir.glob(f"*/{SESSION_FILE}")):
        try:
            s = Session.load(f.parent)
        except Exception:
            continue
        if s.data.get("status") == "recording" and not _pid_alive(s.data.get("pid")):
            out.append(s)
    return out
