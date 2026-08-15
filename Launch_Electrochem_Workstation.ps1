# SensUs Electrochem Workstation - Windows PowerShell Launcher
# Double-click or run: powershell -ExecutionPolicy Bypass -File Launch_Electrochem_Workstation.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:SENSUS_PROJECT_DIR = $PSScriptRoot

$Python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$StampFile = Join-Path $PSScriptRoot ".venv-installed"
$PyProject = Join-Path $PSScriptRoot "pyproject.toml"

# Create venv if needed
if (-not (Test-Path $Python)) {
    Write-Host "Creating the local Python environment..."
    python -m venv (Join-Path $PSScriptRoot ".venv")
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Python environment setup failed."
        Read-Host "Press Enter to close"
        exit 1
    }
}

# Install/reinstall if needed
$needInstall = $false
if (-not (Test-Path $StampFile)) {
    $needInstall = $true
} elseif ((Get-Item $PyProject).LastWriteTime -gt (Get-Item $StampFile).LastWriteTime) {
    Write-Host "pyproject.toml updated, reinstalling..."
    $needInstall = $true
}

if ($needInstall) {
    Write-Host "Installing the SensUs workstation..."
    & $Python -m pip install -e $PSScriptRoot
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Installation failed."
        Read-Host "Press Enter to close"
        exit 1
    }
    New-Item -ItemType File -Path $StampFile -Force | Out-Null
}

# Launch
Start-Process $Python -ArgumentList "-m", "pa_host.gui_server", "--open-browser"
