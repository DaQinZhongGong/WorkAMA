$ErrorActionPreference = 'Stop'

$values = @{}
Get-Content -LiteralPath '.env' -Encoding utf8 | ForEach-Object {
    if ($_ -match '^([^#=]+)=(.*)$') { $values[$matches[1].Trim()] = $matches[2].Trim() }
}
if (-not $values.TEST_ACCOUNT_EMAIL -or -not $values.TEST_ACCOUNT_PASSWORD) { throw 'TEST_ACCOUNT_EMAIL and TEST_ACCOUNT_PASSWORD are required.' }

$baseUrl = if ($env:WORKAMA_BASE_URL) { $env:WORKAMA_BASE_URL.TrimEnd('/') } else { 'http://localhost:20200' }
$login = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/auth/login" -ContentType 'application/json' -Body (@{
    email = $values.TEST_ACCOUNT_EMAIL
    password = $values.TEST_ACCOUNT_PASSWORD
} | ConvertTo-Json)
$headers = @{ Authorization = "Bearer $($login.access_token)" }
$summary = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/admin/observability/summary" -Headers $headers
$contract = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/admin/observability/semantic-contract" -Headers $headers

if ($summary.schema_version -ne 'workama.observability.summary.v1') { throw 'Observability summary schema is incorrect.' }
if (@($summary.snapshots).Count -ne 6) { throw 'Observability summary must expose all six SLOs.' }
if ($contract.schema_version -ne 'workama.ai-mcp.v1') { throw 'Semantic contract version is incorrect.' }
if ($contract.gen_ai.content_fields -ne 'forbidden' -or $contract.mcp.raw_endpoint -ne 'forbidden') { throw 'Semantic content redaction contract is incomplete.' }

$evidence = [ordered]@{
    schema_version = 'workama.observability.readiness.v1'
    timestamp = [DateTimeOffset]::UtcNow.ToString('o')
    verification_scope = $summary.verification_scope
    telemetry_available = [bool]$summary.telemetry_available
    slo_count = @($summary.snapshots).Count
    slo_statuses = @($summary.snapshots | ForEach-Object { @{ key = $_.key; status = $_.status; burn_rate = $_.burn_rate; budget_remaining_percent = $_.budget_remaining_percent } })
    semantic_contract_version = $contract.schema_version
    content_redaction_contract = ($contract.gen_ai.content_fields -eq 'forbidden' -and $contract.mcp.raw_endpoint -eq 'forbidden')
    external_boundary = $summary.external_boundary
}
$evidence | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath 'quality/evidence/observability-readiness.json' -Encoding utf8
$evidence | ConvertTo-Json -Depth 12
