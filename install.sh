#!/usr/bin/env bash
# Instala meetrec para el usuario actual (sin sudo, sin tocar la configuración de audio).
#  - entorno Python en ./.venv
#  - comando global:   ~/.local/bin/meetrec
#  - menú de apps:     ~/.local/share/applications/meetrec.desktop
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
DIR="$PWD"

for cmd in pw-record pw-dump pw-play; do
  command -v "$cmd" >/dev/null || { echo "Falta $cmd (paquete pipewire-bin)"; exit 1; }
done

if command -v uv >/dev/null; then
  [ -x .venv/bin/python ] || uv venv --python 3.12 .venv
  uv pip install --python .venv/bin/python -e .
else
  [ -x .venv/bin/python ] || python3 -m venv .venv
  .venv/bin/pip install -e .
fi

mkdir -p ~/.local/bin ~/.local/share/applications ~/.local/share/icons/hicolor/scalable/apps
ln -sf "$DIR/.venv/bin/meetrec" ~/.local/bin/meetrec
cp meetrec/icon.svg ~/.local/share/icons/hicolor/scalable/apps/meetrec.svg

cat > ~/.local/share/applications/meetrec.desktop <<EOF
[Desktop Entry]
Type=Application
Name=Grabador de Reuniones
GenericName=Grabar y transcribir reuniones
Comment=Graba micrófono y audio del sistema por separado y genera la transcripción en Markdown
Exec=$HOME/.local/bin/meetrec
Icon=meetrec
Terminal=false
Categories=AudioVideo;Audio;Recorder;
Keywords=meet;zoom;teams;reunión;grabar;transcripción;whisper;
StartupWMClass=meetrec
EOF

command -v update-desktop-database >/dev/null && update-desktop-database ~/.local/share/applications || true
command -v kbuildsycoca5 >/dev/null && kbuildsycoca5 >/dev/null 2>&1 || true

echo "Verificando modelo de Whisper (se descarga una sola vez si falta)…"
.venv/bin/python - <<'PY'
from meetrec.config import Config
from huggingface_hub import snapshot_download
from faster_whisper.utils import _MODELS
cfg = Config.load()
repo = _MODELS.get(cfg.model, cfg.model)
try:
    snapshot_download(repo, local_files_only=True); print("Modelo ya disponible:", repo)
except Exception:
    print("Descargando", repo); snapshot_download(repo)
PY

echo
echo "Listo. Abrilo desde el menú (\"Grabador de Reuniones\") o con:  meetrec"
