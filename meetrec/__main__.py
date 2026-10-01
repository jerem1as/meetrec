"""Punto de entrada.

  meetrec                     abre la aplicación
  meetrec transcribe <dir>    (re)transcribe una grabación
  meetrec repair <dir>        repara cabeceras WAV de una grabación interrumpida
  meetrec devices             lista dispositivos de PipeWire
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def ensure_cuda_libs() -> None:
    """CTranslate2 necesita cuBLAS/cuDNN de CUDA 12, instalados como wheels en el venv.

    El linker sólo mira LD_LIBRARY_PATH al iniciar el proceso, así que si falta lo
    agregamos y nos re-ejecutamos. No toca nada del sistema.
    """
    try:
        import nvidia  # paquete namespace de las wheels nvidia-*
    except ImportError:
        return
    libs = []
    for base in nvidia.__path__:
        for sub in ("cublas", "cudnn", "cuda_nvrtc"):
            p = Path(base) / sub / "lib"
            if p.is_dir():
                libs.append(str(p))
    current = os.environ.get("LD_LIBRARY_PATH", "")
    if all(lib in current.split(":") for lib in libs):
        return
    os.environ["LD_LIBRARY_PATH"] = ":".join(libs + ([current] if current else []))
    os.execv(sys.executable, [sys.executable, "-m", "meetrec", *sys.argv[1:]])


def main() -> None:
    parser = argparse.ArgumentParser(prog="meetrec", description="Grabador local de reuniones")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd")
    t = sub.add_parser("transcribe", help="transcribir (o retranscribir) una grabación")
    t.add_argument("directory", type=Path)
    t.add_argument("--progress-json", action="store_true", help=argparse.SUPPRESS)
    t.add_argument("--device", choices=["auto", "cuda", "cpu"])
    t.add_argument("--model")
    t.add_argument("--language", help="código de idioma (es, en...) o 'auto'")
    r = sub.add_parser("repair", help="reparar WAV de una grabación interrumpida")
    r.add_argument("directory", type=Path)
    sub.add_parser("devices", help="listar dispositivos de audio")
    args = parser.parse_args()

    from .config import Config, setup_logging

    if args.cmd == "transcribe":
        ensure_cuda_libs()
        setup_logging(args.verbose)
        from . import transcribe
        cfg = Config.load()
        if args.device:
            cfg.device = args.device
        if args.model:
            cfg.model = args.model
        if args.language:
            cfg.language = None if args.language == "auto" else args.language
        try:
            path = transcribe.run(args.directory, json_progress=args.progress_json, cfg=cfg)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        if not args.progress_json:
            print(path)
    elif args.cmd == "repair":
        from .wavfile import repair
        for f in sorted(args.directory.glob("*.wav")):
            print(f"{f.name}: {'reparado' if repair(f) else 'OK'}")
    elif args.cmd == "devices":
        from . import pw
        s = pw.snapshot()
        print("Micrófonos / entradas:")
        for d in s.sources:
            print(f"  {'*' if d.name == s.default_source else ' '} {d.description}  [{d.name}]")
        print("Salidas (se graba su monitor):")
        for d in s.sinks:
            print(f"  {'*' if d.name == s.default_sink else ' '} {d.description}  [{d.name}]")
    else:
        setup_logging(args.verbose)
        from .gui import run_gui
        sys.exit(run_gui())


if __name__ == "__main__":
    main()
