param([string]$Python = "python")
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VersionLine = Select-String -Path (Join-Path $Root "pyproject.toml") -Pattern '^version = "(.+)"$'
$Version = $VersionLine.Matches[0].Groups[1].Value
$Artifacts = Join-Path $Root "artifacts"
$Build = Join-Path $Artifacts "build\windows-x64"
$WinUsb = Join-Path $Build "winusb"
$OpenOcd = Join-Path $Build "openocd"
$Venv = Join-Path $Artifacts "build-env\windows-x64"
$Release = Join-Path $Artifacts "releases\$Version\SensUs-Workstation-Windows-x64-$Version"
$VenvPython = Join-Path $Venv "Scripts\python.exe"

$RequiredPythonVersion = "3.12.13"
$SelectedVersion = & $Python -c "import platform; print(platform.python_version())"
if ($LASTEXITCODE -ne 0 -or $SelectedVersion.Trim() -ne $RequiredPythonVersion) {
  throw "Portable Windows builds require Python $RequiredPythonVersion; selected: $SelectedVersion"
}
$WinUsbRequired = @(
  "wdi-simple.exe",
  "COPYING",
  "COPYING-LGPL",
  "libwdi-1.5.1-source.zip",
  "libusb-win32-1.4.0.0-source.tar.gz",
  "libusbK-3.1.0.0-source.7z",
  "WDK-License.rtf",
  "WDK-REDIST.txt",
  "COMPONENTS.json",
  "SOURCE.txt"
)
$WinUsbMissing = @(
  $WinUsbRequired | Where-Object { -not (Test-Path (Join-Path $WinUsb $_)) }
)
if ($WinUsbMissing.Count -gt 0) {
  & (Join-Path $Root "packaging\build_windows_winusb_helper.ps1") `
    -Destination $WinUsb
}
foreach ($Name in $WinUsbRequired) {
  if (-not (Test-Path (Join-Path $WinUsb $Name))) {
    throw "WinUSB helper bundle is incomplete after build; missing: $Name"
  }
}

New-Item -ItemType Directory -Force -Path $Build, (Split-Path $Venv), (Split-Path $Release) | Out-Null
if (Test-Path $VenvPython) {
  $VenvVersion = & $VenvPython -c "import platform; print(platform.python_version())"
  if ($VenvVersion.Trim() -ne $RequiredPythonVersion) {
    Remove-Item -Recurse -Force $Venv
  }
}
if (-not (Test-Path $VenvPython)) { & $Python -m venv $Venv }
& $VenvPython -m pip install --disable-pip-version-check --require-hashes `
  -r (Join-Path $Root "packaging\portable-windows.lock")
& $VenvPython -m pip install --disable-pip-version-check `
  --no-deps --no-build-isolation -e $Root
$Icon = Join-Path $Build "SensUs-Workstation.ico"
& $VenvPython (Join-Path $Root "windows\create_icon.py") $Icon
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $Icon)) {
  throw "Failed to generate the Windows application icon: $Icon"
}
& $VenvPython -m PyInstaller --noconfirm --clean `
  --distpath (Join-Path $Build "pyinstaller-dist") `
  --workpath (Join-Path $Build "pyinstaller-work") `
  (Join-Path $Root "packaging\portable.spec")
& $VenvPython (Join-Path $Root "packaging\stage_resources.py") (Join-Path $Build "workstation")
& $VenvPython (Join-Path $Root "packaging\collect_python_licenses.py") `
  (Join-Path $Build "python-licenses")

Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $Release
New-Item -ItemType Directory -Force -Path $Release | Out-Null
Copy-Item -Recurse (Join-Path $Build "pyinstaller-dist\SensUsBackend\*") $Release
Copy-Item -Recurse (Join-Path $Build "workstation") (Join-Path $Release "workstation")
& (Join-Path $Root "packaging\bundle_windows_openocd.ps1") `
  -Source $OpenOcd `
  -Destination (Join-Path $Release "tools\openocd")
$WinUsbDestination = Join-Path $Release "tools\winusb"
New-Item -ItemType Directory -Force -Path $WinUsbDestination | Out-Null
Copy-Item (Join-Path $WinUsb "*") $WinUsbDestination
Copy-Item (Join-Path $Root "packaging\THIRD_PARTY_NOTICES.txt") `
  (Join-Path $Release "THIRD_PARTY_NOTICES.txt")
Copy-Item -Recurse (Join-Path $Build "python-licenses") `
  (Join-Path $Release "THIRD_PARTY_LICENSES")
$Zip = "$Release.zip"
Remove-Item -Force -ErrorAction SilentlyContinue $Zip
Compress-Archive -Path "$Release\*" -DestinationPath $Zip -CompressionLevel Optimal
Get-FileHash -Algorithm SHA256 $Zip
Write-Output $Zip
