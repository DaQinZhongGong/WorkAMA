[CmdletBinding()]
param(
  [Parameter(Position = 0)]
  [ValidateSet("preflight", "init", "up", "status", "health", "upgrade", "release-check", "down")]
  [string]$Command = "status",
  [string]$EnvFile,
  [string]$Project,
  [int]$Timeout = 180,
  [switch]$Force,
  [switch]$Volumes,
  [string]$ReleaseManifest,
  [string]$EvidenceDirectory,
  [switch]$VerifyDockerImage
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $root "deploy/compose/docker-compose.yml"
$releaseValidator = Join-Path $root "quality/release/validate-release.ps1"
$defaultProject = "workama"
if (-not $EnvFile) { $EnvFile = Join-Path $root ".env" }

function Normalize-Project([string]$value) {
  if ([string]::IsNullOrWhiteSpace($value)) { return $defaultProject }
  $normalized = $value.Trim().ToLowerInvariant()
  if ($normalized -notmatch '^workama(?:$|[-_][a-z0-9][a-z0-9_-]*)$') {
    throw "Invalid Compose project '$value'. Use 'workama' or a workama-prefixed variant such as 'workama-ci'."
  }
  return $normalized
}

$Project = Normalize-Project $Project

function Assert-RequiredFile([string]$path, [string]$description) {
  if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
    throw "$description was not found: $path"
  }
}

function Get-CommandOutput([object[]]$output) {
  $lines = @($output | ForEach-Object { if ($_ -ne $null) { [string]$_ } })
  if ($lines.Count -eq 0) { return "(no output)" }
  return ($lines -join [Environment]::NewLine)
}

function Invoke-NativeChecked([string]$FilePath, [string[]]$Arguments, [switch]$Capture) {
  $previousErrorAction = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $output = @(& $FilePath @Arguments 2>&1)
    $exitCode = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $previousErrorAction
  }
  if ($exitCode -ne 0) {
    $detail = Get-CommandOutput $output
    throw "Command failed with exit code ${exitCode}: $FilePath $($Arguments -join ' ')`n$detail"
  }
  if ($Capture) {
    return @($output | ForEach-Object { if ($_ -ne $null) { [string]$_ } })
  }
  $output | Write-Output
}

function Invoke-Docker([string[]]$Arguments, [switch]$Capture) {
  return Invoke-NativeChecked -FilePath "docker" -Arguments $Arguments -Capture:$Capture
}

function Get-EnvValues {
  Assert-RequiredFile $EnvFile "Environment file"
  $values = @{}
  Get-Content -LiteralPath $EnvFile | ForEach-Object {
    if ($_ -match '^([^#=]+)=(.*)$') { $values[$matches[1]] = $matches[2] }
  }
  return $values
}

function Get-Value($values, [string]$key, [string]$fallback) {
  if ($values.ContainsKey($key) -and $values[$key]) { return $values[$key] }
  return $fallback
}

function Invoke-Compose([string[]]$Arguments, [switch]$Capture) {
  Assert-RequiredFile $composeFile "Compose file"
  $commandArgs = @("compose", "--env-file", $EnvFile, "-f", $composeFile, "-p", $Project)
  $commandArgs += $Arguments
  return Invoke-Docker -Arguments $commandArgs -Capture:$Capture
}

function Get-ComposeServices {
  $services = @(Invoke-Compose -Arguments @("config", "--services") -Capture)
  $services = @($services | ForEach-Object { $_.Trim() } | Where-Object { $_ })
  if ($services.Count -eq 0) { throw "Compose did not declare any services for project '$Project'." }
  return $services
}

function Get-ComposeContainerRecords {
  $ids = @(Invoke-Compose -Arguments @("ps", "--all", "--quiet") -Capture)
  $ids = @($ids | ForEach-Object { $_.Trim() } | Where-Object { $_ })
  $records = @()
  foreach ($id in $ids) {
    $raw = @(Invoke-Docker -Arguments @("inspect", $id) -Capture)
    $document = (($raw -join [Environment]::NewLine) | ConvertFrom-Json)
    $container = $document[0]
    $labels = $container.Config.Labels
    $health = "none"
    if ($container.State.Health -and $container.State.Health.Status) { $health = [string]$container.State.Health.Status }
    $records += [pscustomobject]@{
      Id = [string]$container.Id
      Name = ([string]$container.Name).TrimStart('/')
      Service = [string]$labels.'com.docker.compose.service'
      Project = [string]$labels.'com.docker.compose.project'
      State = [string]$container.State.Status
      Health = $health
      Status = [string]$container.State.Status
    }
  }
  return $records
}

