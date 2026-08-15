#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h:h}"
VERSION="${1:-$(date +%Y%m%d)}"
PACKAGE_NAME="SensUs-电化学工作站-macOS-$VERSION"
OUTPUT="$ROOT/../$PACKAGE_NAME.zip"
TEMP_ROOT="$(mktemp -d /tmp/sensus-package.XXXXXX)"
PACKAGE_DIR="$TEMP_ROOT/$PACKAGE_NAME"
export COPYFILE_DISABLE=1

cleanup() {
  rm -rf "$TEMP_ROOT"
}
trap cleanup EXIT

cd "$ROOT"
"$ROOT/macos/build_app.sh"
mkdir -p "$PACKAGE_DIR"

/usr/bin/rsync -a \
  --exclude='.git/' \
  --exclude='.venv/' \
  --exclude='.venv-installed' \
  --exclude='.pytest_cache/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='*.egg-info/' \
  --exclude='.DS_Store' \
  --exclude='build/' \
  --exclude='dist/' \
  --exclude='measurements/' \
  --exclude='software/firmware/build/' \
  --exclude='software/firmware/tests/build/' \
  --exclude='software/firmware/src/measurement_config.h' \
  "$ROOT/" "$PACKAGE_DIR/"

/usr/bin/ditto --norsrc "$ROOT/dist/SensUs Workstation.app" \
  "$PACKAGE_DIR/SensUs 电化学工作站.app"
mkdir -p "$PACKAGE_DIR/measurements/experiment_data" "$PACKAGE_DIR/measurements/gui_runs"
chmod +x "$PACKAGE_DIR"/*.command "$PACKAGE_DIR"/macos/*.sh \
  "$PACKAGE_DIR"/macos/check_environment.py
/usr/bin/xattr -cr "$PACKAGE_DIR" 2>/dev/null || true
/usr/bin/codesign --verify --deep --strict "$PACKAGE_DIR/SensUs 电化学工作站.app"

(
  cd "$PACKAGE_DIR"
  find . -type f ! -name PACKAGE_MANIFEST.sha256 | LC_ALL=C sort | \
    while IFS= read -r file; do
      shasum -a 256 "$file"
    done
) > "$PACKAGE_DIR/PACKAGE_MANIFEST.sha256"

rm -f "$OUTPUT"
/usr/bin/ditto --norsrc -c -k --keepParent "$PACKAGE_DIR" "$OUTPUT"
shasum -a 256 "$OUTPUT"
echo "$OUTPUT"
