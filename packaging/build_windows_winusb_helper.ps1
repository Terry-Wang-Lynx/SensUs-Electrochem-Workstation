param(
  [string]$Source = "",
  [string]$Destination = ""
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$LibwdiUpstream = "https://github.com/pbatard/libwdi.git"
$LibwdiCommit = "9b23b82a2dd1cbffc16d46c212f92c6bf8c0c602"
$WdkUrl = "https://go.microsoft.com/fwlink/p/?LinkID=253170"
$WdkSha256 = "29314207814ce9d5d73695f7e9239539cf37c79e750b9d5ea5a5ef5487a583d6"
$Libusb0Url = "https://github.com/mcuee/libusb-win32/releases/download/release_1.4.0.0/libusb-win32-bin-1.4.0.0.zip"
$Libusb0Sha256 = "9950b7a226e3ea387365c046d13b68bd0b0b18c015c034363601ff601c5b5585"
$LibusbKUrl = "https://github.com/mcuee/libusbk/releases/download/V3.1.0.0/libusbK-3.1.0.0-bin.7z"
$LibusbKSha256 = "38605d8d5a86f408a4b7bec60f6d4a096050eee72f89a63a8d5be125252d3fe7"

if (-not $Source) {
  $Source = Join-Path $Root "artifacts\vendor\libwdi"
}
if (-not $Destination) {
  $Destination = Join-Path $Root "artifacts\build\windows-x64\winusb"
}
$Source = [IO.Path]::GetFullPath($Source)
$Destination = [IO.Path]::GetFullPath($Destination)
$VendorRoot = Split-Path $Source
$DownloadCache = Join-Path $VendorRoot "downloads"

function Get-VerifiedDownload {
  param(
    [Parameter(Mandatory = $true)][string]$Url,
    [Parameter(Mandatory = $true)][string]$Sha256,
    [Parameter(Mandatory = $true)][string]$Path
  )
  if (Test-Path $Path) {
    $ExistingHash = (Get-FileHash -Algorithm SHA256 $Path).Hash.ToLowerInvariant()
    if ($ExistingHash -eq $Sha256) {
      return
    }
    Remove-Item -Force $Path
  }
  Write-Host "Downloading $(Split-Path -Leaf $Path)..."
  Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Path
  $ActualHash = (Get-FileHash -Algorithm SHA256 $Path).Hash.ToLowerInvariant()
  if ($ActualHash -ne $Sha256) {
    Remove-Item -Force $Path
    throw "SHA-256 mismatch for $Url. Expected $Sha256, got $ActualHash"
  }
}

if (-not (Test-Path $Source)) {
  New-Item -ItemType Directory -Force -Path (Split-Path $Source) | Out-Null
  & git clone --filter=blob:none --no-checkout $LibwdiUpstream $Source
  if ($LASTEXITCODE -ne 0) {
    throw "Unable to clone pinned libwdi source"
  }
  & git -C $Source checkout --detach $LibwdiCommit
  if ($LASTEXITCODE -ne 0) {
    throw "Unable to check out libwdi commit $LibwdiCommit"
  }
}
if (-not (Test-Path (Join-Path $Source ".git"))) {
  throw "libwdi source path exists but is not a Git checkout: $Source"
}
$ActualCommit = (& git -C $Source rev-parse HEAD).Trim().ToLowerInvariant()
if ($LASTEXITCODE -ne 0 -or $ActualCommit -ne $LibwdiCommit) {
  throw "libwdi must be checked out at $LibwdiCommit; found $ActualCommit"
}

New-Item -ItemType Directory -Force -Path $DownloadCache | Out-Null
$WdkMsi = Join-Path $DownloadCache "wdk-redist.msi"
$Libusb0Archive = Join-Path $DownloadCache "libusb0-redist.zip"
$LibusbKArchive = Join-Path $DownloadCache "libusbk-redist.7z"
Get-VerifiedDownload -Url $WdkUrl -Sha256 $WdkSha256 -Path $WdkMsi
Get-VerifiedDownload -Url $Libusb0Url -Sha256 $Libusb0Sha256 -Path $Libusb0Archive
Get-VerifiedDownload -Url $LibusbKUrl -Sha256 $LibusbKSha256 -Path $LibusbKArchive

$WdkRoot = Join-Path $Source "wdk"
$Libusb0Root = Join-Path $Source "libusb0"
$LibusbKRoot = Join-Path $Source "libusbk"
$ExtractRoot = Join-Path $Source "extract"
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $WdkRoot, $Libusb0Root, $LibusbKRoot, $ExtractRoot
New-Item -ItemType Directory -Force -Path $WdkRoot, $ExtractRoot | Out-Null

& msiexec.exe /a $WdkMsi /qn "TARGETDIR=$WdkRoot"
if ($LASTEXITCODE -notin @(0, 3010)) {
  throw "WDK redistributable extraction failed with exit code $LASTEXITCODE"
}

$Libusb0Extract = Join-Path $ExtractRoot "libusb0"
Expand-Archive -Path $Libusb0Archive -DestinationPath $Libusb0Extract -Force
$Libusb0Source = Get-ChildItem -Directory $Libusb0Extract |
  Where-Object Name -Like "libusb-win32*" |
  Select-Object -First 1
if (-not $Libusb0Source) {
  throw "libusb-win32 archive did not contain the expected directory"
}
Move-Item $Libusb0Source.FullName $Libusb0Root

$SevenZip = Get-Command 7z.exe -ErrorAction Stop
$LibusbKExtract = Join-Path $ExtractRoot "libusbk"
New-Item -ItemType Directory -Force -Path $LibusbKExtract | Out-Null
& $SevenZip.Source x $LibusbKArchive "-o$LibusbKExtract" -y | Out-Null
if ($LASTEXITCODE -ne 0) {
  throw "libusbK archive extraction failed with exit code $LASTEXITCODE"
}
$LibusbKSource = Get-ChildItem -Directory $LibusbKExtract |
  Where-Object Name -Like "libusbK*" |
  Select-Object -First 1
if (-not $LibusbKSource) {
  throw "libusbK archive did not contain the expected directory"
}
Move-Item $LibusbKSource.FullName $LibusbKRoot
Remove-Item -Recurse -Force $ExtractRoot

$BuildBatch = Join-Path ([IO.Path]::GetTempPath()) ("sensus-libwdi-" + [guid]::NewGuid().ToString("N") + ".cmd")
$env:SENSUS_LIBWDI_SOURCE = $Source
@'
@echo off
msbuild "%SENSUS_LIBWDI_SOURCE%\libwdi.sln" /m /p:Configuration=Release,Platform=Win32,BuildMacros="WDK_DIR=\"../wdk/Windows Kits/8.0\";LIBUSB0_DIR=\"../libusb0\";LIBUSBK_DIR=\"../libusbk/bin\""
exit /b %ERRORLEVEL%
'@ | Set-Content -Encoding ascii $BuildBatch
try {
  & cmd.exe /d /c $BuildBatch
  $BuildExitCode = $LASTEXITCODE
} finally {
  Remove-Item -Force -ErrorAction SilentlyContinue $BuildBatch
  Remove-Item Env:SENSUS_LIBWDI_SOURCE -ErrorAction SilentlyContinue
}
if ($BuildExitCode -ne 0) {
  throw "libwdi wdi-simple build failed with exit code $BuildExitCode"
}
$Helper = Join-Path $Source "Win32\Release\examples\wdi-simple.exe"
if (-not (Test-Path $Helper)) {
  throw "wdi-simple.exe was not produced at $Helper"
}

Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $Destination
New-Item -ItemType Directory -Force -Path $Destination | Out-Null
Copy-Item $Helper (Join-Path $Destination "wdi-simple.exe")
Copy-Item (Join-Path $Source "COPYING") (Join-Path $Destination "COPYING")
Copy-Item (Join-Path $Source "COPYING-LGPL") (Join-Path $Destination "COPYING-LGPL")
Copy-Item (Join-Path $Source "README.md") (Join-Path $Destination "libwdi-README.md")

$SourceArchive = Join-Path $Destination "libwdi-1.5.1-source.zip"
& git -C $Source archive --format=zip "--output=$SourceArchive" $LibwdiCommit
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $SourceArchive)) {
  throw "Unable to create the pinned libwdi source archive"
}
@"
libwdi 1.5.1
Upstream: https://github.com/pbatard/libwdi
Commit: $LibwdiCommit
Build script: packaging/build_windows_winusb_helper.ps1

The bundled wdi-simple.exe is unmodified and is used as a separate process.
Complete source for this exact binary is in libwdi-1.5.1-source.zip.
COPYING and COPYING-LGPL contain the applicable license terms.
"@ | Set-Content -Encoding ascii (Join-Path $Destination "SOURCE.txt")

Get-FileHash -Algorithm SHA256 (Join-Path $Destination "wdi-simple.exe")
Write-Output $Destination
