$ErrorActionPreference = 'Stop'

$ready = $false
try { $ready = (Invoke-RestMethod -Uri 'http://localhost:20200/readyz').status -eq 'ready' } catch { $ready = $false }
$latestPerformance = Get-ChildItem -LiteralPath 'quality/performance/results' -Filter 'baseline-result.json' -Recurse -File | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
$performance = if ($latestPerformance) { Get-Content -LiteralPath $latestPerformance.FullName -Raw | ConvertFrom-Json } else { $null }
$releaseEvidence = Test-Path -LiteralPath 'quality/release/smoke/migration-dry-run.json' -PathType Leaf
$chaosEvidence = Test-Path -LiteralPath 'quality/evidence/availability-chaos-smoke.json' -PathType Leaf
$windowDays = 0

$evidence = [ordered]@{
  schema_version = 'workama.sla.readiness.v1'
  verification_scope = 'local-compose'
  status = 'pending_external'
  availability_target_percent = 99.95
  required_continuous_window_days = 60
  measured_window_days = $windowDays
  local_readyz_signal = $ready
  latest_performance_result = if ($performance) { $performance.status } else { 'missing' }
  latest_performance_evidence = if ($latestPerformance) { $latestPerformance.FullName.Replace((Get-Location).Path + [IO.Path]::DirectorySeparatorChar, '') } else { $null }
  release_migration_evidence_present = $releaseEvidence
  chaos_evidence_present = $chaosEvidence
  production_sla_verified = $false
  multi_az_sla_verified = $false
  independent_pentest_verified = $false
  pending_external = @(
    'staging/production multi-AZ topology and failover evidence',
    'independent penetration test and remediation sign-off',
    'continuous 60-day 99.95% enterprise SLA report'
  )
  timestamp = [DateTimeOffset]::UtcNow.ToString('o')
}
$evidence | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath 'quality/evidence/sla-readiness.json' -Encoding utf8
$evidence | ConvertTo-Json -Depth 8
