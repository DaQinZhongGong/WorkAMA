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

$plan = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/work/plans" -Headers $headers -ContentType 'application/json' -Body (@{
    title = "AMA-Work Smoke $suffix"
    objective = 'Validate the plan, source, artifact, transition, and event contracts.'
} | ConvertTo-Json)
if (-not $plan.id -or $plan.status -ne 'draft') { throw 'AMA-Work plan creation contract is incomplete.' }

$firstTask = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/work/plans/$($plan.id)/tasks" -Headers $headers -ContentType 'application/json' -Body (@{
    title = 'Collect smoke evidence'
    description = 'Record a deterministic result.'
} | ConvertTo-Json)
$secondTask = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/work/plans/$($plan.id)/tasks" -Headers $headers -ContentType 'application/json' -Body (@{
    title = 'Generate Office outputs'
    description = 'Generate docx, xlsx, and pptx outputs.'
} | ConvertTo-Json)
if ($firstTask.position -ne 0 -or $secondTask.position -ne 1) { throw 'AMA-Work task ordering was not persisted.' }

$startedTask = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/work/plans/$($plan.id)/tasks/$($firstTask.id)/status" -Headers $headers -ContentType 'application/json' -Body (@{ status = 'in_progress' } | ConvertTo-Json)
$doneTask = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/work/plans/$($plan.id)/tasks/$($firstTask.id)/status" -Headers $headers -ContentType 'application/json' -Body (@{ status = 'done' } | ConvertTo-Json)
if ($startedTask.task.status -ne 'in_progress' -or $doneTask.task.status -ne 'done') { throw 'AMA-Work task transitions were not persisted.' }

$source = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/work/plans/$($plan.id)/sources" -Headers $headers -ContentType 'application/json' -Body (@{
    url = 'mock://research/brief'
    fetch = $true
} | ConvertTo-Json)
if ($source.source_type -ne 'mock' -or -not $source.fetched.untrusted -or -not $source.fetched.content_sha256) { throw 'AMA-Work deterministic source contract is incomplete.' }

$artifactResults = @()
$artifactFormats = @()
foreach ($format in @('docx', 'xlsx', 'pptx')) {
    $artifact = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/work/plans/$($plan.id)/artifacts" -Headers $headers -ContentType 'application/json' -Body (@{
        format = $format
        filename = "workama-smoke-$format-$suffix"
        upload = $true
    } | ConvertTo-Json)
    if ($artifact.status -ne 'ready' -or $artifact.size_bytes -le 0 -or $artifact.content_sha256.Length -ne 64) { throw "AMA-Work $format artifact contract is incomplete." }
    $artifactResults += $artifact
    $artifactFormats += $format
}

$dryRun = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/work/plans/$($plan.id)/executions" -Headers $headers -ContentType 'application/json' -Body (@{ mode = 'dry_run'; source_ids = @($source.id) } | ConvertTo-Json)
if ($dryRun.status -ne 'draft' -or $dryRun.event.event_type -ne 'plan.execution.requested') { throw 'AMA-Work dry-run execution contract is incomplete.' }

foreach ($next in @('ready', 'running', 'paused', 'running', 'succeeded')) {
    $planStatus = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/work/plans/$($plan.id)/status" -Headers $headers -ContentType 'application/json' -Body (@{ status = $next } | ConvertTo-Json)
    if ($planStatus.plan.status -ne $next) { throw "AMA-Work plan transition to $next was not persisted." }
}

$details = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/work/plans/$($plan.id)" -Headers $headers
$events = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/work/plans/$($plan.id)/events?limit=200" -Headers $headers
if ($details.status -ne 'succeeded' -or @($details.tasks).Count -ne 2 -or @($details.sources).Count -ne 1 -or @($events.items).Count -lt 12) { throw 'AMA-Work aggregate or event timeline is incomplete.' }

