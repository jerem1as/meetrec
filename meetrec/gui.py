"""Interfaz de escritorio (PySide6)."""

from __future__ import annotations

import json
import logging
import shutil
import signal
import subprocess
import sys
import time
import wave
from collections import deque
from pathlib import Path

import numpy as np
from PySide6.QtCore import QLockFile, QProcess, QProcessEnvironment, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QGuiApplication, QIcon, QPainter
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QGroupBox, QHBoxLayout, QLabel, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from . import APP_NAME
from .capture import SIGNAL_DB, AudioEngine, Channel
from .config import STATE_DIR, Config
from .session import Session, find_interrupted
from .wavfile import repair

log = logging.getLogger(__name__)

ICON_PATH = Path(__file__).with_name("icon.svg")
TICK_MS = 50
HISTORY = 160  # 8 s de historial a 50 ms

RED, GREEN, ORANGE, GRAY = "#e53935", "#2e7d32", "#ef6c00", "#757575"


def fmt_hms(t: float) -> str:
    t = int(t)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


class LevelMeter(QWidget):
    """Historial de nivel (barras que se desplazan) + nivel actual. Gris = silencio."""

    def __init__(self):
        super().__init__()
        self.levels: deque[float] = deque([-120.0] * HISTORY, maxlen=HISTORY)
        self.current = -120.0
        self.setMinimumHeight(54)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def push(self, db: float) -> None:
        self.levels.append(db)
        self.current = max(db, self.current - 1.5)  # caída suave del indicador
        self.update()

    @staticmethod
    def _norm(db: float) -> float:
        return min(max((db + 70.0) / 70.0, 0.0), 1.0)

    def paintEvent(self, _):
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor("#1e1e1e"))
        cur_w = 18
        hist_w = w - cur_w - 6
        bw = hist_w / HISTORY
        for i, db in enumerate(self.levels):
            v = self._norm(db)
            if v <= 0:
                continue
            bh = max(1.0, v * (h - 4))
            color = QColor("#555") if db < SIGNAL_DB else QColor("#43a047") if db < -12 else QColor("#fdd835") if db < -3 else QColor(RED)
            p.fillRect(int(i * bw), int(h - 2 - bh), max(1, int(bw) - 0), int(bh), color)
        # barra de nivel actual
        v = self._norm(self.current)
        p.fillRect(w - cur_w, 0, cur_w, h, QColor("#2b2b2b"))
        c = QColor("#555") if self.current < SIGNAL_DB else QColor("#43a047") if self.current < -12 else QColor("#fdd835") if self.current < -3 else QColor(RED)
        p.fillRect(w - cur_w, int(h - v * h), cur_w, int(v * h), c)
        # línea de umbral de señal
        y = int(h - 2 - self._norm(SIGNAL_DB) * (h - 4))
        p.setPen(QColor("#444"))
        p.drawLine(0, y, hist_w, y)
        p.end()


