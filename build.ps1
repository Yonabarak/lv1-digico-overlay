# Build script for the LV1-DiGiCo Overlay app.
#
# Produces a one-folder bundle under dist\lv1_overlay\.
# Run from the project root:   .\build.ps1
#
# Settings, log, and pid file live next to the .exe (see _BASE_DIR in
# lv1_overlay.py), so the folder is fully portable.

$ErrorActionPreference = "Stop"
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Host "venv not found at $py" -ForegroundColor Red
    exit 1
}

Write-Host "=== Cleaning previous build artifacts ==="
Remove-Item -Recurse -Force build, dist, __pycache__ -ErrorAction SilentlyContinue

Write-Host "=== Running PyInstaller (one-folder, no console, version info) ==="
# We only use PyQt6.QtCore / QtGui / QtWidgets — exclude every other Qt
# submodule.  Saves ~120 MB and a few seconds of startup time.
$excludes = @(
    "PyQt6.QtBluetooth", "PyQt6.QtDBus", "PyQt6.QtDesigner", "PyQt6.QtHelp",
    "PyQt6.QtMultimedia", "PyQt6.QtMultimediaWidgets", "PyQt6.QtNetwork",
    "PyQt6.QtNfc", "PyQt6.QtOpenGL", "PyQt6.QtOpenGLWidgets",
    "PyQt6.QtPdf", "PyQt6.QtPdfWidgets", "PyQt6.QtPositioning",
    "PyQt6.QtPrintSupport", "PyQt6.QtQml", "PyQt6.QtQuick",
    "PyQt6.QtQuick3D", "PyQt6.QtQuickWidgets", "PyQt6.QtRemoteObjects",
    "PyQt6.QtSensors", "PyQt6.QtSerialPort", "PyQt6.QtSpatialAudio",
    "PyQt6.QtSql", "PyQt6.QtStateMachine", "PyQt6.QtSvg",
    "PyQt6.QtSvgWidgets", "PyQt6.QtTest", "PyQt6.QtTextToSpeech",
    "PyQt6.QtWebChannel", "PyQt6.QtWebSockets", "PyQt6.QtXml",
    "PyQt6.QAxContainer", "PyQt6.lupdate", "PyQt6.uic"
)
$excludeArgs = $excludes | ForEach-Object { @("--exclude-module", $_) }

& $py -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --name LV1-Digico-Overlay `
    --version-file version_info.txt `
    --hidden-import lv1_session `
    --hidden-import digico_multichannel `
    @excludeArgs `
    lv1_overlay.py

if ($LASTEXITCODE -ne 0) {
    Write-Host "Build failed (exit $LASTEXITCODE)" -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "=== Build complete ===" -ForegroundColor Green
Write-Host "Bundle: $PSScriptRoot\dist\LV1-Digico-Overlay\"
Write-Host "EXE   : $PSScriptRoot\dist\LV1-Digico-Overlay\LV1-Digico-Overlay.exe"
$bundle = Join-Path $PSScriptRoot "dist\LV1-Digico-Overlay"
if (Test-Path $bundle) {
    $size = (Get-ChildItem $bundle -Recurse | Measure-Object -Property Length -Sum).Sum
    Write-Host "Size  : $([math]::Round($size / 1MB, 1)) MB"
}