function Test-ComposeHealth([switch]$Quiet) {
  $services = @(Get-ComposeServices)
  $containers = @(Get-ComposeContainerRecords)
  $issues = @()
  if ($containers.Count -eq 0) { $issues += "No containers exist for Compose project '$Project'." }

  foreach ($service in $services) {
    $matching = @($containers | Where-Object { $_.Service -eq $service })
    if ($matching.Count -eq 0) {
      $issues += "Service '$service' has no container."
      continue
    }
    foreach ($container in $matching) {
      if ($container.Project -ne $Project) {
        $issues += "$($container.Name): Compose project label '$($container.Project)' does not match '$Project'."
      }
      if ($container.State -ne "running") {
        $issues += "$($container.Name): state is '$($container.State)' (expected running)."
      } elseif ($container.Health -eq "unhealthy" -or $container.Health -eq "starting") {
        $issues += "$($container.Name): Docker health is '$($container.Health)'."
      }
    }
  }

  if ($issues.Count -gt 0) {
    if (-not $Quiet) {
      $containers | Select-Object Service, Name, State, Health, Project | Format-Table -AutoSize | Out-String | Write-Host
    }
    throw ("Docker health check failed:`n - " + ($issues -join "`n - "))
  }

  if (-not $Quiet) {
    $containers | Sort-Object Service, Name | Select-Object Service, Name, State, Health | Format-Table -AutoSize | Out-String | Write-Host
    Write-Host "Docker health check passed for Compose project '$Project'."
  }
  return $containers
}

function Wait-ComposeHealth {
  $deadline = (Get-Date).AddSeconds($Timeout)
  $lastError = "no status available"
  while ((Get-Date) -lt $deadline) {
    try {
      Test-ComposeHealth -Quiet | Out-Null
      return
    } catch {
      $lastError = $_.Exception.Message
      Start-Sleep -Seconds 2
    }
  }
  throw "Docker health check timed out after $Timeout seconds. Last error: $lastError"
}

function Wait-WorkAMA {
  $values = Get-EnvValues
  $urls = @(
    "http://localhost:$(Get-Value $values 'PLATFORM_API_PORT' '8000')/readyz",
    "http://localhost:$(Get-Value $values 'GATEWAY_PORT' '8080')/healthz",
    "http://localhost:$(Get-Value $values 'AGENT_PORT' '8001')/healthz",
    "http://localhost:$(Get-Value $values 'SANDBOX_FLEET_PORT' '8002')/healthz",
    "http://localhost:$(Get-Value $values 'WEB_PORT' '3000')/"
  )
  $deadline = (Get-Date).AddSeconds($Timeout)
  foreach ($url in $urls) {
    $healthy = $false
    while ((Get-Date) -lt $deadline) {
      try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 3
        if ($response.StatusCode -lt 400) { $healthy = $true; break }
      } catch { Start-Sleep -Seconds 2 }
    }
    if (-not $healthy) { throw "Public health check timed out: $url" }
  }
  Write-Host "All public services are healthy."
}

