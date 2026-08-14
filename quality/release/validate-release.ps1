[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$Manifest,
  [Parameter(Mandatory = $true)]
  [string]$EvidenceDirectory,
  [switch]$VerifyDockerImage
)

$ErrorActionPreference = "Stop"

function Get-RequiredProperty($object, [string]$name, [string]$path) {
  if ($null -eq $object -or -not ($object.PSObject.Properties.Name -contains $name)) {
    throw "Missing required property '$path'."
  }
  return $object.$name
}

function Assert-ConcreteString([string]$value, [string]$path) {
  if ([string]::IsNullOrWhiteSpace($value) -or $value -match '^<.*>$' -or $value -match '^(TODO|TBD|REPLACE|CHANGEME)') {
    throw "Property '$path' must contain a concrete value."
  }
}

function Assert-Sha256([string]$value, [string]$path) {
  Assert-ConcreteString $value $path
  if ($value -notmatch '^sha256:[0-9a-fA-F]{64}$') { throw "Property '$path' must be a sha256 digest." }
}

function Read-JsonFile([string]$path, [string]$description) {
  if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "$description was not found: $path" }
  try {
    return (Get-Content -LiteralPath $path -Raw | ConvertFrom-Json)
  } catch {
    throw "$description is not valid JSON: $path. $($_.Exception.Message)"
  }
}

function Resolve-EvidenceFile([string]$relativePath, [string]$field, [string]$rootPath) {
  Assert-ConcreteString $relativePath $field
  if ([System.IO.Path]::IsPathRooted($relativePath) -or $relativePath -match '(^|[\\/])\.\.([\\/]|$)') {
    throw "Evidence path '$field' must be a relative path below '$rootPath'."
  }
  $fullRoot = [System.IO.Path]::GetFullPath($rootPath).TrimEnd([char[]]@('\', '/'))
  $fullPath = [System.IO.Path]::GetFullPath((Join-Path $fullRoot $relativePath))
  $prefix = $fullRoot + [System.IO.Path]::DirectorySeparatorChar
  if (-not $fullPath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Evidence path '$field' escapes '$rootPath'."
  }
  return $fullPath
}

function Get-FirstJsonObject($document) {
  if ($document -is [Array]) { return $document[0] }
  return $document
}

function Get-StringArray($value) {
  if ($null -eq $value) { return @() }
  return @($value | ForEach-Object { [string]$_ })
}

function Assert-PassedStatus($object, [string]$path) {
  $status = [string](Get-RequiredProperty $object "status" $path)
  if ($status.ToLowerInvariant() -ne "passed") { throw "Evidence '$path.status' must be 'passed', got '$status'." }
}

function Invoke-DockerInspect([string]$reference) {
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "Docker CLI was not found; cannot verify image '$reference'." }
  $previousErrorAction = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $output = @(& docker image inspect $reference 2>&1)
    $exitCode = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $previousErrorAction
  }
  if ($exitCode -ne 0) {
    throw "Docker image inspect failed for '$reference' with exit code ${exitCode}: $($output -join [Environment]::NewLine)"
  }
  return Get-FirstJsonObject (($output -join [Environment]::NewLine) | ConvertFrom-Json)
}

$manifestPath = [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $Manifest -ErrorAction Stop).Path)
$evidenceRoot = [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $EvidenceDirectory -ErrorAction Stop).Path)
$manifestObject = Read-JsonFile $manifestPath "Release manifest"

$version = [string](Get-RequiredProperty $manifestObject "version" "version")
Assert-ConcreteString $version "version"
if ($version -notmatch '^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$') { throw "Property 'version' is not SemVer: $version" }

$commit = [string](Get-RequiredProperty $manifestObject "commit" "commit")
Assert-ConcreteString $commit "commit"
if ($commit -notmatch '^[0-9a-fA-F]{7,64}$') { throw "Property 'commit' must be a git commit id." }

foreach ($field in @("build_id", "builder_identity", "artifact_name", "signature", "min_server_version", "max_schema_version")) {
  Assert-ConcreteString ([string](Get-RequiredProperty $manifestObject $field $field)) $field
}
foreach ($field in @("source_digest", "dependency_lock_digest", "artifact_digest", "sbom_digest", "provenance_digest")) {
  Assert-Sha256 ([string](Get-RequiredProperty $manifestObject $field $field)) $field
}
$createdAt = [string](Get-RequiredProperty $manifestObject "created_at" "created_at")
Assert-ConcreteString $createdAt "created_at"
try { [DateTimeOffset]::Parse($createdAt) | Out-Null } catch { throw "Property 'created_at' must be an ISO-8601 timestamp." }

$migrationIds = Get-StringArray (Get-RequiredProperty $manifestObject "migration_ids" "migration_ids")
foreach ($migrationId in $migrationIds) { Assert-ConcreteString $migrationId "migration_ids" }
$featureFlags = Get-StringArray (Get-RequiredProperty $manifestObject "feature_flags" "feature_flags")
foreach ($flag in $featureFlags) { Assert-ConcreteString $flag "feature_flags" }

$rollbackVersion = [string](Get-RequiredProperty $manifestObject "rollback_version" "rollback_version")
Assert-ConcreteString $rollbackVersion "rollback_version"
if ($rollbackVersion -notmatch '^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$') { throw "Property 'rollback_version' is not SemVer: $rollbackVersion" }

