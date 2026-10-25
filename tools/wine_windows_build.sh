#!/bin/bash
# Build the Windows executables with Wine.
# Works on Linux and macOS. Requires Windows Python installed inside the Wine prefix.
#
# Result:
#   dist/windows_x64_gnsf.exe      – CLI (console)
#   dist/windows_x64_gnsf-GUI.exe  – GUI (no console)

set -euo pipefail

cd "$(dirname "$0")/.."
export WINEDEBUG="-all"

WINE="${WINE:-$(command -v wine || command -v wine64 || true)}"
if [ -z "$WINE" ]; then
    echo "Error: Wine is not installed (set WINE=/path/to/wine to override)."
    exit 1
fi

WINE_PYTHON="${WINE_PYTHON:-python.exe}"
VENV=".venv_windows64_build"
PYINSTALLER=("$WINE" "$VENV/Scripts/pyinstaller.exe" --onefile --clean --noconfirm
             --icon AppIcons/app_icon.ico --add-data "AppIcons;AppIcons")

echo "Creating build environment ($VENV)..."
rm -rf "$VENV"
"$WINE" "$WINE_PYTHON" -m venv "$VENV"
"$WINE" "$VENV/Scripts/python.exe" -m pip install -q -r requirements.txt pyinstaller

echo "Building CLI version..."
"${PYINSTALLER[@]}" --console --name windows_x64_gnsf gnsf.py

echo "Building GUI version..."
"${PYINSTALLER[@]}" --windowed --name windows_x64_gnsf-GUI gnsf-GUI.py

rm -rf build ./*.spec "$VENV"

echo "Done:"
echo "  dist/windows_x64_gnsf.exe"
echo "  dist/windows_x64_gnsf-GUI.exe"