function New-Secret([int]$bytes = 36) {
  $buffer = New-Object byte[] $bytes
  $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
  try { $rng.GetBytes($buffer) } finally { $rng.Dispose() }
  return [Convert]::ToBase64String($buffer).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

switch ($Command) {
  "preflight" {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "Docker CLI was not found in PATH." }
    Invoke-Docker -Arguments @("compose", "version") | Out-Null
    Invoke-Docker -Arguments @("info", "--format", "{{json .ServerVersion}}") | Out-Null
    Write-Host "Preflight passed: Docker Engine and Compose are available. Compose project: $Project"
  }
  "init" {
    if ((Test-Path -LiteralPath $EnvFile) -and -not $Force) { throw "$EnvFile already exists; use -Force to replace it." }
    $encryptionBytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($encryptionBytes) } finally { $rng.Dispose() }
    $setupToken = New-Secret 32
    $lines = @(
      "POSTGRES_DB=workama", "POSTGRES_USER=workama", "POSTGRES_PASSWORD=$(New-Secret 32)",
      "JWT_SECRET=$(New-Secret 48)", "INTERNAL_TOKEN=$(New-Secret 48)", "KEY_PEPPER=$(New-Secret 48)",
      "ENCRYPTION_KEY=$([Convert]::ToBase64String($encryptionBytes))", "MINIO_ROOT_USER=workama", "MINIO_ROOT_PASSWORD=$(New-Secret 32)",
      "SETUP_TOKEN=$setupToken", "AUTH_DEBUG_TOKENS=false", "VITE_PLATFORM_API_URL=http://localhost:20200", "VITE_AGENT_WS_URL=ws://localhost:20201",
      "WEB_PORT=3000", "PLATFORM_API_PORT=8000", "AGENT_PORT=8001", "SANDBOX_FLEET_PORT=8002", "GATEWAY_PORT=8080", "MINIO_PORT=9010", "MINIO_CONSOLE_PORT=9011",
      "SANDBOX_RUNTIME=runsc", "SANDBOX_REQUIRE_GVISOR=false", "SANDBOX_IDLE_SECONDS=900", "SANDBOX_TTL_SECONDS=86400",
      "POSTGRES_PORT=55432", "REDIS_PORT=56379", "NATS_PORT=54222", "NATS_MONITOR_PORT=58222",
      "OTEL_GRPC_PORT=14317", "OTEL_HTTP_PORT=14318", "OTEL_METRICS_PORT=19464", "OTEL_HEALTH_PORT=13133",
      "SMTP_HOST=", "SMTP_PORT=25", "SMTP_FROM=notifications@workama.local", "OTEL_ENABLED=true"
    )
    Set-Content -LiteralPath $EnvFile -Value $lines -Encoding utf8
    Write-Host "Environment created: $EnvFile"
    Write-Host "Setup URL: http://localhost:20204/setup"
    Write-Host "Setup token: $setupToken"
  }
  "up" {
    Get-EnvValues | Out-Null
    Invoke-Compose -Arguments @("up", "--build", "-d") | Out-Null
    Wait-ComposeHealth
    Wait-WorkAMA
    $values = Get-EnvValues
    Write-Host "Web: http://localhost:$(Get-Value $values 'WEB_PORT' '3000')"
    Write-Host "Setup: http://localhost:$(Get-Value $values 'WEB_PORT' '3000')/setup"
  }
  "status" {
    Get-EnvValues | Out-Null
    Invoke-Compose -Arguments @("ps", "--all") | Out-Null
    Test-ComposeHealth | Out-Null
    Wait-WorkAMA
  }
  "health" {
    Get-EnvValues | Out-Null
    Test-ComposeHealth | Out-Null
  }
  "upgrade" {
    Get-EnvValues | Out-Null
    $stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
    $target = Join-Path $root "quality/evidence/install/$stamp"
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    $values = Get-EnvValues
    $dump = @(Invoke-Compose -Arguments @("exec", "-T", "postgres", "pg_dump", "-U", (Get-Value $values 'POSTGRES_USER' 'workama'), (Get-Value $values 'POSTGRES_DB' 'workama')) -Capture)
    Set-Content -LiteralPath (Join-Path $target "postgres.sql") -Value $dump -Encoding utf8
    @{ created_at = (Get-Date).ToUniversalTime().ToString("o"); project = $Project; compose = $composeFile } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $target "manifest.json") -Encoding utf8
    Write-Host "Backup point created: $target"
    Invoke-Compose -Arguments @("pull", "--ignore-buildable") | Out-Null
    Invoke-Compose -Arguments @("up", "--build", "-d") | Out-Null
    Wait-ComposeHealth
    Wait-WorkAMA
  }
  "release-check" {
    if (-not (Test-Path -LiteralPath $releaseValidator -PathType Leaf)) { throw "Release validator was not found: $releaseValidator" }
    if (-not $ReleaseManifest) { throw "-ReleaseManifest is required for release-check." }
    if (-not $EvidenceDirectory) { throw "-EvidenceDirectory is required for release-check." }
    & $releaseValidator -Manifest $ReleaseManifest -EvidenceDirectory $EvidenceDirectory -VerifyDockerImage:$VerifyDockerImage
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw "Release evidence check failed with exit code $LASTEXITCODE." }
  }
  "down" {
    $arguments = @("down")
    if ($Volumes) { $arguments += "--volumes" }
    Invoke-Compose -Arguments $arguments | Out-Null
  }
}
