$ErrorActionPreference = 'Stop'
$values = @{}
Get-Content -LiteralPath '.env' -Encoding utf8 | ForEach-Object { if ($_ -match '^([^#=]+)=(.*)$') { $values[$matches[1].Trim()] = $matches[2].Trim() } }
if (-not $values.TEST_ACCOUNT_EMAIL -or -not $values.TEST_ACCOUNT_PASSWORD) { throw 'TEST_ACCOUNT_EMAIL and TEST_ACCOUNT_PASSWORD are required.' }
$apiBase = if ($env:WORKAMA_BASE_URL) { $env:WORKAMA_BASE_URL.TrimEnd('/') } else { 'http://localhost:20200' }
$gatewayBase = if ($env:WORKAMA_GATEWAY_BASE_URL) { $env:WORKAMA_GATEWAY_BASE_URL.TrimEnd('/') } else { 'http://localhost:20202' }
$login = Invoke-RestMethod -Method Post -Uri "$apiBase/api/v1/auth/login" -ContentType 'application/json' -Body (@{ email = $values.TEST_ACCOUNT_EMAIL; password = $values.TEST_ACCOUNT_PASSWORD } | ConvertTo-Json)
$headers = @{ Authorization = "Bearer $($login.access_token)" }
$key = Invoke-RestMethod -Method Post -Uri "$apiBase/api/v1/gateway/tokens" -Headers $headers -ContentType 'application/json' -Body (@{ name = "Responses smoke $([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())"; rpm_limit = 120; tpm_limit = 100000; model_whitelist = @('workama-chat','workama-image','workama-tts','workama-stt') } | ConvertTo-Json)
$gatewayHeaders = @{ Authorization = "Bearer $($key.key)" }

$sync = Invoke-RestMethod -Method Post -Uri "$gatewayBase/v1/responses" -Headers $gatewayHeaders -ContentType 'application/json' -Body (@{ model = 'workama-chat'; input = 'Return a deterministic response.' } | ConvertTo-Json)
if ($sync.status -ne 'completed' -or -not $sync.output_text) { throw 'Synchronous Responses contract failed.' }

$streamPayload = (@{ model = 'workama-chat'; input = 'Return a streamed deterministic response.'; stream = $true } | ConvertTo-Json)
Add-Type -AssemblyName System.Net.Http
$streamClient = [System.Net.Http.HttpClient]::new()
$streamRequest = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::Post, "$gatewayBase/v1/responses")
$streamRequest.Headers.Authorization = [System.Net.Http.Headers.AuthenticationHeaderValue]::new('Bearer', $key.key)
$streamRequest.Content = [System.Net.Http.StringContent]::new($streamPayload, [System.Text.Encoding]::UTF8, 'application/json')
$streamResponse = $streamClient.SendAsync($streamRequest).GetAwaiter().GetResult()
$streamBody = $streamResponse.Content.ReadAsStringAsync().GetAwaiter().GetResult()
$streamContentType = [string]$streamResponse.Content.Headers.ContentType
$streamStatus = [int]$streamResponse.StatusCode
$streamResponse.Dispose(); $streamRequest.Dispose(); $streamClient.Dispose()
if ($streamStatus -ne 200 -or $streamContentType -notlike 'text/event-stream*' -or $streamBody -notmatch 'response\.created' -or $streamBody -notmatch 'response\.output_text\.delta' -or $streamBody -notmatch 'response\.completed' -or $streamBody -notmatch '\[DONE\]') { throw "Streaming Responses contract failed: status=$streamStatus content_type=$streamContentType body=$streamBody" }

