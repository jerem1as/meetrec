"""Captura paralela de audio desde PipeWire.

Cada canal es un proceso `pw-record` que crea un *stream de captura nuevo* conectado
a la fuente (mic) o a los puertos monitor del sink (audio del sistema). Es exactamente
lo mismo que hace cualquier otra app que escucha el mic: PipeWire reparte la señal a
todos los consumidores. No se cambian dispositivos predeterminados, ni enlaces de otras
apps, ni volúmenes, y no se crean dispositivos virtuales. Al terminar el proceso
PipeWire elimina el nodo y sus enlaces automáticamente (también si la app se cuelga).
"""

from __future__ import annotations

import logging
import math
import os
import select
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from . import pw
from .session import Session
from .wavfile import CrashSafeWav

log = logging.getLogger(__name__)

SIGNAL_DB = -50.0  # RMS por encima de esto se considera "hay señal"
NO_DATA_TIMEOUT = 2.5  # s sin bytes del proceso => reiniciar captura
GAP_TOLERANCE = 0.5  # s de atraso respecto del reloj antes de rellenar con silencio


class ChannelWriter:
    """Escribe un canal alineado al reloj de pared común a ambos canales.

    Si el canal deja de entregar audio (dispositivo desconectado, PipeWire reiniciado),
    se rellena con silencio al volver, para que mic.wav y meeting.wav sigan compartiendo
    la misma línea de tiempo.
    """

    def __init__(self, path: Path, rate: int, t0: float, key: str, session: Session):
        self.wav = CrashSafeWav(path, rate)
        self.rate = rate
        self.t0 = t0
        self.key = key
        self.session = session

    def write(self, pcm: bytes, now: float) -> None:
        n = len(pcm) // 2
        behind = (now - self.t0) * self.rate - (self.wav.samples + n)
        if behind > GAP_TOLERANCE * self.rate:
            self._pad(int(behind))
        self.wav.write(pcm)

    def _pad(self, samples: int) -> None:
        secs = samples / self.rate
        at = self.wav.samples / self.rate
        if secs >= 1.0:
            log.warning("Canal %s: %.1f s sin audio en %.1f s, relleno con silencio", self.key, secs, at)
            self.session.add_event(type="gap", channel=self.key, at=round(at, 2), duration=round(secs, 2))
        self.wav.write_silence(samples)

    def finish(self, t_stop: float) -> None:
        behind = int((t_stop - self.t0) * self.rate) - self.wav.samples
        if behind > 0:
            self._pad(behind)
        self.wav.close()


