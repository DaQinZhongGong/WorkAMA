$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$project = "workama-install-smoke"
$envFile = Join-Path $env:TEMP "workama-install-smoke.env"

try {
  & "$PSScriptRoot/workama-ctl.ps1" init -EnvFile $envFile -Force
  $content = Get-Content $envFile -Raw
  $content = $content.Replace("WEB_PORT=3000", "WEB_PORT=3300")
  $content = $content.Replace("PLATFORM_API_PORT=8000", "PLATFORM_API_PORT=8300")
  $content = $content.Replace("AGENT_PORT=8001", "AGENT_PORT=8301")
  $content = $content.Replace("SANDBOX_FLEET_PORT=8002", "SANDBOX_FLEET_PORT=8302")
  $content = $content.Replace("GATEWAY_PORT=8080", "GATEWAY_PORT=8380")
  $content = $content.Replace("MINIO_PORT=9010", "MINIO_PORT=9310")
  $content = $content.Replace("MINIO_CONSOLE_PORT=9011", "MINIO_CONSOLE_PORT=9311")
  $content = $content.Replace("POSTGRES_PORT=55432", "POSTGRES_PORT=58432")
  $content = $content.Replace("REDIS_PORT=56379", "REDIS_PORT=59379")
  $content = $content.Replace("NATS_PORT=54222", "NATS_PORT=57222")
  $content = $content.Replace("NATS_MONITOR_PORT=58222", "NATS_MONITOR_PORT=59222")
  $content = $content.Replace("OTEL_GRPC_PORT=14317", "OTEL_GRPC_PORT=15317")
  $content = $content.Replace("OTEL_HTTP_PORT=14318", "OTEL_HTTP_PORT=15318")
  $content = $content.Replace("OTEL_METRICS_PORT=19464", "OTEL_METRICS_PORT=19564")
  $content = $content.Replace("OTEL_HEALTH_PORT=13133", "OTEL_HEALTH_PORT=15133")
  Set-Content -Path $envFile -Value $content -NoNewline

  & "$PSScriptRoot/workama-ctl.ps1" up -EnvFile $envFile -Project $project -Timeout 300
  $values = @{}
  Get-Content $envFile | ForEach-Object { if ($_ -match '^([^#=]+)=(.*)$') { $values[$matches[1]] = $matches[2] } }
  $status = Invoke-RestMethod -Uri "http://localhost:8300/api/v1/setup/status"
  if ($status.initialized) { throw "Fresh install unexpectedly contains a user" }
  $headers = @{ "X-Setup-Token" = $values.SETUP_TOKEN }
  $body = @{
    email = "install-smoke@workama.example.com"; password = "WorkAMA-Install-Smoke-2026!"
    display_name = "Install Smoke"; organization_name = "Install Validation"; workspace_name = "Validation"
  } | ConvertTo-Json
  Invoke-RestMethod -Method Post -Uri "http://localhost:8300/api/v1/setup/bootstrap" -Headers $headers -ContentType "application/json" -Body $body | Out-Null
  $after = Invoke-RestMethod -Uri "http://localhost:8300/api/v1/setup/status"
  if (-not $after.initialized) { throw "Bootstrap did not initialize the installation" }
  $login = Invoke-RestMethod -Method Post -Uri "http://localhost:8300/api/v1/auth/login" -ContentType "application/json" -Body (@{ email = "install-smoke@workama.example.com"; password = "WorkAMA-Install-Smoke-2026!" } | ConvertTo-Json)
  if (-not $login.access_token) { throw "First administrator cannot log in" }
  Write-Host "Clean install smoke passed: isolated stack, bootstrap guard, first Owner login, health checks."
}
finally {
  if (Test-Path $envFile) { docker compose --env-file $envFile -f "$root/deploy/compose/docker-compose.yml" -p $project down --volumes --remove-orphans }
  Remove-Item -LiteralPath $envFile -Force -ErrorAction SilentlyContinue
}
