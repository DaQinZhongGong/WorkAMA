$ErrorActionPreference = 'Stop'
$values = @{}
Get-Content -LiteralPath '.env' -Encoding utf8 | ForEach-Object { if ($_ -match '^([^#=]+)=(.*)$') { $values[$matches[1].Trim()] = $matches[2].Trim() } }
if (-not $values.TEST_ACCOUNT_EMAIL -or -not $values.TEST_ACCOUNT_PASSWORD) { throw 'TEST_ACCOUNT_EMAIL and TEST_ACCOUNT_PASSWORD are required.' }
$baseUrl = if ($env:WORKAMA_BASE_URL) { $env:WORKAMA_BASE_URL.TrimEnd('/') } else { 'http://localhost:20200' }
$login = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/auth/login" -ContentType 'application/json' -Body (@{ email = $values.TEST_ACCOUNT_EMAIL; password = $values.TEST_ACCOUNT_PASSWORD } | ConvertTo-Json)
$headers = @{ Authorization = "Bearer $($login.access_token)" }
function Get-ErrorStatus($errorRecord) { if ($errorRecord.Exception.Response -and $errorRecord.Exception.Response.StatusCode) { return [int]$errorRecord.Exception.Response.StatusCode }; return 0 }
$groups = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/enterprise/groups" -Headers $headers
$roles = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/enterprise/roles" -Headers $headers
$authMatrix = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/enterprise/auth-strength-matrix" -Headers $headers
$stepUpStatus = 0
try { Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/enterprise/roles" -Headers $headers -ContentType 'application/json' -Body (@{ name = 'Smoke Role'; capabilities = @('dataset:read'); idempotency_key = 'enterprise-rbac-smoke' } | ConvertTo-Json) | Out-Null } catch { $stepUpStatus = Get-ErrorStatus $_ }
$externalProvisionStatus = 0
try { Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/enterprise/groups" -Headers $headers -ContentType 'application/json' -Body (@{ name = 'External Smoke Group'; source = 'scim'; idempotency_key = 'external-group-smoke' } | ConvertTo-Json) | Out-Null } catch { $externalProvisionStatus = Get-ErrorStatus $_ }
if ($stepUpStatus -ne 403 -or $externalProvisionStatus -ne 422) { throw "Enterprise RBAC fail-closed contract failed: step_up=$stepUpStatus external=$externalProvisionStatus" }
$evidence = [ordered]@{
    timestamp = [DateTimeOffset]::UtcNow.ToString('o'); groups_read = ($null -ne $groups.items); roles_read = ($null -ne $roles.items); auth_matrix_read = ($null -ne $authMatrix.items)
    high_assurance_step_up_status = $stepUpStatus; external_provision_pending_status = $externalProvisionStatus; fail_closed = ($stepUpStatus -eq 403 -and $externalProvisionStatus -eq 422)
}
$evidence | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath 'quality/evidence/enterprise-rbac-smoke.json' -Encoding utf8
$evidence | ConvertTo-Json -Depth 12