class Channel:
    def __init__(self, key: str, kind: str, rate: int):
        self.key = key  # "mic" | "system"
        self.kind = kind  # "source" | "sink"
        self.rate = rate
        self.target: str | None = None
        self.description: str = ""
        self.state = "starting"  # starting | ok | no_device | no_data
        self.error: str = ""
        self.last_data = 0.0
        self.last_signal = 0.0
        self.bytes_total = 0
        self._level = -120.0
        self._level_lock = threading.Lock()
        self._writer: ChannelWriter | None = None
        self._writer_lock = threading.Lock()
        self._restart = threading.Event()
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._thread = threading.Thread(target=self._run, name=f"capture-{key}", daemon=True)

    # --- API usada por el motor / GUI -------------------------------------------------
    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._restart.set()
        self._thread.join(timeout=5)

    def set_target(self, name: str | None, description: str) -> None:
        if name != self.target:
            log.info("Canal %s -> %s", self.key, name)
            self.target = name
            self.description = description
            self._restart.set()
        else:
            self.description = description

    def take_level(self) -> float:
        """Nivel RMS máximo (dBFS) desde la última llamada; así la GUI no pierde picos."""
        with self._level_lock:
            v, self._level = self._level, -120.0
        return v

    def attach_writer(self, w: ChannelWriter | None) -> ChannelWriter | None:
        with self._writer_lock:
            old, self._writer = self._writer, w
        return old

    # --- hilo de captura --------------------------------------------------------------
    def _cmd(self, target: str) -> list[str]:
        props = [
            f"node.name=meetrec.{self.key}",
            f'node.description="Meetrec captura {"micrófono" if self.key == "mic" else "sistema"}"',
            "application.name=meetrec",
            # No dejar que WirePlumber nos reconecte a otro dispositivo por su cuenta:
            # el cambio de dispositivo lo decide la app y queda registrado.
            "node.dont-reconnect=true",
        ]
        if self.kind == "sink":
            props.append("stream.capture.sink=true")
        return [
            "pw-record", "--target", target,
            "--rate", str(self.rate), "--channels", "1", "--format", "s16",
            "--latency", "50ms",
            "-P", "{ " + " ".join(props) + " }",
            "-",
        ]

    def _run(self) -> None:
        backoff = 0.5
        while not self._stop.is_set():
            self._restart.clear()
            target = self.target
            if not target:
                self.state = "no_device"
                self._restart.wait(0.5)
                continue
            ok = self._capture_once(target)
            if self._stop.is_set():
                break
            if ok:
                backoff = 0.5
            else:
                self._restart.wait(backoff)
                backoff = min(backoff * 2, 4.0)

    def _capture_once(self, target: str) -> bool:
        self.state = "starting"
        try:
            proc = subprocess.Popen(
                self._cmd(target), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True,  # no recibir el Ctrl+C de la terminal: lo maneja la app
            )
        except FileNotFoundError:
            self.state, self.error = "no_data", "pw-record no está instalado"
            return False
        self._proc = proc
        fd = proc.stdout.fileno()
        started = time.monotonic()
        got_data = False
        rest = b""
        try:
            while not self._restart.is_set():
                r, _, _ = select.select([fd], [], [], 0.25)
                now = time.monotonic()
                if r:
                    data = os.read(fd, 65536)
                    if not data:
                        err = proc.stderr.read(2000).decode(errors="replace").strip() if proc.poll() is not None else ""
                        self.error = err or "pw-record terminó"
                        log.warning("Canal %s: captura terminó (%s)", self.key, self.error)
                        break
                    data = rest + data
                    cut = len(data) - (len(data) % 2)
                    rest = data[cut:]
                    self._on_pcm(data[:cut], now)
                    got_data = True
                elif now - max(self.last_data, started) > NO_DATA_TIMEOUT:
                    self.state = "no_data"
                    self.error = "El dispositivo no entrega audio"
                    log.warning("Canal %s: sin datos de %s, reinicio", self.key, target)
                    break
        finally:
            self._proc = None
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            proc.stdout.close()
            proc.stderr.close()
        if self.state == "ok" and not self._restart.is_set():
            self.state = "no_data"
        return got_data

    def _on_pcm(self, pcm: bytes, now: float) -> None:
        if not pcm:
            return
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        rms = math.sqrt(float(np.mean(x * x))) / 32768.0 if x.size else 0.0
        db = 20 * math.log10(rms) if rms > 1e-6 else -120.0
        with self._level_lock:
            self._level = max(self._level, db)
        self.last_data = now
        self.bytes_total += len(pcm)
        if db > SIGNAL_DB:
            self.last_signal = now
        self.state, self.error = "ok", ""
        with self._writer_lock:
            if self._writer is not None:
                try:
                    self._writer.write(pcm, now)
                except OSError as e:  # disco lleno, etc.: no tirar la captura del otro canal
                    log.error("Error escribiendo %s: %s", self.key, e)
                    self.error = f"Error de escritura: {e}"


