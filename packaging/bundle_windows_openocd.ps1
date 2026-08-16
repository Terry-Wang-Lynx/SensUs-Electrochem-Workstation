param(
    [Parameter(Mandatory = $true)]
    [string]$Destination
)

$ErrorActionPreference = "Stop"

# The official Windows archive is 32-bit, but runs on supported Windows x64
# through WOW64. It is built with libjaylink, so the portable package can use
# a J-Link without redistributing SEGGER's proprietary JLink.exe.
$OpenOcdVersion = "0.12.0"
$OpenOcdAsset = "openocd-v0.12.0-i686-w64-mingw32.tar.gz"
$OpenOcdUrl = "https://github.com/openocd-org/openocd/releases/download/v$OpenOcdVersion/$OpenOcdAsset"
$OpenOcdSha256 = "d7168545a6d5df4772b6090d470650f3eb8c9732dbd19b1f9027824c7f4a6fa3"

$TempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("sensus-openocd-" + [guid]::NewGuid().ToString("N"))
$Archive = Join-Path $TempRoot $OpenOcdAsset
$Extracted = Join-Path $TempRoot "extracted"

try {
    New-Item -ItemType Directory -Force -Path $TempRoot, $Extracted | Out-Null
    Write-Host "Downloading OpenOCD $OpenOcdVersion..."
    Invoke-WebRequest -Uri $OpenOcdUrl -OutFile $Archive -UseBasicParsing

    $actualHash = (Get-FileHash -Path $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $OpenOcdSha256) {
        throw "OpenOCD archive SHA-256 mismatch: expected $OpenOcdSha256, got $actualHash"
    }

    $tar = (Get-Command tar.exe -ErrorAction SilentlyContinue).Source
    if (-not $tar) {
        throw "Windows tar.exe is required to unpack the OpenOCD archive"
    }
    & $tar -xzf $Archive -C $Extracted
    if ($LASTEXITCODE -ne 0) {
        throw "OpenOCD archive extraction failed with exit code $LASTEXITCODE"
    }

    $bundleRoot = Get-ChildItem -Path $Extracted -Directory | Select-Object -First 1
    if (-not $bundleRoot) {
        throw "OpenOCD archive did not contain a top-level directory"
    }
    $sourceBin = Join-Path $bundleRoot.FullName "bin"
    $sourceScripts = Join-Path $bundleRoot.FullName "share\openocd\scripts"
    $sourceExecutable = Join-Path $sourceBin "openocd.exe"
    if (-not (Test-Path $sourceExecutable) -or -not (Test-Path $sourceScripts)) {
        throw "OpenOCD archive is missing openocd.exe or its scripts"
    }

    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $Destination
    $destinationBin = Join-Path $Destination "bin"
    $destinationScripts = Join-Path $Destination "share\openocd\scripts"
    New-Item -ItemType Directory -Force -Path $destinationBin, $destinationScripts | Out-Null
    Copy-Item -Path (Join-Path $sourceBin "*") -Destination $destinationBin -Recurse -Force
    Copy-Item -Path (Join-Path $sourceScripts "*") -Destination $destinationScripts -Recurse -Force
    Write-Host "Bundled OpenOCD at $Destination"
}
finally {
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $TempRoot
}
