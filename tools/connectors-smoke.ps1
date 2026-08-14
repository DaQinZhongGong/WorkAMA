$ErrorActionPreference = 'Stop'

$baseUrl = if ($env:WORKAMA_API_BASE_URL) { $env:WORKAMA_API_BASE_URL.TrimEnd('/') } else { 'http://localhost:20200' }
$token = if ($env:WORKAMA_TEST_TOKEN) { $env:WORKAMA_TEST_TOKEN.Trim() } else { '' }
if ([string]::IsNullOrWhiteSpace($token) -and (Test-Path -LiteralPath '.env')) {
    $values = @{}
    Get-Content -LiteralPath '.env' -Encoding utf8 | ForEach-Object {
        if ($_ -match '^([^#=]+)=(.*)$') { $values[$matches[1].Trim()] = $matches[2].Trim() }
    }
    if ($values.TEST_ACCOUNT_EMAIL -and $values.TEST_ACCOUNT_PASSWORD) {
        $login = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/auth/login" -ContentType 'application/json' -Body (@{
            email = $values.TEST_ACCOUNT_EMAIL
            password = $values.TEST_ACCOUNT_PASSWORD
        } | ConvertTo-Json)
        $token = $login.access_token
    }
}
if ([string]::IsNullOrWhiteSpace($token)) {
    Write-Output '{"skipped":true,"reason":"WORKAMA_TEST_TOKEN or test account credentials are not configured"}'
    exit 0
}

$headers = @{ Authorization = "Bearer $token" }
$suffix = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$connector = $null

function Get-ErrorStatus($errorRecord) {
    if ($errorRecord.Exception.Response -and $errorRecord.Exception.Response.StatusCode) {
        return [int]$errorRecord.Exception.Response.StatusCode
    }
    return 0
}

try {
    $name = "Connector Smoke $suffix"
    $body = @{
        name = $name
        provider = 'mock'
        auth_mode = 'none'
        manifest = @{
            documents = @(
                @{
                    source_id = "smoke:$suffix:public"
                    source_version = '1'
                    title = 'Smoke document'
                    content = 'Deterministic connector smoke content.'
                    acl = @{ roles = @('owner', 'admin', 'member', 'viewer') }
                }
            )
        }
        enabled = $true
    } | ConvertTo-Json -Depth 15
    $connector = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/connectors" -Headers $headers -ContentType 'application/json' -Body $body
    if (-not $connector.connector.id -or $connector.connector.provider -ne 'mock' -or $connector.connector.credential_configured) { throw 'Connector create contract is incomplete.' }

    $syncHeaders = @{ Authorization = $headers.Authorization; 'Idempotency-Key' = "connector-smoke-$suffix" }
    $syncBody = @{ mode = 'full' } | ConvertTo-Json
    $full = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/connectors/$($connector.connector.id)/sync" -Headers $syncHeaders -ContentType 'application/json' -Body $syncBody
    $replay = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/connectors/$($connector.connector.id)/sync" -Headers $syncHeaders -ContentType 'application/json' -Body $syncBody
    if ($full.run.status -ne 'succeeded' -or -not $full.run.executed -or $replay.run.id -ne $full.run.id) { throw 'Connector full sync/idempotency contract is incomplete.' }

    $incremental = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/connectors/$($connector.connector.id)/sync" -Headers @{ Authorization = $headers.Authorization; 'Idempotency-Key' = "connector-incremental-$suffix" } -ContentType 'application/json' -Body (@{ mode = 'incremental' } | ConvertTo-Json)
    if ($incremental.run.status -ne 'succeeded' -or $incremental.run.documents_seen -ne 0) { throw 'Connector incremental cursor contract is incomplete.' }

    $documents = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/connectors/$($connector.connector.id)/documents" -Headers $headers
    $runs = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/connectors/$($connector.connector.id)/sync-runs" -Headers $headers
    if (@($documents.items).Count -ne 1 -or @($runs.items).Count -lt 2) { throw 'Connector document or run projection is incomplete.' }

    $pending = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/connectors" -Headers $headers -ContentType 'application/json' -Body (@{
        name = "Connector OAuth Pending $suffix"
        provider = 'mock'
        auth_mode = 'oauth'
        credentials = @{ client_id = 'smoke-client'; client_secret = 'smoke-secret' }
        enabled = $true
    } | ConvertTo-Json -Depth 15)
    $pendingSync = $null
    try {
        $pendingSync = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/connectors/$($pending.connector.id)/sync" -Headers @{ Authorization = $headers.Authorization; 'Idempotency-Key' = "connector-pending-$suffix" } -ContentType 'application/json' -Body $syncBody
    } catch {
        if ((Get-ErrorStatus $_) -ne 409) { throw }
    }
    if ($pending.connector.status -ne 'pending') { throw 'OAuth connector did not remain pending.' }

    $evidence = [ordered]@{
        timestamp = [DateTimeOffset]::UtcNow.ToString('o')
        created = [bool]$connector.connector.id
        full_sync_succeeded = ($full.run.status -eq 'succeeded')
        full_sync_executed = [bool]$full.run.executed
        idempotency_replayed = ($replay.run.id -eq $full.run.id)
        incremental_seen = $incremental.run.documents_seen
        document_count = @($documents.items).Count
        run_count = @($runs.items).Count
        oauth_status = $pending.connector.status
        oauth_sync_without_external_execution = ($null -eq $pendingSync -or $pendingSync.run.execution_status -eq 'unsupported')
    }
    $evidence | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath 'quality/evidence/connectors-smoke.json' -Encoding utf8
    $evidence | ConvertTo-Json -Depth 12
}
finally {
    if ($connector -and $connector.connector.id) { try { Invoke-RestMethod -Method Delete -Uri "$baseUrl/api/v1/connectors/$($connector.connector.id)" -Headers $headers | Out-Null } catch {} }
    if ($pending -and $pending.connector.id) { try { Invoke-RestMethod -Method Delete -Uri "$baseUrl/api/v1/connectors/$($pending.connector.id)" -Headers $headers | Out-Null } catch {} }
}
