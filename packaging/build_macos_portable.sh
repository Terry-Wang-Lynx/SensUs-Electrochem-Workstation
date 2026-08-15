#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h:h}"
VERSION="$(awk -F'"' '/^version = / { print $2; exit }' "$ROOT/pyproject.toml")"
ARTIFACTS="$ROOT/artifacts"
BUILD="$ARTIFACTS/build/macos-arm64"
RELEASES="$ARTIFACTS/releases/$VERSION"
VENV="$ARTIFACTS/build-env/macos-arm64"
APP_NAME="SensUs Workstation"
APP="$RELEASES/$APP_NAME.app"

mkdir -p "$BUILD" "$RELEASES" "${VENV:h}"
if [[ ! -x "$VENV/bin/python" ]]; then
  "${PYTHON:-python3}" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --disable-pip-version-check -e "${ROOT}[portable]"
"$VENV/bin/python" -m PyInstaller --noconfirm --clean \
  --distpath "$BUILD/pyinstaller-dist" --workpath "$BUILD/pyinstaller-work" \
  "$ROOT/packaging/portable.spec"
"$VENV/bin/python" "$ROOT/packaging/stage_resources.py" "$BUILD/workstation"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/backend"
xcrun swiftc -swift-version 5 -O -target arm64-apple-macos13.0 \
  -framework AppKit -framework WebKit "$ROOT/macos/Sources/main.swift" \
  -o "$APP/Contents/MacOS/SensUsWorkstation"
cp "$ROOT/macos/Info.plist" "$APP/Contents/Info.plist"
ditto "$BUILD/pyinstaller-dist/SensUsBackend" "$APP/Contents/Resources/backend"
ditto "$BUILD/workstation" "$APP/Contents/Resources/workstation"

ICON_SOURCE="$BUILD/AppIcon-1024.png"
ICONSET="$BUILD/AppIcon.iconset"
"$VENV/bin/python" "$ROOT/macos/create_icon.py" "$ICON_SOURCE"
rm -rf "$ICONSET"
mkdir -p "$ICONSET"
for spec in \
  "16 icon_16x16.png" "32 icon_16x16@2x.png" "32 icon_32x32.png" \
  "64 icon_32x32@2x.png" "128 icon_128x128.png" "256 icon_128x128@2x.png" \
  "256 icon_256x256.png" "512 icon_256x256@2x.png" \
  "512 icon_512x512.png" "1024 icon_512x512@2x.png"; do
  pixels="${spec%% *}"; name="${spec#* }"
  sips -z "$pixels" "$pixels" "$ICON_SOURCE" --out "$ICONSET/$name" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

OPENOCD="${SENSUS_OPENOCD_EXE:-}"
if [[ -z "$OPENOCD" ]]; then
  OPENOCD="$(command -v openocd || true)"
fi
if [[ -n "$OPENOCD" && -x "$OPENOCD" ]]; then
  "$ROOT/packaging/bundle_macos_openocd.sh" "$OPENOCD" \
    "$APP/Contents/Resources/tools/openocd"
fi

xattr -cr "$APP" 2>/dev/null || true
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"
echo "$APP"
