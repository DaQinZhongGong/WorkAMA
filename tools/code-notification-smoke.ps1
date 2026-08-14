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

$repository = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/code/repositories" -Headers $headers -ContentType 'application/json' -Body (@{
    name = "Code Smoke $suffix"
    provider = 'local'
    default_branch = 'main'
    credential = 'should-never-be-returned'
} | ConvertTo-Json)
if ($repository.PSObject.Properties.Name -contains 'credential' -or $repository.PSObject.Properties.Name -contains 'credential_enc') { throw 'Code repository response leaked credential fields.' }

$task = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/code/tasks" -Headers $headers -ContentType 'application/json' -Body (@{
    repository_id = $repository.id
    title = "Code smoke task $suffix"
    prompt = 'Add a smoke-test change and validate it.'
    branch = "workama/smoke-$suffix"
} | ConvertTo-Json)
foreach ($next in @('running', 'paused', 'running', 'succeeded')) {
    $taskState = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/code/tasks/$($task.id)/status" -Headers $headers -ContentType 'application/json' -Body (@{ status = $next } | ConvertTo-Json)
}
Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/code/tasks/$($task.id)/events" -Headers $headers -ContentType 'application/json' -Body (@{
    type = 'diff'
    payload = @{ files = @('README.md'); authorization = 'Bearer event-secret' }
} | ConvertTo-Json -Depth 8) | Out-Null
Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/code/tasks/$($task.id)/events" -Headers $headers -ContentType 'application/json' -Body (@{
    type = 'terminal'
    payload = @{ command = 'git diff --check'; exit_code = 0 }
} | ConvertTo-Json -Depth 8) | Out-Null
Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/code/tasks/$($task.id)/events" -Headers $headers -ContentType 'application/json' -Body (@{
    type = 'test'
    payload = @{ passed = 1; failed = 0; token = 'secret-test-token' }
} | ConvertTo-Json -Depth 8) | Out-Null
$events = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/code/tasks/$($task.id)/events?limit=20" -Headers $headers
if (@($events.items).Count -ne 3 -or $events.items[0].type -ne 'code.diff' -or $events.items[1].type -ne 'terminal.output' -or $events.items[2].type -ne 'test.report') { throw 'Code event contract is incomplete.' }
if ($events.items[0].payload.authorization -ne '<redacted>' -or $events.items[2].payload.token -ne '<redacted>') { throw 'Code event payload was not redacted.' }

$preference = Invoke-RestMethod -Method Put -Uri "$baseUrl/api/v1/notification-preferences" -Headers $headers -ContentType 'application/json' -Body (@{
    event_type = 'agent.completed'
    channel = 'email'
    enabled = $false
} | ConvertTo-Json)
if ($preference.event_type -ne 'agent.completed' -or $preference.enabled -ne $false) { throw 'Notification preference was not persisted.' }
$preferences = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/notification-preferences" -Headers $headers
$forcedStatus = 0
try {
    Invoke-RestMethod -Method Put -Uri "$baseUrl/api/v1/notification-preferences" -Headers $headers -ContentType 'application/json' -Body (@{
        event_type = 'billing.low_balance'
        channel = 'in_app'
        enabled = $false
    } | ConvertTo-Json) | Out-Null
} catch { $forcedStatus = [int]$_.Exception.Response.StatusCode }
if ($forcedStatus -ne 409) { throw 'Forced in-app notification preference was allowed to disable.' }

$evidence = [ordered]@{
    timestamp = [DateTimeOffset]::UtcNow.ToString('o')
    repository_created = [bool]$repository.id
    task_status = $taskState.task.status
    event_count = @($events.items).Count
    event_types = @($events.items.type)
    sensitive_fields_redacted = ($events.items[0].payload.authorization -eq '<redacted>' -and $events.items[2].payload.token -eq '<redacted>')
    notification_preference_saved = ($preference.enabled -eq $false)
    forced_in_app_rejected = ($forcedStatus -eq 409)
    preference_count = @($preferences.items).Count
}
$evidence | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath 'quality/evidence/code-notification-smoke.json' -Encoding utf8
$evidence | ConvertTo-Json -Depth 12
