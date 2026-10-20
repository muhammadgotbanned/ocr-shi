#!/bin/bash
# Warraq installer for Linux (bonus platform; Windows and macOS are the main targets).
set -euo pipefail
cd "$(dirname "$0")"
command -v uv >/dev/null 2>&1 || { curl -LsSf https://astral.sh/uv/install.sh | sh; export PATH="$HOME/.local/bin:$PATH"; }
[ -x .venv/bin/python ] || uv venv --python 3.12 .venv
PY=.venv/bin/python
INDEX=https://download.pytorch.org/whl/cpu
if command -v nvidia-smi >/dev/null 2>&1; then
  DRIVER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1 | cut -d. -f1)
  CC=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1)
  if [ "$DRIVER" -ge 580 ] && awk "BEGIN{exit !($CC >= 7.5)}"; then INDEX=https://download.pytorch.org/whl/cu130
  elif [ "$DRIVER" -ge 525 ]; then INDEX=https://download.pytorch.org/whl/cu126; fi
fi
echo "Installing PyTorch from $INDEX"
uv pip install --python $PY torch torchvision --index-url $INDEX
uv pip install --python $PY -e .
echo "Done. Start with: .venv/bin/python -m warraq   (add --browser to use your web browser)"