$ssrfStatus = 0
try {
    Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/work/plans/$($plan.id)/sources" -Headers $headers -ContentType 'application/json' -Body (@{ url = 'https://localhost/private'; fetch = $false } | ConvertTo-Json) | Out-Null
} catch { $ssrfStatus = [int]$_.Exception.Response.StatusCode }
if ($ssrfStatus -ne 422) { throw "AMA-Work research SSRF guard returned $ssrfStatus instead of 422." }

$asyncPlan = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/work/plans" -Headers $headers -ContentType 'application/json' -Body (@{
    title = "AMA-Work Async Smoke $suffix"
    objective = 'Execute a deterministic plan through the shared operation and worker queues.'
} | ConvertTo-Json)
$asyncTaskOne = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/work/plans/$($asyncPlan.id)/tasks" -Headers $headers -ContentType 'application/json' -Body (@{ title = 'Run deterministic research' } | ConvertTo-Json)
$asyncTaskTwo = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/work/plans/$($asyncPlan.id)/tasks" -Headers $headers -ContentType 'application/json' -Body (@{ title = 'Complete accountable plan' } | ConvertTo-Json)
$asyncSource = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/work/plans/$($asyncPlan.id)/sources" -Headers $headers -ContentType 'application/json' -Body (@{ url = 'mock://research/async'; fetch = $true } | ConvertTo-Json)
$executionKey = "work-async-$suffix"
$executionHeaders = @{ Authorization = $headers.Authorization; 'Idempotency-Key' = $executionKey }
$asyncRequest = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/work/plans/$($asyncPlan.id)/executions" -Headers $executionHeaders -ContentType 'application/json' -Body (@{
    mode = 'requested'
    source_ids = @($asyncSource.id)
} | ConvertTo-Json)
if (-not $asyncRequest.operation_id -or $asyncRequest.execution_status -ne 'queued') { throw 'AMA-Work async execution was not accepted into the shared operation queue.' }
$asyncReplay = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/work/plans/$($asyncPlan.id)/executions" -Headers $executionHeaders -ContentType 'application/json' -Body (@{
    mode = 'requested'
    source_ids = @($asyncSource.id)
} | ConvertTo-Json)
if ($asyncReplay.operation_id -ne $asyncRequest.operation_id) { throw 'AMA-Work execution idempotency did not replay the same operation.' }

$asyncDeadline = (Get-Date).ToUniversalTime().AddSeconds(30)
$asyncOperation = $null
$asyncDetails = $null
do {
    Start-Sleep -Milliseconds 250
    $asyncOperation = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/operations/$($asyncRequest.operation_id)" -Headers $headers
    $asyncDetails = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/work/plans/$($asyncPlan.id)" -Headers $headers
} while ($asyncOperation.status -notin @('succeeded', 'failed', 'cancelled') -and (Get-Date).ToUniversalTime() -lt $asyncDeadline)
if ($asyncOperation.status -ne 'succeeded' -or $asyncDetails.status -ne 'succeeded' -or @($asyncDetails.tasks | Where-Object { $_.status -ne 'done' }).Count -ne 0) { throw "AMA-Work async execution did not complete successfully: operation=$($asyncOperation.status), plan=$($asyncDetails.status)." }

$sseResponse = Invoke-WebRequest -UseBasicParsing -Method Get -Uri "$baseUrl/api/v1/work/plans/$($asyncPlan.id)/events/stream?timeout_seconds=20" -Headers $headers
if ($sseResponse.StatusCode -ne 200 -or $sseResponse.Content -notmatch 'plan\.execution\.completed') { throw 'AMA-Work SSE stream did not expose the terminal completion event.' }
$asyncEvents = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/work/plans/$($asyncPlan.id)/events?limit=200" -Headers $headers
if (@($asyncEvents.items | Where-Object { $_.event_type -eq 'task.execution.completed' }).Count -ne 2 -or @($asyncEvents.items | Where-Object { $_.event_type -eq 'research.source.fetched' }).Count -ne 1) { throw 'AMA-Work async task/source execution events are incomplete.' }

