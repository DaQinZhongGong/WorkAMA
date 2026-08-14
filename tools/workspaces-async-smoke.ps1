$ErrorActionPreference = 'Stop'

$values = @{}
Get-Content -LiteralPath '.env' -Encoding utf8 | ForEach-Object {
    if ($_ -match '^([^#=]+)=(.*)$') { $values[$matches[1].Trim()] = $matches[2].Trim() }
}

$baseUrl = if ($env:WORKAMA_BASE_URL) { $env:WORKAMA_BASE_URL.TrimEnd('/') } else { 'http://localhost:20200' }
$internalToken = $values.INTERNAL_TOKEN
if (-not $internalToken) { throw 'INTERNAL_TOKEN is required in .env' }

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$totpHelper = Join-Path $scriptDir 'totp_now.py'
if (-not (Test-Path $totpHelper)) { throw "TOTP helper not found: $totpHelper" }

function Register-TestUser([string]$Email, [string]$Password, [string]$DisplayName) {
    $reg = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/auth/register" -ContentType 'application/json' -Body (@{
        email = $Email
        password = $Password
        display_name = $DisplayName
    } | ConvertTo-Json)
    $auth = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/auth/verify-email" -ContentType 'application/json' -Body (@{ token = $reg.debug_token } | ConvertTo-Json)
    return $auth
}

function New-HighAssuranceSession([string]$Email, [string]$Password, [string]$DisplayName) {
    $auth = Register-TestUser -Email $Email -Password $Password -DisplayName $DisplayName
    $headers = @{ Authorization = "Bearer $($auth.access_token)" }

    # Setup TOTP MFA.
    $mfa = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/auth/mfa/setup" -Headers $headers -ContentType 'application/json' -Body '{}'
    if (-not $mfa.secret) { throw 'MFA setup did not return a secret' }
    $code = python $totpHelper $mfa.secret
    $confirmed = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/auth/mfa/confirm" -Headers $headers -ContentType 'application/json' -Body (@{ code = $code } | ConvertTo-Json)
    if (-not $confirmed.mfa_enabled) { throw 'MFA confirm failed' }

    # Login returns an MFA ticket; complete the challenge to obtain auth_strength=2 token.
    $login = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/auth/login" -ContentType 'application/json' -Body (@{
        email = $Email
        password = $Password
    } | ConvertTo-Json)
    if (-not $login.mfa_ticket) { throw 'Login did not return MFA ticket' }
    $challengeCode = python $totpHelper $mfa.secret
    $session = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/auth/mfa/challenge" -ContentType 'application/json' -Body (@{
        ticket = $login.mfa_ticket
        code = $challengeCode
    } | ConvertTo-Json)
    $headers2 = @{ Authorization = "Bearer $($session.access_token)" }
    $me = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/auth/me" -Headers $headers2
    $session | Add-Member -NotePropertyName 'user' -NotePropertyValue $me -Force
    $session | Add-Member -NotePropertyName 'mfa_secret' -NotePropertyValue $mfa.secret -Force
    return $session
}

$suffix = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$ownerEmail = "ws-owner-$suffix@example.com"
$targetEmail = "ws-target-$suffix@example.com"
$password = 'WorkAMA-Smoke-2026!'

$ownerAuth = New-HighAssuranceSession -Email $ownerEmail -Password $password -DisplayName 'Owner Smoke'
$targetAuth = New-HighAssuranceSession -Email $targetEmail -Password $password -DisplayName 'Target Smoke'
$ownerHeaders = @{ Authorization = "Bearer $($ownerAuth.access_token)" }
$targetHeaders = @{ Authorization = "Bearer $($targetAuth.access_token)" }

# Owner onboarding to developer role so workspace mgmt capabilities are present.
Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/auth/onboarding" -Headers $ownerHeaders -ContentType 'application/json' -Body (@{
    user_role = 'developer'; primary_goal = 'gateway'; team_size = '1'; data_sensitivity = 'standard'
    preferred_model = 'workama-chat'; notification_preference = 'in_app'
} | ConvertTo-Json) | Out-Null

$ownerWorkspaces = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/workspaces" -Headers $ownerHeaders
$workspaceId = $ownerWorkspaces.items[0].id
$orgId = $ownerWorkspaces.items[0].org_id
if (-not $workspaceId -or -not $orgId) { throw 'Owner default workspace/org not found' }

# Invite target user into the workspace as admin so they are an active org user.
$invitation = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/workspaces/$workspaceId/invitations" -Headers $ownerHeaders -ContentType 'application/json' -Body (@{
    email = $targetEmail
    role = 'admin'
    expires_in_seconds = 600
} | ConvertTo-Json)
if (-not $invitation.token) { throw 'Invitation token missing' }

$accepted = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/invitations/$($invitation.id)/accept" -Headers $targetHeaders -ContentType 'application/json' -Body (@{ token = $invitation.token } | ConvertTo-Json)
if (-not $accepted.accepted) { throw 'Invitation acceptance failed' }
if (-not $accepted.workspace_token) { throw 'Invitation acceptance did not return a workspace token' }

# Exchange the workspace token for an access token scoped to the owner's org/workspace.
$targetContext = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/workspaces/context/exchange" -Headers $targetHeaders -ContentType 'application/json' -Body (@{
    workspace_token = $accepted.workspace_token
} | ConvertTo-Json)
$targetHeaders = @{ Authorization = "Bearer $($targetContext.access_token)" }

