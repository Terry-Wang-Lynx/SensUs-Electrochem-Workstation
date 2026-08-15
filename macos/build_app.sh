#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h:h}"
APP_NAME="SensUs Workstation"
BUILD_DIR="$ROOT/build/macos"
APP_BUILD="$BUILD_DIR/$APP_NAME.app"
APP_DIST="$ROOT/dist/$APP_NAME.app"
SOURCE="$ROOT/macos/Sources/main.swift"
PYTHON="$ROOT/.venv/bin/python3"

if [[ ! -x "$PYTHON" ]]; then
  echo "Missing $PYTHON. Run 'make install' first." >&2
  exit 1
fi

mkdir -p "$BUILD_DIR" "$ROOT/dist"

ICON_SOURCE="$BUILD_DIR/AppIcon-1024.png"
ICONSET="$BUILD_DIR/AppIcon.iconset"
"$PYTHON" "$ROOT/macos/create_icon.py" "$ICON_SOURCE"
rm -rf "$ICONSET"
mkdir -p "$ICONSET"

for spec in \
  "16 icon_16x16.png" \
  "32 icon_16x16@2x.png" \
  "32 icon_32x32.png" \
  "64 icon_32x32@2x.png" \
  "128 icon_128x128.png" \
  "256 icon_128x128@2x.png" \
  "256 icon_256x256.png" \
  "512 icon_256x256@2x.png" \
  "512 icon_512x512.png" \
  "1024 icon_512x512@2x.png"; do
  pixels="${spec%% *}"
  name="${spec#* }"
  /usr/bin/sips -z "$pixels" "$pixels" "$ICON_SOURCE" --out "$ICONSET/$name" >/dev/null
done
/usr/bin/iconutil -c icns "$ICONSET" -o "$BUILD_DIR/AppIcon.icns"

ARM_BINARY="$BUILD_DIR/SensUsWorkstation-arm64"
X86_BINARY="$BUILD_DIR/SensUsWorkstation-x86_64"
UNIVERSAL_BINARY="$BUILD_DIR/SensUsWorkstation"

xcrun swiftc -swift-version 5 -O -target arm64-apple-macos13.0 \
  -framework AppKit -framework WebKit "$SOURCE" -o "$ARM_BINARY"
xcrun swiftc -swift-version 5 -O -target x86_64-apple-macos13.0 \
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