class ChannelPanel(QGroupBox):
    def __init__(self, title: str, channel: Channel, engine: AudioEngine, hint: str, on_pin):
        super().__init__(title)
        self.on_pin = on_pin
        self.channel = channel
        self.engine = engine
        self.hint = hint
        self._devices_key = None
        self.combo = QComboBox()
        self.combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.combo.currentIndexChanged.connect(self._on_combo)
        self.meter = LevelMeter()
        self.status = QLabel("…")
        self.status.setTextFormat(Qt.RichText)
        top = QHBoxLayout()
        top.addWidget(self.combo, 1)
        self.top = top
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.meter)
        lay.addWidget(self.status)

    def _on_combo(self, idx: int) -> None:
        if self._devices_key is None or idx < 0:
            return
        name = self.combo.itemData(idx)
        if name != self.engine.pins[self.channel.key]:
            self.engine.set_pin(self.channel.key, name)
            self.on_pin(self.channel.key, name)

    def refresh_devices(self) -> None:
        snap = self.engine.snapshot
        if snap is None:
            return
        devs = snap.sources if self.channel.kind == "source" else snap.sinks
        default = snap.default_source if self.channel.kind == "source" else snap.default_sink
        default_desc = next((d.description for d in devs if d.name == default), "ninguno")
        key = (tuple(devs), default_desc, self.engine.pins[self.channel.key])
        if key == self._devices_key:
            return
        self._devices_key = None  # evita disparar _on_combo mientras se rearma
        self.combo.clear()
        self.combo.addItem(f"Predeterminado del sistema — {default_desc}", None)
        pin = self.engine.pins[self.channel.key]
        sel = 0
        for d in devs:
            self.combo.addItem(d.description, d.name)
            if d.name == pin:
                sel = self.combo.count() - 1
        if pin and sel == 0:
            self.combo.addItem(f"⚠ {pin} (no conectado)", pin)
            sel = self.combo.count() - 1
        self.combo.setCurrentIndex(sel)
        self._devices_key = key

    def tick(self, now: float, recording: bool) -> tuple[str, str]:
        """Actualiza el panel y devuelve (nivel_de_problema, texto) para el banner."""
        self.meter.push(self.channel.take_level())
        ch = self.channel
        rec = ""
        w = ch._writer
        if recording and w is not None:
            rec = f" · grabado {fmt_hms(w.wav.samples / w.rate)}"
        problem = ("", "")
        if self.engine.pw_error:
            txt, color = f"✖ {self.engine.pw_error}", RED
            problem = ("error", self.engine.pw_error)
        elif ch.state == "no_device":
            txt, color = f"✖ Dispositivo no disponible: {ch.description}", RED
            problem = ("error", f"{self.title()}: dispositivo no disponible")
        elif ch.state in ("no_data", "starting") and now - ch.last_data > 1.0:
            starting = ch.state == "starting" and ch.last_data == 0
            txt = "… conectando" if starting else f"✖ No llega audio ({ch.error or 'reconectando'})"
            color = GRAY if starting else RED
            if not starting:
                problem = ("error", f"{self.title()}: no llega audio")
        else:
            since = now - ch.last_signal
            if ch.last_signal and since < 1.5:
                txt, color = "● Recibiendo señal", GREEN
            elif not ch.last_signal:
                txt, color = f"○ Conectado, sin señal todavía — {self.hint}", ORANGE
            else:
                txt, color = f"○ Silencio (última señal hace {fmt_hms(since)})", GRAY
                if recording and since > 180:
                    color = ORANGE
                    problem = ("warn", f"{self.title()}: sin señal hace {int(since // 60)} min")
        self.status.setText(f'<span style="color:{color}">{txt}</span>'
                            f'<span style="color:{GRAY}">{rec}</span>')
        return problem


