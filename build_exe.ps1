<#
.SYNOPSIS
    Build the Teams Transcript Muncher standalone executable.

.DESCRIPTION
    Produces dist\TeamsMuncher\ — a self-contained folder you can zip and share.
    Recipients need only Edge installed; no Python required.

    Prerequisites (run once):
        pip install -e .
        pip install pyinstaller

    Then run this script from the repo root:
        .\build_exe.ps1
#>

$ErrorActionPreference = "Stop"

# ── Ensure PyInstaller is available ─────────────────────────────────────────
if (-not (Get-Command pyinstaller -ErrorAction SilentlyContinue)) {
    Write-Host "Installing PyInstaller..." -ForegroundColor Cyan
    pip install pyinstaller
}

# ── Clean previous build ─────────────────────────────────────────────────────
if (Test-Path "dist\TeamsMuncher") {
    Write-Host "Removing previous dist\TeamsMuncher..." -ForegroundColor Cyan
    Remove-Item -Recurse -Force "dist\TeamsMuncher"
}

# ── Build ────────────────────────────────────────────────────────────────────
Write-Host "Building TeamsMuncher..." -ForegroundColor Cyan

pyinstaller `
    --onedir `
    --windowed `
    --name TeamsMuncher `
    --collect-all playwright `
    --hidden-import teams_transcript `
    --hidden-import teams_transcript.extractor `
    --hidden-import teams_transcript.formatter `
    --hidden-import teams_transcript.selectors `
    --hidden-import teams_transcript.gui `
    src\teams_transcript\gui.py

if ($LASTEXITCODE -ne 0) {
    Write-Host "Build failed." -ForegroundColor Red
    exit 1
}

# ── Done ─────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "Build complete: dist\TeamsMuncher\" -ForegroundColor Green
Write-Host ""
Write-Host "To distribute to teammates:"
Write-Host "  Compress-Archive dist\TeamsMuncher TeamsMuncher.zip"
Write-Host "  Share TeamsMuncher.zip — they unzip and run TeamsMuncher.exe."
Write-Host ""
Write-Host "First run on a new machine: click 'Sign In' in the app to"
Write-Host "authenticate. Subsequent runs go straight to the fetch screen."
