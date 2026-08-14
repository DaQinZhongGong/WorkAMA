$ErrorActionPreference = 'Stop'

$values = @{}
Get-Content -LiteralPath '.env' -Encoding utf8 | ForEach-Object {
    if ($_ -match '^([^#=]+)=(.*)$') { $values[$matches[1].Trim()] = $matches[2].Trim() }
}
if (-not $values.TEST_ACCOUNT_EMAIL -or -not $values.TEST_ACCOUNT_PASSWORD) { throw 'TEST_ACCOUNT_EMAIL and TEST_ACCOUNT_PASSWORD are required.' }
$baseUrl = if ($env:WORKAMA_BASE_URL) { $env:WORKAMA_BASE_URL.TrimEnd('/') } else { 'http://localhost:20200' }
$login = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/auth/login" -ContentType 'application/json' -Body (@{ email = $values.TEST_ACCOUNT_EMAIL; password = $values.TEST_ACCOUNT_PASSWORD } | ConvertTo-Json)
$headers = @{ Authorization = "Bearer $($login.access_token)" }
$suffix = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$existingServers = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/mcp-servers?query=MCP%20Smoke" -Headers $headers
foreach ($existingServer in @($existingServers.items)) {
    Invoke-RestMethod -Method Delete -Uri "$baseUrl/api/v1/mcp-servers/$($existingServer.id)" -Headers $headers | Out-Null
}

$org = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/orgs/$($login.user.org_id)" -Headers $headers
$serviceAccountHeaders = @{ Authorization = "Bearer $($login.access_token)"; 'Idempotency-Key' = "mcp-service-account-$suffix" }
$serviceAccount = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/service-accounts" -Headers $serviceAccountHeaders -ContentType 'application/json' -Body (@{
    name = "MCP Smoke Service $suffix"
    workspace_id = $login.user.workspace_id
    purpose = 'MCP registry smoke test'
    scopes = @('mcp_server:read')
} | ConvertTo-Json -Depth 10)
if (-not $serviceAccount.token -or $serviceAccount.credential.configured -ne $true) { throw 'Service-account token was not returned once at creation.' }
$serviceHeaders = @{ Authorization = "Bearer $($serviceAccount.token)" }
$mcpWithService = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/mcp-servers" -Headers $serviceHeaders
$highRisk = $null
try {
    Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/service-accounts/$($serviceAccount.id)/credential-rotations" -Headers $headers -ContentType 'application/json' -Body (@{ reason = 'smoke step-up check' } | ConvertTo-Json) | Out-Null
} catch {
    $highRisk = $_.Exception.Response.StatusCode.value__
}
if ($highRisk -ne 403) { throw "Expected service-account rotation to require step-up authentication, got $highRisk." }

$server = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/mcp-servers" -Headers $headers -ContentType 'application/json' -Body (@{
    name = "MCP Smoke $suffix"
    transport = 'stdio'
    endpoint_or_command = 'mcp-server --mode smoke'
    protocol_version = '2025-06-18'
    capabilities = @{ tools = @(@{ name = 'execute_shell'; description = 'Execute a shell command'; input_schema = @{ type = 'object' } }) }
} | ConvertTo-Json -Depth 12)
$started = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/mcp-servers/$($server.id)/start" -Headers $headers
$discovery = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/mcp-servers/$($server.id)/discoveries" -Headers $headers -ContentType 'application/json' -Body (@{
    protocol_version = '2025-06-18'
    tools = @(@{ name = 'execute_shell'; description = 'Execute a shell command'; input_schema = @{ type = 'object' }; risk = 'low' })
    resources = @()
    prompts = @()
} | ConvertTo-Json -Depth 12)
if ($discovery.capability_snapshot.tools[0].platform_risk -notin @('high', 'critical') -or $discovery.capability_snapshot.tools[0].server_risk_ignored -ne $true) { throw 'MCP risk was not independently classified.' }
Invoke-RestMethod -Method Delete -Uri "$baseUrl/api/v1/mcp-servers/$($server.id)" -Headers @{ Authorization = $headers.Authorization; 'If-Match' = '*' } | Out-Null

$evidence = @{ timestamp = [DateTimeOffset]::UtcNow.ToString('o'); organization_status = $org.status; service_account_mcp_read = (@($mcpWithService.items).Count -ge 0); rotation_step_up_status = $highRisk; mcp_risk = $discovery.capability_snapshot.tools[0].platform_risk; server_deleted = $true }
$evidence | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath 'quality/evidence/mcp-enterprise-smoke.json' -Encoding utf8
$evidence | ConvertTo-Json -Depth 12
