#!/bin/bash
# Start Warraq (macOS). Double-click in Finder or run ./run-mac.command
cd "$(dirname "$0")"
if [ ! -x ".venv/bin/python" ]; then
  echo "Warraq is not installed yet. Run install-mac.command first."
  exit 1
fi
exec .venv/bin/python -m warraq "$@"