class MainWindow(QMainWindow):
    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.engine = AudioEngine(cfg.sample_rate, cfg.mic_target, cfg.system_target)
        self.transcriber: QProcess | None = None
        self.last_session: Session | None = None
        self._stderr_tail: deque[str] = deque(maxlen=20)

        central = QWidget()
        lay = QVBoxLayout(central)
        self.mic_panel = ChannelPanel(f"Micrófono — {cfg.label_mic}", self.engine.mic, self.engine,
                                      "hablá para probar", self.save_pin)
        self.sys_panel = ChannelPanel(f"Audio del sistema — {cfg.label_system}", self.engine.system,
                                      self.engine, "reproducí algo o tocá «Probar»", self.save_pin)
        self.test_btn = QPushButton("Probar")
        self.test_btn.setToolTip("Reproduce un sonido corto por esta salida para verificar el indicador")
        self.test_btn.clicked.connect(self.play_test_sound)
        self.sys_panel.top.addWidget(self.test_btn)
        lay.addWidget(self.mic_panel)
        lay.addWidget(self.sys_panel)

        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.hide()
        lay.addWidget(self.banner)

        self.rec_label = QLabel("Listo para grabar")
        f = QFont()
        f.setPointSize(16)
        f.setBold(True)
        self.rec_label.setFont(f)
        self.rec_label.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.rec_label)

        self.rec_btn = QPushButton("●  Iniciar grabación")
        self.rec_btn.setMinimumHeight(48)
        bf = QFont()
        bf.setPointSize(13)
        self.rec_btn.setFont(bf)
        self.rec_btn.clicked.connect(self.toggle_recording)
        lay.addWidget(self.rec_btn)

        # --- Transcripción ---
        self.tx_box = QGroupBox("Transcripción")
        tl = QVBoxLayout(self.tx_box)
        self.tx_label = QLabel()
        self.tx_label.setWordWrap(True)
        self.tx_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.tx_bar = QProgressBar()
        self.tx_bar.setRange(0, 1000)
        tl.addWidget(self.tx_label)
        tl.addWidget(self.tx_bar)
        row = QHBoxLayout()
        self.open_md_btn = QPushButton("Abrir transcripción")
        self.open_md_btn.clicked.connect(lambda: self._open(self.last_session.transcript_path))
        self.open_dir_btn = QPushButton("Abrir carpeta")
        self.open_dir_btn.clicked.connect(lambda: self._open(self.last_session.dir))
        self.copy_btn = QPushButton("Copiar Markdown")
        self.copy_btn.clicked.connect(self.copy_markdown)
        self.retry_btn = QPushButton("Reintentar")
        self.retry_btn.clicked.connect(lambda: self.start_transcription(self.last_session))
        for b in (self.open_md_btn, self.copy_btn, self.open_dir_btn, self.retry_btn):
            row.addWidget(b)
        tl.addLayout(row)
        self.tx_box.hide()
        lay.addWidget(self.tx_box)

        foot = QHBoxLayout()
        b1 = QPushButton("Carpeta de grabaciones")
        b1.setFlat(True)
        b1.clicked.connect(lambda: self._open(self.cfg.recordings_path))
        b2 = QPushButton("Transcribir otra grabación…")
        b2.setFlat(True)
        b2.clicked.connect(self.pick_and_transcribe)
        foot.addWidget(b1)
        foot.addStretch(1)
        foot.addWidget(b2)
        lay.addLayout(foot)

        self.setCentralWidget(central)
        self.resize(560, 0)

        self.engine.start()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(TICK_MS)
        self._slow = 0
        self._blink = False
        QTimer.singleShot(300, self.check_interrupted)

    # --- ciclo de UI ------------------------------------------------------------------
    def tick(self) -> None:
        now = time.monotonic()
        rec = self.engine.recording
        problems = [self.mic_panel.tick(now, rec), self.sys_panel.tick(now, rec)]
        self._slow += 1
        if self._slow % 10 == 0:  # cada 0.5 s
            self.mic_panel.refresh_devices()
            self.sys_panel.refresh_devices()
            if self._slow % 60 == 0:
                free = shutil.disk_usage(self.cfg.recordings_path if self.cfg.recordings_path.exists() else Path.home()).free
                self._disk_low = free < 1_000_000_000
            if getattr(self, "_disk_low", False):
                problems.append(("error" if rec else "warn", "Queda menos de 1 GB libre en disco"))
            msgs = [(lvl, t) for lvl, t in problems if lvl]
            if msgs:
                worst = "error" if any(l == "error" for l, _ in msgs) else "warn"
                color = RED if worst == "error" else ORANGE
                self.banner.setText(f'<b style="color:{color}">⚠ ' + "<br>⚠ ".join(t for _, t in msgs) + "</b>")
                self.banner.show()
            else:
                self.banner.hide()
            if rec:
                self._blink = not self._blink
                dot = "●" if self._blink else "○"
                self.rec_label.setText(f'<span style="color:{RED}">{dot} GRABANDO — {fmt_hms(self.engine.elapsed())}</span>')
                self.setWindowTitle(f"● REC {fmt_hms(self.engine.elapsed())} — {APP_NAME}")

    def save_pin(self, key: str, name: str | None) -> None:
        setattr(self.cfg, "mic_target" if key == "mic" else "system_target", name)
        self.cfg.save()

    # --- grabación --------------------------------------------------------------------
    def toggle_recording(self) -> None:
        if self.engine.recording:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self) -> None:
        if self.engine.pw_error:
            QMessageBox.critical(self, APP_NAME, f"No se puede grabar: {self.engine.pw_error}")
            return
        bad = [p.title() for p in (self.mic_panel, self.sys_panel)
               if p.channel.state != "ok" or time.monotonic() - p.channel.last_data > 1.0]
        if bad:
            r = QMessageBox.warning(
                self, APP_NAME,
                "Estos canales NO están entregando audio en este momento:\n\n  • " + "\n  • ".join(bad)
                + "\n\nSi grabás igual, esos archivos pueden quedar vacíos.\n¿Grabar de todos modos?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                return
        try:
            s = self.engine.start_recording(self.cfg.recordings_path)
        except OSError as e:
            QMessageBox.critical(self, APP_NAME, f"No se pudo crear la carpeta de grabación:\n{e}")
            return
        self.rec_btn.setText("■  Detener grabación")
        self.rec_label.setToolTip(str(s.dir))
        for w in (self.mic_panel.combo, self.sys_panel.combo, self.test_btn):
            w.setEnabled(False)

    def stop_recording(self) -> Session:
        s = self.engine.stop_recording()
        self.rec_btn.setText("●  Iniciar grabación")
        self.rec_label.setText(f"Guardado en {s.dir.name}")
        self.setWindowTitle(APP_NAME)
        for w in (self.mic_panel.combo, self.sys_panel.combo, self.test_btn):
            w.setEnabled(True)
        self.start_transcription(s)
        return s

    def play_test_sound(self) -> None:
        target = self.engine.system.target
        if not target:
            return
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        f = STATE_DIR / "test-sound.wav"
        if not f.exists():
            sr = 48000
            t = np.arange(int(sr * 1.2)) / sr
            tone = sum(np.sin(2 * np.pi * fr * t) * np.exp(-3 * (t - d).clip(0)) * (t >= d)
                       for fr, d in ((660, 0.0), (880, 0.25), (990, 0.5)))
            data = (tone / 3 * 0.25 * 32767).astype(np.int16)
            with wave.open(str(f), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(sr)
                w.writeframes(data.tobytes())
        subprocess.Popen(["pw-play", "--target", target, str(f)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # --- transcripción ----------------------------------------------------------------
    def start_transcription(self, session: Session | None) -> None:
        if session is None:
            return
        if self.transcriber is not None:
            QMessageBox.information(self, APP_NAME, "Ya hay una transcripción en curso. Esperá a que termine.")
            return
        self.last_session = session
        self._stderr_tail.clear()
        self.tx_box.show()
        self.tx_box.setTitle(f"Transcripción — {session.dir.name}")
        self.tx_label.setText("Iniciando…")
        self.tx_bar.setRange(0, 0)
        for b in (self.open_md_btn, self.copy_btn, self.retry_btn):
            b.setEnabled(False)
        p = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        p.setProcessEnvironment(env)
        p.readyReadStandardOutput.connect(self._tx_stdout)
        p.readyReadStandardError.connect(self._tx_stderr)
        p.finished.connect(self._tx_finished)
        p.start(sys.executable, ["-m", "meetrec", "transcribe", str(session.dir), "--progress-json"])
        self.transcriber = p

    def _tx_stdout(self) -> None:
        p = self.transcriber
        while p and p.canReadLine():
            line = bytes(p.readLine()).decode(errors="replace").strip()
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            st = ev.get("stage")
            if st in ("loading_model", "fallback"):
                self.tx_bar.setRange(0, 0)
                self.tx_label.setText(ev.get("message", ""))
            elif st == "transcribing":
                self.tx_bar.setRange(0, 1000)
                base = 0 if ev["channel"] == "mic" else 500
                self.tx_bar.setValue(int(base + ev["progress"] * 500))
                name = self.cfg.label_mic if ev["channel"] == "mic" else self.cfg.label_system
                dev = " (CPU: puede tardar)" if ev.get("device") == "cpu" else ""
                self.tx_label.setText(f"Transcribiendo canal {name}… {ev['progress'] * 100:.0f}%{dev}")

    def _tx_stderr(self) -> None:
        data = bytes(self.transcriber.readAllStandardError()).decode(errors="replace")
        for line in data.splitlines():
            if line.strip():
                self._stderr_tail.append(line)

    def _tx_finished(self, code: int, _status) -> None:
        self._tx_stdout()
        self.transcriber = None
        s = self.last_session
        self.tx_bar.setRange(0, 1000)
        self.retry_btn.setEnabled(True)
        if code == 0 and s and s.transcript_path.exists():
            self.tx_bar.setValue(1000)
            self.tx_label.setText(f"✔ Transcripción lista: {s.transcript_path}")
            self.open_md_btn.setEnabled(True)
            self.copy_btn.setEnabled(True)
        else:
            self.tx_bar.setValue(0)
            err = next((l for l in reversed(self._stderr_tail) if l.startswith("Error")), "")
            self.tx_label.setText(
                f'<span style="color:{RED}">✖ Falló la transcripción. {err}</span><br>'
                "Los audios están guardados intactos; podés reintentar ahora o más tarde "
                f"(<code>meetrec transcribe {s.dir if s else ''}</code>).")

    def copy_markdown(self) -> None:
        if self.last_session and self.last_session.transcript_path.exists():
            QGuiApplication.clipboard().setText(self.last_session.transcript_path.read_text(encoding="utf-8"))
            self.tx_label.setText(self.tx_label.text().split("<br>")[0] + "<br>📋 Copiado al portapapeles")

    def pick_and_transcribe(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Elegí la carpeta de una grabación", str(self.cfg.recordings_path))
        if d:
            s = Session.load(Path(d))
            if not s.mic_path.exists() and not s.system_path.exists():
                QMessageBox.warning(self, APP_NAME, "Esa carpeta no contiene mic.wav ni meeting.wav.")
                return
            self.start_transcription(s)

    def check_interrupted(self) -> None:
        for s in find_interrupted(self.cfg.recordings_path):
            for f in (s.mic_path, s.system_path):
                if f.exists():
                    repair(f)
            s.data["status"] = "interrupted"
            s.save()
            r = QMessageBox.question(
                self, APP_NAME,
                f"Se encontró una grabación que quedó interrumpida (la app se cerró mientras grababa):\n\n"
                f"{s.dir}\n\nLos audios se recuperaron. ¿Transcribirla ahora?")
            if r == QMessageBox.Yes:
                self.start_transcription(s)
                break

    def _open(self, path: Path) -> None:
        if path and Path(path).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    # --- cierre -----------------------------------------------------------------------
    def closeEvent(self, ev) -> None:
        if self.engine.recording:
            r = QMessageBox.question(self, APP_NAME, "Hay una grabación en curso.\n¿Detenerla, guardarla y salir?")
            if r != QMessageBox.Yes:
                ev.ignore()
                return
            s = self.engine.stop_recording()
            QMessageBox.information(self, APP_NAME, f"Grabación guardada en:\n{s.dir}\n\n"
                                    "Podés transcribirla después con «Transcribir otra grabación…».")
        if self.transcriber is not None:
            r = QMessageBox.question(self, APP_NAME, "Hay una transcripción en curso. ¿Cancelarla y salir?\n"
                                     "(Los audios quedan guardados; podés retranscribir después.)")
            if r != QMessageBox.Yes:
                ev.ignore()
                return
            self.transcriber.kill()
            self.transcriber.waitForFinished(3000)
        self.shutdown()
        ev.accept()

    def shutdown(self) -> None:
        self.timer.stop()
        self.engine.shutdown()  # detiene la grabación (si hay) y cierra los WAV correctamente


def run_gui() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName("meetrec")
    app.setWindowIcon(QIcon(str(ICON_PATH)))

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(STATE_DIR / "gui.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(200):
        QMessageBox.information(None, APP_NAME, "La aplicación ya está abierta.")
        return 0

    cfg = Config.load()
    win = MainWindow(cfg)
    win.show()

    # Ctrl+C / SIGTERM (p.ej. cierre de sesión): cerrar guardando la grabación.
    def _graceful(*_):
        log.info("Señal recibida: guardando y saliendo")
        win.shutdown()
        app.quit()
    signal.signal(signal.SIGINT, _graceful)
    signal.signal(signal.SIGTERM, _graceful)
    app.aboutToQuit.connect(win.engine.shutdown)

    code = app.exec()
    lock.unlock()
    return code
