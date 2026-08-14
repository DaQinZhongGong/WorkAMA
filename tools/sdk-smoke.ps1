$ErrorActionPreference = 'Stop'

$suffix = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$registration = Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/api/v1/auth/register' -ContentType 'application/json' -Body (@{
  email = "sdk-$suffix@example.com"; password = 'WorkAMA-SDK-2026!'; display_name = 'SDK Test'
} | ConvertTo-Json)
$auth = Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/api/v1/auth/verify-email' -ContentType 'application/json' -Body (@{ token = $registration.debug_token } | ConvertTo-Json)
$headers = @{ Authorization = "Bearer $($auth.access_token)" }
$token = Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/api/v1/gateway/tokens' -Headers $headers -ContentType 'application/json' -Body (@{
  name = 'Official SDK Key'; rpm_limit = 30; tpm_limit = 100000; model_whitelist = @('workama-chat', 'workama-embed')
} | ConvertTo-Json)

docker build -f quality/sdk/python/Dockerfile -t workama-sdk-python .
if ($LASTEXITCODE -ne 0) { throw 'Python SDK image build failed' }
$pythonRaw = docker run --rm --network workama_default -e "WORKAMA_API_KEY=$($token.key)" -e 'WORKAMA_BASE_URL=http://gateway:8080/v1' workama-sdk-python
if ($LASTEXITCODE -ne 0) { throw 'Python SDK smoke test failed' }

docker build -f quality/sdk/node/Dockerfile -t workama-sdk-node .
if ($LASTEXITCODE -ne 0) { throw 'Node SDK image build failed' }
$nodeRaw = docker run --rm --network workama_default -e "WORKAMA_API_KEY=$($token.key)" -e 'WORKAMA_BASE_URL=http://gateway:8080/v1' workama-sdk-node
if ($LASTEXITCODE -ne 0) { throw 'Node SDK smoke test failed' }

$evidence = @{
  timestamp = [DateTimeOffset]::UtcNow.ToString('o')
  python = (($pythonRaw -join [Environment]::NewLine) | ConvertFrom-Json)
  node = (($nodeRaw -join [Environment]::NewLine) | ConvertFrom-Json)
}
$evidence | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8 quality/evidence/sdk-smoke.json
$evidence | ConvertTo-Json -Depth 8
