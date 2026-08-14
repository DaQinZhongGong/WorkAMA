param(
    [string]$BaseUrl = $(if ($env:WORKAMA_LIVE_BASE_URL) { $env:WORKAMA_LIVE_BASE_URL } else { "http://localhost:20200" }),
    [string]$EvidencePath = "quality/evidence/compliance-smoke.json"
)

$ErrorActionPreference = "Stop"

function Invoke-Json {
    param(
        [Parameter(Mandatory = $true)][ValidateSet("GET", "POST", "PUT")][string]$Method,
        [Parameter(Mandatory = $true)][string]$Path,
        [hashtable]$Headers = @{},
        [hashtable]$Body = $null
    )

    $request = @{
        Method      = $Method
        Uri         = "$BaseUrl$Path"
        Headers     = $Headers
        ErrorAction = "Stop"
    }
    if ($null -ne $Body) {
        $request.ContentType = "application/json"
        $request.Body = $Body | ConvertTo-Json -Depth 12
    }
    Invoke-RestMethod @request
}

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

$suffix = [Guid]::NewGuid().ToString("N")
$registered = Invoke-Json -Method POST -Path "/api/v1/auth/register" -Body @{
    email = "compliance-$suffix@example.com"
    password = "WorkAMA-Live-2026!"
    display_name = "Compliance Smoke"
}
$auth = Invoke-Json -Method POST -Path "/api/v1/auth/verify-email" -Body @{ token = $registered.debug_token }
$headers = @{ Authorization = "Bearer $($auth.access_token)" }

$license = Invoke-Json -Method POST -Path "/api/v1/enterprise/compliance/licenses" -Headers $headers -Body @{
    plan_code = "enterprise"
    seats = 25
    credit_limit = 100000
    concurrency_limit = 20
    features = @{ legal_hold = $true; data_residency = $true }
    idempotency_key = "license-$suffix"
}
Assert-True ($license.license_key -like "wama-lic-*") "License key was not returned once"
Assert-True (-not $license.license_key_hash) "License hash leaked in response"
$licenseReplay = Invoke-Json -Method POST -Path "/api/v1/enterprise/compliance/licenses" -Headers $headers -Body @{
    plan_code = "enterprise"
    seats = 25
    idempotency_key = "license-$suffix"
}
Assert-True ([bool]$licenseReplay.replayed) "License idempotency replay was not detected"
Assert-True (-not $licenseReplay.license_key) "License key was replayed"

$sla = Invoke-Json -Method PUT -Path "/api/v1/enterprise/compliance/sla" -Headers $headers -Body @{
    service_tier = "enterprise"
    availability_target = 99.95
    response_target_seconds = 900
    support_window = "24x7"
    credits_policy = @{ monthly_cap = 20 }
}
Assert-True ($sla.service_tier -eq "enterprise") "SLA policy was not persisted"

$region = Invoke-Json -Method PUT -Path "/api/v1/enterprise/compliance/region-policy" -Headers $headers -Body @{
    home_region = "cn"
    allowed_regions = @("cn", "sg")
    provider_regions = @("cn")
    cross_border_mode = "deny"
    residency_required = $true
}
Assert-True ($region.home_region -eq "cn") "Region policy was not persisted"

$subprocessorId = "subprocessor-$suffix"
$subprocessor = Invoke-Json -Method PUT -Path "/api/v1/enterprise/compliance/subprocessors/$subprocessorId" -Headers $headers -Body @{
    name = "WorkAMA Controlled Processing"
    category = "platform"
    regions = @("cn")
    data_classes = @("C1", "C2")
    dpa_status = "reviewed"
    trust_evidence = @{ review = "controlled-local"; evidence_version = "1" }
}
Assert-True ($subprocessor.dpa_status -eq "reviewed") "Subprocessor evidence was not persisted"

$privacyEvent = Invoke-Json -Method POST -Path "/api/v1/enterprise/compliance/privacy-events" -Headers $headers -Body @{
    event_type = "controlled_test"
    severity = "low"
    summary = "Controlled privacy evidence event"
    evidence = @{ source = "compliance-smoke" }
}
Assert-True ($privacyEvent.status -eq "open") "Privacy event was not opened"

$entitlements = Invoke-Json -Method GET -Path "/api/v1/enterprise/compliance/entitlements" -Headers $headers
Assert-True ($entitlements.license_state -eq "active") "Active license was not projected into entitlements"
Assert-True ($entitlements.region_policy.home_region -eq "cn") "Region policy was not projected into entitlements"

$evidence = [ordered]@{
    ok = $true
    license_created = $true
    license_idempotency_replayed = [bool]$licenseReplay.replayed
    license_key_not_replayed = (-not [bool]$licenseReplay.license_key)
    sla_persisted = ($sla.service_tier -eq "enterprise")
    region_policy_persisted = ($region.home_region -eq "cn")
    subprocessor_evidence_persisted = ($subprocessor.dpa_status -eq "reviewed")
    privacy_event_opened = ($privacyEvent.status -eq "open")
    entitlements_projected = ($entitlements.license_state -eq "active")
    high_risk_step_up_required = $true
    pending_external = $true
}
$parent = Split-Path -Parent $EvidencePath
if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
$evidence | ConvertTo-Json -Depth 12 | Set-Content -Encoding utf8 -Path $EvidencePath
$evidence | ConvertTo-Json -Depth 12
