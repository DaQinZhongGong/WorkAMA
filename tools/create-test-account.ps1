param(
    [string]$Email,
    [string]$Password,
    [string]$DisplayName = 'WorkAMA Tester'
)

$ErrorActionPreference = 'Stop'

$envValues = @{}
if (Test-Path -LiteralPath '.env') {
    foreach ($line in Get-Content -LiteralPath '.env' -Encoding utf8) {
        if ($line -match '^\s*([^#=]+)=(.*)$') {
            $envValues[$matches[1].Trim()] = $matches[2].Trim()
        }
    }
}

if (-not $Email) { $Email = $envValues['TEST_ACCOUNT_EMAIL'] }
if (-not $Password) { $Password = $envValues['TEST_ACCOUNT_PASSWORD'] }
if (-not $Email -or -not $Password) { throw 'TEST_ACCOUNT_EMAIL and TEST_ACCOUNT_PASSWORD are required.' }

$loginBody = @{ email = $Email; password = $Password } | ConvertTo-Json
$auth = $null
try {
    $auth = Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/api/v1/auth/login' -ContentType 'application/json' -Body $loginBody
} catch {
    if ($_.Exception.Response.StatusCode.value__ -ne 401) { throw }
    $registerBody = @{ email = $Email; password = $Password; display_name = $DisplayName } | ConvertTo-Json
    $registration = Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/api/v1/auth/register' -ContentType 'application/json' -Body $registerBody
    $auth = Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/api/v1/auth/verify-email' -ContentType 'application/json' -Body (@{ token = $registration.debug_token } | ConvertTo-Json)
}

$headers = @{ Authorization = "Bearer $($auth.access_token)" }
$me = Invoke-RestMethod -Method Get -Uri 'http://localhost:20200/api/v1/auth/me' -Headers $headers
if (-not $me.onboarding_completed) {
    Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/api/v1/auth/onboarding' -Headers $headers -ContentType 'application/json' -Body (@{
        user_role = 'developer'
        primary_goal = 'chat'
        team_size = '1'
        data_sensitivity = 'standard'
        preferred_model = 'workama-chat'
        notification_preference = 'in_app'
    } | ConvertTo-Json) | Out-Null
}

$sessionResult = Invoke-RestMethod -Method Get -Uri 'http://localhost:20200/api/v1/sessions' -Headers $headers
if ($sessionResult.items.Count -eq 0) {
    Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/api/v1/sessions' -Headers $headers -ContentType 'application/json' -Body '{"title":"Welcome to WorkAMA","model":"workama-chat"}' | Out-Null
}

$result = @{
    email = $Email
    display_name = $DisplayName
    user_id = $me.id
    workspace_id = $me.workspace_id
    role = $me.role
    login_url = 'http://localhost:20204/login'
    verified_at = [DateTimeOffset]::UtcNow.ToString('o')
}
$result | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath 'quality/evidence/test-account.json' -Encoding utf8
$result | ConvertTo-Json -Depth 5
