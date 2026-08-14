$ErrorActionPreference = 'Stop'

$values = @{}
Get-Content -LiteralPath '.env' -Encoding utf8 | ForEach-Object {
    if ($_ -match '^([^#=]+)=(.*)$') { $values[$matches[1].Trim()] = $matches[2].Trim() }
}
if (-not $values.TEST_ACCOUNT_EMAIL -or -not $values.TEST_ACCOUNT_PASSWORD) { throw 'TEST_ACCOUNT_EMAIL and TEST_ACCOUNT_PASSWORD are required.' }
$login = Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/api/v1/auth/login' -ContentType 'application/json' -Body (@{ email = $values.TEST_ACCOUNT_EMAIL; password = $values.TEST_ACCOUNT_PASSWORD } | ConvertTo-Json)
$headers = @{ Authorization = "Bearer $($login.access_token)" }
$suffix = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$channel = @{
    name = "Imported Ollama $suffix"
    provider = 'ollama'
    base_url = 'https://ollama.example.com/v1'
    key = 'import-secret-not-returned'
    models = @('llama3.2')
}
$dryRun = Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/api/v1/gateway/channels/import' -Headers $headers -ContentType 'application/json' -Body (@{ source = 'one-api'; channels = @($channel); dry_run = $true } | ConvertTo-Json -Depth 10)
if (@($dryRun.candidates).Count -ne 1 -or $dryRun.candidates[0].has_credential -ne $true -or $dryRun.candidates[0].PSObject.Properties.Name -contains 'api_key') { throw 'Gateway import dry-run leaked or lost candidate data.' }
$created = Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/api/v1/gateway/channels/import' -Headers $headers -ContentType 'application/json' -Body (@{ source = 'new-api'; channels = @($channel); dry_run = $false } | ConvertTo-Json -Depth 10)
if (@($created.created).Count -ne 1 -or $created.created[0].status -ne 'disabled' -or $created.created[0].has_credential -ne $true) { throw 'Gateway channel import did not create a disabled encrypted channel.' }

$evidence = @{ timestamp = [DateTimeOffset]::UtcNow.ToString('o'); dry_run_candidates = @($dryRun.candidates).Count; created_channels = @($created.created).Count; imported_status = $created.created[0].status; credential_not_returned = ($dryRun.candidates[0].PSObject.Properties.Name -notcontains 'api_key') }
$evidence | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath 'quality/evidence/gateway-import-smoke.json' -Encoding utf8
$evidence | ConvertTo-Json -Depth 8
