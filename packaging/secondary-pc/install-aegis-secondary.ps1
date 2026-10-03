param(
    [string]$InstallRoot = "C:\AEGIS-Secondary"
)

$ErrorActionPreference = "Stop"
$packageRoot = $PSScriptRoot
$runtimeSource = Join-Path $packageRoot "runtime"
$runtimeRoot = Join-Path $InstallRoot "runtime"
$venvRoot = Join-Path $runtimeRoot ".venv"

if (-not (Test-Path $runtimeSource)) {
    throw "Pacchetto runtime mancante: $runtimeSource"
}

$python = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $python) {
    throw "Python 3.12+ non trovato. Installalo da python.org e ripeti."
}
$version = & $python.Source --version 2>&1
if ($version -notmatch "Python 3\.(1[2-9]|[2-9][0-9])") {
    throw "Serve Python 3.12 o superiore. Versione trovata: $version"
}

New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null
Copy-Item (Join-Path $runtimeSource "*") $runtimeRoot -Recurse -Force

if (-not (Test-Path (Join-Path $venvRoot "Scripts\python.exe"))) {
    & $python.Source -m venv $venvRoot
}
$runtimePython = Join-Path $venvRoot "Scripts\python.exe"
& $runtimePython -m pip install --upgrade pip
& $runtimePython -m pip install -e $runtimeRoot

$envPath = Join-Path $runtimeRoot ".env"
if (-not (Test-Path $envPath)) {
    Copy-Item (Join-Path $runtimeRoot ".env.example") $envPath
}

Write-Output "Installazione completata: $runtimeRoot"
Write-Output "Modalita iniziale: Demo/read-only, kill switch attivo, ordini disabilitati."
Write-Output "Configura le credenziali solo nel file .env locale del secondo PC."
