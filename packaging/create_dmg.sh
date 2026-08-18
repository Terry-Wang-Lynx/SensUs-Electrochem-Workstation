#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h:h}"
VERSION="$(awk -F'"' '/^version = / { print $2; exit }' "$ROOT/pyproject.toml")"
RELEASES="${SENSUS_RELEASES_DIR:-$ROOT/artifacts/releases/$VERSION}"
APP="$RELEASES/SensUs Workstation.app"
DMG="${SENSUS_DMG_PATH:-$RELEASES/SensUs-Workstation-macOS-arm64-$VERSION.dmg}"
STAGE="$(mktemp -d /tmp/sensus-dmg.XXXXXX)"
trap 'rm -rf "$STAGE"' EXIT
"$ROOT/packaging/build_macos_portable.sh"
/bin/cp -R -X "$APP" "$STAGE/SensUs Workstation.app"
cp "$ROOT/packaging/THIRD_PARTY_NOTICES.txt" "$STAGE/THIRD_PARTY_NOTICES.txt"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
if ! hdiutil create -quiet -volname "SensUs Workstation" -srcfolder "$STAGE" \
  -format UDZO "$DMG"; then
  # Sandboxed build runners cannot start hdiejectd, but can still emit a
  # mountable HFS disk image. Normal macOS builds keep the compressed UDZO path.
  rm -f "$DMG"
  hdiutil makehybrid -quiet -hfs -o "$DMG" "$STAGE"
fi
SIGN_IDENTITY="${SENSUS_MACOS_SIGN_IDENTITY:--}"
if [[ "$SIGN_IDENTITY" != "-" ]]; then
  codesign --force --sign "$SIGN_IDENTITY" --timestamp "$DMG"
fi
if [[ -n "${SENSUS_NOTARY_PROFILE:-}" ]]; then
  xcrun notarytool submit "$DMG" \
    --keychain-profile "$SENSUS_NOTARY_PROFILE" --wait
  xcrun stapler staple "$DMG"
  xcrun stapler validate "$DMG"
fi
shasum -a 256 "$DMG"
echo "$DMG"