$researchPlan = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/work/plans" -Headers $headers -ContentType 'application/json' -Body (@{
    title = "AMA-Work Deep Research Smoke $suffix"
    objective = 'Generate a numbered citation report from controlled source fixtures.'
} | ConvertTo-Json)
$researchTask = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/work/plans/$($researchPlan.id)/tasks" -Headers $headers -ContentType 'application/json' -Body (@{ title = 'Cross validate controlled evidence' } | ConvertTo-Json)
$researchSourceOne = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/work/plans/$($researchPlan.id)/sources" -Headers $headers -ContentType 'application/json' -Body (@{ url = 'mock://research/deep-one'; fetch = $true } | ConvertTo-Json)
$researchSourceTwo = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/work/plans/$($researchPlan.id)/sources" -Headers $headers -ContentType 'application/json' -Body (@{ url = 'mock://research/deep-two'; fetch = $true } | ConvertTo-Json)
$researchKey = "work-deep-research-$suffix"
$researchHeaders = @{ Authorization = $headers.Authorization; 'Idempotency-Key' = $researchKey }
$researchRequest = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/work/plans/$($researchPlan.id)/executions" -Headers $researchHeaders -ContentType 'application/json' -Body (@{
    mode = 'deep_research'
    source_ids = @()
} | ConvertTo-Json)
if ($researchRequest.execution_status -ne 'queued') { throw 'AMA-Work deep research was not accepted into the shared queue.' }
$researchDeadline = (Get-Date).ToUniversalTime().AddSeconds(30)
$researchOperation = $null
$researchDetails = $null
do {
    Start-Sleep -Milliseconds 250
    $researchOperation = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/operations/$($researchRequest.operation_id)" -Headers $headers
    $researchDetails = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/work/plans/$($researchPlan.id)" -Headers $headers
} while ($researchOperation.status -notin @('succeeded', 'failed', 'cancelled') -and (Get-Date).ToUniversalTime() -lt $researchDeadline)
if ($researchOperation.status -ne 'succeeded' -or $researchDetails.status -ne 'succeeded') { throw "AMA-Work deep research did not complete: operation=$($researchOperation.status), plan=$($researchDetails.status)." }
$researchEvents = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/work/plans/$($researchPlan.id)/events?limit=200" -Headers $headers
$researchArtifacts = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/work/plans/$($researchPlan.id)/artifacts" -Headers $headers
if (@($researchArtifacts.items | Where-Object { $_.kind -eq 'research_report' }).Count -ne 2) { throw 'AMA-Work deep research did not create Markdown and PDF report artifacts.' }
if (@($researchEvents.items | Where-Object { $_.event_type -eq 'research.round.completed' }).Count -ne 2 -or @($researchEvents.items | Where-Object { $_.event_type -eq 'research.report.artifact.created' }).Count -ne 2) { throw 'AMA-Work deep research round or artifact events are incomplete.' }
$researchMarkdown = @($researchArtifacts.items | Where-Object { $_.content_type -eq 'text/markdown' })[0]
$researchPdf = @($researchArtifacts.items | Where-Object { $_.content_type -eq 'application/pdf' })[0]
$markdownResponse = Invoke-WebRequest -UseBasicParsing -Method Get -Uri "$baseUrl/api/v1/work/plans/$($researchPlan.id)/artifacts/$($researchMarkdown.id)/content" -Headers $headers
$pdfResponse = Invoke-WebRequest -UseBasicParsing -Method Get -Uri "$baseUrl/api/v1/work/plans/$($researchPlan.id)/artifacts/$($researchPdf.id)/content" -Headers $headers
$markdownText = [string]$markdownResponse.Content
$pdfBytes = if ($pdfResponse.Content -is [byte[]]) { $pdfResponse.Content } else { [Text.Encoding]::ASCII.GetBytes([string]$pdfResponse.Content) }
$pdfHeader = [Text.Encoding]::ASCII.GetString($pdfBytes[0..3])
if ($markdownResponse.StatusCode -ne 200 -or $markdownText -notmatch '\[1\]' -or $markdownText -notmatch '## References' -or $pdfResponse.StatusCode -ne 200 -or $pdfHeader -ne '%PDF') { throw 'AMA-Work deep research report content contract is incomplete.' }

