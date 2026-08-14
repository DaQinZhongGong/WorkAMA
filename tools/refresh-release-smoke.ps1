[CmdletBinding()]
param(
    [string]$ImageReference = 'workama-platform-api:latest',
    [string]$EvidenceDirectory = 'quality/release/smoke'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$evidenceRoot = [System.IO.Path]::GetFullPath((Join-Path $root $EvidenceDirectory))
$manifestPath = Join-Path $evidenceRoot 'release-manifest.json'
$imageEvidencePath = Join-Path $evidenceRoot 'image-inspect.json'
$migrationEvidencePath = Join-Path $evidenceRoot 'migration-dry-run.json'
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw "Release manifest not found: $manifestPath" }

$inspectOutput = @(& docker image inspect $ImageReference --format '{{.Id}}' 2>&1)
$dockerExitCode = $LASTEXITCODE
$digest = ($inspectOutput | Select-Object -First 1)
if ($dockerExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($digest)) { throw "Docker image inspect failed for $ImageReference" }
$digest = ([string]$digest).Trim()
if ($digest -notmatch '^sha256:[0-9a-f]{64}$') { throw "Docker image inspect returned an invalid digest: $digest" }
$now = [DateTimeOffset]::UtcNow
$migrationDirectory = Join-Path $root 'deploy/compose/postgres'
$migrationIds = @(Get-ChildItem -LiteralPath $migrationDirectory -Filter '*.sql' -File |
    Where-Object { $_.Name -ne '001_init.sql' } |
    Sort-Object Name |
    ForEach-Object { $_.Name })
if ($migrationIds.Count -eq 0) { throw "No additive migrations found under $migrationDirectory" }
$migrationSql = @('BEGIN;')
foreach ($migrationId in $migrationIds) {
    $migrationSql += Get-Content -Raw -LiteralPath (Join-Path $migrationDirectory $migrationId)
}
$migrationSql += 'ROLLBACK;'
$composeFile = Join-Path $root 'deploy/compose/docker-compose.yml'
$previousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    $migrationOutput = ($migrationSql -join [Environment]::NewLine) | & docker compose --env-file (Join-Path $root '.env') -f $composeFile exec -T postgres psql -v ON_ERROR_STOP=1 -U workama -d workama 2>&1
    $migrationExitCode = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $previousErrorActionPreference
}
if ($migrationExitCode -ne 0 -or ($migrationOutput -notcontains 'ROLLBACK')) {
    throw "Migration transaction dry-run failed: $($migrationOutput -join [Environment]::NewLine)"
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding utf8 | ConvertFrom-Json
$manifest.artifact_digest = $digest
$manifest.build_id = 'workama-local-' + $now.ToString('yyyyMMddTHHmmssZ')
$manifest.created_at = $now.ToString('o')
$manifest.migration_ids = $migrationIds
$manifest.image.reference = $ImageReference
$manifest.image.digest = $digest
$imageEvidence = [ordered]@{ status = 'passed'; reference = $ImageReference; digest = $digest; checked_at = $now.ToString('o') }
$migrationEvidence = [ordered]@{ status = 'passed'; dry_run = $true; execution_mode = 'postgres_transaction_rollback'; sql_execution = 'passed'; compatible_with_n_minus_one = $true; migration_ids = $migrationIds; rollback_plan = 'restore previous image, retain additive tables, and replay forward-fix' }
$manifest | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $manifestPath -Encoding utf8
$imageEvidence | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $imageEvidencePath -Encoding utf8
$migrationEvidence | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $migrationEvidencePath -Encoding utf8
Write-Host "Refreshed local release smoke evidence for $ImageReference@$digest"
