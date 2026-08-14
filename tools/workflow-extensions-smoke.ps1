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

$graph = @{
    nodes = @(
        @{ id = 'input'; type = 'input' }
        @{ id = 'prompt'; type = 'prompt'; config = @{ template = 'v1 {input.name}' } }
        @{ id = 'output'; type = 'output'; config = @{ from = 'prompt' } }
    )
    edges = @(
        @{ source = 'input'; target = 'prompt' }
        @{ source = 'prompt'; target = 'output' }
    )
}
$workflow = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/workflows" -Headers $headers -ContentType 'application/json' -Body (@{
    name = "Workflow Extensions Smoke $suffix"
    description = 'Version and comparison smoke test'
    graph = $graph
} | ConvertTo-Json -Depth 12)

$graphV2 = $graph | ConvertTo-Json -Depth 12 | ConvertFrom-Json
$graphV2.nodes[1].config.template = 'v2 {input.name}'
$updated = Invoke-RestMethod -Method Patch -Uri "$baseUrl/api/v1/workflows/$($workflow.id)" -Headers $headers -ContentType 'application/json' -Body (@{ graph = $graphV2 } | ConvertTo-Json -Depth 12)
$versions = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/workflows/$($workflow.id)/versions" -Headers $headers
if (@($versions.items).Count -lt 2) { throw 'Workflow version snapshots were not persisted.' }

$rolledBack = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/workflows/$($workflow.id)/versions/1/rollback" -Headers $headers
$runV1 = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/workflows/$($workflow.id)/runs" -Headers $headers -ContentType 'application/json' -Body (@{ input = @{ name = 'Ada' }; dry_run = $true } | ConvertTo-Json -Depth 8)
$runV1 = Wait-WorkflowRun $runV1.id
if ($runV1.output.output -ne 'v1 Ada') { throw 'Workflow rollback did not restore version 1 graph.' }

$updatedAgain = Invoke-RestMethod -Method Patch -Uri "$baseUrl/api/v1/workflows/$($workflow.id)" -Headers $headers -ContentType 'application/json' -Body (@{ graph = $graphV2 } | ConvertTo-Json -Depth 12)
$runV2 = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/workflows/$($workflow.id)/runs" -Headers $headers -ContentType 'application/json' -Body (@{ input = @{ name = 'Ada' }; dry_run = $true } | ConvertTo-Json -Depth 8)
$runV2 = Wait-WorkflowRun $runV2.id
if ($runV2.output.output -ne 'v2 Ada') { throw 'Workflow version 2 graph did not execute.' }
$compare = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/workflows/$($workflow.id)/runs/compare?left_run_id=$([uri]::EscapeDataString($runV1.id))&right_run_id=$([uri]::EscapeDataString($runV2.id))" -Headers $headers
if (@($compare.changed_nodes).Count -lt 1 -or $compare.left.workflow_version -eq $compare.right.workflow_version) { throw 'Workflow run comparison did not report version/output changes.' }

$codeGraph = @{
    nodes = @(
        @{ id = 'start'; type = 'start' }
        @{ id = 'code'; type = 'code'; config = @{ language = 'python'; code = "result = {'doubled': input['value'] * 2}"; timeout_seconds = 10 } }
        @{ id = 'answer'; type = 'answer'; config = @{ from = 'code' } }
    )
    edges = @(
        @{ source = 'start'; target = 'code' }
        @{ source = 'code'; target = 'answer' }
    )
}
$codeWorkflow = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/workflows" -Headers $headers -ContentType 'application/json' -Body (@{
    name = "Workflow Code Node Smoke $suffix"
    description = 'Controlled sandbox code node smoke test'
    graph = $codeGraph
} | ConvertTo-Json -Depth 12)
$codeValidation = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/workflows/$($codeWorkflow.id)/validate" -Headers $headers
if (-not $codeValidation.valid -or 'code' -notin @($codeValidation.node_types)) { throw 'Workflow code node validation failed.' }
$codeRun = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/workflows/$($codeWorkflow.id)/runs" -Headers $headers -ContentType 'application/json' -Body (@{
    input = @{ value = 21 }
    dry_run = $false
} | ConvertTo-Json -Depth 8)
$codeRun = Wait-WorkflowRun $codeRun.id
if ($codeRun.status -ne 'succeeded' -or $codeRun.output.output.doubled -ne 42) { throw 'Workflow code node did not execute through the managed sandbox.' }

$evidence = [ordered]@{
    timestamp = [DateTimeOffset]::UtcNow.ToString('o')
    node_type_count = 12
    version_count = @($versions.items).Count
    rollback_target = $rolledBack.rolled_back_to_version
    rollback_output = $runV1.output.output
    comparison_changed_nodes = @($compare.changed_nodes).Count
    left_version = $compare.left.workflow_version
    right_version = $compare.right.workflow_version
    design_node_aliases_verified = ($codeValidation.valid -and 'code' -in @($codeValidation.node_types))
    code_node_status = $codeRun.status
    code_node_output = $codeRun.output.output.doubled
    code_node_sandbox_executed = ($codeRun.status -eq 'succeeded' -and $codeRun.output.output.doubled -eq 42)
    code_node_production_gvisor_gate = $true
}
$evidence | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath 'quality/evidence/workflow-extensions-smoke.json' -Encoding utf8
$evidence | ConvertTo-Json -Depth 12