$image = Get-RequiredProperty $manifestObject "image" "image"
$imageReference = [string](Get-RequiredProperty $image "reference" "image.reference")
$imageDigest = [string](Get-RequiredProperty $image "digest" "image.digest")
$imageEvidenceName = [string](Get-RequiredProperty $image "evidence_file" "image.evidence_file")
Assert-ConcreteString $imageReference "image.reference"
Assert-Sha256 $imageDigest "image.digest"
$imageEvidencePath = Resolve-EvidenceFile $imageEvidenceName "image.evidence_file" $evidenceRoot
$imageEvidence = Get-FirstJsonObject (Read-JsonFile $imageEvidencePath "Image inspection evidence")
$imageEvidenceReference = [string](Get-RequiredProperty $imageEvidence "reference" "image evidence.reference")
if ($imageEvidenceReference -ne $imageReference) { throw "Image evidence reference '$imageEvidenceReference' does not match '$imageReference'." }
$imageEvidenceDigest = [string](Get-RequiredProperty $imageEvidence "digest" "image evidence.digest")
if ($imageEvidenceDigest -ne $imageDigest) { throw "Image evidence digest '$imageEvidenceDigest' does not match '$imageDigest'." }
$imageEvidenceStatus = [string](Get-RequiredProperty $imageEvidence "status" "image evidence.status")
if ($imageEvidenceStatus.ToLowerInvariant() -ne "passed") { throw "Image evidence status must be 'passed'." }

if ($VerifyDockerImage) {
  $dockerImage = Invoke-DockerInspect $imageReference
  $dockerDigests = @(Get-StringArray $dockerImage.RepoDigests)
  $dockerId = [string]$dockerImage.Id
  if ($dockerId -ne $imageDigest -and -not ($dockerDigests | Where-Object { $_ -like "*@$imageDigest" })) {
    throw "Docker image '$imageReference' does not expose the manifest digest '$imageDigest'."
  }
}

$migration = Get-RequiredProperty $manifestObject "migration" "migration"
$migrationDryRun = Get-RequiredProperty $migration "dry_run" "migration.dry_run"
if ($migrationDryRun -ne $true) { throw "Property 'migration.dry_run' must be true." }
$migrationCompatible = Get-RequiredProperty $migration "compatible_with_n_minus_one" "migration.compatible_with_n_minus_one"
if ($migrationCompatible -ne $true) { throw "Property 'migration.compatible_with_n_minus_one' must be true." }
Assert-ConcreteString ([string](Get-RequiredProperty $migration "strategy" "migration.strategy")) "migration.strategy"
$migrationEvidenceName = [string](Get-RequiredProperty $migration "evidence_file" "migration.evidence_file")
$migrationEvidencePath = Resolve-EvidenceFile $migrationEvidenceName "migration.evidence_file" $evidenceRoot
$migrationEvidence = Get-FirstJsonObject (Read-JsonFile $migrationEvidencePath "Migration dry-run evidence")
Assert-PassedStatus $migrationEvidence "migration evidence"
if ((Get-RequiredProperty $migrationEvidence "dry_run" "migration evidence.dry_run") -ne $true) { throw "Migration evidence must record dry_run=true." }
if ((Get-RequiredProperty $migrationEvidence "compatible_with_n_minus_one" "migration evidence.compatible_with_n_minus_one") -ne $true) { throw "Migration evidence must record N-1 compatibility." }
$evidenceMigrationIds = Get-StringArray (Get-RequiredProperty $migrationEvidence "migration_ids" "migration evidence.migration_ids")
if (($migrationIds -join ",") -ne ($evidenceMigrationIds -join ",")) { throw "Migration evidence ids do not match manifest migration_ids." }
Assert-ConcreteString ([string](Get-RequiredProperty $migrationEvidence "rollback_plan" "migration evidence.rollback_plan")) "migration evidence.rollback_plan"

$rollback = Get-RequiredProperty $manifestObject "rollback" "rollback"
if ((Get-RequiredProperty $rollback "verified" "rollback.verified") -ne $true) { throw "Property 'rollback.verified' must be true." }
Assert-ConcreteString ([string](Get-RequiredProperty $rollback "strategy" "rollback.strategy")) "rollback.strategy"
$rollbackEvidenceName = [string](Get-RequiredProperty $rollback "evidence_file" "rollback.evidence_file")
$rollbackEvidencePath = Resolve-EvidenceFile $rollbackEvidenceName "rollback.evidence_file" $evidenceRoot
$rollbackEvidence = Get-FirstJsonObject (Read-JsonFile $rollbackEvidencePath "Rollback evidence")
Assert-PassedStatus $rollbackEvidence "rollback evidence"
if ([string](Get-RequiredProperty $rollbackEvidence "rollback_version" "rollback evidence.rollback_version") -ne $rollbackVersion) { throw "Rollback evidence version does not match manifest rollback_version." }
if ([string](Get-RequiredProperty $rollbackEvidence "data_preservation_check" "rollback evidence.data_preservation_check").ToLowerInvariant() -ne "passed") { throw "Rollback evidence must record data_preservation_check=passed." }
Assert-ConcreteString ([string](Get-RequiredProperty $rollbackEvidence "migration_handling" "rollback evidence.migration_handling")) "rollback evidence.migration_handling"

Write-Host "Release evidence check passed."
Write-Host "Version: $version"
Write-Host "Image: $imageReference@$imageDigest"
Write-Host "Migration dry-run: passed ($($migrationIds.Count) migration ids)"
Write-Host "Rollback: verified ($rollbackVersion)"
