# 10_調査部\.env の FRED_API_KEY を GitHub の Secret に登録する（キーは画面に表示しない）
# 使い方: PowerShell で  .\register_fred_secret.ps1
$envFile = Join-Path (Split-Path $PSScriptRoot -Parent) ".env"
$line = Get-Content -LiteralPath $envFile -Encoding UTF8 | Where-Object { $_ -match '^FRED_API_KEY=' } | Select-Object -First 1
if (-not $line) { Write-Error "FRED_API_KEY not found in $envFile"; exit 1 }
$key = ($line -replace '^FRED_API_KEY=', '').Trim().Trim('"')
$key | gh secret set FRED_API_KEY --repo sin88123451/crash-warning-monitor
if ($LASTEXITCODE -eq 0) { Write-Host "OK: Secret FRED_API_KEY registered" }
