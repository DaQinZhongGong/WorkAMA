$ErrorActionPreference = 'Stop'

$values = @{}
Get-Content .env | ForEach-Object { if ($_ -match '^([^#=]+)=(.*)$') { $values[$matches[1]] = $matches[2] } }
$apiBase = 'http://localhost:20200'
$fleetBase = 'http://localhost:8002'
$internal = @{ 'X-Internal-Token' = $values.INTERNAL_TOKEN }
$login = Invoke-RestMethod -Method Post -Uri "$apiBase/api/v1/auth/login" -ContentType 'application/json' -Body (@{ email=$values.TEST_ACCOUNT_EMAIL; password=$values.TEST_ACCOUNT_PASSWORD } | ConvertTo-Json)
$workspaceId = [string]$login.user.workspace_id
if ($workspaceId -notmatch '^[A-Za-z0-9_-]{3,80}$') { throw 'Test workspace id is invalid.' }

$query = "select id from ag_sandbox where workspace_id='$workspaceId' and status in ('active','sleeping') order by started_at"
$previousErrorAction = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
  $raw = @(& docker exec workama-postgres-1 psql -U workama -d workama -Atc $query 2>&1)
  $exitCode = $LASTEXITCODE
} finally {
  $ErrorActionPreference = $previousErrorAction
}
if ($exitCode -ne 0) { throw "Could not enumerate test sandboxes: $($raw -join "`n")" }
$sandboxIds = @($raw | ForEach-Object { [string]$_ } | Where-Object { $_ -match '^sbx_[A-Za-z0-9_-]+$' })
$released = 0
foreach ($sandboxId in $sandboxIds) {
  Invoke-RestMethod -Method Delete -Uri "$fleetBase/internal/sandboxes/$sandboxId" -Headers $internal | Out-Null
  $released++
}

$evidence = [ordered]@{
  schema_version = 'workama.sandbox.test-cleanup.v1'
  verification_scope = 'local-compose'
  test_workspace_id = $workspaceId
  enumerated = $sandboxIds.Count
  released = $released
  only_test_workspace = $true
  formal_release_api_used = $true
  timestamp = [DateTimeOffset]::UtcNow.ToString('o')
}
$evidence | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath 'quality/evidence/sandbox-test-cleanup.json' -Encoding utf8
$evidence | ConvertTo-Json -Depth 6
