"""Transcripción local con faster-whisper y armado del Markdown.

Se ejecuta como proceso separado (`meetrec transcribe <carpeta>`): si se queda sin
VRAM o falla CUDA, la GUI y los WAV no se ven afectados, y siempre se puede reintentar.
Cada canal se transcribe por separado, así la autoría (YO / REUNIÓN) es determinista.
"""

from __future__ import annotations

import gc
import json
import logging
import re
import sys
import time
import wave
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from .config import Config
from .session import Session
from .wavfile import repair

log = logging.getLogger(__name__)

WORD_GAP_SPLIT = 1.0  # s de pausa entre palabras => nuevo fragmento (mejor intercalado)
SAME_SPEAKER_MERGE = 8.0  # s: fragmentos consecutivos del mismo canal se unen en un bloque
PARAGRAPH_GAP = 3.0  # s: dentro de un bloque, pausa larga => nuevo párrafo

# Alucinaciones típicas de Whisper en silencio/ruido (frases que nunca se dicen en una reunión).
HALLUCINATIONS = re.compile(
    r"amara\.org|subt[ií]tulos? (realizados|creados|hechos)? ?por|"
    r"subtitulado por|gracias por ver el v[ií]deo|^\W*(¡)?suscr[ií]bete\W*$",
    re.IGNORECASE,
)


@dataclass
class Utterance:
    channel: str  # "mic" | "system"
    start: float
    end: float
    text: str


class Progress:
    def __init__(self, json_mode: bool):
        self.json_mode = json_mode

    def emit(self, stage: str, **kw) -> None:
        if self.json_mode:
            print(json.dumps({"stage": stage, **kw}, ensure_ascii=False), flush=True)
        else:
            msg = kw.get("message") or ""
            if "progress" in kw:
                msg = f"{kw.get('channel', '')} {kw['progress'] * 100:5.1f}%"
            print(f"[{stage}] {msg}", file=sys.stderr, flush=True)


def _attempts(device: str) -> list[tuple[str, str]]:
    out = []
    if device in ("auto", "cuda"):
        out += [("cuda", "float16"), ("cuda", "int8_float16")]
    if device in ("auto", "cpu"):
        out += [("cpu", "int8")]
    return out


def _split_segment(seg, channel: str) -> list[Utterance]:
    words = list(seg.words or [])
    if not words:
        return [Utterance(channel, seg.start, seg.end, seg.text.strip())]
    groups, cur = [], [words[0]]
    for w in words[1:]:
        if w.start - cur[-1].end > WORD_GAP_SPLIT:
            groups.append(cur)
            cur = [w]
        else:
            cur.append(w)
    groups.append(cur)
    return [Utterance(channel, g[0].start, g[-1].end, "".join(w.word for w in g).strip()) for g in groups]


def load_audio(path: Path) -> np.ndarray:
    """Lee el WAV PCM16 mono a float32 16 kHz (sin depender de ffmpeg/PyAV)."""
    with wave.open(str(path), "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"{path.name}: se esperaba PCM 16 bits")
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    if rate != 16000 and a.size:
        n = int(round(a.size * 16000 / rate))
        a = np.interp(np.linspace(0, a.size - 1, n), np.arange(a.size), a).astype(np.float32)
    return a


def load_model(name: str, device: str, compute: str):
    from faster_whisper import WhisperModel
    try:  # primero sin red, desde la caché local
        return WhisperModel(name, device=device, compute_type=compute, local_files_only=True)
    except Exception as e:
        if "out of memory" in str(e).lower() or "CUDA" in str(e):
            raise
        log.info("Modelo %s no está en caché local, descargando…", name)
        return WhisperModel(name, device=device, compute_type=compute)


def _transcribe_channel(model, path: Path, channel: str, cfg: Config, progress: Progress) -> list[Utterance]:
    audio = load_audio(path)
    total = max(audio.size / 16000, 0.001)
    if not audio.size or float(np.abs(audio).max()) == 0.0:
        log.info("Canal %s en silencio digital total", channel)
        return []
    segments, info = model.transcribe(
        audio,
        language=cfg.language or None,
        task="transcribe",
        beam_size=cfg.beam_size,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 300},
        word_timestamps=True,
        condition_on_previous_text=False,  # evita bucles de repetición en reuniones largas
        initial_prompt=cfg.initial_prompt or None,
    )
    out: list[Utterance] = []
    last_emit = 0.0
    for seg in segments:
        text = seg.text.strip()
        if not text or HALLUCINATIONS.search(text):
            if text:
                log.info("Descarto posible alucinación en %s @%.1f: %r", channel, seg.start, text)
            continue
        out.extend(u for u in _split_segment(seg, channel) if u.text)
        if time.monotonic() - last_emit > 0.5:
            progress.emit("transcribing", channel=channel, progress=min(seg.end / total, 1.0))
            last_emit = time.monotonic()
    progress.emit("transcribing", channel=channel, progress=1.0)
    return out