# Initiate owner transfer from owner to target.
$transfer = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/orgs/$orgId/owner-transfers" -Headers $ownerHeaders -ContentType 'application/json' -Body (@{
    target_user_id = $targetAuth.user.id
    reason = 'Smoke test ownership transfer'
    expires_in_seconds = 600
} | ConvertTo-Json)
if ($transfer.status -ne 'pending') { throw 'Owner transfer not in pending status' }
if (-not $transfer.confirmation_token) { throw 'Owner transfer confirmation token missing' }

# Target confirms the transfer, enqueuing the async propagation operation.
$confirmed = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/orgs/$orgId/owner-transfers/$($transfer.id)/confirm" -Headers $targetHeaders -ContentType 'application/json' -Body (@{
    confirmation_token = $transfer.confirmation_token
} | ConvertTo-Json)
if ($confirmed.status -ne 'confirmed' -or -not $confirmed.operation_id) { throw 'Owner transfer confirmation did not enqueue operation' }
$transferOperationId = $confirmed.operation_id

# The confirmation endpoint returns a fresh owner-scoped access token.
$targetHeaders = @{ Authorization = "Bearer $($confirmed.access_token)" }
$targetContext = @{ access_token = $confirmed.access_token }

function Get-OperationStatus([string]$Token, [string]$OperationId) {
    $items = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/admin/operations?limit=100" -Headers @{ Authorization = "Bearer $Token" }
    foreach ($op in $items.items) {
        if ($op.id -eq $OperationId) { return $op.status }
    }
    return $null
}

# Wait for the owner-transfer propagation operation to complete.
$transferStatus = $null
$deadline = (Get-Date).ToUniversalTime().AddSeconds(30)
do {
    Start-Sleep -Milliseconds 500
    $transferStatus = Get-OperationStatus -Token $targetContext.access_token -OperationId $transferOperationId
} while ($transferStatus -notin @('succeeded', 'failed', 'cancelled') -and (Get-Date).ToUniversalTime() -lt $deadline)
if ($transferStatus -ne 'succeeded') { throw "Owner transfer propagation operation ended with status: $transferStatus" }

# Verify organization owner changed.
$org = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/orgs/$orgId" -Headers $targetHeaders
if ($org.owner_user_id -ne $targetAuth.user.id) { throw 'Organization owner did not update after transfer' }

# New owner requests organization deletion with a 1-day retention window.
$deletion = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/orgs/$orgId/deletion-requests" -Headers $targetHeaders -ContentType 'application/json' -Body (@{
    reason = 'Smoke test organization deletion'
    retention_days = 1
} | ConvertTo-Json)
if ($deletion.status -ne 'retention' -or -not $deletion.operation_id) { throw 'Organization deletion request did not create retention operation' }
$deletionOperationId = $deletion.operation_id

# Verify the deletion operation is queued (scheduled in the future).
$deletionOpStatus = Get-OperationStatus -Token $targetContext.access_token -OperationId $deletionOperationId
if ($deletionOpStatus -ne 'queued') { throw "Organization deletion operation expected queued, got $deletionOpStatus" }

# Cancel the deletion request before retention elapses; the linked operation must be cancelled.
$cancelled = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/orgs/$orgId/deletion-requests/$($deletion.request_id)/cancel" -Headers $targetHeaders -ContentType 'application/json' -Body (@{ reason = 'Smoke test cancellation' } | ConvertTo-Json)
if ($cancelled.status -ne 'cancelled') { throw 'Organization deletion request cancellation failed' }

$cancelledOpStatus = $null
$deadline = (Get-Date).ToUniversalTime().AddSeconds(10)
do {
    Start-Sleep -Milliseconds 500
    $cancelledOpStatus = Get-OperationStatus -Token $targetContext.access_token -OperationId $deletionOperationId
} while ($cancelledOpStatus -ne 'cancelled' -and (Get-Date).ToUniversalTime() -lt $deadline)
if ($cancelledOpStatus -ne 'cancelled') { throw "Organization deletion operation expected cancelled, got $cancelledOpStatus" }

# Verify organization is active again.
$orgAfterCancel = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/orgs/$orgId" -Headers $targetHeaders
if ($orgAfterCancel.status -ne 'active') { throw "Organization status after cancellation is $($orgAfterCancel.status), expected active" }

$evidence = @{
    timestamp = [DateTimeOffset]::UtcNow.ToString('o')
    base_url = $baseUrl
    owner_email = $ownerEmail
    target_email = $targetEmail
    org_id = $orgId
    workspace_id = $workspaceId
    transfer_id = $transfer.id
    transfer_operation_id = $transferOperationId
    transfer_operation_status = $transferStatus
    deletion_request_id = $deletion.request_id
    deletion_operation_id = $deletionOperationId
    deletion_operation_status = $cancelledOpStatus
    organization_status_after_cancel = $orgAfterCancel.status
    owner_after_transfer = $org.owner_user_id
}

$evidenceDir = 'quality/evidence'
if (-not (Test-Path $evidenceDir)) { New-Item -ItemType Directory -Path $evidenceDir -Force | Out-Null }
$evidence | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8 "$evidenceDir/workspaces-async-smoke.json"
$evidence | ConvertTo-Json -Depth 8
