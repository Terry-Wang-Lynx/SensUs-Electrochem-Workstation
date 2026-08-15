#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h:h}"
VERSION="$(awk -F'"' '/^version = / { print $2; exit }' "$ROOT/pyproject.toml")"
APP="$ROOT/artifacts/releases/$VERSION/SensUs Workstation.app"
DMG="$ROOT/artifacts/releases/$VERSION/SensUs-Workstation-macOS-arm64-$VERSION.dmg"
STAGE="$(mktemp -d /tmp/sensus-dmg.XXXXXX)"
trap 'rm -rf "$STAGE"' EXIT
"$ROOT/packaging/build_macos_portable.sh"
ditto "$APP" "$STAGE/SensUs Workstation.app"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
hdiutil create -quiet -volname "SensUs Workstation" -srcfolder "$STAGE" \
  -format UDZO "$DMG"
shasum -a 256 "$DMG"
echo "$DMG"
