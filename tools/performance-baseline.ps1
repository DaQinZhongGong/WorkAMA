[CmdletBinding()]
param(
  [string]$BaseUrl = "http://localhost:20202",
  [string]$Endpoint = "/healthz",
  [string]$Scenario,
  [string]$OutputDirectory,
  [string]$Project = "workama",
  [string]$K6Image = "grafana/k6:0.52.0",
  [ValidateRange(1, 100000)]
  [int]$VUs = 1,
  [string]$Duration = "30s",
  [switch]$UseLocalK6
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$defaultScenario = Join-Path $root "quality/performance/k6-baseline.js"
$defaultOutputRoot = Join-Path $root "quality/performance/results"

function Normalize-Project([string]$value) {
  if ([string]::IsNullOrWhiteSpace($value)) { return "workama" }
  $normalized = $value.Trim().ToLowerInvariant()
  if ($normalized -notmatch '^workama(?:$|[-_][a-z0-9][a-z0-9_-]*)$') {
    throw "Invalid Compose project '$value'. Use 'workama' or a workama-prefixed variant such as 'workama-ci'."
  }
  return $normalized
}

function Resolve-UserPath([string]$path, [string]$fallback) {
  if ([string]::IsNullOrWhiteSpace($path)) { return $fallback }
  if ([System.IO.Path]::IsPathRooted($path)) { return [System.IO.Path]::GetFullPath($path) }
  return [System.IO.Path]::GetFullPath((Join-Path (Get-Location).Path $path))
}

function Invoke-NativeChecked([string]$FilePath, [string[]]$Arguments) {
  $previousErrorAction = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $output = @(& $FilePath @Arguments 2>&1)
    $exitCode = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $previousErrorAction
  }
  $lines = @($output | ForEach-Object { if ($_ -ne $null) { [string]$_ } })
  if ($exitCode -ne 0) {
    $detail = "(no output)"
    if ($lines.Count -gt 0) { $detail = $lines -join [Environment]::NewLine }
    throw "Command failed with exit code ${exitCode}: $FilePath $($Arguments -join ' ')`n$detail"
  }
  return $lines
}

function Convert-BaseUrlForDocker([string]$url) {
  $uri = $null
  if (-not [Uri]::TryCreate($url, [UriKind]::Absolute, [ref]$uri) -or $uri.Scheme -notin @("http", "https")) {
    throw "BaseUrl must be an absolute http or https URL: $url"
  }
  if ($uri.UserInfo) { throw "BaseUrl must not include credentials." }
  if ($uri.Host -eq "localhost" -or $uri.Host -eq "127.0.0.1") {
    $builder = New-Object System.UriBuilder($uri)
    $builder.Host = "host.docker.internal"
    return $builder.Uri.AbsoluteUri.TrimEnd('/')
  }
  return $url.TrimEnd('/')
}

function Set-K6Environment([string]$baseUrl, [string]$runProject) {
  $env:K6_BASE_URL = $baseUrl
  $env:K6_ENDPOINT = $Endpoint
  $env:K6_VUS = [string]$VUs
  $env:K6_DURATION = $Duration
  $env:K6_PROJECT = $runProject
}

function Restore-Environment([hashtable]$previous) {
  foreach ($key in $previous.Keys) {
    if ($null -eq $previous[$key]) {
      Remove-Item -LiteralPath "Env:$key" -ErrorAction SilentlyContinue
    } else {
      Set-Item -LiteralPath "Env:$key" -Value $previous[$key]
    }
  }
}

