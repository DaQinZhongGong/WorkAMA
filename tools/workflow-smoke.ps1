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

function Wait-WorkflowRun([string]$runId) {
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        $current = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/workflow-runs/$runId" -Headers $headers
        if (@('succeeded', 'failed', 'cancelled', 'pending_approval') -contains [string]$current.status) { return $current }
        Start-Sleep -Milliseconds 300
    }
    throw "Workflow run $runId did not reach a terminal state."
}

$assistant = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/assistants" -Headers $headers -ContentType 'application/json' -Body (@{
    name = "Smoke Assistant $suffix"
    description = 'Assistant publishing smoke test'
} | ConvertTo-Json)
$version = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/assistants/$($assistant.id)/versions" -Headers $headers -ContentType 'application/json' -Body (@{
    system_prompt = 'Answer briefly.'
    model = 'workama-chat'
    model_config = @{}
    toolset = @()
    dataset_ids = @()
    greeting = 'Hello from the smoke assistant.'
} | ConvertTo-Json -Depth 8)
$published = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/assistants/$($assistant.id)/versions/$($version.id)/publish" -Headers $headers
$public = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/public/assistants/$([uri]::EscapeDataString($published.share_token))"
if ($public.id -ne $assistant.id -or $public.version -ne $version.version) { throw 'Published assistant metadata is inconsistent.' }
$gatewayToken = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/gateway/tokens" -Headers $headers -ContentType 'application/json' -Body (@{
    name = "Workflow Smoke Key $suffix"
    rpm_limit = 60
    tpm_limit = 100000
    model_whitelist = @('workama-chat')
} | ConvertTo-Json -Depth 8)
$invocation = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/assistants/$($assistant.id)/invoke" -Headers $headers -ContentType 'application/json' -Body (@{
    message = 'Return a short smoke-test response.'
    gateway_api_key = $gatewayToken.key
} | ConvertTo-Json -Depth 8)
if (-not $invocation.response.choices[0].message.content) { throw 'Assistant invocation returned no message content.' }

$graph = @{
    nodes = @(
        @{ id = 'input'; type = 'input' }
        @{ id = 'prompt'; type = 'prompt'; config = @{ template = 'Hello {input.name}' } }
        @{ id = 'output'; type = 'output'; config = @{ from = 'prompt' } }
    )
    edges = @(
        @{ source = 'input'; target = 'prompt' }
        @{ source = 'prompt'; target = 'output' }
    )
}
$workflow = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/workflows" -Headers $headers -ContentType 'application/json' -Body (@{
    name = "Smoke Workflow $suffix"
    description = 'Assistant and workflow smoke test'
    graph = $graph
} | ConvertTo-Json -Depth 12)
$validation = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/workflows/$($workflow.id)/validate" -Headers $headers
if (-not $validation.valid) { throw "Workflow validation failed: $($validation.errors -join '; ')" }
$runHeaders = @{ Authorization = "Bearer $($login.access_token)"; 'Idempotency-Key' = "workflow-smoke-$suffix" }
$run = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/workflows/$($workflow.id)/runs" -Headers $runHeaders -ContentType 'application/json' -Body (@{
    input = @{ name = 'Ada' }
    dry_run = $true
} | ConvertTo-Json -Depth 8)
$acceptedStatus = [string]$run.status
$sameRun = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/workflows/$($workflow.id)/runs" -Headers $runHeaders -ContentType 'application/json' -Body (@{
    input = @{ name = 'Ada' }
    dry_run = $true
} | ConvertTo-Json -Depth 8)
if ($sameRun.id -ne $run.id) { throw 'Workflow idempotency replay created a second run.' }
$run = Wait-WorkflowRun $run.id
if ($run.status -ne 'succeeded' -or $run.output.output -ne 'Hello Ada') { throw 'Workflow dry-run output is incorrect.' }
$events = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/workflow-runs/$($run.id)/events?limit=200" -Headers $headers
if (-not $events.items -or $events.items[0].event_type -ne 'workflow.run.started' -or $events.items[-1].event_type -ne 'workflow.run.completed') { throw 'Workflow event replay is incomplete.' }
$streamUrl = "$baseUrl/api/v1/workflow-runs/$($run.id)/events/stream?timeout_seconds=2"
$streamContent = (& curl.exe --fail-with-body --silent --show-error --max-time 10 -H "Authorization: Bearer $($login.access_token)" $streamUrl | Out-String)
if ($LASTEXITCODE -ne 0) { throw "Workflow SSE request failed with exit code $LASTEXITCODE." }
if ($streamContent -notmatch 'workflow.run.completed' -or $streamContent -notmatch 'workflow.node.succeeded') { throw 'Workflow SSE event stream is incomplete.' }
$cancelHeaders = @{ Authorization = "Bearer $($login.access_token)"; 'Idempotency-Key' = "workflow-cancel-$suffix" }
$cancelRun = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/workflows/$($workflow.id)/runs" -Headers $cancelHeaders -ContentType 'application/json' -Body (@{
    input = @{ name = 'Cancel me' }
    dry_run = $true
} | ConvertTo-Json -Depth 8)
if ($cancelRun.status -ne 'queued') { throw 'Workflow cancellation fixture was not queued.' }
Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/operations/$($cancelRun.operation_id)/cancellations" -Headers $headers -ContentType 'application/json' -Body (@{ reason = 'Workflow smoke cancellation.' } | ConvertTo-Json -Depth 8) | Out-Null
$cancelledRun = Wait-WorkflowRun $cancelRun.id
$cancelledEvents = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/workflow-runs/$($cancelRun.id)/events?limit=200" -Headers $headers
if ($cancelledRun.status -ne 'cancelled' -or -not $cancelledEvents.items -or $cancelledEvents.items[-1].event_type -ne 'workflow.run.cancelled') { throw 'Workflow cancellation did not produce a terminal cancelled event.' }

$evidence = @{
    timestamp = [DateTimeOffset]::UtcNow.ToString('o')
    assistant_published = ($public.id -eq $assistant.id)
    assistant_invoked = [bool]$invocation.response.choices[0].message.content
    assistant_version = $public.version
    workflow_valid = [bool]$validation.valid
    workflow_run_status = $run.status
    workflow_accepted_status = $acceptedStatus
    workflow_idempotency_replayed = ($sameRun.id -eq $run.id)
    workflow_output = $run.output.output
    trace_count = @($run.trace).Count
    workflow_event_count = @($events.items).Count
    workflow_events_replayed = $true
    workflow_cancelled = ($cancelledRun.status -eq 'cancelled')
    workflow_cancel_event_replayed = ($cancelledEvents.items[-1].event_type -eq 'workflow.run.cancelled')
    workflow_sse_completed = ($streamContent -match 'workflow.run.completed')
    workflow_sse_node_event = ($streamContent -match 'workflow.node.succeeded')
}
$evidence | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath 'quality/evidence/workflow-smoke.json' -Encoding utf8
$evidence | ConvertTo-Json -Depth 12
