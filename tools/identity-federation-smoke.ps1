$ErrorActionPreference = 'Stop'

$baseUrl = if ($env:WORKAMA_API_BASE_URL) { $env:WORKAMA_API_BASE_URL.TrimEnd('/') } elseif ($env:WORKAMA_BASE_URL) { $env:WORKAMA_BASE_URL.TrimEnd('/') } else { 'http://localhost:20200' }
$token = $env:WORKAMA_TEST_TOKEN
$scimToken = $env:WORKAMA_SCIM_TOKEN
$workspaceId = $env:WORKAMA_TEST_WORKSPACE_ID
$suffix = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$config = $null
$scimTokenId = $null
$createdScimToken = $null

function Get-ErrorStatus($errorRecord) {
    if ($errorRecord.Exception.Response -and $errorRecord.Exception.Response.StatusCode) { return [int]$errorRecord.Exception.Response.StatusCode }
    return 0
}

if (-not $token) {
    $envFile = Join-Path (Get-Location) '.env'
    if (Test-Path -LiteralPath $envFile) {
        $values = @{}
        Get-Content -LiteralPath $envFile -Encoding utf8 | ForEach-Object { if ($_ -match '^([^#=]+)=(.*)$') { $values[$matches[1].Trim()] = $matches[2].Trim() } }
        if ($values.TEST_ACCOUNT_EMAIL -and $values.TEST_ACCOUNT_PASSWORD) {
            $login = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/auth/login" -ContentType 'application/json' -Body (@{ email = $values.TEST_ACCOUNT_EMAIL; password = $values.TEST_ACCOUNT_PASSWORD } | ConvertTo-Json)
            $token = $login.access_token
            if (-not $workspaceId) { $workspaceId = $login.user.workspace_id }
        }
    }
}

if (-not $token) {
    Write-Output 'SKIP: set WORKAMA_TEST_TOKEN or TEST_ACCOUNT_EMAIL/TEST_ACCOUNT_PASSWORD to run identity federation smoke.'
    exit 0
}

$headers = @{ Authorization = "Bearer $token" }
try {
    $config = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/identity-federation" -Headers $headers -ContentType 'application/json' -Body (@{
        name = "Identity Federation Smoke $suffix"
        provider = 'oidc'
        issuer = 'https://idp.example.com'
        metadata_url = 'https://idp.example.com/.well-known/openid-configuration'
        client_id = 'workama-smoke'
        client_secret = "secret-$suffix"
        redirect_allowlist = @('https://console.example.com/sso/callback')
        mapping = @{ email = 'email'; display_name = 'name' }
    } | ConvertTo-Json -Depth 10)
    if (-not $config.id -or $config.status -ne 'disabled' -or $config.client_secret_hash -or $config.client_secret -or -not $config.client_secret_configured) { throw 'SSO config default/secret contract failed.' }

    $ssrfStatus = 0
    try {
        Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/identity-federation" -Headers $headers -ContentType 'application/json' -Body (@{ name = "SSRF $suffix"; provider = 'oidc'; issuer = 'https://127.0.0.1'; redirect_allowlist = @('https://console.example.com/callback') } | ConvertTo-Json) | Out-Null
    } catch { $ssrfStatus = Get-ErrorStatus $_ }
    if ($ssrfStatus -ne 422) { throw "SSRF validation returned $ssrfStatus instead of 422." }

    $pending = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/identity-federation/$($config.id)/enable" -Headers $headers
    if ($pending.status -ne 'pending' -or $pending.verification -ne 'pending/not_configured') { throw 'SSO enable must remain pending/not_configured without upstream verification.' }

    if (-not $workspaceId) { throw 'WORKAMA_TEST_WORKSPACE_ID or a login response workspace_id is required for SCIM smoke.' }
    if (-not $scimToken) {
        $created = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/identity-federation/scim-tokens" -Headers $headers -ContentType 'application/json' -Body (@{ workspace_id = $workspaceId } | ConvertTo-Json)
        $scimToken = $created.token
        $scimTokenId = $created.id
        $createdScimToken = $scimToken
    }
    $scimHeaders = @{ Authorization = "Bearer $scimToken" }
    $externalId = "identity-smoke-$suffix"
    $userBody = @{ schemas = @('urn:ietf:params:scim:schemas:core:2.0:User'); externalId = $externalId; userName = "identity-$suffix@example.com"; displayName = 'Identity Smoke'; active = $true } | ConvertTo-Json -Depth 10
    $user = Invoke-RestMethod -Method Post -Uri "$baseUrl/scim/v2.0/$workspaceId/Users" -Headers $scimHeaders -ContentType 'application/scim+json' -Body $userBody
    $replay = Invoke-RestMethod -Method Post -Uri "$baseUrl/scim/v2.0/$workspaceId/Users" -Headers $scimHeaders -ContentType 'application/scim+json' -Body $userBody
    if (-not $user.id -or $replay.id -ne $user.id -or -not $replay.idempotent_replay) { throw 'SCIM User idempotency failed.' }
    $patched = Invoke-RestMethod -Method Patch -Uri "$baseUrl/scim/v2.0/$workspaceId/Users/$($user.id)" -Headers $scimHeaders -ContentType 'application/scim+json' -Body (@{ schemas = @('urn:ietf:params:scim:api:messages:2.0:PatchOp'); Operations = @(@{ op = 'Replace'; path = 'active'; value = $false }) } | ConvertTo-Json -Depth 10)
    if ($patched.active) { throw 'SCIM User deprovision failed.' }
    $group = Invoke-RestMethod -Method Post -Uri "$baseUrl/scim/v2.0/$workspaceId/Groups" -Headers $scimHeaders -ContentType 'application/scim+json' -Body (@{ schemas = @('urn:ietf:params:scim:schemas:core:2.0:Group'); externalId = "group-$suffix"; displayName = 'Identity Smoke Group'; members = @(@{ value = $user.id; display = 'Identity Smoke' }) } | ConvertTo-Json -Depth 10)
    if (-not $group.id -or @($group.members).Count -ne 1) { throw 'SCIM Group provisioning failed.' }

    $evidence = [ordered]@{
        timestamp = [DateTimeOffset]::UtcNow.ToString('o')
        sso_default_disabled = ($config.status -eq 'disabled')
        secret_not_returned = (-not $config.client_secret -and -not $config.client_secret_hash)
        ssrf_status = $ssrfStatus
        sso_pending_not_configured = ($pending.status -eq 'pending' -and $pending.verification -eq 'pending/not_configured')
        scim_user_idempotent = ($replay.id -eq $user.id -and [bool]$replay.idempotent_replay)
        scim_user_deprovisioned = (-not $patched.active)
        scim_group_provisioned = ($group.members.Count -eq 1)
    }
    $evidence | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath 'quality/evidence/identity-federation-smoke.json' -Encoding utf8
    $evidence | ConvertTo-Json -Depth 10
}
finally {
    if ($config -and $config.id) { try { Invoke-RestMethod -Method Delete -Uri "$baseUrl/api/v1/identity-federation/$($config.id)" -Headers $headers | Out-Null } catch {} }
    if ($scimTokenId) { try { Invoke-RestMethod -Method Delete -Uri "$baseUrl/api/v1/identity-federation/scim-tokens/$scimTokenId" -Headers $headers | Out-Null } catch {} }
}
