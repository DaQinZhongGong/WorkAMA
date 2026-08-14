[CmdletBinding()]
param(
    [switch]$RunSmoke
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$RepoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../..'))
$ToolPath = Join-Path $RepoRoot 'tools/community-release.ps1'
$RequiredFiles = @(
    'LICENSE',
    'CONTRIBUTING.md',
    'SECURITY.md',
    'RELEASE.md',
    'docs/README.md',
    'docs/community/README.md',
    'docs/community/CONTRIBUTING.md',
    'docs/community/SECURITY.md',
    'docs/community/RELEASE.md',
    'tools/community-release.ps1',
    'quality/release/community/README.md',
    'quality/release/community/package-manifest.template.json',
    'quality/release/community/package-manifest.schema.json',
    'quality/release/community/provenance.template.json',
    'quality/release/community/provenance.schema.json',
    'quality/release/community/sbom-validation.template.json',
    'quality/release/community/sbom-validation.schema.json',
    'quality/release/community/smoke-result.template.json',
    'quality/release/community/smoke-result.schema.json'
)

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) {
        throw $Message
    }
}

function Read-Json {
    param([string]$Path)
    try {
        return Get-Content -LiteralPath $Path -Raw -Encoding utf8 | ConvertFrom-Json
    }
    catch {
        throw "Invalid JSON in $Path`: $($_.Exception.Message)"
    }
}

function Assert-PowerShellSyntax {
    param([string]$Path)
    $tokens = $null
    $errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errors) | Out-Null
    Assert-True ($errors.Count -eq 0) "PowerShell syntax errors in $Path`: $($errors -join '; ')"
}

foreach ($relativePath in $RequiredFiles) {
    $path = Join-Path $RepoRoot $relativePath
    Assert-True (Test-Path -LiteralPath $path -PathType Leaf) "Required community release file is missing: $relativePath"
}

Assert-PowerShellSyntax $ToolPath
Assert-PowerShellSyntax (Join-Path $PSScriptRoot 'test-community-release.ps1')

$licenseText = Get-Content -LiteralPath (Join-Path $RepoRoot 'LICENSE') -Raw -Encoding utf8
Assert-True ($licenseText -match 'Apache License' -and $licenseText -match 'Version 2\.0') 'Root LICENSE must be Apache License 2.0.'

$manifestTemplate = Read-Json (Join-Path $PSScriptRoot 'package-manifest.template.json')
Assert-True ([string]$manifestTemplate.schema_version -eq 'workama.community.package-manifest.v1') 'Unexpected package manifest schema.'
Assert-True ([bool]$manifestTemplate.local_evidence.signed -eq $false) 'Package manifest template must remain unsigned.'
Assert-True ([bool]$manifestTemplate.local_evidence.production_attestation -eq $false) 'Package manifest template must not claim production attestation.'
Assert-True ([string]$manifestTemplate.sbom.status -eq 'not-generated') 'Package manifest template must not claim an ungenerated SBOM.'

$provenanceTemplate = Read-Json (Join-Path $PSScriptRoot 'provenance.template.json')
Assert-True ([bool]$provenanceTemplate.local_only -eq $true) 'Provenance template must be local-only.'
Assert-True ([bool]$provenanceTemplate.signed -eq $false) 'Provenance template must remain unsigned.'
Assert-True ([bool]$provenanceTemplate.production_attestation -eq $false) 'Provenance template must not claim production attestation.'

$sbomTemplate = Read-Json (Join-Path $PSScriptRoot 'sbom-validation.template.json')
Assert-True ([string]$sbomTemplate.format -eq 'CycloneDX JSON') 'SBOM validation template must name its format.'
Assert-True ([bool]$sbomTemplate.is_sbom -eq $false) 'An unrun SBOM validation template must not claim an SBOM.'
Assert-True ([bool]$sbomTemplate.signed -eq $false) 'SBOM validation template must remain unsigned.'

[void](Read-Json (Join-Path $PSScriptRoot 'package-manifest.schema.json'))
[void](Read-Json (Join-Path $PSScriptRoot 'provenance.schema.json'))
[void](Read-Json (Join-Path $PSScriptRoot 'sbom-validation.schema.json'))
$smokeTemplate = Read-Json (Join-Path $PSScriptRoot 'smoke-result.template.json')
Assert-True ([string]$smokeTemplate.evidence_scope -eq 'local-only') 'Smoke template must be local-only.'
Assert-True ([bool]$smokeTemplate.containers_started -eq $false) 'Smoke template must not start containers.'
Assert-True ([bool]$smokeTemplate.signed -eq $false) 'Smoke template must remain unsigned.'
[void](Read-Json (Join-Path $PSScriptRoot 'smoke-result.schema.json'))

$docs = @(
    (Get-Content -LiteralPath (Join-Path $RepoRoot 'docs/community/README.md') -Raw -Encoding utf8),
    (Get-Content -LiteralPath (Join-Path $RepoRoot 'docs/community/RELEASE.md') -Raw -Encoding utf8),
    (Get-Content -LiteralPath (Join-Path $RepoRoot 'quality/release/community/README.md') -Raw -Encoding utf8)
) -join "`n"
Assert-True ($docs -match '(?i)local.*not.*production' -or $docs -match '(?i)not.*production.*signature') 'Community docs must distinguish local evidence from production signing.'
Assert-True ($docs -match '(?i)SBOM') 'Community docs must describe SBOM handling.'

$toolText = Get-Content -LiteralPath $ToolPath -Raw -Encoding utf8
Assert-True ($toolText -match "signature_status = 'not-signed'") 'Release tool must emit an explicit unsigned status.'
Assert-True ($toolText -match 'production_attestation = \$false') 'Release tool must emit a false production attestation flag.'
Assert-True ($toolText -match 'no SBOM was fabricated') 'Release tool must fail clearly instead of fabricating an SBOM.'
Assert-True ($toolText -notmatch '(?i)docker\s+compose.*\b(up|down|rm|stop|pull|build)\b') 'Community smoke must not start or destroy Compose resources.'

if ($RunSmoke) {
    $smokeSuffix = '{0}-{1}' -f [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ'), [Guid]::NewGuid().ToString('N')
    $smokeOutput = Join-Path ([System.IO.Path]::GetTempPath()) "workama-community-test-$smokeSuffix"
    & $ToolPath -Action smoke -OutputDirectory $smokeOutput
    & $ToolPath -Action verify -OutputDirectory $smokeOutput
    Write-Host "Executable smoke output retained for inspection: $smokeOutput"
}

Write-Host 'Community release static checks passed.'
