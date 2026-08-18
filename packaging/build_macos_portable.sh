#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h:h}"
VERSION="$(awk -F'"' '/^version = / { print $2; exit }' "$ROOT/pyproject.toml")"
PYTHON="${PYTHON:-python3}"
REQUIRED_PYTHON_VERSION="${SENSUS_PORTABLE_PYTHON_VERSION:-3.12.13}"
MINIMUM_MACOS_VERSION="$(/usr/libexec/PlistBuddy -c 'Print :LSMinimumSystemVersion' "$ROOT/macos/Info.plist")"
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

if ! "$PYTHON" -c "import platform; raise SystemExit(platform.python_version() != '$REQUIRED_PYTHON_VERSION')"; then
  echo "Portable macOS builds require Python $REQUIRED_PYTHON_VERSION; got $($PYTHON -V 2>&1)" >&2
  exit 1
fi

mkdir -p "$BUILD" "$RELEASES" "${VENV:h}"
if [[ -x "$VENV/bin/python" ]] && ! "$VENV/bin/python" -c \
  "import platform; raise SystemExit(platform.python_version() != '$REQUIRED_PYTHON_VERSION')"; then
  rm -rf "$VENV"
fi
if [[ ! -x "$VENV/bin/python" ]]; then
  "$PYTHON" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --disable-pip-version-check \
  --require-hashes -r "$ROOT/packaging/portable-macos.lock"
"$VENV/bin/python" -m pip install --disable-pip-version-check \
  --no-deps --no-build-isolation -e "$ROOT"
"$VENV/bin/python" -m PyInstaller --noconfirm --clean \
  --distpath "$BUILD/pyinstaller-dist" --workpath "$BUILD/pyinstaller-work" \
  "$ROOT/packaging/portable.spec"
"$VENV/bin/python" "$ROOT/packaging/stage_resources.py" "$BUILD/workstation"
"$VENV/bin/python" "$ROOT/packaging/collect_python_licenses.py" \
  "$BUILD/python-licenses"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/backend"
xcrun swiftc -swift-version 5 -O -target "arm64-apple-macos${MINIMUM_MACOS_VERSION}" \
  -module-cache-path "$SWIFT_MODULECACHE_PATH" \
  -framework AppKit -framework WebKit "$ROOT/macos/Sources/main.swift" \
  -o "$APP/Contents/MacOS/SensUsWorkstation"
cp "$ROOT/macos/Info.plist" "$APP/Contents/Info.plist"
cp "$ROOT/packaging/THIRD_PARTY_NOTICES.txt" \
  "$APP/Contents/Resources/THIRD_PARTY_NOTICES.txt"
/bin/cp -R -X "$BUILD/python-licenses" \
  "$APP/Contents/Resources/THIRD_PARTY_LICENSES"
/bin/cp -R -X \
  "$BUILD/pyinstaller-dist/SensUsBackend" "$APP/Contents/Resources/backend"
/bin/cp -R -X \
  "$BUILD/workstation" "$APP/Contents/Resources/workstation"

ICON_SOURCE="$BUILD/AppIcon-1024.png"
"$VENV/bin/python" "$ROOT/macos/create_icon.py" "$ICON_SOURCE" \
  "$APP/Contents/Resources/AppIcon.icns"

"$ROOT/packaging/bundle_macos_openocd.sh" \
  "$APP/Contents/Resources/tools/openocd" "$MINIMUM_MACOS_VERSION"

for required in \
  "$APP/Contents/Resources/tools/openocd/bin/openocd" \
  "$APP/Contents/Resources/tools/openocd/lib/libusb-1.0.0.dylib" \
  "$APP/Contents/Resources/tools/openocd/COMPONENTS.json" \
  "$APP/Contents/Resources/tools/openocd/source/libusb-1.0.29.tar.bz2" \
  "$APP/Contents/Resources/tools/openocd/share/openocd/scripts/interface/jlink.cfg" \
  "$APP/Contents/Resources/tools/openocd/share/openocd/scripts/target/nrf52.cfg"; do
  [[ -e "$required" ]] || { echo "Missing portable resource: $required" >&2; exit 1; }
done

xattr -cr "$APP" 2>/dev/null || true
SIGN_IDENTITY="${SENSUS_MACOS_SIGN_IDENTITY:--}"
SIGN_OPTIONS=(--force --deep --sign "$SIGN_IDENTITY")
if [[ "$SIGN_IDENTITY" != "-" ]]; then
  SIGN_OPTIONS+=(--options runtime --timestamp)
fi
codesign "${SIGN_OPTIONS[@]}" "$APP"
codesign --verify --deep --strict "$APP"
if [[ "${SENSUS_SKIP_COMPATIBILITY_CHECK:-0}" != "1" ]]; then
  "$VENV/bin/python" "$ROOT/packaging/verify_macos_bundle.py" \
    "$APP" --minimum-version "$MINIMUM_MACOS_VERSION"
fi
echo "$APP"
