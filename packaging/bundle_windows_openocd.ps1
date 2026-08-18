param(
  [Parameter(Mandatory = $true)][string]$Source,
  [Parameter(Mandatory = $true)][string]$Destination
)

$ErrorActionPreference = "Stop"
$Source = [IO.Path]::GetFullPath($Source)
$Destination = [IO.Path]::GetFullPath($Destination)
if ($Source -eq $Destination -or $Destination -eq [IO.Path]::GetPathRoot($Destination)) {
  throw "Unsafe OpenOCD source or destination"
}

$Required = @(
  "bin\openocd.exe",
  "bin\libusb-1.0.dll",
  "share\openocd\scripts\interface\jlink.cfg",
  "share\openocd\scripts\target\nrf52.cfg",
  "source\openocd-0.12.0.tar.bz2",
  "source\libusb-1.0.29.tar.bz2",
  "licenses\OpenOCD-COPYING",
  "licenses\libusb-COPYING",
  "BINARY_DEPENDENCIES.txt",
  "COMPONENTS.json"
)
foreach ($Relative in $Required) {
  if (-not (Test-Path (Join-Path $Source $Relative))) {
    throw "Pinned OpenOCD build is incomplete; missing: $Relative"
  }
}

$Manifest = Get-Content (Join-Path $Source "COMPONENTS.json") -Raw |
  ConvertFrom-Json
if ($Manifest.schema -ne 1 -or $Manifest.platform -ne "windows-x64") {
  throw "Pinned OpenOCD component manifest has an unexpected schema or platform"
}
$ExpectedComponents = @{
  "OpenOCD" = "bin\openocd.exe"
  "libusb" = "bin\libusb-1.0.dll"
}
foreach ($Name in $ExpectedComponents.Keys) {
  $Component = @($Manifest.components | Where-Object name -eq $Name)
  if ($Component.Count -ne 1) {
    throw "Pinned OpenOCD manifest must contain exactly one $Name component"
  }
  $Binary = Join-Path $Source $ExpectedComponents[$Name]
  $Actual = (Get-FileHash -Algorithm SHA256 $Binary).Hash.ToLowerInvariant()
  if ($Actual -ne $Component[0].binary_sha256) {
    throw "$Name binary hash does not match COMPONENTS.json"
  }
  $SourceArchive = Join-Path $Source $Component[0].source
  $SourceHash = (Get-FileHash -Algorithm SHA256 $SourceArchive).Hash.ToLowerInvariant()
  if ($SourceHash -ne $Component[0].source_sha256) {
    throw "$Name source hash does not match COMPONENTS.json"
  }
}

$RuntimeDlls = @(Get-ChildItem (Join-Path $Source "bin") -File -Filter "*.dll" |
  ForEach-Object { $_.Name.ToLowerInvariant() })
if ($RuntimeDlls.Count -ne 1 -or $RuntimeDlls[0] -ne "libusb-1.0.dll") {
  throw "Pinned OpenOCD contains an unexpected runtime DLL set: $($RuntimeDlls -join ', ')"
}
$Dependencies = @(Get-Content (Join-Path $Source "BINARY_DEPENDENCIES.txt") |
  ForEach-Object { $_.Trim() } | Where-Object { $_ })
$AllowedDependencies = @(
  "libusb-1.0.dll", "kernel32.dll", "msvcrt.dll", "ws2_32.dll",
  "advapi32.dll", "user32.dll", "shell32.dll", "ole32.dll",
  "setupapi.dll", "cfgmgr32.dll", "ntdll.dll"
)
$UnexpectedDependencies = @($Dependencies | Where-Object {
  $_.ToLowerInvariant() -notin $AllowedDependencies
})
if ($UnexpectedDependencies.Count -gt 0) {
  throw "Pinned OpenOCD contains unexpected native dependencies: $($UnexpectedDependencies -join ', ')"
}

Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $Destination
New-Item -ItemType Directory -Force -Path $Destination | Out-Null
Copy-Item -Recurse (Join-Path $Source "*") $Destination
Copy-Item $PSCommandPath `
  (Join-Path $Destination "source\bundle_windows_openocd.ps1")

$OldPath = $env:PATH
try {
  $env:PATH = (Join-Path $Destination "bin") + ";" + $OldPath
  $OpenOcd = Join-Path $Destination "bin\openocd.exe"
  $VersionOutput = (& $OpenOcd --version 2>&1) -join "`n"
  if ($LASTEXITCODE -ne 0 -or $VersionOutput -notmatch "Open On-Chip Debugger 0\.12\.0") {
    throw "Bundled OpenOCD failed its version smoke test: $VersionOutput"
  }
  $AdapterOutput = (& $OpenOcd -c "echo [adapter list]; shutdown" 2>&1) -join "`n"
  if ($LASTEXITCODE -ne 0 -or $AdapterOutput -notmatch "(?i)jlink") {
    throw "Bundled OpenOCD does not expose the J-Link adapter: $AdapterOutput"
  }
} finally {
  $env:PATH = $OldPath
}
Write-Host "Bundled pinned J-Link-only OpenOCD at $Destination"
