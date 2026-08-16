#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h:h}"
VERSION="$(awk -F'"' '/^version = / { print $2; exit }' "$ROOT/pyproject.toml")"
ARTIFACTS="$ROOT/artifacts"
BUILD="$ARTIFACTS/build/macos-arm64"
RELEASES="$ARTIFACTS/releases/$VERSION"
RELEASES="${SENSUS_RELEASES_DIR:-$RELEASES}"
VENV="$ARTIFACTS/build-env/macos-arm64"
APP_NAME="SensUs Workstation"
APP="$RELEASES/$APP_NAME.app"

# PyInstaller's global cache can live in a protected or cloud-synced folder.
# Keep each packaging run's cache in a local temporary directory instead.
if [[ -z "${PYINSTALLER_CONFIG_DIR:-}" ]]; then
  PYINSTALLER_CONFIG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/sensus-pyinstaller.XXXXXX")"
  export PYINSTALLER_CONFIG_DIR
fi
if [[ -z "${SWIFT_MODULECACHE_PATH:-}" ]]; then
  SWIFT_MODULECACHE_PATH="$(mktemp -d "${TMPDIR:-/tmp}/sensus-swift-cache.XXXXXX")"
  export SWIFT_MODULECACHE_PATH
fi

mkdir -p "$BUILD" "$RELEASES" "${VENV:h}"
if [[ ! -x "$VENV/bin/python" ]]; then
  "${PYTHON:-python3}" -m venv "$VENV"
fi
INSTALL_FLAGS=()
if "$VENV/bin/python" -c 'import setuptools, wheel' >/dev/null 2>&1; then
  # Reuse the pinned build environment when packaging offline. A fresh venv
  # still falls back to pip's normal isolated build dependency installation.
  INSTALL_FLAGS+=(--no-build-isolation)
fi
"$VENV/bin/python" -m pip install --disable-pip-version-check \
  "${INSTALL_FLAGS[@]}" -e "${ROOT}[portable]"
"$VENV/bin/python" -m PyInstaller --noconfirm --clean \
  --distpath "$BUILD/pyinstaller-dist" --workpath "$BUILD/pyinstaller-work" \
  "$ROOT/packaging/portable.spec"
"$VENV/bin/python" "$ROOT/packaging/stage_resources.py" "$BUILD/workstation"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/backend"
xcrun swiftc -swift-version 5 -O -target arm64-apple-macos13.0 \
  -module-cache-path "$SWIFT_MODULECACHE_PATH" \
  -framework AppKit -framework WebKit "$ROOT/macos/Sources/main.swift" \
  -o "$APP/Contents/MacOS/SensUsWorkstation"
cp "$ROOT/macos/Info.plist" "$APP/Contents/Info.plist"
/bin/cp -R -X \
  "$BUILD/pyinstaller-dist/SensUsBackend" "$APP/Contents/Resources/backend"
/bin/cp -R -X \
  "$BUILD/workstation" "$APP/Contents/Resources/workstation"

ICON_SOURCE="$BUILD/AppIcon-1024.png"
"$VENV/bin/python" "$ROOT/macos/create_icon.py" "$ICON_SOURCE" \
  "$APP/Contents/Resources/AppIcon.icns"

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
