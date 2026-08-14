$ErrorActionPreference = 'Stop'

$compose = @('--env-file', '.env', '-f', 'deploy/compose/docker-compose.yml')
$apiBase = 'http://localhost:20200'
$project = 'workama'

function Invoke-Compose([string[]]$Arguments) {
  $previousErrorAction = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    $output = @(& docker compose @compose @Arguments 2>&1)
    $exitCode = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $previousErrorAction
  }
  if ($exitCode -ne 0) { throw "Compose command failed: docker compose $($Arguments -join ' ')`n$($output -join "`n")" }
  return $output
}

function Get-WorkerHealth([string]$ContainerId) {
  if ([string]::IsNullOrWhiteSpace($ContainerId)) { return 'missing' }
  $health = (& docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' $ContainerId 2>$null)
  if ($LASTEXITCODE -ne 0) { return 'missing' }
  return [string]$health
}

$workerId = ([string](Invoke-Compose @('ps', '-q', 'platform-worker'))).Trim()
if ([string]::IsNullOrWhiteSpace($workerId)) { throw 'platform-worker container was not found' }

$baseline = Invoke-RestMethod -Uri "$apiBase/readyz"
$stopped = $false
$readyDuringFault = $false
$recovered = $false
try {
  Invoke-Compose @('stop', 'platform-worker') | Out-Null
  $stopped = $true
  try {
    $during = Invoke-RestMethod -Uri "$apiBase/readyz"
    $readyDuringFault = $during.status -eq 'ready'
  } catch {
    $readyDuringFault = $false
  }
} finally {
  Invoke-Compose @('start', 'platform-worker') | Out-Null
}

$deadline = (Get-Date).AddSeconds(45)
while ((Get-Date) -lt $deadline) {
  $health = Get-WorkerHealth $workerId
  if ($health -eq 'healthy') { $recovered = $true; break }
  Start-Sleep -Milliseconds 500
}
$after = Invoke-RestMethod -Uri "$apiBase/readyz"
$projectContainers = @(& docker ps --filter "label=com.docker.compose.project=$project" --format '{{.Names}}')
$allPrefixed = $projectContainers.Count -gt 0 -and @($projectContainers | Where-Object { $_ -notlike 'workama-*' }).Count -eq 0

$evidence = [ordered]@{
  schema_version = 'workama.availability.chaos.v1'
  verification_scope = 'local-compose'
  status = if ($stopped -and $readyDuringFault -and $recovered -and $after.status -eq 'ready') { 'verified_local' } else { 'failed' }
  experiment = 'platform-worker-stop-restart'
  baseline_ready = $baseline.status -eq 'ready'
  readyz_during_worker_fault = $readyDuringFault
  worker_stop_applied = $stopped
  worker_recovered = $recovered
  worker_health_after = Get-WorkerHealth $workerId
  readyz_after_recovery = $after.status -eq 'ready'
  all_workama_containers_prefixed = $allPrefixed
  container_count = $projectContainers.Count
  multi_az_topology_verified = $false
  multi_az_pending_external = $true
  node_failure_pending_external = $true
  independent_chaos_harness_pending_external = $true
  notes = @(
    'This local experiment validates a controlled worker interruption and recovery only.',
    'It is not evidence of multi-AZ failover, node failure recovery, or production SLA.'
  )
  timestamp = [DateTimeOffset]::UtcNow.ToString('o')
}
$evidence | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath 'quality/evidence/availability-chaos-smoke.json' -Encoding utf8
if ($evidence.status -ne 'verified_local') { throw 'Availability chaos smoke did not recover cleanly.' }
$evidence | ConvertTo-Json -Depth 8
