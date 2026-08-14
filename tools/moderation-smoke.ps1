$ErrorActionPreference = 'Stop'

$values = @{}
Get-Content -LiteralPath '.env' -Encoding utf8 | ForEach-Object {
    if ($_ -match '^([^#=]+)=(.*)$') { $values[$matches[1].Trim()] = $matches[2].Trim() }
}
if (-not $values.TEST_ACCOUNT_EMAIL -or -not $values.TEST_ACCOUNT_PASSWORD) { throw 'TEST_ACCOUNT_EMAIL and TEST_ACCOUNT_PASSWORD are required.' }
$login = Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/api/v1/auth/login' -ContentType 'application/json' -Body (@{ email = $values.TEST_ACCOUNT_EMAIL; password = $values.TEST_ACCOUNT_PASSWORD } | ConvertTo-Json)
$headers = @{ Authorization = "Bearer $($login.access_token)" }
$suffix = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$policy = Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/api/v1/security/moderation-policies' -Headers $headers -ContentType 'application/json' -Body (@{
    name = "Smoke Guard $suffix"
    default_input_action = 'log'
    default_output_action = 'block'
    rules = @(@{ id = 'secret'; kind = 'sensitive_word'; direction = 'both'; pattern = 'secret'; action = 'block' }, @{ id = 'email'; kind = 'regex'; direction = 'output'; pattern = '\S+@\S+'; action = 'mask'; replacement = '[email]' })
} | ConvertTo-Json -Depth 12)
$blocked = Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/api/v1/security/moderation-tests' -Headers $headers -ContentType 'application/json' -Body (@{ policy_id = $policy.id; direction = 'output'; text = 'secret alice@example.com'; request_id = "moderation-$suffix" } | ConvertTo-Json)
$logs = Invoke-RestMethod -Method Get -Uri 'http://localhost:20200/api/v1/security/moderation-logs' -Headers $headers
if ($blocked.action -ne 'block' -or $blocked.text -or @($logs.items).Count -lt 1) { throw 'Moderation block or audit evidence failed.' }
Invoke-RestMethod -Method Delete -Uri "http://localhost:20200/api/v1/security/moderation-policies/$($policy.id)" -Headers $headers | Out-Null
$evidence = @{ timestamp = [DateTimeOffset]::UtcNow.ToString('o'); policy_version = $policy.version; blocked = ($blocked.action -eq 'block'); original_not_returned = ($null -eq $blocked.text); audit_count = @($logs.items).Count }
$evidence | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath 'quality/evidence/moderation-smoke.json' -Encoding utf8
$evidence | ConvertTo-Json -Depth 8
