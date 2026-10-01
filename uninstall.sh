#!/usr/bin/env bash
# Quita el lanzador, la entrada de menú y el ícono. NO borra grabaciones ni el entorno .venv.
set -euo pipefail
rm -f ~/.local/bin/meetrec ~/.local/share/applications/meetrec.desktop \
      ~/.local/share/icons/hicolor/scalable/apps/meetrec.svg
command -v kbuildsycoca5 >/dev/null && kbuildsycoca5 >/dev/null 2>&1 || true
echo "Desinstalado. Las grabaciones y ./.venv quedan intactas (borralas a mano si querés)."
