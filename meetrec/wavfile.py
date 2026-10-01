"""WAV PCM16 mono escrito de forma incremental y a prueba de cortes.

La cabecera se reescribe periódicamente con el tamaño real, de modo que si la app
o la máquina se caen, el archivo sigue siendo reproducible (como mucho pierde los
últimos segundos sin cabecera actualizada, que `repair()` recupera igual).
"""

from __future__ import annotations

import os
import struct
import time
from pathlib import Path

HEADER_SIZE = 44


def _header(data_bytes: int, rate: int, channels: int = 1, bits: int = 16) -> bytes:
    block = channels * bits // 8
    return (
        b"RIFF"
        + struct.pack("<I", 36 + data_bytes)
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * block, block, bits)
        + b"data"
        + struct.pack("<I", data_bytes)
    )


class CrashSafeWav:
    def __init__(self, path: Path, rate: int, header_interval: float = 2.0, fsync_interval: float = 10.0):
        self.path = Path(path)
        self.rate = rate
        self._f = open(self.path, "wb")
        self._f.write(_header(0, rate))
        self.data_bytes = 0
        self._header_interval = header_interval
        self._fsync_interval = fsync_interval
        self._last_header = self._last_sync = time.monotonic()

    @property
    def samples(self) -> int:
        return self.data_bytes // 2

    def write(self, pcm: bytes) -> None:
        self._f.write(pcm)
        self.data_bytes += len(pcm)
        now = time.monotonic()
        if now - self._last_header >= self._header_interval:
            self._update_header()
            self._f.flush()
            self._last_header = now
        if now - self._last_sync >= self._fsync_interval:
            os.fsync(self._f.fileno())
            self._last_sync = now

    def write_silence(self, samples: int) -> None:
        chunk = b"\x00\x00" * min(samples, self.rate)
        while samples > 0:
            n = min(samples, self.rate)
            self.write(chunk[: n * 2])
            samples -= n

    def _update_header(self) -> None:
        pos = self._f.tell()
        self._f.seek(0)
        self._f.write(_header(self.data_bytes, self.rate))
        self._f.seek(pos)

    def close(self) -> None:
        if self._f.closed:
            return
        self._update_header()
        self._f.flush()
        os.fsync(self._f.fileno())
        self._f.close()


def repair(path: Path) -> bool:
    """Corrige la cabecera de un WAV PCM16 de meetrec según el tamaño real. Devuelve True si cambió algo."""
    path = Path(path)
    size = path.stat().st_size
    if size < HEADER_SIZE:
        return False
    with open(path, "r+b") as f:
        head = f.read(HEADER_SIZE)
        if head[:4] != b"RIFF" or head[36:40] != b"data":
            return False
        rate = struct.unpack("<I", head[24:28])[0]
        data_bytes = (size - HEADER_SIZE) // 2 * 2
        if struct.unpack("<I", head[40:44])[0] == data_bytes:
            return False
        f.seek(0)
        f.write(_header(data_bytes, rate))
    return True


def duration(path: Path) -> float:
    path = Path(path)
    with open(path, "rb") as f:
        head = f.read(HEADER_SIZE)
    rate = struct.unpack("<I", head[24:28])[0]
    return max(0, path.stat().st_size - HEADER_SIZE) / 2 / rate
