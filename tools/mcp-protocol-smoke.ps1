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
$server = $null
try {
    $server = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/mcp-servers" -Headers $headers -ContentType 'application/json' -Body (@{
        name = "MCP Protocol Smoke $suffix"
        transport = 'streamable_http'
        endpoint_or_command = 'mock://deterministic'
        auth_type = 'none'
        protocol_version = '2025-06-18'
        capabilities = @{}
    } | ConvertTo-Json -Depth 12)
    if (-not $server.id -or $server.endpoint_or_command -ne 'mock://deterministic' -or $server.auth.configured) { throw 'MCP mock server registration contract is incomplete.' }

    $started = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/mcp-servers/$($server.id)/start" -Headers $headers
    if ($started.status -ne 'enabled') { throw 'MCP mock server did not enter enabled state.' }

    $initialize = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/mcp-servers/$($server.id)/rpc" -Headers $headers -ContentType 'application/json' -Body (@{
        jsonrpc = '2.0'; id = 1; method = 'initialize'; params = @{ protocolVersion = '2025-06-18'; capabilities = @{}; clientInfo = @{ name = 'workama-smoke'; version = '1.0.0' } }
    } | ConvertTo-Json -Depth 12)
    $tools = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/mcp-servers/$($server.id)/rpc" -Headers $headers -ContentType 'application/json' -Body (@{
        jsonrpc = '2.0'; id = 2; method = 'tools/list'; params = @{}
    } | ConvertTo-Json -Depth 12)
    $call = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/mcp-servers/$($server.id)/rpc" -Headers $headers -ContentType 'application/json' -Body (@{
        jsonrpc = '2.0'; id = 3; method = 'tools/call'; params = @{ name = 'echo'; arguments = @{ text = 'WorkAMA MCP smoke' } }
    } | ConvertTo-Json -Depth 12)
    if ($initialize.result.serverInfo.name -ne 'workama-mock-deterministic') { throw 'MCP initialize response is incomplete.' }
    if (@($tools.result.tools).Count -lt 2 -or $tools.result.tools[0].inputSchema.type -ne 'object') { throw 'MCP tools/list response is incomplete.' }
    if ($call.result.isError -ne $false -or $call.result.content[0].text -ne 'WorkAMA MCP smoke') { throw 'MCP tools/call response is incomplete.' }

    $invalid = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/mcp-servers/$($server.id)/rpc" -Headers $headers -ContentType 'application/json' -Body (@{
        jsonrpc = '2.0'; id = 4; method = 'tools/call'; params = @{ name = 'echo'; arguments = @{ text = 5 } }
    } | ConvertTo-Json -Depth 12)
    if ($invalid.error.code -ne -32602) { throw 'MCP invalid tool arguments were not rejected.' }

    $oauth = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/mcp-servers" -Headers $headers -ContentType 'application/json' -Body (@{
        name = "MCP OAuth Pending $suffix"
        transport = 'streamable_http'
        endpoint_or_command = 'mock://deterministic'
        auth_type = 'oauth'
        protocol_version = '2025-06-18'
    } | ConvertTo-Json -Depth 12)
    $oauthRpc = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/mcp-servers/$($oauth.id)/rpc" -Headers $headers -ContentType 'application/json' -Body (@{
        jsonrpc = '2.0'; id = 5; method = 'initialize'; params = @{ protocolVersion = '2025-06-18'; capabilities = @{}; clientInfo = @{} }
    } | ConvertTo-Json -Depth 12)
    if ($oauthRpc.result -or $oauthRpc.error.code -ne -32003 -or $oauthRpc.error.data.oauth_pending -ne $true) { throw 'MCP OAuth pending boundary is not explicit.' }

    $authorization = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/mcp-servers/$($oauth.id)/authorizations" -Headers $headers -ContentType 'application/json' -Body (@{ scopes = @('mcp:tools') } | ConvertTo-Json -Depth 12)
    $callback = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/mcp-servers/oauth/callback?code=provider-code&state=$([uri]::EscapeDataString($authorization.state))"
    $oauthReplayStatus = 0
    try {
        Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/mcp-servers/oauth/callback?code=provider-code&state=$([uri]::EscapeDataString($authorization.state))" | Out-Null
    } catch {
        $oauthReplayStatus = [int]$_.Exception.Response.StatusCode.value__
    }
    if ($authorization.status -ne 'pending_external' -or $authorization.code_challenge_method -ne 'S256' -or $callback.status -ne 'pending_external_exchange' -or $callback.credential_persisted -ne $false -or $oauthReplayStatus -ne 400) { throw 'MCP OAuth PKCE state machine contract failed.' }

    $evidence = [ordered]@{
        timestamp = [DateTimeOffset]::UtcNow.ToString('o')
        initialize = ($initialize.result.serverInfo.name -eq 'workama-mock-deterministic')
        tools_list_count = @($tools.result.tools).Count
        tool_call_echo = ($call.result.content[0].text -eq 'WorkAMA MCP smoke')
        invalid_arguments_rejected = ($invalid.error.code -eq -32602)
        oauth_pending = ($oauthRpc.error.data.oauth_pending -eq $true)
        oauth_pkce_state_reserved = ($authorization.status -eq 'pending_external' -and $authorization.code_challenge_method -eq 'S256')
        oauth_callback_pending_external = ($callback.status -eq 'pending_external_exchange' -and $callback.credential_persisted -eq $false)
        oauth_state_replay_rejected = ($oauthReplayStatus -eq 400)
        credential_fields_exposed = [bool]($call | ConvertTo-Json -Depth 20 | Select-String -Quiet -Pattern 'authorization|token|secret|credential')
    }
    if ($evidence.credential_fields_exposed) { throw 'MCP response exposed a credential-like field.' }
    $evidence | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath 'quality/evidence/mcp-protocol-smoke.json' -Encoding utf8
    $evidence | ConvertTo-Json -Depth 12
} finally {
    if ($server -and $server.id) {
        try { Invoke-RestMethod -Method Delete -Uri "$baseUrl/api/v1/mcp-servers/$($server.id)" -Headers @{ Authorization = $headers.Authorization; 'If-Match' = '*' } | Out-Null } catch { }
    }
    if ($oauth -and $oauth.id) {
        try { Invoke-RestMethod -Method Delete -Uri "$baseUrl/api/v1/mcp-servers/$($oauth.id)" -Headers @{ Authorization = $headers.Authorization; 'If-Match' = '*' } | Out-Null } catch { }
    }
}
