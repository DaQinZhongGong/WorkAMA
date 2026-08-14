$ErrorActionPreference = 'Stop'

$suffix = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$email = "smoke-$suffix@example.com"
$registerBody = @{ email = $email; password = 'Workama-Smoke-2026!'; display_name = 'Smoke Test' } | ConvertTo-Json
$registration = Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/api/v1/auth/register' -ContentType 'application/json' -Body $registerBody
$auth = Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/api/v1/auth/verify-email' -ContentType 'application/json' -Body (@{ token = $registration.debug_token } | ConvertTo-Json)
$headers = @{ Authorization = "Bearer $($auth.access_token)" }

Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/api/v1/auth/onboarding' -Headers $headers -ContentType 'application/json' -Body (@{
  user_role = 'developer'; primary_goal = 'gateway'; team_size = '1'; data_sensitivity = 'standard';
  preferred_model = 'workama-chat'; notification_preference = 'in_app'
} | ConvertTo-Json) | Out-Null

$gatewayToken = Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/api/v1/gateway/tokens' -Headers $headers -ContentType 'application/json' -Body (@{
  name = 'Smoke Key'; rpm_limit = 60; tpm_limit = 100000; model_whitelist = @('workama-chat', 'workama-embed')
} | ConvertTo-Json)
$gatewayHeaders = @{ Authorization = "Bearer $($gatewayToken.key)" }
$models = Invoke-RestMethod -Method Get -Uri 'http://localhost:20202/v1/models' -Headers $gatewayHeaders
$completion = Invoke-RestMethod -Method Post -Uri 'http://localhost:20202/v1/chat/completions' -Headers $gatewayHeaders -ContentType 'application/json' -Body (@{
  model = 'workama-chat'; messages = @(@{ role = 'user'; content = 'Return a smoke-test response.' }); stream = $false
} | ConvertTo-Json -Depth 8)

$session = Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/api/v1/sessions' -Headers $headers -ContentType 'application/json' -Body '{"title":"Smoke session","model":"workama-chat"}'
$ticket = Invoke-RestMethod -Method Post -Uri "http://localhost:20200/api/v1/sessions/$($session.id)/ws-tickets" -Headers $headers
$wsUrl = "ws://localhost:20201/ws/sessions/$($session.id)?ticket=$([uri]::EscapeDataString($ticket.ticket))"
$wsResult = node tools/ws-smoke.mjs $wsUrl 'Create an integration response.' | ConvertFrom-Json
$billing = Invoke-RestMethod -Method Get -Uri 'http://localhost:20200/api/v1/billing/account' -Headers $headers
$logs = Invoke-RestMethod -Method Get -Uri 'http://localhost:20200/api/v1/gateway/logs' -Headers $headers

$evidence = @{
  timestamp = [DateTimeOffset]::UtcNow.ToString('o')
  user = $email
  model_count = $models.data.Count
  completion_ok = [bool]$completion.choices[0].message.content
  websocket_ok = [bool]$wsResult.ok
  request_log_count = $logs.items.Count
  remaining_credits = $billing.total_balance
}
$evidence | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8 quality/evidence/smoke.json
$evidence | ConvertTo-Json -Depth 8
