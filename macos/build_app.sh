#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h:h}"
APP_NAME="SensUs Workstation"
BUILD_DIR="$ROOT/build/macos"
APP_BUILD="$BUILD_DIR/$APP_NAME.app"
APP_DIST="$ROOT/dist/$APP_NAME.app"
SOURCE="$ROOT/macos/Sources/main.swift"
PYTHON="$ROOT/.venv/bin/python3"
if [[ -z "${SWIFT_MODULECACHE_PATH:-}" ]]; then
  SWIFT_MODULECACHE_PATH="$(mktemp -d "${TMPDIR:-/tmp}/sensus-swift-cache.XXXXXX")"
  export SWIFT_MODULECACHE_PATH
fi

if [[ ! -x "$PYTHON" ]]; then
  echo "Missing $PYTHON. Run 'make install' first." >&2
  exit 1
fi

mkdir -p "$BUILD_DIR" "$ROOT/dist"

ICON_SOURCE="$BUILD_DIR/AppIcon-1024.png"
"$PYTHON" "$ROOT/macos/create_icon.py" "$ICON_SOURCE" "$BUILD_DIR/AppIcon.icns"

ARM_BINARY="$BUILD_DIR/SensUsWorkstation-arm64"
X86_BINARY="$BUILD_DIR/SensUsWorkstation-x86_64"
UNIVERSAL_BINARY="$BUILD_DIR/SensUsWorkstation"

xcrun swiftc -swift-version 5 -O -target arm64-apple-macos13.0 \
  -module-cache-path "$SWIFT_MODULECACHE_PATH" \
  -framework AppKit -framework WebKit "$SOURCE" -o "$ARM_BINARY"
xcrun swiftc -swift-version 5 -O -target x86_64-apple-macos13.0 \
  -module-cache-path "$SWIFT_MODULECACHE_PATH" \
  -framework AppKit -framework WebKit "$SOURCE" -o "$X86_BINARY"
/usr/bin/lipo -create "$ARM_BINARY" "$X86_BINARY" -output "$UNIVERSAL_BINARY"

rm -rf "$APP_BUILD"
mkdir -p "$APP_BUILD/Contents/MacOS" "$APP_BUILD/Contents/Resources"
cp "$UNIVERSAL_BINARY" "$APP_BUILD/Contents/MacOS/SensUsWorkstation"
cp "$ROOT/macos/Info.plist" "$APP_BUILD/Contents/Info.plist"
cp "$BUILD_DIR/AppIcon.icns" "$APP_BUILD/Contents/Resources/AppIcon.icns"

/usr/bin/codesign --force --deep --sign - "$APP_BUILD"
rm -rf "$APP_DIST"
/usr/bin/ditto "$APP_BUILD" "$APP_DIST"

echo "$APP_DIST"