$cancelPlan = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/work/plans" -Headers $headers -ContentType 'application/json' -Body (@{
    title = "AMA-Work Cancel Smoke $suffix"
    objective = 'Verify queued Work operation cancellation is terminal and visible on the plan.'
} | ConvertTo-Json)
$cancelTask = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/work/plans/$($cancelPlan.id)/tasks" -Headers $headers -ContentType 'application/json' -Body (@{ title = 'Cancel before execution' } | ConvertTo-Json)
$cancelKey = "work-cancel-$suffix"
$cancelHeaders = @{ Authorization = $headers.Authorization; 'Idempotency-Key' = $cancelKey }
$cancelRequest = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/work/plans/$($cancelPlan.id)/executions" -Headers $cancelHeaders -ContentType 'application/json' -Body (@{ mode = 'requested'; source_ids = @() } | ConvertTo-Json)
$cancelResult = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/operations/$($cancelRequest.operation_id)/cancellations" -Headers $headers -ContentType 'application/json' -Body (@{ reason = 'Work smoke queued cancellation.' } | ConvertTo-Json)
Start-Sleep -Milliseconds 500
$cancelOperation = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/operations/$($cancelRequest.operation_id)" -Headers $headers
$cancelDetails = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/work/plans/$($cancelPlan.id)" -Headers $headers
if ($cancelResult.status -ne 'cancelled' -or $cancelOperation.status -ne 'cancelled' -or $cancelDetails.status -ne 'cancelled') { throw "AMA-Work queued cancellation was not terminal: operation=$($cancelOperation.status), plan=$($cancelDetails.status)." }

$evidence = [ordered]@{
    timestamp = [DateTimeOffset]::UtcNow.ToString('o')
    plan_id = $plan.id
    final_plan_status = $details.status
    task_count = @($details.tasks).Count
    source_count = @($details.sources).Count
    event_count = @($events.items).Count
    artifact_formats = @($artifactFormats)
    artifact_sizes = @($artifactResults.size_bytes)
    deterministic_source = ($source.fetched.untrusted -eq $true)
    ssrf_rejected = ($ssrfStatus -eq 422)
    async_execution_accepted_status = $asyncRequest.execution_status
    async_execution_completed = ($asyncOperation.status -eq 'succeeded' -and $asyncDetails.status -eq 'succeeded')
    async_idempotency_replayed = ($asyncReplay.operation_id -eq $asyncRequest.operation_id)
    async_task_completion_events = @($asyncEvents.items | Where-Object { $_.event_type -eq 'task.execution.completed' }).Count
    async_sse_terminal_event = ($sseResponse.Content -match 'plan\.execution\.completed')
    deep_research_completed = ($researchOperation.status -eq 'succeeded' -and $researchDetails.status -eq 'succeeded')
    deep_research_rounds = @($researchEvents.items | Where-Object { $_.event_type -eq 'research.round.completed' }).Count
    deep_research_artifacts = @($researchArtifacts.items | Where-Object { $_.kind -eq 'research_report' }).Count
    deep_research_markdown_downloaded = ($markdownText -match '## References')
    deep_research_pdf_downloaded = ($pdfHeader -eq '%PDF')
    queued_cancellation_terminal = ($cancelOperation.status -eq 'cancelled' -and $cancelDetails.status -eq 'cancelled')
}
$evidence | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath 'quality/evidence/work-smoke.json' -Encoding utf8
$evidence | ConvertTo-Json -Depth 12