def fmt_ts(t: float) -> str:
    t = int(max(t, 0))
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def build_markdown(session: Session, utterances: list[Utterance], cfg: Config, notes: list[str]) -> str:
    labels = {"mic": cfg.label_mic, "system": cfg.label_system}
    started = session.started
    dur = session.data.get("duration")
    lines = [
        "# Reunión",
        f"Fecha: {started:%Y-%m-%d}",
        f"Inicio: {started:%H:%M}",
    ]
    if dur:
        lines.append(f"Duración: {fmt_ts(dur)}")
    lines.append(f"Participantes: {labels['mic']} (quien grabó, micrófono local); "
                 f"{labels['system']} (resto de participantes, audio de la computadora)")
    lines += ["", "## Transcripción", ""]
    for n in notes:
        lines += [f"> {n}", ""]

    # Avisos de huecos de audio dentro de la línea de tiempo (no son contenido de la reunión).
    gaps = [
        Utterance("_gap", e["at"], e["at"] + e["duration"],
                  f"_[{fmt_ts(e['at'])}] Sin audio del canal {labels.get(e['channel'], e['channel'])} "
                  f"durante {e['duration']:.0f} s (problema de captura)._")
        for e in session.data.get("events", []) if e.get("type") == "gap" and e.get("duration", 0) >= 2
    ]

    items = sorted(utterances + gaps, key=lambda u: (u.start, u.channel))
    blocks: list[tuple[str, float, list[list[str]], float]] = []  # (canal, inicio, párrafos, fin)
    for u in items:
        if u.channel == "_gap":
            blocks.append(("_gap", u.start, [[u.text]], u.end))
            continue
        if blocks and blocks[-1][0] == u.channel and u.start - blocks[-1][3] < SAME_SPEAKER_MERGE:
            ch, st, paras, end = blocks[-1]
            if u.start - end > PARAGRAPH_GAP:
                paras.append([u.text])
            else:
                paras[-1].append(u.text)
            blocks[-1] = (ch, st, paras, max(end, u.end))
        else:
            blocks.append((u.channel, u.start, [[u.text]], u.end))

    for ch, st, paras, _ in blocks:
        if ch == "_gap":
            lines += [paras[0][0], ""]
            continue
        lines += [f"**[{fmt_ts(st)}] {labels[ch]}**", ""]
        for p in paras:
            lines += [" ".join(p), ""]
    if not utterances:
        lines += ["_(No se detectó habla en ninguno de los dos canales.)_", ""]
    return "\n".join(lines).rstrip() + "\n"


def run(directory: Path, json_progress: bool = False, cfg: Config | None = None) -> Path:
    cfg = cfg or Config.load()
    progress = Progress(json_progress)
    session = Session.load(directory)
    if session.data.get("status") == "recording":
        session.data["status"] = "interrupted"
        session.save()

    channels = []
    notes = []
    for key, path in (("mic", session.mic_path), ("system", session.system_path)):
        if not path.exists():
            notes.append(f"⚠ No existe {path.name}; el canal {key} no se transcribió.")
            continue
        if repair(path):
            log.info("Cabecera WAV reparada: %s", path)
        channels.append((key, path))

    results: dict[str, list[Utterance]] = {}
    used = None
    errors = []
    for device, compute in _attempts(cfg.device):
        pending = [(k, p) for k, p in channels if k not in results]
        if not pending:
            break
        progress.emit("loading_model", message=f"Cargando {cfg.model} ({device}/{compute})…",
                      device=device)
        model = None
        try:
            model = load_model(cfg.model, device, compute)
            for key, path in pending:
                progress.emit("transcribing", channel=key, progress=0.0, device=device)
                results[key] = _transcribe_channel(model, path, key, cfg, progress)
            used = (device, compute)
        except Exception as e:  # OOM de CUDA, librerías faltantes, etc. => siguiente opción
            log.exception("Falló transcripción con %s/%s", device, compute)
            errors.append(f"{device}/{compute}: {e}")
            progress.emit("fallback", message=f"Falló {device}/{compute}: {e}")
        finally:
            del model
            gc.collect()

    missing = [k for k, _ in channels if k not in results]
    if missing:
        msg = "No se pudo transcribir: " + ", ".join(missing) + ". " + " | ".join(errors)
        progress.emit("error", message=msg)
        raise RuntimeError(msg)

    utterances = [u for k in ("mic", "system") for u in results.get(k, [])]
    md = build_markdown(session, utterances, cfg, notes)
    tmp = session.transcript_path.with_suffix(".md.tmp")
    tmp.write_text(md, encoding="utf-8")
    tmp.replace(session.transcript_path)

    (session.dir / "transcript.json").write_text(json.dumps(
        {"utterances": [asdict(u) for u in sorted(utterances, key=lambda u: u.start)]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    session.data["transcription"] = {
        "finished": datetime.now().astimezone().isoformat(timespec="seconds"),
        "model": cfg.model, "language": cfg.language,
        "device": used[0] if used else None, "compute_type": used[1] if used else None,
    }
    session.save()
    progress.emit("done", path=str(session.transcript_path), message="Transcripción lista")
    return session.transcript_path
