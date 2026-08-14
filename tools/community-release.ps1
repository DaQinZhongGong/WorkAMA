[CmdletBinding()]
param(
    [ValidateSet('smoke', 'package', 'provenance', 'sbom', 'verify')]
    [string]$Action = 'smoke',
    [string]$OutputDirectory = '',
    [string]$PackageDirectory = '',
    [string]$SbomPath = '',
    [ValidateSet('auto', 'syft', 'docker')]
    [string]$SbomTool = 'auto',
    [switch]$RunComposeConfig
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$RepoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$RequiredRootFiles = @(
    'LICENSE',
    'README.md',
    '.env.example',
    'CONTRIBUTING.md',
    'SECURITY.md',
    'RELEASE.md',
    'deploy/compose/docker-compose.yml'
)
$RequiredPackageFiles = @(
    'LICENSE',
    '.env.example',
    'CONTRIBUTING.md',
    'SECURITY.md',
    'RELEASE.md',
    'deploy/compose/docker-compose.yml',
    'docs/README.md',
    'docs/community/README.md',
    'docs/community/CONTRIBUTING.md',
    'docs/community/SECURITY.md',
    'docs/community/RELEASE.md',
    'quality/release/community/README.md'
)
$SourceRoots = @(
    'apps',
    'api',
    'deploy',
    'packages',
    'tools',
    'docs/community',
    'quality/release/community'
)
$RootPackageFiles = @(
    'LICENSE',
    'README.md',
    '.env.example',
    'Makefile',
    'package.json',
    'pnpm-lock.yaml',
    'pnpm-workspace.yaml',
    'docs/README.md',
    'CONTRIBUTING.md',
    'SECURITY.md',
    'RELEASE.md'
)
$ExcludedDirectoryNames = @(
    '.git',
    '.data',
    '.idea',
    'node_modules',
    '.pnpm-store',
    'dist',
    'coverage',
    '.pytest_cache',
    '__pycache__',
    'playwright-report',
    'test-results'
)

function Resolve-OutputPath {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        $suffix = '{0}-{1}' -f [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ'), [Guid]::NewGuid().ToString('N')
        return [System.IO.Path]::GetFullPath((Join-Path ([System.IO.Path]::GetTempPath()) "workama-community-release-$suffix"))
    }
    if ([System.IO.Path]::IsPathRooted($Value)) {
        return [System.IO.Path]::GetFullPath($Value)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $RepoRoot $Value))
}