function Invoke-LocalK6([string]$scenarioPath, [string]$baseUrl, [string]$runProject, [string]$summaryPath, [string]$rawPath, [string]$logPath) {
  if (-not (Get-Command k6 -ErrorAction SilentlyContinue)) {
    throw "k6 was not found in PATH. Install k6, or run without -UseLocalK6 to use the k6 Docker image. No result was generated."
  }
  $keys = @("K6_BASE_URL", "K6_ENDPOINT", "K6_VUS", "K6_DURATION", "K6_PROJECT")
  $previous = @{}
  foreach ($key in $keys) {
    $item = Get-Item -LiteralPath "Env:$key" -ErrorAction SilentlyContinue
    $previous[$key] = if ($item) { $item.Value } else { $null }
  }
  try {
    Set-K6Environment -baseUrl $baseUrl -runProject $runProject
    $output = @(Invoke-NativeChecked -FilePath "k6" -Arguments @("run", "--summary-export", $summaryPath, "--out", "json=$rawPath", $scenarioPath))
    Set-Content -LiteralPath $logPath -Value $output -Encoding utf8
  } catch {
    if (-not (Test-Path -LiteralPath $logPath)) { Set-Content -LiteralPath $logPath -Value $_.Exception.Message -Encoding utf8 }
    throw "Local k6 baseline failed. See $logPath. No result was generated. $($_.Exception.Message)"
  } finally {
    Restore-Environment $previous
  }
  return "local"
}

function Invoke-DockerK6([string]$scenarioPath, [string]$baseUrl, [string]$runProject, [string]$summaryPath, [string]$rawPath, [string]$logPath, [string]$outputDirectory) {
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker CLI was not found in PATH. Start Docker or use -UseLocalK6 with a local k6 installation. No result was generated."
  }
  try {
    Invoke-NativeChecked -FilePath "docker" -Arguments @("info", "--format", "{{json .ServerVersion}}") | Out-Null
  } catch {
    throw "Docker Engine is unavailable for the k6 baseline. Start Docker and retry. No result was generated. $($_.Exception.Message)"
  }

  $scenarioDirectory = Split-Path -Parent $scenarioPath
  $scenarioName = Split-Path -Leaf $scenarioPath
  $containerName = "workama-performance-k6-" + (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssfffZ")
  $scenarioMount = "$scenarioDirectory`:/scripts:ro"
  $outputMount = "$outputDirectory`:/results"
  $arguments = @(
    "run", "--rm", "--name", $containerName,
    "--label", "com.workama.tool=performance-baseline",
    "--label", "com.workama.compose.project=$runProject",
    "--add-host", "host.docker.internal:host-gateway",
    "-e", "K6_BASE_URL=$baseUrl",
    "-e", "K6_ENDPOINT=$Endpoint",
    "-e", "K6_VUS=$VUs",
    "-e", "K6_DURATION=$Duration",
    "-e", "K6_PROJECT=$runProject",
    "-v", $scenarioMount,
    "-v", $outputMount,
    $K6Image, "run", "--summary-export", "/results/summary.json", "--out", "json=/results/raw.json", "/scripts/$scenarioName"
  )
  try {
    $output = @(Invoke-NativeChecked -FilePath "docker" -Arguments $arguments)
    Set-Content -LiteralPath $logPath -Value $output -Encoding utf8
  } catch {
    if (-not (Test-Path -LiteralPath $logPath)) { Set-Content -LiteralPath $logPath -Value $_.Exception.Message -Encoding utf8 }
    throw "k6 Docker baseline failed. Check Docker access and image '$K6Image'. See $logPath. No result was generated. $($_.Exception.Message)"
  }
  return "docker"
}

function Get-ThresholdFailures($metrics) {
  $failures = @()
  foreach ($metricProperty in $metrics.PSObject.Properties) {
    $metric = $metricProperty.Value
    if (-not $metric.thresholds) { continue }
    foreach ($thresholdProperty in $metric.thresholds.PSObject.Properties) {
      $expression = [string]$thresholdProperty.Name
      if ($expression -notmatch '^(rate|p\(\d+\)|avg|max|min|med)(<=|>=|<|>)(-?[0-9]+(?:\.[0-9]+)?)$') {
        $failures += "$($metricProperty.Name): unsupported threshold '$expression'"
        continue
      }
      $selector = $matches[1]
      $operator = $matches[2]
      $expected = [double]$matches[3]
      $valueProperty = $metric.PSObject.Properties[$selector]
      if ($selector -eq 'rate') { $valueProperty = $metric.PSObject.Properties['value'] }
      if (-not $valueProperty) {
        $failures += "$($metricProperty.Name): threshold '$expression' has no value"
        continue
      }
      $actual = [double]$valueProperty.Value
      $passed = switch ($operator) {
        '<' { $actual -lt $expected }
        '<=' { $actual -le $expected }
        '>' { $actual -gt $expected }
        '>=' { $actual -ge $expected }
      }
      if (-not $passed) { $failures += "$($metricProperty.Name): $selector $actual does not satisfy $expression" }
    }
  }
  return $failures
}

