#!/usr/bin/env bash
set -euo pipefail

DEST="${1:?destination required}"
OPENOCD_VERSION="0.12.0"
OPENOCD_ASSET="openocd-$OPENOCD_VERSION.tar.bz2"
OPENOCD_URL="https://downloads.sourceforge.net/project/openocd/openocd/$OPENOCD_VERSION/$OPENOCD_ASSET"
OPENOCD_SHA256="af254788be98861f2bd9103fe6e60a774ec96a8c374744eef9197f6043075afa"
LIBUSB_VERSION="1.0.29"
LIBUSB_ASSET="libusb-$LIBUSB_VERSION.tar.bz2"
LIBUSB_URL="https://github.com/libusb/libusb/releases/download/v$LIBUSB_VERSION/$LIBUSB_ASSET"
LIBUSB_SHA256="5977fc950f8d1395ccea9bd48c06b3f808fd3c2c961b44b0c2e6e29fc3a70a85"

BUILD_ROOT="$(mktemp -d "${RUNNER_TEMP:-/tmp}/sensus-openocd-windows.XXXXXX")"
trap 'rm -rf "$BUILD_ROOT"' EXIT
PREFIX="$BUILD_ROOT/prefix"
DOWNLOADS="$BUILD_ROOT/downloads"
mkdir -p "$DOWNLOADS" "$PREFIX"

download_verified() {
  local url="$1" expected="$2" output="$3" actual
  curl --fail --location --silent --show-error "$url" -o "$output"
  actual="$(sha256sum "$output" | awk '{print $1}')"
  [[ "$actual" == "$expected" ]] || {
    echo "SHA-256 mismatch for $url: expected $expected, got $actual" >&2
    exit 1
  }
}

download_verified "$LIBUSB_URL" "$LIBUSB_SHA256" "$DOWNLOADS/$LIBUSB_ASSET"
download_verified "$OPENOCD_URL" "$OPENOCD_SHA256" "$DOWNLOADS/$OPENOCD_ASSET"
tar -xjf "$DOWNLOADS/$LIBUSB_ASSET" -C "$BUILD_ROOT"
tar -xjf "$DOWNLOADS/$OPENOCD_ASSET" -C "$BUILD_ROOT"

(
  cd "$BUILD_ROOT/libusb-$LIBUSB_VERSION"
  ./configure \
    --host=x86_64-w64-mingw32 \
    --prefix="$PREFIX" \
    --enable-shared \
    --disable-static
  make -j"${NUMBER_OF_PROCESSORS:-4}"
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
    LDFLAGS="-static-libgcc" \
    ./configure \
      --host=x86_64-w64-mingw32 \
      --prefix="$PREFIX/openocd" \
      --enable-jlink \
      --without-capstone \
      --disable-werror \
      --disable-doxygen-html \
      "${configure_flags[@]}"
  make -j"${NUMBER_OF_PROCESSORS:-4}"
  make install
)

rm -rf "$DEST"
mkdir -p "$DEST/bin" "$DEST/share/openocd" "$DEST/source" "$DEST/licenses"
cp "$PREFIX/openocd/bin/openocd.exe" "$DEST/bin/openocd.exe"
cp "$PREFIX/bin/libusb-1.0.dll" "$DEST/bin/libusb-1.0.dll"
cp -R "$PREFIX/openocd/share/openocd/scripts" "$DEST/share/openocd/scripts"
cp "$DOWNLOADS/$OPENOCD_ASSET" "$DEST/source/$OPENOCD_ASSET"
cp "$DOWNLOADS/$LIBUSB_ASSET" "$DEST/source/$LIBUSB_ASSET"
tar -xOf "$DOWNLOADS/$OPENOCD_ASSET" \
  "openocd-$OPENOCD_VERSION/COPYING" > "$DEST/licenses/OpenOCD-COPYING"
tar -xOf "$DOWNLOADS/$LIBUSB_ASSET" \
  "libusb-$LIBUSB_VERSION/COPYING" > "$DEST/licenses/libusb-COPYING"

dependencies="$(
  for binary in "$DEST/bin/openocd.exe" "$DEST/bin/libusb-1.0.dll"; do
    objdump -p "$binary" | sed -n 's/^[[:space:]]*DLL Name: //p'
  done | sort -fu
)"
printf '%s\n' "$dependencies" > "$DEST/BINARY_DEPENDENCIES.txt"
if printf '%s\n' "$dependencies" | grep -Eqi 'libgcc|libstdc\+\+|libwinpthread'; then
  echo "OpenOCD unexpectedly depends on a MinGW runtime DLL" >&2
  exit 1
fi
unexpected="$(printf '%s\n' "$dependencies" | grep -Eiv \
  '^(libusb-1\.0\.dll|kernel32\.dll|msvcrt\.dll|ws2_32\.dll|advapi32\.dll|user32\.dll|shell32\.dll|ole32\.dll|setupapi\.dll|cfgmgr32\.dll|ntdll\.dll)$' || true)"
[[ -z "$unexpected" ]] || {
  echo "Unexpected OpenOCD dependencies:" >&2
  printf '%s\n' "$unexpected" >&2
  exit 1
}

version_output="$(PATH="$DEST/bin:$PATH" "$DEST/bin/openocd.exe" --version 2>&1)"
[[ "$version_output" == *"Open On-Chip Debugger $OPENOCD_VERSION"* ]] || {
  echo "Unexpected OpenOCD version: $version_output" >&2
  exit 1
}
adapters="$(PATH="$DEST/bin:$PATH" "$DEST/bin/openocd.exe" -c 'echo [adapter list]; shutdown' 2>&1)"
[[ "$adapters" == *"jlink"* ]] || {
  echo "Pinned OpenOCD build does not contain the J-Link adapter" >&2
  exit 1
}

openocd_binary_sha="$(sha256sum "$DEST/bin/openocd.exe" | awk '{print $1}')"
libusb_binary_sha="$(sha256sum "$DEST/bin/libusb-1.0.dll" | awk '{print $1}')"
cat > "$DEST/COMPONENTS.json" <<EOF
{
  "schema": 1,
  "platform": "windows-x64",
  "components": [
    {
      "name": "OpenOCD",
      "version": "$OPENOCD_VERSION",
      "license": "GPL-2.0-or-later",
      "binary": "bin/openocd.exe",
      "binary_sha256": "$openocd_binary_sha",
      "source": "source/$OPENOCD_ASSET",
      "source_url": "$OPENOCD_URL",
      "source_sha256": "$OPENOCD_SHA256"
    },
    {
      "name": "libusb",
      "version": "$LIBUSB_VERSION",
      "license": "LGPL-2.1-or-later",
      "binary": "bin/libusb-1.0.dll",
      "binary_sha256": "$libusb_binary_sha",
      "source": "source/$LIBUSB_ASSET",
      "source_url": "$LIBUSB_URL",
      "source_sha256": "$LIBUSB_SHA256"
    }
  ]
}
EOF
printf '%s\n' \
  "This runtime was built from the pinned OpenOCD and libusb source archives." \
  "OpenOCD includes its bundled Jim Tcl and libjaylink sources in $OPENOCD_ASSET." \
  "Build script: packaging/build_windows_openocd.sh" \
  > "$DEST/source/SOURCE.txt"