function Assert-OutputOutsideRepository {
    param([string]$Path)

    $normalizedRoot = $RepoRoot.TrimEnd([char[]]@('\', '/'))
    $normalizedPath = [System.IO.Path]::GetFullPath($Path).TrimEnd([char[]]@('\', '/'))
    $prefix = $normalizedRoot + [System.IO.Path]::DirectorySeparatorChar
    if ($normalizedPath.Equals($normalizedRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
        $normalizedPath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "OutputDirectory must be outside the repository so generated evidence cannot be copied into the package. Path: $Path"
    }
}

function Ensure-Directory {
    param([string]$Path)
    New-Item -ItemType Directory -Path $Path -Force | Out-Null
}

function Resolve-RepoFile {
    param([string]$RelativePath)

    $fullPath = [System.IO.Path]::GetFullPath((Join-Path $RepoRoot $RelativePath))
    $prefix = $RepoRoot.TrimEnd([char[]]@('\', '/')) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $fullPath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase) -and
        -not $fullPath.Equals($RepoRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Repository path escapes the repository: $RelativePath"
    }
    return $fullPath
}

function Get-RelativePathCompat {
    param([string]$BasePath, [string]$TargetPath)

    $baseFullPath = [System.IO.Path]::GetFullPath($BasePath)
    if (-not $baseFullPath.EndsWith([System.IO.Path]::DirectorySeparatorChar)) {
        $baseFullPath += [System.IO.Path]::DirectorySeparatorChar
    }
    $targetFullPath = [System.IO.Path]::GetFullPath($TargetPath)
    $baseUri = New-Object -TypeName System.Uri -ArgumentList $baseFullPath
    $targetUri = New-Object -TypeName System.Uri -ArgumentList $targetFullPath
    return [System.Uri]::UnescapeDataString($baseUri.MakeRelativeUri($targetUri).ToString()).Replace('/', '\')
}

function Get-RelativePackagePath {
    param([string]$PackageDirectory, [string]$FilePath)
    return (Get-RelativePathCompat $PackageDirectory $FilePath).Replace('\', '/')
}

function Get-RelativeRepoPath {
    param([string]$FilePath)
    return (Get-RelativePathCompat $RepoRoot $FilePath).Replace('\', '/')
}

function Get-JsonProperty {
    param(
        [AllowNull()]$Object,
        [string]$Name,
        [string]$Path
    )

    if ($null -eq $Object) {
        throw "Missing JSON object while reading '$Path'."
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        throw "Missing required JSON property '$Path'."
    }
    return $property.Value
}

function Read-JsonFile {
    param([string]$Path, [string]$Description)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Description was not found: $Path"
    }
    try {
        return Get-Content -LiteralPath $Path -Raw -Encoding utf8 | ConvertFrom-Json
    }
    catch {
        throw "$Description is not valid JSON: $Path. $($_.Exception.Message)"
    }
}

function Write-JsonFile {
    param([string]$Path, [AllowNull()]$Value)

    $parent = Split-Path -Parent $Path
    Ensure-Directory $parent
    $Value | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $Path -Encoding utf8
}

function Get-Sha256 {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-BytesSha256 {
    param([byte[]]$Bytes)

    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($algorithm.ComputeHash($Bytes)) -replace '-', '').ToLowerInvariant()
    }
    finally {
        $algorithm.Dispose()
    }
}

function Test-SkippedPackagePath {
    param([string]$RelativePath)

    $normalized = $RelativePath.Replace('\', '/')
    $parts = $normalized.Split('/')
    foreach ($part in $parts) {
        if ($ExcludedDirectoryNames -contains $part) {
            return $true
        }
    }
    $leaf = $parts[$parts.Count - 1]
    if (($leaf -like '.env*') -and $leaf -ne '.env.example') {
        return $true
    }
    if ($normalized -like 'quality/evidence/*' -or $normalized -like 'quality/performance/results/*') {
        return $true
    }
    return $false
}

function Assert-SourceFile {
    param([string]$RelativePath)
    $path = Resolve-RepoFile $RelativePath
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required community package source file is missing: $RelativePath"
    }
    return $path
}

function Copy-PackageFile {
    param(
        [string]$SourcePath,
        [string]$RelativePath,
        [string]$PackageDirectory
    )

    $targetPath = Join-Path $PackageDirectory ($RelativePath.Replace('/', [System.IO.Path]::DirectorySeparatorChar))
    Ensure-Directory (Split-Path -Parent $targetPath)
    Copy-Item -LiteralPath $SourcePath -Destination $targetPath -Force
}

function New-CommunityPackage {
    param(
        [string]$OutputRoot,
        [string]$PackageDirectory
    )

    foreach ($file in $RequiredRootFiles) {
        [void](Assert-SourceFile $file)
    }
    $packageParent = Split-Path -Parent $PackageDirectory
    Ensure-Directory $packageParent
    if (Test-Path -LiteralPath $PackageDirectory) {
        $existing = Get-ChildItem -LiteralPath $PackageDirectory -Force | Select-Object -First 1
        if ($null -ne $existing) {
            throw "PackageDirectory must be new or empty; refusing to overwrite existing content: $PackageDirectory"
        }
    }
    Ensure-Directory $PackageDirectory

    foreach ($relativePath in $RootPackageFiles) {
        $sourcePath = Assert-SourceFile $relativePath
        Copy-PackageFile $sourcePath $relativePath $PackageDirectory
    }
    foreach ($sourceRoot in $SourceRoots) {
        $sourcePath = Resolve-RepoFile $sourceRoot
        if (-not (Test-Path -LiteralPath $sourcePath -PathType Container)) {
            throw "Required package source directory is missing: $sourceRoot"
        }
        $sourceFiles = Get-ChildItem -LiteralPath $sourcePath -File -Recurse -Force
        foreach ($sourceFile in $sourceFiles) {
            $relativePath = Get-RelativeRepoPath $sourceFile.FullName
            if (Test-SkippedPackagePath $relativePath) {
                continue
            }
            Copy-PackageFile $sourceFile.FullName $relativePath $PackageDirectory
        }
    }

    return $PackageDirectory
}

function Get-PackageInventory {
    param([string]$PackageDirectory)

    $files = @(Get-ChildItem -LiteralPath $PackageDirectory -File -Recurse -Force)
    if ($files.Count -eq 0) {
        throw "The community package contains no files: $PackageDirectory"
    }
    $inventory = @(
        $files |
            Sort-Object FullName |
            ForEach-Object {
                [ordered]@{
                    path = Get-RelativePackagePath $PackageDirectory $_.FullName
                    size = [int64]$_.Length
                    sha256 = Get-Sha256 $_.FullName
                }
            }
    )
    return $inventory
}

function Get-InventoryDigest {
    param([object[]]$Inventory)

    $canonical = ($Inventory | ForEach-Object {
        '{0}|{1}|{2}' -f $_.path, $_.size, $_.sha256
    }) -join "`n"
    return 'sha256:' + (Get-BytesSha256 ([System.Text.Encoding]::UTF8.GetBytes($canonical)))
}

function New-PackageManifest {
    param(
        [string]$OutputRoot,
        [string]$PackageDirectory
    )

    $inventory = @(Get-PackageInventory $PackageDirectory)
    $manifest = [ordered]@{
        schema_version = 'workama.community.package-manifest.v1'
        package_name = 'workama-community'
        package_type = 'source'
        release_channel = 'community'
        generated_at = [DateTimeOffset]::UtcNow.ToString('o')
        package_directory = 'package'
        file_count = $inventory.Count
        inventory_digest = Get-InventoryDigest $inventory
        files = $inventory
        local_evidence = [ordered]@{
            status = 'passed'
            scope = 'local-only'
            signed = $false
            signature_status = 'not-signed'
            production_attestation = $false
        }
        sbom = [ordered]@{
            status = 'not-generated'
            file = $null
            is_sbom = $false
            tool = $null
            production_attestation = $false
        }
        provenance = [ordered]@{
            status = 'generated-local-only'
            file = 'provenance.json'
            signed = $false
            signature_status = 'not-signed'
            production_attestation = $false
        }
    }
    $manifestPath = Join-Path $OutputRoot 'package-manifest.json'
    Write-JsonFile $manifestPath $manifest
    return Read-JsonFile $manifestPath 'Community package manifest'
}

function New-LocalSbomStatus {
    param([string]$OutputRoot)

    $status = [ordered]@{
        schema_version = 'workama.community.sbom-status.v1'
        status = 'not-generated'
        is_sbom = $false
        tool = $null
        reason = 'No SBOM generator was invoked. Run Action sbom with Syft to create a CycloneDX document.'
        local_only = $true
        signed = $false
        signature_status = 'not-signed'
        production_attestation = $false
    }
    Write-JsonFile (Join-Path $OutputRoot 'sbom-status.json') $status
}

function New-LocalProvenance {
    param(
        [string]$OutputRoot,
        $Manifest
    )

    $provenance = [ordered]@{
        schema_version = 'workama.community.provenance.v1'
        predicate_type = 'https://slsa.dev/provenance/v1'
        build_type = 'https://workama.dev/community/source-package'
        builder = [ordered]@{
            id = 'workama.local/community-release.ps1'
        }
        subject = @(
            [ordered]@{
                name = 'workama-community-source-package'
                digest = [ordered]@{
                    sha256 = ([string](Get-JsonProperty $Manifest 'inventory_digest' 'inventory_digest')).Substring(7)
                }
            }
        )
        invocation = [ordered]@{
            command = 'tools/community-release.ps1'
            action = 'local-package'
            repository = 'working-tree'
            generated_at = [DateTimeOffset]::UtcNow.ToString('o')
        }
        local_only = $true
        signed = $false
        signature_status = 'not-signed'
        production_attestation = $false
        notes = 'Local provenance records file hashes for smoke testing. It is not a production attestation or signature.'
    }
    Write-JsonFile (Join-Path $OutputRoot 'provenance.json') $provenance
}

function Assert-RelativePackagePath {
    param([string]$RelativePath, [string]$Description)

    if ([string]::IsNullOrWhiteSpace($RelativePath) -or
        [System.IO.Path]::IsPathRooted($RelativePath) -or
        $RelativePath -match '(^|[\\/])\.\.([\\/]|$)') {
        throw "$Description must be a relative path below the package directory: $RelativePath"
    }
}

function Assert-LocalEvidenceFlags {
    param($Object, [string]$Description)

    if ([bool](Get-JsonProperty $Object 'local_only' "$Description.local_only") -ne $true) {
        throw "$Description must set local_only=true."
    }
    if ([bool](Get-JsonProperty $Object 'signed' "$Description.signed") -ne $false) {
        throw "$Description must set signed=false."
    }
    if ([bool](Get-JsonProperty $Object 'production_attestation' "$Description.production_attestation") -ne $false) {
        throw "$Description must set production_attestation=false."
    }
    $signatureStatus = [string](Get-JsonProperty $Object 'signature_status' "$Description.signature_status")
    if ($signatureStatus -ne 'not-signed') {
        throw "$Description must set signature_status=not-signed."
    }
}

function Test-CycloneDxSbom {
    param([string]$Path)

    $sbom = Read-JsonFile $Path 'SBOM'
    if ([string](Get-JsonProperty $sbom 'bomFormat' 'sbom.bomFormat') -ne 'CycloneDX') {
        throw "SBOM must be CycloneDX JSON: $Path"
    }
    $specVersion = [string](Get-JsonProperty $sbom 'specVersion' 'sbom.specVersion')
    if ([string]::IsNullOrWhiteSpace($specVersion)) {
        throw "SBOM is missing CycloneDX specVersion: $Path"
    }
    if ($null -eq $sbom.PSObject.Properties['components']) {
        throw "SBOM is missing the CycloneDX components property: $Path"
    }
    return $sbom
}

function Test-PackageManifest {
    param(
        [string]$PackageDirectory,
        [string]$ManifestPath
    )

    $manifest = Read-JsonFile $ManifestPath 'Community package manifest'
    if ([string](Get-JsonProperty $manifest 'schema_version' 'schema_version') -ne 'workama.community.package-manifest.v1') {
        throw 'Unsupported community package manifest schema.'
    }
    if ([string](Get-JsonProperty $manifest 'release_channel' 'release_channel') -ne 'community') {
        throw 'Community package manifest release_channel must be community.'
    }
    if ([string](Get-JsonProperty $manifest 'package_type' 'package_type') -ne 'source') {
        throw 'Community package manifest package_type must be source.'
    }
    $localEvidence = Get-JsonProperty $manifest 'local_evidence' 'local_evidence'
    if ([string](Get-JsonProperty $localEvidence 'scope' 'local_evidence.scope') -ne 'local-only') {
        throw 'Local evidence scope must be local-only.'
    }
    if ([bool](Get-JsonProperty $localEvidence 'signed' 'local_evidence.signed') -ne $false -or
        [bool](Get-JsonProperty $localEvidence 'production_attestation' 'local_evidence.production_attestation') -ne $false) {
        throw 'Community package evidence cannot claim production signing or attestation.'
    }

    foreach ($requiredPath in $RequiredPackageFiles) {
        if (-not (Test-Path -LiteralPath (Join-Path $PackageDirectory $requiredPath) -PathType Leaf)) {
            throw "Community package is missing required file: $requiredPath"
        }
    }

    $packagePrefix = [System.IO.Path]::GetFullPath($PackageDirectory).TrimEnd([char[]]@('\', '/')) + [System.IO.Path]::DirectorySeparatorChar
    $seen = @{}
    $files = @(Get-JsonProperty $manifest 'files' 'files')
    foreach ($entry in $files) {
        $relativePath = [string](Get-JsonProperty $entry 'path' 'files[].path')
        Assert-RelativePackagePath $relativePath 'Manifest file path'
        if ($seen.ContainsKey($relativePath)) {
            throw "Duplicate package file in manifest: $relativePath"
        }
        $seen[$relativePath] = $true
        $fullPath = [System.IO.Path]::GetFullPath((Join-Path $PackageDirectory $relativePath))
        if (-not $fullPath.StartsWith($packagePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Manifest file escapes package directory: $relativePath"
        }
        if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
            throw "Manifest file does not exist: $relativePath"
        }
        if ((Get-Sha256 $fullPath) -ne ([string](Get-JsonProperty $entry 'sha256' "files[$relativePath].sha256")).ToLowerInvariant()) {
            throw "Manifest hash mismatch: $relativePath"
        }
        if ($relativePath -eq '.env' -or $relativePath -match '(^|[\\/])\.env$') {
            throw 'A real .env file must never be included in a community package.'
        }
    }
    $inventoryDigest = [string](Get-JsonProperty $manifest 'inventory_digest' 'inventory_digest')
    if ($inventoryDigest -notmatch '^sha256:[0-9a-f]{64}$') {
        throw 'Manifest inventory_digest must be a SHA-256 digest.'
    }
    return $manifest
}

function Test-LocalProvenance {
    param(
        [string]$Path,
        $Manifest
    )

    $provenance = Read-JsonFile $Path 'Local provenance'
    if ([string](Get-JsonProperty $provenance 'schema_version' 'provenance.schema_version') -ne 'workama.community.provenance.v1') {
        throw 'Unsupported community provenance schema.'
    }
    Assert-LocalEvidenceFlags $provenance 'provenance'
    $subjects = @(Get-JsonProperty $provenance 'subject' 'provenance.subject')
    if ($subjects.Count -ne 1) {
        throw 'Local provenance must contain exactly one package subject.'
    }
    $digest = [string](Get-JsonProperty (Get-JsonProperty $subjects[0] 'digest' 'provenance.subject[0].digest') 'sha256' 'provenance.subject[0].digest.sha256')
    $expected = ([string](Get-JsonProperty $Manifest 'inventory_digest' 'inventory_digest')).Substring(7)
    if ($digest -ne $expected) {
        throw 'Local provenance subject does not match the package inventory digest.'
    }
}

function Invoke-ComposeConfig {
    param([string]$PackageDirectory)

    $docker = Get-Command docker -ErrorAction SilentlyContinue
    if ($null -eq $docker) {
        throw 'Docker CLI was not found; -RunComposeConfig cannot be completed.'
    }
    $composeFile = Join-Path $PackageDirectory 'deploy/compose/docker-compose.yml'
    $envFile = Join-Path $PackageDirectory '.env.example'
    $arguments = @(
        'compose',
        '-p', 'workama-community-smoke',
        '--env-file', $envFile,
        '-f', $composeFile,
        'config',
        '--quiet'
    )
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& $docker.Source @arguments 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($exitCode -ne 0) {
        throw "Docker Compose config failed with exit code ${exitCode}: $($output -join [Environment]::NewLine)"
    }
}

function Invoke-SbomGeneration {
    param(
        [string]$PackageDirectory,
        [string]$OutputRoot,
        [string]$ToolSelection
    )

    $outputPath = if ([string]::IsNullOrWhiteSpace($SbomPath)) {
        Join-Path $OutputRoot 'sbom.cdx.json'
    }
    else {
        [System.IO.Path]::GetFullPath($SbomPath)
    }
    $outputRootFullPath = [System.IO.Path]::GetFullPath($OutputRoot).TrimEnd([char[]]@('\', '/'))
    $outputRootPrefix = $outputRootFullPath + [System.IO.Path]::DirectorySeparatorChar
    if (-not $outputPath.StartsWith($outputRootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'SbomPath must remain below OutputDirectory so the evidence bundle stays self-contained.'
    }
    Ensure-Directory (Split-Path -Parent $outputPath)
    $selectedTool = $ToolSelection
    if ($selectedTool -eq 'auto') {
        if ($null -ne (Get-Command syft -ErrorAction SilentlyContinue)) {
            $selectedTool = 'syft'
        }
        else {
            throw 'SBOM generation requires Syft. Install syft or rerun with -SbomTool docker; no SBOM was fabricated.'
        }
    }

    if ($selectedTool -eq 'syft') {
        $syft = Get-Command syft -ErrorAction SilentlyContinue
        if ($null -eq $syft) {
            throw 'Syft was not found. Install syft or use -SbomTool docker; no SBOM was fabricated.'
        }
        $arguments = @("dir:$PackageDirectory", '-o', "cyclonedx-json=$outputPath")
        $previousErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $output = @(& $syft.Source @arguments 2>&1)
            $exitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
        if ($exitCode -ne 0) {
            throw "Syft SBOM generation failed with exit code ${exitCode}: $($output -join [Environment]::NewLine)"
        }
    }
    elseif ($selectedTool -eq 'docker') {
        $docker = Get-Command docker -ErrorAction SilentlyContinue
        if ($null -eq $docker) {
            throw 'Docker CLI was not found; cannot run the Syft Docker image.'
        }
        $image = if ([string]::IsNullOrWhiteSpace($env:WORKAMA_SYFT_IMAGE)) { 'anchore/syft:latest' } else { $env:WORKAMA_SYFT_IMAGE }
        $packageMount = '{0}:/src:ro' -f $PackageDirectory
        $outputRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $outputPath))
        $outputMount = '{0}:/out' -f $outputRoot
        $outputFileName = [System.IO.Path]::GetFileName($outputPath)
        $arguments = @(
            'run', '--rm', '--network', 'none',
            '--volume', $packageMount,
            '--volume', $outputMount,
            $image,
            'dir:/src',
            '-o', "cyclonedx-json=/out/$outputFileName"
        )
        $previousErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $output = @(& $docker.Source @arguments 2>&1)
            $exitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
        if ($exitCode -ne 0) {
            throw "Syft Docker SBOM generation failed with exit code ${exitCode}: $($output -join [Environment]::NewLine)"
        }
    }
    else {
        throw "Unsupported SBOM tool selection: $ToolSelection"
    }

    [void](Test-CycloneDxSbom $outputPath)
    return $outputPath
}

function Update-ManifestForSbom {
    param([string]$ManifestPath, [string]$GeneratedSbomPath, [string]$OutputRoot, [string]$ToolSelection)

    $manifest = Read-JsonFile $ManifestPath 'Community package manifest'
    $relativeSbomPath = (Get-RelativePathCompat $OutputRoot $GeneratedSbomPath).Replace('\', '/')
    Assert-RelativePackagePath $relativeSbomPath 'SBOM output path'
    $manifest.sbom = [ordered]@{
        status = 'generated-local-only'
        file = $relativeSbomPath
        is_sbom = $true
        tool = $ToolSelection
        digest = 'sha256:' + (Get-Sha256 $GeneratedSbomPath)
        production_attestation = $false
    }
    Write-JsonFile $ManifestPath $manifest
    $status = [ordered]@{
        schema_version = 'workama.community.sbom-status.v1'
        status = 'generated-local-only'
        is_sbom = $true
        tool = $ToolSelection
        file = $relativeSbomPath
        digest = 'sha256:' + (Get-Sha256 $GeneratedSbomPath)
        local_only = $true
        signed = $false
        signature_status = 'not-signed'
        production_attestation = $false
    }
    Write-JsonFile (Join-Path $OutputRoot 'sbom-status.json') $status
}

function Invoke-CommunitySmoke {
    param([string]$OutputRoot)

    Ensure-Directory $OutputRoot
    $packageDirectory = Join-Path $OutputRoot 'package'
    [void](New-CommunityPackage $OutputRoot $packageDirectory)
    $manifestPath = Join-Path $OutputRoot 'package-manifest.json'
    $manifest = New-PackageManifest $OutputRoot $packageDirectory
    New-LocalSbomStatus $OutputRoot
    New-LocalProvenance $OutputRoot $manifest
    [void](Test-PackageManifest $packageDirectory $manifestPath)
    Test-LocalProvenance (Join-Path $OutputRoot 'provenance.json') $manifest
    if ($RunComposeConfig) {
        Invoke-ComposeConfig $packageDirectory
    }
    $smoke = [ordered]@{
        schema_version = 'workama.community.smoke.v1'
        status = 'passed'
        evidence_scope = 'local-only'
        package_type = 'source'
        package_directory = 'package'
        compose_project_name = 'workama-community-smoke'
        compose_config_checked = [bool]$RunComposeConfig
        containers_started = $false
        destructive_actions = $false
        signed = $false
        signature_status = 'not-signed'
        production_attestation = $false
        note = 'A passing local smoke is not a production signature or attestation.'
    }
    Write-JsonFile (Join-Path $OutputRoot 'smoke-result.json') $smoke
    Write-Host "Community release package smoke passed (LOCAL ONLY): $OutputRoot"
    Write-Host 'No containers were started and no production signature or attestation was created.'
    return $OutputRoot
}

$evidenceRoot = Resolve-OutputPath $OutputDirectory
Assert-OutputOutsideRepository $evidenceRoot
$defaultPackageDirectory = Join-Path $evidenceRoot 'package'
$resolvedPackageDirectory = if ([string]::IsNullOrWhiteSpace($PackageDirectory)) {
    $defaultPackageDirectory
}
elseif ([System.IO.Path]::IsPathRooted($PackageDirectory)) {
    [System.IO.Path]::GetFullPath($PackageDirectory)
}
else {
    [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $PackageDirectory))
}

switch ($Action) {
    'smoke' {
        [void](Invoke-CommunitySmoke $evidenceRoot)
    }
    'package' {
        Ensure-Directory $evidenceRoot
        [void](New-CommunityPackage $evidenceRoot $resolvedPackageDirectory)
        $manifest = New-PackageManifest $evidenceRoot $resolvedPackageDirectory
        New-LocalSbomStatus $evidenceRoot
        New-LocalProvenance $evidenceRoot $manifest
        Write-Host "Community release package created (LOCAL ONLY): $resolvedPackageDirectory"
    }
    'provenance' {
        $manifestPath = Join-Path $evidenceRoot 'package-manifest.json'
        $manifest = Test-PackageManifest $resolvedPackageDirectory $manifestPath
        New-LocalProvenance $evidenceRoot $manifest
        Test-LocalProvenance (Join-Path $evidenceRoot 'provenance.json') $manifest
        Write-Host "Local provenance generated (NOT SIGNED): $(Join-Path $evidenceRoot 'provenance.json')"
    }
    'sbom' {
        $manifestPath = Join-Path $evidenceRoot 'package-manifest.json'
        $manifest = Test-PackageManifest $resolvedPackageDirectory $manifestPath
        $generatedPath = Invoke-SbomGeneration $resolvedPackageDirectory $evidenceRoot $SbomTool
        Update-ManifestForSbom $manifestPath $generatedPath $evidenceRoot $SbomTool
        New-LocalProvenance $evidenceRoot (Read-JsonFile $manifestPath 'Community package manifest')
        Write-Host "CycloneDX SBOM generated locally (NOT SIGNED): $generatedPath"
    }
    'verify' {
        $manifestPath = Join-Path $evidenceRoot 'package-manifest.json'
        $manifest = Test-PackageManifest $resolvedPackageDirectory $manifestPath
        $provenancePath = Join-Path $evidenceRoot 'provenance.json'
        Test-LocalProvenance $provenancePath $manifest
        $statusPath = Join-Path $evidenceRoot 'sbom-status.json'
        if (Test-Path -LiteralPath $statusPath -PathType Leaf) {
            $status = Read-JsonFile $statusPath 'SBOM status'
            if ([bool](Get-JsonProperty $status 'production_attestation' 'sbom-status.production_attestation') -ne $false) {
                throw 'SBOM status cannot claim production attestation.'
            }
            if ([bool](Get-JsonProperty $status 'signed' 'sbom-status.signed') -ne $false) {
                throw 'SBOM status cannot claim signing.'
            }
            if ([bool](Get-JsonProperty $status 'is_sbom' 'sbom-status.is_sbom')) {
                $sbomFile = [string](Get-JsonProperty $status 'file' 'sbom-status.file')
                Assert-RelativePackagePath $sbomFile 'SBOM status file'
                [void](Test-CycloneDxSbom (Join-Path $evidenceRoot $sbomFile))
            }
        }
        if (-not [string]::IsNullOrWhiteSpace($SbomPath)) {
            [void](Test-CycloneDxSbom ([System.IO.Path]::GetFullPath($SbomPath)))
        }
        Write-Host 'Community release package verification passed (LOCAL ONLY).'
    }
}
