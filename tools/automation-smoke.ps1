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
$suffix = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$cron = $null
$webhook = $null
$webhookSecret = $null

function Get-ErrorStatus($errorRecord) {
    if ($errorRecord.Exception.Response -and $errorRecord.Exception.Response.StatusCode) {
        return [int]$errorRecord.Exception.Response.StatusCode
    }
    return 0
}

try {
    $cron = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/automations" -Headers $headers -ContentType 'application/json' -Body (@{
        name = "Automation Cron Smoke $suffix"
        trigger_type = 'cron'
        cron_expression = '*/5 * * * *'
        timezone = 'Asia/Shanghai'
        target_type = 'agent'
        target_id = 'agent_smoke'
        payload = @{ api_key = 'must-not-leak'; request_id = "cron-$suffix" }
        enabled = $true
    } | ConvertTo-Json -Depth 10)
    if (-not $cron.id -or $cron.status -ne 'active' -or -not $cron.next_run_at -or $cron.payload.api_key -ne '<redacted>') { throw 'Cron schedule contract is incomplete.' }

    $cronDetail = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/automations/$($cron.id)" -Headers $headers
    if ($cronDetail.webhook_secret -or $cronDetail.payload.api_key -ne '<redacted>') { throw 'Schedule detail leaked a secret or sensitive payload.' }

    $manualHeaders = @{ Authorization = $headers.Authorization; 'Idempotency-Key' = "automation-manual-$suffix" }
    $manualBody = @{ payload = @{ request_id = "manual-$suffix" } } | ConvertTo-Json -Depth 10
    $manual = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/automations/$($cron.id)/trigger" -Headers $manualHeaders -ContentType 'application/json' -Body $manualBody
    $manualReplay = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/automations/$($cron.id)/trigger" -Headers $manualHeaders -ContentType 'application/json' -Body $manualBody
    if (-not $manual.run.id -or $manual.run.status -ne 'queued' -or $manualReplay.run.id -ne $manual.run.id) { throw 'Manual automation idempotency contract is incomplete.' }

    $manualConflictStatus = 0
    try {
        Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/automations/$($cron.id)/trigger" -Headers $manualHeaders -ContentType 'application/json' -Body (@{ payload = @{ request_id = "different-$suffix" } } | ConvertTo-Json -Depth 10) | Out-Null
    } catch { $manualConflictStatus = Get-ErrorStatus $_ }
    if ($manualConflictStatus -ne 409) { throw "Manual idempotency conflict returned $manualConflictStatus instead of 409." }

    $runs = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/automations/$($cron.id)/runs" -Headers $headers
    if (@($runs.items).Count -lt 1) { throw 'Automation run history is empty.' }

    $webhook = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/automations" -Headers $headers -ContentType 'application/json' -Body (@{
        name = "Automation Webhook Smoke $suffix"
        trigger_type = 'webhook'
        timezone = 'UTC'
        target_type = 'workflow'
        target_id = 'workflow_smoke'
        payload = @{ authorization = 'Bearer must-not-leak'; safe = 'default' }
        enabled = $true
    } | ConvertTo-Json -Depth 10)
    $webhookSecret = $webhook.webhook_secret
    if (-not $webhook.id -or [string]::IsNullOrWhiteSpace($webhookSecret) -or $webhook.payload.authorization -ne '<redacted>') { throw 'Webhook schedule contract is incomplete.' }

    $listed = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/automations" -Headers $headers
    $listedWebhook = @($listed.items | Where-Object { $_.id -eq $webhook.id })[0]
    if (-not $listedWebhook -or $listedWebhook.webhook_secret -or $listedWebhook.payload.authorization -ne '<redacted>') { throw 'Webhook secret or payload leaked from list endpoint.' }

    $missingSecretStatus = 0
    try { Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/automation-webhooks/$($webhook.id)" -ContentType 'application/json' -Body '{"event":"missing"}' | Out-Null } catch { $missingSecretStatus = Get-ErrorStatus $_ }
    if ($missingSecretStatus -ne 401) { throw "Missing webhook secret returned $missingSecretStatus instead of 401." }

    $webhookHeaders = @{ 'X-Webhook-Secret' = $webhookSecret; 'Idempotency-Key' = "automation-webhook-$suffix" }
    $webhookBody = @{ event = 'created'; payload = @{ safe = "webhook-$suffix" } } | ConvertTo-Json -Depth 10
    $hookRun = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/automation-webhooks/$($webhook.id)" -Headers $webhookHeaders -ContentType 'application/json' -Body $webhookBody
    $hookReplay = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/automation-webhooks/$($webhook.id)" -Headers $webhookHeaders -ContentType 'application/json' -Body $webhookBody
    if (-not $hookRun.run.id -or $hookRun.run.status -ne 'queued' -or $hookReplay.run.id -ne $hookRun.run.id) { throw 'Webhook idempotency contract is incomplete.' }

    $hookConflictStatus = 0
    try {
        Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/automation-webhooks/$($webhook.id)" -Headers $webhookHeaders -ContentType 'application/json' -Body (@{ event = 'different' } | ConvertTo-Json) | Out-Null
    } catch { $hookConflictStatus = Get-ErrorStatus $_ }
    if ($hookConflictStatus -ne 409) { throw "Webhook idempotency conflict returned $hookConflictStatus instead of 409." }

    $evidence = [ordered]@{
        timestamp = [DateTimeOffset]::UtcNow.ToString('o')
        cron_next_run = [bool]$cron.next_run_at
        cron_payload_redacted = ($cron.payload.api_key -eq '<redacted>')
        manual_run_queued = ($manual.run.status -eq 'queued')
        manual_idempotency_replayed = ($manualReplay.run.id -eq $manual.run.id)
        manual_conflict_status = $manualConflictStatus
        webhook_secret_issued_once = (-not [string]::IsNullOrWhiteSpace($webhookSecret))
        webhook_list_secret_hidden = (-not $listedWebhook.webhook_secret)
        webhook_missing_secret_status = $missingSecretStatus
        webhook_run_queued = ($hookRun.run.status -eq 'queued')
        webhook_idempotency_replayed = ($hookReplay.run.id -eq $hookRun.run.id)
        webhook_conflict_status = $hookConflictStatus
        run_count = @($runs.items).Count
    }
    $evidence | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath 'quality/evidence/automation-smoke.json' -Encoding utf8
    $evidence | ConvertTo-Json -Depth 12
}
finally {
    if ($cron -and $cron.id) { try { Invoke-RestMethod -Method Delete -Uri "$baseUrl/api/v1/automations/$($cron.id)" -Headers $headers | Out-Null } catch {} }
    if ($webhook -and $webhook.id) { try { Invoke-RestMethod -Method Delete -Uri "$baseUrl/api/v1/automations/$($webhook.id)" -Headers $headers | Out-Null } catch {} }
}
