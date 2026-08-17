param([string]$Python = "python")
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VersionLine = Select-String -Path (Join-Path $Root "pyproject.toml") -Pattern '^version = "(.+)"$'
$Version = $VersionLine.Matches[0].Groups[1].Value
$Artifacts = Join-Path $Root "artifacts"
$Build = Join-Path $Artifacts "build\windows-x64"
$Venv = Join-Path $Artifacts "build-env\windows-x64"
$Release = Join-Path $Artifacts "releases\$Version\SensUs-Workstation-Windows-x64-$Version"
$VenvPython = Join-Path $Venv "Scripts\python.exe"

$SelectedVersion = & $Python -c "import json,sys; print(json.dumps(list(sys.version_info[:2])))"
if ($LASTEXITCODE -ne 0 -or $SelectedVersion.Trim() -ne '[3, 12]') {
  throw "Portable Windows builds require Python 3.12; selected: $SelectedVersion"
}

New-Item -ItemType Directory -Force -Path $Build, (Split-Path $Venv), (Split-Path $Release) | Out-Null
if (Test-Path $VenvPython) {
  $VenvVersion = & $VenvPython -c "import json,sys; print(json.dumps(list(sys.version_info[:2])))"
  if ($VenvVersion.Trim() -ne '[3, 12]') {
    Remove-Item -Recurse -Force $Venv
  }
}
if (-not (Test-Path $VenvPython)) { & $Python -m venv $Venv }
& $VenvPython -m pip install --disable-pip-version-check -e "${Root}[portable]"
& $VenvPython -m PyInstaller --noconfirm --clean `
  --distpath (Join-Path $Build "pyinstaller-dist") `
  --workpath (Join-Path $Build "pyinstaller-work") `
  (Join-Path $Root "packaging\portable.spec")
& $VenvPython (Join-Path $Root "packaging\stage_resources.py") (Join-Path $Build "workstation")

Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $Release
New-Item -ItemType Directory -Force -Path $Release | Out-Null
Copy-Item -Recurse (Join-Path $Build "pyinstaller-dist\SensUsBackend\*") $Release
Copy-Item -Recurse (Join-Path $Build "workstation") (Join-Path $Release "workstation")
& (Join-Path $Root "packaging\bundle_windows_openocd.ps1") `
  -Destination (Join-Path $Release "tools\openocd")
Copy-Item (Join-Path $Root "packaging\THIRD_PARTY_NOTICES.txt") `
  (Join-Path $Release "THIRD_PARTY_NOTICES.txt")
$Zip = "$Release.zip"
Remove-Item -Force -ErrorAction SilentlyContinue $Zip
Compress-Archive -Path "$Release\*" -DestinationPath $Zip -CompressionLevel Optimal
Get-FileHash -Algorithm SHA256 $Zip
Write-Output $Zip
