#!/bin/zsh
set -euo pipefail

SCRIPT_PATH="${0:A}"
DEST="${1:?destination required}"
MINIMUM_MACOS_VERSION="${2:?minimum macOS version required}"
OPENOCD_VERSION="0.12.0"
OPENOCD_ASSET="openocd-$OPENOCD_VERSION.tar.bz2"
OPENOCD_URL="https://downloads.sourceforge.net/project/openocd/openocd/$OPENOCD_VERSION/$OPENOCD_ASSET"
OPENOCD_SHA256="af254788be98861f2bd9103fe6e60a774ec96a8c374744eef9197f6043075afa"
LIBUSB_VERSION="1.0.29"
LIBUSB_ASSET="libusb-$LIBUSB_VERSION.tar.bz2"
LIBUSB_URL="https://github.com/libusb/libusb/releases/download/v$LIBUSB_VERSION/$LIBUSB_ASSET"
LIBUSB_SHA256="5977fc950f8d1395ccea9bd48c06b3f808fd3c2c961b44b0c2e6e29fc3a70a85"
PYTHON="${PYTHON:-python3}"

[[ -n "$DEST" && "$DEST" != "/" ]] || {
  echo "Unsafe OpenOCD destination" >&2
  exit 1
}
for command_name in curl make pkg-config shasum tar; do
  command -v "$command_name" >/dev/null || {
    echo "Missing OpenOCD build prerequisite: $command_name" >&2
    exit 1
  }
done

BUILD_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/sensus-openocd-macos.XXXXXX")"
trap 'rm -rf "$BUILD_ROOT"' EXIT
PREFIX="$BUILD_ROOT/prefix"
OPENOCD_PREFIX="$BUILD_ROOT/openocd-prefix"
DOWNLOADS="$BUILD_ROOT/downloads"
mkdir -p "$DOWNLOADS" "$PREFIX" "$OPENOCD_PREFIX"

download_verified() {
  local url="$1" expected="$2" output="$3" actual
  curl --fail --location --silent --show-error --retry 3 "$url" -o "$output"
  actual="$(shasum -a 256 "$output" | awk '{print $1}')"
  [[ "$actual" == "$expected" ]] || {
    echo "SHA-256 mismatch for $url: expected $expected, got $actual" >&2
    exit 1
  }
}

download_verified "$LIBUSB_URL" "$LIBUSB_SHA256" "$DOWNLOADS/$LIBUSB_ASSET"
download_verified "$OPENOCD_URL" "$OPENOCD_SHA256" "$DOWNLOADS/$OPENOCD_ASSET"
tar -xjf "$DOWNLOADS/$LIBUSB_ASSET" -C "$BUILD_ROOT"
tar -xjf "$DOWNLOADS/$OPENOCD_ASSET" -C "$BUILD_ROOT"

jobs="$(sysctl -n hw.logicalcpu 2>/dev/null || echo 4)"
architecture_flags="-arch arm64 -mmacosx-version-min=$MINIMUM_MACOS_VERSION"
(
  cd "$BUILD_ROOT/libusb-$LIBUSB_VERSION"
  env CFLAGS="$architecture_flags" LDFLAGS="$architecture_flags" \
    ./configure --prefix="$PREFIX" --enable-shared --disable-static
  make -j"$jobs"
  make install
)

disabled_adapters=(
  dummy rshim ftdi stlink ti-icdi ulink usb-blaster-2 ft232r vsllink xds110
  cmsis-dap-v2 osbdm opendous armjtagew rlink usbprog esp-usb-jtag cmsis-dap
  nulink kitprog usb-blaster presto openjtag buspirate aice parport jtag_vpi
  vdebug jtag_dpi amtjtagaccel bcm2835gpio imx_gpio am335xgpio ep93xx
  at91rm9200 gw16012 sysfsgpio xlnx-pcie-xvc remote-bitbang
)
configure_flags=()
for adapter in "${disabled_adapters[@]}"; do
  configure_flags+=("--disable-$adapter")
done
(
  cd "$BUILD_ROOT/openocd-$OPENOCD_VERSION"
  env \
    PKG_CONFIG_LIBDIR="$PREFIX/lib/pkgconfig" \
    PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig" \
    CFLAGS="$architecture_flags" \
    LDFLAGS="$architecture_flags" \
    ./configure \
      --prefix="$OPENOCD_PREFIX" \
      --enable-jlink \
      --without-capstone \
      --disable-werror \
      --disable-doxygen-html \
      "${configure_flags[@]}"
  make -j"$jobs"
  make install
)

rm -rf "$DEST"
mkdir -p "$DEST/bin" "$DEST/lib" "$DEST/share/openocd" \
  "$DEST/source" "$DEST/licenses"
cp "$OPENOCD_PREFIX/bin/openocd" "$DEST/bin/openocd"
cp -L "$PREFIX/lib/libusb-1.0.0.dylib" "$DEST/lib/libusb-1.0.0.dylib"
ditto "$OPENOCD_PREFIX/share/openocd/scripts" "$DEST/share/openocd/scripts"
cp "$DOWNLOADS/$OPENOCD_ASSET" "$DEST/source/$OPENOCD_ASSET"
cp "$DOWNLOADS/$LIBUSB_ASSET" "$DEST/source/$LIBUSB_ASSET"
cp "$SCRIPT_PATH" "$DEST/source/build_macos_openocd.sh"
tar -xOf "$DOWNLOADS/$OPENOCD_ASSET" \
  "openocd-$OPENOCD_VERSION/COPYING" > "$DEST/licenses/OpenOCD-COPYING"
