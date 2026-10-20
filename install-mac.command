#!/bin/bash
# Warraq installer for macOS (Apple Silicon recommended).
# Double-click in Finder (or run: bash install-mac.command).
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"

say() { printf "  %s\n" "$1"; }
printf "\n  \033[33mWarraq · ورّاق\033[0m\n  \033[90mLocal Arabic OCR & translation\033[0m\n\n"

if [ "$(uname -m)" != "arm64" ]; then
  say "⚠︎  This Mac has an Intel processor. Current PyTorch releases only support"
  say "   Apple Silicon (M1–M5) Macs, so the installation will most likely fail."
  read -r -p "  Continue anyway? [y/N] " ans
  [[ "${ans:-n}" =~ ^[Yy]$ ]] || exit 1
fi

# --------------------------------------------------------------- uv
if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv (Python manager)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

# --------------------------------------------------------------- environment
if [ ! -x ".venv/bin/python" ]; then
  say "Creating Python 3.12 environment…"
  uv venv --python 3.12 .venv >/dev/null
fi
PY="$ROOT/.venv/bin/python"

say "Installing PyTorch (with Apple Metal GPU support)…"
uv pip install --python "$PY" --upgrade torch torchvision
say "Installing Warraq…"
uv pip install --python "$PY" -e .
"$PY" -c "import torch; print('  PyTorch', torch.__version__, '| Apple GPU (Metal) available:', torch.backends.mps.is_available())"

# --------------------------------------------------------------- Warraq.app
APP="$HOME/Applications/Warraq.app"
say "Creating $APP …"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cat > "$APP/Contents/MacOS/Warraq" <<EOF
#!/bin/bash
cd "$ROOT"
exec "$PY" -m warraq "\$@"
EOF
chmod +x "$APP/Contents/MacOS/Warraq"
cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Warraq</string>
  <key>CFBundleDisplayName</key><string>Warraq</string>
  <key>CFBundleIdentifier</key><string>app.warraq.ocr</string>
  <key>CFBundleVersion</key><string>1.0.0</string>
  <key>CFBundleShortVersionString</key><string>1.0.0</string>
  <key>CFBundleExecutable</key><string>Warraq</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSApplicationCategoryType</key><string>public.app-category.productivity</string>
</dict></plist>
EOF
if command -v iconutil >/dev/null 2>&1 && command -v sips >/dev/null 2>&1; then
  ICONSET="$(mktemp -d)/AppIcon.iconset"
  mkdir -p "$ICONSET"
  for s in 16 32 128 256 512; do
    sips -z $s $s "warraq/assets/icon.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
    d=$((s * 2)); sips -z $d $d "warraq/assets/icon.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns" || true
fi
touch "$APP"

if [[ "${1:-}" == "--download-models" ]]; then
  say "Downloading the OCR models (≈2.3 GB)…"
  "$PY" -m warraq download
fi

printf "\n  \033[32mAll set.\033[0m Open Warraq from ~/Applications (or Launchpad), or run ./run-mac.command\n\n"
read -r -p "  Start Warraq now? [Y/n] " start
[[ "${start:-y}" =~ ^[Nn]$ ]] || open "$APP"