$Project = Normalize-Project $Project
if ([string]::IsNullOrWhiteSpace($Scenario)) { $Scenario = $defaultScenario }
$Scenario = Resolve-UserPath $Scenario $defaultScenario
if (-not (Test-Path -LiteralPath $Scenario -PathType Leaf)) { throw "k6 scenario was not found: $Scenario" }

$OutputDirectory = Resolve-UserPath $OutputDirectory (Join-Path $defaultOutputRoot ((Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")))
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null

$baseUri = $null
if (-not [Uri]::TryCreate($BaseUrl, [UriKind]::Absolute, [ref]$baseUri) -or $baseUri.Scheme -notin @("http", "https")) {
  throw "BaseUrl must be an absolute http or https URL: $BaseUrl"
}
if ($baseUri.UserInfo) { throw "BaseUrl must not include credentials." }
if ([string]::IsNullOrWhiteSpace($Endpoint) -or $Endpoint[0] -ne "/") { throw "Endpoint must be an absolute path beginning with '/'." }
if ($Duration -notmatch '^[0-9]+(ms|s|m|h)([0-9]+(ms|s|m|h))*$') { throw "Duration is not a valid k6 duration: $Duration" }

$summaryPath = Join-Path $OutputDirectory "summary.json"
$rawPath = Join-Path $OutputDirectory "raw.json"
$logPath = Join-Path $OutputDirectory "runner.log"
$resultPath = Join-Path $OutputDirectory "baseline-result.json"
$startedAt = (Get-Date).ToUniversalTime()
$containerBaseUrl = Convert-BaseUrlForDocker $BaseUrl
$runner = if ($UseLocalK6) {
  Invoke-LocalK6 -scenarioPath $Scenario -baseUrl $BaseUrl -runProject $Project -summaryPath $summaryPath -rawPath $rawPath -logPath $logPath
} else {
  Invoke-DockerK6 -scenarioPath $Scenario -baseUrl $containerBaseUrl -runProject $Project -summaryPath $summaryPath -rawPath $rawPath -logPath $logPath -outputDirectory $OutputDirectory
}

if (-not (Test-Path -LiteralPath $summaryPath -PathType Leaf)) {
  throw "k6 exited successfully but did not produce $summaryPath. No result was generated."
}
try {
  $summary = Get-Content -LiteralPath $summaryPath -Raw | ConvertFrom-Json
} catch {
  throw "k6 summary is not valid JSON: $summaryPath. No result was generated."
}
if (-not $summary.metrics) { throw "k6 summary contains no metrics: $summaryPath. No result was generated." }
$thresholdFailures = @(Get-ThresholdFailures $summary.metrics)

$finishedAt = (Get-Date).ToUniversalTime()
$result = [ordered]@{
  schema_version = "workama.performance.baseline.v1"
  status = if ($thresholdFailures.Count -eq 0) { "passed" } else { "failed" }
  run_id = Split-Path -Leaf $OutputDirectory
  runner = $runner
  project = $Project
  base_url = $BaseUrl.TrimEnd('/')
  endpoint = $Endpoint
  vus = $VUs
  duration = $Duration
  scenario = "k6-baseline.js"
  k6_image = if ($runner -eq "docker") { $K6Image } else { $null }
  started_at = $startedAt.ToString("o")
  finished_at = $finishedAt.ToString("o")
  artifacts = @("summary.json", "raw.json", "runner.log")
  threshold_failures = $thresholdFailures
}
$result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $resultPath -Encoding utf8
if ($thresholdFailures.Count -gt 0) {
  throw "Performance baseline thresholds failed: $($thresholdFailures -join '; ')"
}
Write-Host "Performance baseline passed."
Write-Host "Runner: $runner"
Write-Host "Result: $resultPath"
