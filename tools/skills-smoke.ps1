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
$skillName = "smoke-skill-$suffix"
$artifactRef = "mock://skill/workama/$skillName/1.2.3"
$idempotencyKey = "skills-smoke-$suffix"
$manifest = @{
    schema_version = 1
    name = $skillName
    version = '1.2.3'
    publisher = 'workama'
    description = 'Deterministic skill smoke package'
    trigger_description = 'Run local skill smoke checks'
    required_tools = @('web.search')
    permissions = @()
    files = @('skill.yaml', 'prompt.md')
    entrypoint = 'prompt.md'
} | ConvertTo-Json -Depth 10

function Get-ErrorStatus($errorRecord) {
    if ($errorRecord.Exception.Response -and $errorRecord.Exception.Response.StatusCode) {
        return [int]$errorRecord.Exception.Response.StatusCode
    }
    return 0
}

$install = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/skills/install" -Headers (@{ Authorization = $headers.Authorization; 'Idempotency-Key' = $idempotencyKey }) -ContentType 'application/json' -Body (@{
    artifact_ref = $artifactRef
    manifest = ($manifest | ConvertFrom-Json)
} | ConvertTo-Json -Depth 12)
$replay = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/skills/install" -Headers (@{ Authorization = $headers.Authorization; 'Idempotency-Key' = $idempotencyKey }) -ContentType 'application/json' -Body (@{
    artifact_ref = $artifactRef
    manifest = ($manifest | ConvertFrom-Json)
} | ConvertTo-Json -Depth 12)
$skillId = $install.skill.id
if (-not $skillId -or $install.skill.review_status -ne 'pending' -or $install.skill.installation.enabled -or -not $replay.deduplicated) { throw 'Skill install/idempotency contract is incomplete.' }

$enablePendingStatus = 0
try { Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/skills/$skillId/enable" -Headers $headers | Out-Null } catch { $enablePendingStatus = Get-ErrorStatus $_ }
if ($enablePendingStatus -ne 409) { throw "Pending skill enable returned $enablePendingStatus instead of 409." }

$review = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/skills/$skillId/review" -Headers $headers -ContentType 'application/json' -Body (@{
    review_status = 'approved'
    reason = 'Local smoke approval'
} | ConvertTo-Json)
if ($review.skill.review_status -ne 'approved') { throw 'Skill approval was not persisted.' }

$enabled = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/skills/$skillId/enable" -Headers $headers
$disabled = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/skills/$skillId/disable" -Headers $headers
if (-not $enabled.skill.installation.enabled -or $disabled.skill.installation.enabled) { throw 'Skill enable/disable state contract is incomplete.' }

$urlStatus = 0
try { Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/skills/install" -Headers $headers -ContentType 'application/json' -Body (@{ artifact_ref = 'https://example.com/skill.zip' } | ConvertTo-Json) | Out-Null } catch { $urlStatus = Get-ErrorStatus $_ }
$secretStatus = 0
try {
    $secretManifest = $manifest | ConvertFrom-Json
    $secretManifest | Add-Member -NotePropertyName api_key -NotePropertyValue 'must-not-persist'
    Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/skills/install" -Headers $headers -ContentType 'application/json' -Body (@{ artifact_ref = "mock://skill/workama/$skillName/1.2.4"; manifest = $secretManifest } | ConvertTo-Json -Depth 12) | Out-Null
} catch { $secretStatus = Get-ErrorStatus $_ }
if ($urlStatus -ne 422 -or $secretStatus -ne 422) { throw "Skill package security guards returned url=$urlStatus secret=$secretStatus instead of 422." }

$list = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/skills?review_status=approved" -Headers $headers
$listed = @($list.items | Where-Object { $_.id -eq $skillId })[0]
if (-not $listed -or $listed.manifest.api_key) { throw 'Skill list did not preserve tenant-scoped reviewed metadata safely.' }

$evidence = [ordered]@{
    timestamp = [DateTimeOffset]::UtcNow.ToString('o')
    installed = [bool]$skillId
    idempotency_replayed = [bool]$replay.deduplicated
    pending_enable_status = $enablePendingStatus
    approved = ($review.skill.review_status -eq 'approved')
    enabled_then_disabled = ($enabled.skill.installation.enabled -and -not $disabled.skill.installation.enabled)
    arbitrary_url_status = $urlStatus
    secret_manifest_status = $secretStatus
    listed_without_secret = (-not $listed.manifest.api_key)
}
$evidence | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath 'quality/evidence/skills-smoke.json' -Encoding utf8
$evidence | ConvertTo-Json -Depth 12
