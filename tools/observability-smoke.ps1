$ErrorActionPreference = 'Stop'

$metrics = Invoke-WebRequest -UseBasicParsing -Uri 'http://localhost:20242/metrics'
if ($metrics.StatusCode -ne 200 -or $metrics.Content -notmatch 'wama_platform_api_http_requests_total') { throw 'OTel Prometheus metrics are not exported.' }
$promReady = Invoke-WebRequest -UseBasicParsing -Uri 'http://localhost:20231/-/ready'
if ($promReady.StatusCode -ne 200) { throw 'Prometheus is not ready.' }
$query = Invoke-RestMethod -Method Get -Uri 'http://localhost:20231/api/v1/query?query=up%7Bjob%3D%22workama-otel%22%7D'
if ($query.status -ne 'success' -or @($query.data.result).Count -lt 1) { throw 'Prometheus did not scrape the WorkAMA OTel target.' }
$rules = Invoke-RestMethod -Method Get -Uri 'http://localhost:20231/api/v1/rules'
$ruleNames = @($rules.data.groups | ForEach-Object { $_.rules } | ForEach-Object { $_.name })
if ($ruleNames -notcontains 'workama:sli:gateway:burn_rate_5m' -or $ruleNames -notcontains 'WorkamaGatewayErrorBudgetFastBurn') { throw 'WorkAMA SLI/error-budget rules are not loaded.' }
$grafana = Invoke-RestMethod -Method Get -Uri 'http://localhost:20230/api/health'
if ($grafana.database -ne 'ok') { throw 'Grafana health check failed.' }
$dashboard = Invoke-RestMethod -Method Get -Uri 'http://localhost:20230/api/dashboards/uid/workama-overview'
if ($dashboard.dashboard.uid -ne 'workama-overview' -or @($dashboard.dashboard.panels).Count -ne 6) { throw 'WorkAMA Grafana dashboard is not provisioned with six panels.' }

$evidence = [ordered]@{
    timestamp = [DateTimeOffset]::UtcNow.ToString('o')
    otel_metrics = $true
    prometheus_ready = $true
    prometheus_targets = @($query.data.result).Count
    recording_rules = @($ruleNames | Where-Object { $_ -like 'workama:sli:*' }).Count
    alert_rules = @($ruleNames | Where-Object { $_ -like 'Workama*' }).Count
    grafana_ready = $true
    dashboard_uid = $dashboard.dashboard.uid
    dashboard_panel_count = @($dashboard.dashboard.panels).Count
}
$evidence | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath 'quality/evidence/observability-smoke.json' -Encoding utf8
$evidence | ConvertTo-Json -Depth 10
