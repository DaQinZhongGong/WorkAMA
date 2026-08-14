$ErrorActionPreference = 'Stop'

$values = @{}
Get-Content -LiteralPath '.env' -Encoding utf8 | ForEach-Object { if ($_ -match '^([^#=]+)=(.*)$') { $values[$matches[1].Trim()] = $matches[2].Trim() } }
if (-not $values.TEST_ACCOUNT_EMAIL -or -not $values.TEST_ACCOUNT_PASSWORD) { throw 'TEST_ACCOUNT_EMAIL and TEST_ACCOUNT_PASSWORD are required.' }
$baseUrl = if ($env:WORKAMA_BASE_URL) { $env:WORKAMA_BASE_URL.TrimEnd('/') } else { 'http://localhost:20200' }
$login = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/auth/login" -ContentType 'application/json' -Body (@{ email = $values.TEST_ACCOUNT_EMAIL; password = $values.TEST_ACCOUNT_PASSWORD } | ConvertTo-Json)
$headers = @{ Authorization = "Bearer $($login.access_token)" }
$suffix = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
function Get-ErrorStatus($errorRecord) { if ($errorRecord.Exception.Response -and $errorRecord.Exception.Response.StatusCode) { return [int]$errorRecord.Exception.Response.StatusCode }; return 0 }

$project = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/design/projects" -Headers $headers -ContentType 'application/json' -Body (@{ name = "Design Smoke $suffix"; slug = "design-smoke-$suffix"; description = 'controlled local design smoke'; canvas_width = 1440; canvas_height = 900 } | ConvertTo-Json)
$jobBody = @{ operation = 'generate'; prompt = 'Create a restrained product dashboard frame'; source_refs = @('mock://source/workama/brief'); output_format = 'png'; idempotency_key = "design-$suffix" } | ConvertTo-Json -Depth 8
$job = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/design/projects/$($project.id)/jobs" -Headers $headers -ContentType 'application/json' -Body $jobBody
$replay = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/design/projects/$($project.id)/jobs" -Headers $headers -ContentType 'application/json' -Body $jobBody
$loaded = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/design/jobs/$($job.id)" -Headers $headers
$assets = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/design/projects/$($project.id)/assets" -Headers $headers
$artifactQuery = [uri]::EscapeDataString($job.artifact_ref)
$metadata = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/design/artifacts?artifact_ref=$artifactQuery" -Headers $headers
$downloadPath = Join-Path ([System.IO.Path]::GetTempPath()) "workama-design-$suffix.png"
$downloadClient = New-Object System.Net.WebClient
$downloadClient.Headers.Add('Authorization', $headers.Authorization)
$downloadClient.DownloadFile("$baseUrl/api/v1/design/artifacts/download?artifact_ref=$artifactQuery", $downloadPath)
$downloadHeaders = $downloadClient.ResponseHeaders
$downloadBytes = [System.IO.File]::ReadAllBytes($downloadPath)
$downloadHash = (Get-FileHash -LiteralPath $downloadPath -Algorithm SHA256).Hash.ToLowerInvariant()
$pngMagic = (($downloadBytes[0..7] | ForEach-Object { $_.ToString('X2') }) -join '') -eq '89504E470D0A1A0A'
$signaturePresent = [bool]$loaded.provenance.content_credentials.signature.value
$signatureStatus = $loaded.provenance.content_credentials.signature_status -eq 'signed_detached' -and $loaded.provenance.content_credentials.signature.status -eq 'signed_detached'
$verifierProfile = $loaded.provenance.content_credentials.verifier_profile -eq 'workama-content-credential-v1'
$standardEmbedded = $loaded.provenance.content_credentials.standard_embedded -eq $false
$downloadManifestPresent = [bool]$downloadHeaders['X-WorkAMA-Content-Credential-Manifest']
$downloadSignatureStatus = [string]$downloadHeaders['X-WorkAMA-Content-Credential-Status'] -eq 'signed_detached'
$downloadVerifierProfile = [string]$downloadHeaders['X-WorkAMA-Content-Credential-Profile'] -eq 'workama-content-credential-v1'
$downloadStandardEmbedded = [string]$downloadHeaders['X-WorkAMA-Content-Credential-Standard-Embedded'] -eq 'false'
$parentBody = @{ operation = 'edit'; prompt = 'Refine the generated frame'; source_refs = @('mock://source/workama/brief'); parent_asset_ids = @($job.asset_id); output_format = 'jpeg'; idempotency_key = "design-parent-$suffix" } | ConvertTo-Json -Depth 8
$parentJob = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/design/projects/$($project.id)/jobs" -Headers $headers -ContentType 'application/json' -Body $parentBody
$invalidStatus = 0
try { Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/design/projects/$($project.id)/jobs" -Headers $headers -ContentType 'application/json' -Body (@{ operation = 'generate'; prompt = 'unsafe'; source_refs = @('https://evil.example/image.png') } | ConvertTo-Json) | Out-Null } catch { $invalidStatus = Get-ErrorStatus $_ }
if ($job.status -ne 'succeeded' -or $replay.id -ne $job.id -or $loaded.provenance.external_provider -ne 'pending' -or -not $signaturePresent -or -not $signatureStatus -or -not $verifierProfile -or -not $standardEmbedded -or $metadata.signature_status -ne 'signed_detached' -or $metadata.verifier_profile -ne 'workama-content-credential-v1' -or $metadata.standard_embedded -ne $false -or $downloadHash -ne $job.content_sha256 -or -not $pngMagic -or -not $downloadManifestPresent -or -not $downloadSignatureStatus -or -not $downloadVerifierProfile -or -not $downloadStandardEmbedded -or $parentJob.provenance.parents[0].asset_id -ne $job.asset_id -or @($assets.items).Count -lt 1 -or $invalidStatus -ne 422) { throw "Design contract failed: status=$($job.status) replay=$($replay.id) invalid=$invalidStatus png=$pngMagic signature=$signatureStatus" }

$evidence = [ordered]@{
    timestamp = [DateTimeOffset]::UtcNow.ToString('o'); project_created = [bool]$project.id; job_succeeded = ($job.status -eq 'succeeded')
    idempotency_replayed = ($replay.id -eq $job.id); artifact_ref = $job.artifact_ref; provenance_hash_present = [bool]$job.provenance_hash
    external_provider_pending = ($loaded.provenance.external_provider -eq 'pending'); signature_status = $loaded.provenance.content_credentials.signature_status
    verifier_profile = $loaded.provenance.content_credentials.verifier_profile; standard_embedded = $loaded.provenance.content_credentials.standard_embedded
    detached_signature_present = $signaturePresent; binary_png = $pngMagic; downloaded_sha256_matches = ($downloadHash -eq $job.content_sha256)
    download_manifest_present = $downloadManifestPresent; download_signature_status = $downloadSignatureStatus; parent_claimed = ($parentJob.provenance.parents[0].asset_id -eq $job.asset_id)
    asset_count = @($assets.items).Count; unsafe_source_status = $invalidStatus
}
$evidence | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath 'quality/evidence/design-smoke.json' -Encoding utf8
$evidence | ConvertTo-Json -Depth 12