tar -xOf "$DOWNLOADS/$LIBUSB_ASSET" \
  "libusb-$LIBUSB_VERSION/COPYING" > "$DEST/licenses/libusb-COPYING"

while IFS= read -r dependency; do
  case "$dependency" in
    *libusb-1.0*.dylib)
      install_name_tool -change "$dependency" \
        "@executable_path/../lib/libusb-1.0.0.dylib" "$DEST/bin/openocd"
      ;;
  esac
done < <(otool -L "$DEST/bin/openocd" | tail -n +2 | awk '{print $1}')
install_name_tool -id "@rpath/libusb-1.0.0.dylib" \
  "$DEST/lib/libusb-1.0.0.dylib"

{
  echo "openocd:"
  otool -L "$DEST/bin/openocd" | tail -n +2 | sed 's/^[[:space:]]*//'
  echo "libusb-1.0.0.dylib:"
  otool -L "$DEST/lib/libusb-1.0.0.dylib" | tail -n +2 | sed 's/^[[:space:]]*//'
} > "$DEST/BINARY_DEPENDENCIES.txt"
for binary in "$DEST/bin/openocd" "$DEST/lib/libusb-1.0.0.dylib"; do
  unexpected="$(otool -L "$binary" | tail -n +2 | awk '{print $1}' | \
    grep -Ev '^(@executable_path/|@loader_path/|@rpath/|/usr/lib/|/System/Library/)' || true)"
  [[ -z "$unexpected" ]] || {
    echo "Unbundled dependency in $binary: $unexpected" >&2
    exit 1
  }
  [[ "$(lipo -archs "$binary")" == *"arm64"* ]] || {
    echo "Bundled native file is missing arm64: $binary" >&2
    exit 1
  }
done

codesign --force --sign - "$DEST/lib/libusb-1.0.0.dylib"
codesign --force --sign - "$DEST/bin/openocd"
openocd_binary_sha="$(shasum -a 256 "$DEST/bin/openocd" | awk '{print $1}')"
libusb_binary_sha="$(shasum -a 256 "$DEST/lib/libusb-1.0.0.dylib" | awk '{print $1}')"
export SENSUS_OPENOCD_MANIFEST="$DEST/COMPONENTS.json"
export SENSUS_OPENOCD_BINARY_SHA="$openocd_binary_sha"
export SENSUS_LIBUSB_BINARY_SHA="$libusb_binary_sha"
export SENSUS_OPENOCD_SOURCE_URL="$OPENOCD_URL"
export SENSUS_LIBUSB_SOURCE_URL="$LIBUSB_URL"
export SENSUS_MINIMUM_MACOS_VERSION="$MINIMUM_MACOS_VERSION"
"$PYTHON" - <<'PY'
import json
import os
from pathlib import Path

manifest = {
    "schema": 1,
    "platform": "macos-arm64",
    "minimum_macos": os.environ["SENSUS_MINIMUM_MACOS_VERSION"],
    "components": [
        {
            "name": "OpenOCD",
            "version": "0.12.0",
            "license": "GPL-2.0-or-later",
            "binary": "bin/openocd",
            "binary_sha256": os.environ["SENSUS_OPENOCD_BINARY_SHA"],
            "source": "source/openocd-0.12.0.tar.bz2",
            "source_url": os.environ["SENSUS_OPENOCD_SOURCE_URL"],
            "source_sha256": "af254788be98861f2bd9103fe6e60a774ec96a8c374744eef9197f6043075afa",
        },
        {
            "name": "libusb",
            "version": "1.0.29",
            "license": "LGPL-2.1-or-later",
            "binary": "lib/libusb-1.0.0.dylib",
            "binary_sha256": os.environ["SENSUS_LIBUSB_BINARY_SHA"],
            "source": "source/libusb-1.0.29.tar.bz2",
            "source_url": os.environ["SENSUS_LIBUSB_SOURCE_URL"],
            "source_sha256": "5977fc950f8d1395ccea9bd48c06b3f808fd3c2c961b44b0c2e6e29fc3a70a85",
        },
    ],
}
Path(os.environ["SENSUS_OPENOCD_MANIFEST"]).write_text(
    json.dumps(manifest, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
)
PY
unset SENSUS_OPENOCD_MANIFEST SENSUS_OPENOCD_BINARY_SHA \
  SENSUS_LIBUSB_BINARY_SHA SENSUS_OPENOCD_SOURCE_URL SENSUS_LIBUSB_SOURCE_URL \
  SENSUS_MINIMUM_MACOS_VERSION
printf '%s\n' \
  "This runtime was built from the pinned OpenOCD and libusb source archives." \
  "OpenOCD includes its bundled Jim Tcl and libjaylink sources in $OPENOCD_ASSET." \
  "Build script: build_macos_openocd.sh (included in this directory)." \
  > "$DEST/source/SOURCE.txt"

version_output="$("$DEST/bin/openocd" --version 2>&1)"
[[ "$version_output" == *"Open On-Chip Debugger $OPENOCD_VERSION"* ]] || {
  echo "Unexpected bundled OpenOCD version: $version_output" >&2
  exit 1
}
adapter_output="$("$DEST/bin/openocd" -c 'echo [adapter list]; shutdown' 2>&1)"
[[ "$adapter_output" == *"jlink"* ]] || {
  echo "Bundled OpenOCD does not expose the J-Link adapter" >&2
  exit 1
}