class AudioEngine:
    """Dos canales + vigilancia de dispositivos (cambios de predeterminado, conexiones, reinicios)."""

    def __init__(self, rate: int, mic_pin: str | None = None, system_pin: str | None = None):
        self.rate = rate
        self.mic = Channel("mic", "source", rate)
        self.system = Channel("system", "sink", rate)
        self.pins = {"mic": mic_pin, "system": system_pin}
        self.snapshot: pw.Snapshot | None = None
        self.pw_error: str = ""
        self.session: Session | None = None
        self.rec_t0: float | None = None
        self._stop = threading.Event()
        self._poke = threading.Event()
        self._lock = threading.Lock()
        self._monitor = threading.Thread(target=self._watch, name="pw-watch", daemon=True)

    @property
    def channels(self) -> tuple[Channel, Channel]:
        return self.mic, self.system

    def start(self) -> None:
        self._refresh()
        self.mic.start()
        self.system.start()
        self._monitor.start()

    def shutdown(self) -> None:
        if self.recording:
            self.stop_recording()
        self._stop.set()
        self._poke.set()
        for ch in self.channels:
            ch.stop()

    def set_pin(self, key: str, name: str | None) -> None:
        self.pins[key] = name
        self._refresh()

    # --- dispositivos -----------------------------------------------------------------
    def _watch(self) -> None:
        while not self._stop.is_set():
            self._poke.wait(2.0)
            self._poke.clear()
            if not self._stop.is_set():
                self._refresh()

    def _refresh(self) -> None:
        with self._lock:
            try:
                snap = pw.snapshot()
                self.snapshot, self.pw_error = snap, ""
            except pw.PipeWireUnavailable as e:
                self.pw_error = str(e)
                return
            for ch, default in ((self.mic, snap.default_source), (self.system, snap.default_sink)):
                pinned = self.pins[ch.key]
                name = pinned or default
                dev = snap.find(name)
                if pinned and dev is None:
                    # El dispositivo elegido no está: no grabar en silencio otro dispositivo.
                    ch.set_target(None, f"{pinned} (no conectado)")
                elif dev is None:
                    ch.set_target(None, "No hay dispositivo")
                else:
                    if self.session and ch.target and ch.target != dev.name:
                        self._event(type="device_change", channel=ch.key, device=dev.description)
                    ch.set_target(dev.name, dev.description)

    # --- grabación --------------------------------------------------------------------
    @property
    def recording(self) -> bool:
        return self.session is not None

    def _event(self, **ev) -> None:
        if self.session and self.rec_t0 is not None:
            ev["at"] = round(time.monotonic() - self.rec_t0, 2)
            self.session.add_event(**ev)

    def start_recording(self, recordings_dir: Path) -> Session:
        assert not self.recording
        session = Session.create(recordings_dir, datetime.now().astimezone())
        session.data["sample_rate"] = self.rate
        session.data["channels"] = {
            ch.key: {"file": p.name, "device": ch.description, "node": ch.target}
            for ch, p in ((self.mic, session.mic_path), (self.system, session.system_path))
        }
        session.save()
        t0 = time.monotonic()
        self.rec_t0 = t0
        self.session = session
        self.mic.attach_writer(ChannelWriter(session.mic_path, self.rate, t0, "mic", session))
        self.system.attach_writer(ChannelWriter(session.system_path, self.rate, t0, "system", session))
        log.info("Grabando en %s", session.dir)
        return session

    def stop_recording(self) -> Session:
        assert self.session is not None
        t_stop = time.monotonic()
        session = self.session
        for ch in self.channels:
            w = ch.attach_writer(None)
            if w:
                w.finish(t_stop)
        session.data["stopped"] = datetime.now().astimezone().isoformat(timespec="seconds")
        session.data["duration"] = round(t_stop - (self.rec_t0 or t_stop), 2)
        session.data["status"] = "stopped"
        session.save()
        self.session = None
        self.rec_t0 = None
        log.info("Grabación detenida: %s", session.dir)
        return session

    def elapsed(self) -> float:
        return time.monotonic() - self.rec_t0 if self.rec_t0 is not None else 0.0
