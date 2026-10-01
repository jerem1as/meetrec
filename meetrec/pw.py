"""Consultas de solo lectura a PipeWire (pw-dump). Nunca modifica el grafo ni la configuración."""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Device:
    name: str  # node.name (estable entre reinicios)
    description: str
    kind: str  # "source" | "sink"


@dataclass
class Snapshot:
    sources: list[Device]
    sinks: list[Device]
    default_source: str | None
    default_sink: str | None

    def find(self, name: str | None) -> Device | None:
        for d in self.sources + self.sinks:
            if d.name == name:
                return d
        return None


class PipeWireUnavailable(RuntimeError):
    pass


def snapshot(timeout: float = 3.0) -> Snapshot:
    try:
        out = subprocess.run(
            ["pw-dump", "--no-colors"], capture_output=True, text=True, timeout=timeout, check=True
        ).stdout
        objects = json.loads(out)
    except FileNotFoundError as e:
        raise PipeWireUnavailable("pw-dump no está instalado") from e
    except (subprocess.SubprocessError, json.JSONDecodeError) as e:
        raise PipeWireUnavailable(f"PipeWire no responde: {e}") from e

    sources: list[Device] = []
    sinks: list[Device] = []
    defaults: dict[str, str] = {}
    for o in objects:
        if o.get("type") == "PipeWire:Interface:Metadata":
            if (o.get("props") or {}).get("metadata.name") != "default":
                continue
            for m in o.get("metadata") or []:
                v = m.get("value")
                if isinstance(v, dict) and "name" in v:
                    defaults[m.get("key")] = v["name"]
            continue
        props = (o.get("info") or {}).get("props") or {}
        mclass = props.get("media.class", "")
        name = props.get("node.name")
        if not name or name.startswith("meetrec."):
            continue
        desc = props.get("node.description") or props.get("node.nick") or name
        if mclass in ("Audio/Source", "Audio/Source/Virtual", "Audio/Duplex"):
            sources.append(Device(name, desc, "source"))
        elif mclass in ("Audio/Sink", "Audio/Sink/Virtual"):
            sinks.append(Device(name, desc, "sink"))

    return Snapshot(
        sources=sources,
        sinks=sinks,
        default_source=defaults.get("default.audio.source"),
        default_sink=defaults.get("default.audio.sink"),
    )
