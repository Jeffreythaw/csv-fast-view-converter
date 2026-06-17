#!/bin/zsh
set -e

APP_DIR="$HOME/.csv-fast-view-converter"
SCRIPT_URL="https://raw.githubusercontent.com/Jeffreythaw/csv-fast-view-converter/main/tools/local_converter.py"
WEB_URL="https://csv-fast-view-converter.vercel.app"

mkdir -p "$APP_DIR"
cd "$APP_DIR"

echo "Setting up CSV Fast View Converter..."
curl -fsSL "$SCRIPT_URL" -o "$APP_DIR/local_converter.py"

if [ ! -d "$APP_DIR/.venv" ]; then
  python3 -m venv "$APP_DIR/.venv"
fi

"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
"$APP_DIR/.venv/bin/python" -m pip install "xlsxwriter==3.2.0"

echo ""
echo "Local converter is ready."
echo "Opening the web page..."
open "$WEB_URL" || true
echo ""
echo "Keep this window open while converting CSV files."
"$APP_DIR/.venv/bin/python" "$APP_DIR/local_converter.py" --serve-local
