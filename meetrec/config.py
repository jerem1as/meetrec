"""Configuración del usuario (~/.config/meetrec/config.json) y rutas estándar."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, fields
from logging.handlers import RotatingFileHandler
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "meetrec"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "meetrec"
CONFIG_FILE = CONFIG_DIR / "config.json"


@dataclass
class Config:
    # Carpeta donde se crean las grabaciones (una subcarpeta por reunión).
    recordings_dir: str = str(PROJECT_DIR / "recordings")
    # Frecuencia de los WAV guardados. 16 kHz es lo que usa Whisper y ocupa ~115 MB/h por canal.
    sample_rate: int = 16000
    # Dispositivo fijo (node.name de PipeWire) o null = seguir al predeterminado del sistema.
    mic_target: str | None = None
    system_target: str | None = None
    # Transcripción
    model: str = "large-v3-turbo"
    language: str | None = "es"  # null = autodetectar
    device: str = "auto"  # auto | cuda | cpu
    beam_size: int = 5
    # Texto opcional para orientar a Whisper (nombres propios, jerga técnica...).
    initial_prompt: str | None = None
    # Etiquetas en el Markdown
    label_mic: str = "YO"
    label_system: str = "REUNIÓN"

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text())
                names = {f.name for f in fields(cls)}
                for k, v in data.items():
                    if k in names:
                        setattr(cfg, k, v)
            except Exception:
                logging.exception("No se pudo leer %s; uso valores por defecto", CONFIG_FILE)
        return cfg

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False) + "\n")
        tmp.replace(CONFIG_FILE)

    @property
    def recordings_path(self) -> Path:
        return Path(os.path.expanduser(self.recordings_dir))


def setup_logging(verbose: bool = False) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log_file = STATE_DIR / "meetrec.log"
    handlers: list[logging.Handler] = [
        RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"),
        logging.StreamHandler(),
    ]
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    return log_file