$image = Invoke-RestMethod -Method Post -Uri "$gatewayBase/v1/images/generations" -Headers $gatewayHeaders -ContentType 'application/json' -Body (@{ model = 'workama-chat'; prompt = 'A deterministic WorkAMA product card'; n = 1 } | ConvertTo-Json)
$edit = Invoke-RestMethod -Method Post -Uri "$gatewayBase/v1/images/edits" -Headers $gatewayHeaders -ContentType 'application/json' -Body (@{ model = 'workama-chat'; prompt = 'Add a badge'; image_ref = 'mock://image/source' } | ConvertTo-Json)
$speech = Invoke-RestMethod -Method Post -Uri "$gatewayBase/v1/audio/speech" -Headers $gatewayHeaders -ContentType 'application/json' -Body (@{ model = 'workama-chat'; input = 'Hello from WorkAMA'; voice = 'alloy' } | ConvertTo-Json)
$transcription = Invoke-RestMethod -Method Post -Uri "$gatewayBase/v1/audio/transcriptions" -Headers $gatewayHeaders -ContentType 'application/json' -Body (@{ model = 'workama-chat'; input_ref = 'mock://audio/source' } | ConvertTo-Json)
if (-not $image.data[0].url -or -not $edit.data[0].url -or -not $speech.audio_ref -or -not $transcription.text) { throw 'Media compatibility contract failed.' }

$background = Invoke-RestMethod -Method Post -Uri "$gatewayBase/v1/responses" -Headers $gatewayHeaders -ContentType 'application/json' -Body (@{ model = 'workama-chat'; input = 'Complete a background response.'; background = $true } | ConvertTo-Json)
$poll = $null
for ($i = 0; $i -lt 40; $i++) {
    $poll = Invoke-RestMethod -Method Get -Uri "$gatewayBase/v1/responses/$($background.id)" -Headers $gatewayHeaders
    if ($poll.status -in @('completed','failed','cancelled')) { break }
    Start-Sleep -Milliseconds 50
}
if ($poll.status -ne 'completed' -or -not $poll.output_text) { throw "Background Responses did not complete: $($poll.status)" }

docker compose --env-file .env -f deploy/compose/docker-compose.yml restart gateway | Out-Null
$healthy = $false
for ($i = 0; $i -lt 30; $i++) {
    try { Invoke-RestMethod -Method Get -Uri "$gatewayBase/healthz" | Out-Null; $healthy = $true; break } catch { Start-Sleep -Milliseconds 250 }
}
if (-not $healthy) { throw 'Gateway did not become healthy after Responses persistence restart.' }
$recovered = Invoke-RestMethod -Method Get -Uri "$gatewayBase/v1/responses/$($background.id)" -Headers $gatewayHeaders
if ($recovered.status -ne 'completed' -or -not $recovered.output_text) { throw "Completed response was not recovered after Gateway restart: $($recovered.status)" }

$toCancel = Invoke-RestMethod -Method Post -Uri "$gatewayBase/v1/responses" -Headers $gatewayHeaders -ContentType 'application/json' -Body (@{ model = 'workama-chat'; input = 'Cancel this background response.'; background = $true } | ConvertTo-Json)
$cancel = Invoke-RestMethod -Method Post -Uri "$gatewayBase/v1/responses/$($toCancel.id)/cancel" -Headers $gatewayHeaders
$cancelAgain = Invoke-RestMethod -Method Post -Uri "$gatewayBase/v1/responses/$($toCancel.id)/cancel" -Headers $gatewayHeaders
if ($cancel.status -ne 'cancelled' -or $cancelAgain.status -ne 'cancelled') { throw 'Response cancellation was not idempotent.' }

$evidence = [ordered]@{
    timestamp = [DateTimeOffset]::UtcNow.ToString('o'); sync_completed = ($sync.status -eq 'completed')
    stream_completed = ($streamStatus -eq 200 -and $streamBody -match 'response\.completed')
    image_generation_mock = [bool]$image.data[0].url; image_edit_mock = [bool]$edit.data[0].url
    audio_speech_mock = [bool]$speech.audio_ref; audio_transcription_mock = [bool]$transcription.text
    background_accepted = ($background.status -in @('queued','in_progress','completed')); background_completed = ($poll.status -eq 'completed')
    restart_recovered = ($recovered.status -eq 'completed')
    cancel_idempotent = ($cancel.status -eq 'cancelled' -and $cancelAgain.status -eq 'cancelled')
    persistence_mode = 'file-volume'
}
$evidence | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath 'quality/evidence/responses-smoke.json' -Encoding utf8
$evidence | ConvertTo-Json -Depth 12
