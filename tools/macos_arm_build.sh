#!/bin/bash
# Build the macOS binaries.
#
# Result:
#   dist/GetNewSamsungFirmware-macOS-<arch>.dmg  – GUI app (GNSF GUI.app)
#   dist/macos_<arch>_gnsf                       – CLI binary

set -euo pipefail

cd "$(dirname "$0")/.."

ARCH=$(uname -m)
if [ "$ARCH" != "arm64" ]; then
    echo "Note: building on $ARCH, the binaries will be $ARCH-only."
fi

VENV=".venv_macos_build"
APP_NAME="GNSF GUI"
CLI_NAME="macos_${ARCH}_gnsf"
DMG_NAME="GetNewSamsungFirmware-macOS-$ARCH.dmg"

echo "Creating build environment ($VENV)..."
rm -rf "$VENV"
python3 -m venv "$VENV"
# certifi gives the bundled binaries a CA store (python.org builds have none by default)
"$VENV/bin/pip" install -q -r requirements.txt pyinstaller certifi

echo "Building CLI version..."
"$VENV/bin/pyinstaller" gnsf.py --onefile --console --clean --noconfirm --name "$CLI_NAME"

echo "Building GUI version..."
"$VENV/bin/pyinstaller" gnsf-GUI.py \
    --windowed --onedir --clean --noconfirm \
    --name "$APP_NAME" \
    --icon AppIcons/app_icon.icns \
    --add-data "AppIcons:AppIcons"

echo "Creating DMG..."
STAGE=$(mktemp -d)
cp -R "dist/$APP_NAME.app" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "GetNewSamsungFirmware" -srcfolder "$STAGE" -ov -format UDZO "dist/$DMG_NAME"

rm -rf "$STAGE" build ./*.spec "$VENV" "dist/$APP_NAME" "dist/$APP_NAME.app"

echo "Done:"
echo "  dist/$DMG_NAME"
echo "  dist/$CLI_NAME"
