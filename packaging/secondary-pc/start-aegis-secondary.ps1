param(
    [string]$InstallRoot = "C:\AEGIS-Secondary",
    [string]$RelayUrl = "https://relay.aquariusageai.com/api/secondary/news"
)

$ErrorActionPreference = "Stop"
$runtimeRoot = Join-Path $InstallRoot "runtime"
$python = Join-Path $runtimeRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Runtime non installato. Esegui prima install-aegis-secondary.ps1."
}

$relayKeyPath = Join-Path $InstallRoot "relay-key.txt"
$previousRelayKey = $env:AEGIS_SECONDARY_NEWS_RELAY_KEY
$previousRelayUrl = $env:AEGIS_SECONDARY_NEWS_RELAY_URL
try {
    if (Test-Path -LiteralPath $relayKeyPath) {
        $env:AEGIS_SECONDARY_NEWS_RELAY_KEY = (Get-Content -Raw -LiteralPath $relayKeyPath).Trim()
    }
    if ($RelayUrl) { $env:AEGIS_SECONDARY_NEWS_RELAY_URL = $RelayUrl }
    Start-Process -FilePath $python -ArgumentList "-m", "app.main", "etoro-demo-runtime" -WorkingDirectory $runtimeRoot -WindowStyle Hidden
} finally {
    $env:AEGIS_SECONDARY_NEWS_RELAY_KEY = $previousRelayKey
    $env:AEGIS_SECONDARY_NEWS_RELAY_URL = $previousRelayUrl
}
Write-Output "AEGIS secondary runtime avviato in modalita read-only: $runtimeRoot"
