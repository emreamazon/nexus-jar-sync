[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BundleRoot,
    [string]$InstallationRoot = (Join-Path $env:USERPROFILE "NexusJarSync"),
    [string]$PythonExecutable = "python",
    [switch]$PlanOnly
)

$ErrorActionPreference = "Stop"
$script:SetupLog = $null

function Write-Phase([string]$Name) {
    $message = "[$([DateTime]::UtcNow.ToString('o'))] PHASE: $Name"
    Write-Host "`n=== $Name ==="
    if ($script:SetupLog) { Add-Content -LiteralPath $script:SetupLog -Value $message -Encoding UTF8 }
}

function Write-SafeLog([string]$Message) {
    if ($script:SetupLog) {
        Add-Content -LiteralPath $script:SetupLog -Value "[$([DateTime]::UtcNow.ToString('o'))] $Message" -Encoding UTF8
    }
}

function Confirm-Step([string]$Prompt) {
    if ($PlanOnly) { return $false }
    return (Read-Host "$Prompt [y/N]").Trim().ToUpperInvariant() -eq "Y"
}

function Resolve-SafeRoot([string]$Value) {
    $candidate = [IO.Path]::GetFullPath($Value)
    $profile = [IO.Path]::GetFullPath($env:USERPROFILE).TrimEnd('\')
    $bundle = [IO.Path]::GetFullPath($BundleRoot).TrimEnd('\')
    $root = [IO.Path]::GetPathRoot($candidate).TrimEnd('\')
    $trimmed = $candidate.TrimEnd('\')
    if ($trimmed -eq $root -or $trimmed -eq $profile -or $trimmed -eq $bundle) {
        throw "Installation root is too broad or conflicts with the bundle/user profile."
    }
    if ($trimmed.StartsWith($bundle + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Installation root must be outside the extracted bundle."
    }
    return $trimmed
}

function Invoke-Checked([string]$Executable, [string[]]$Arguments, [string]$Description, [string]$WorkingDirectory = "") {
    if ($WorkingDirectory) { Push-Location -LiteralPath $WorkingDirectory }
    try {
        & $Executable @Arguments
        $code = $LASTEXITCODE
    } finally {
        if ($WorkingDirectory) { Pop-Location }
    }
    Write-SafeLog "$Description exit_code=$code"
    if ($code -ne 0) { throw "$Description failed with exit code $code." }
}

function Get-UniqueTestOutput([string]$Root) {
    $parent = Join-Path $Root "test-downloads"
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    do {
        $name = "run-$([DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss'))-$([Guid]::NewGuid().ToString('N').Substring(0,8))"
        $candidate = Join-Path $parent $name
    } while (Test-Path -LiteralPath $candidate)
    return $candidate
}

try {
    $bundle = [IO.Path]::GetFullPath($BundleRoot).TrimEnd('\')
    $install = Resolve-SafeRoot $InstallationRoot
    $manifest = Join-Path $bundle "SHA256SUMS.json"
    $metadataPath = Join-Path $bundle "BUILD-METADATA.json"
    $verifier = Join-Path $bundle "tools\verify_manifest.py"
    $wheelhouse = Join-Path $bundle "wheelhouse"
    if (-not (Test-Path -LiteralPath $manifest -PathType Leaf) -or -not (Test-Path -LiteralPath $verifier -PathType Leaf)) {
        throw "Bundle manifest or verifier is missing."
    }

    Write-Phase "1 - Bundle verification"
    Invoke-Checked $PythonExecutable @($verifier, $bundle) "manifest verification"
    $metadata = Get-Content -LiteralPath $metadataPath -Raw | ConvertFrom-Json
    Write-Host "Application version: $($metadata.application_version)"
    Write-Host "Source commit: $($metadata.source_commit)"
    Write-Host "Built at UTC: $($metadata.built_at_utc)"
    Write-Host "Bundle Python: $($metadata.python_implementation) $($metadata.python_version)"
    Write-Host "Bundle platform: $($metadata.platform) $($metadata.architecture)"
    if ($metadata.platform -ne "Windows") { throw "Bundle platform does not match Windows." }
    $hostArchitecture = $env:PROCESSOR_ARCHITECTURE
    $bundleArchitecture = [string]$metadata.architecture
    if ($hostArchitecture -and $bundleArchitecture -and $hostArchitecture.ToLowerInvariant() -notin @($bundleArchitecture.ToLowerInvariant(), "amd64") ) {
        throw "Bundle architecture does not match this host."
    }
    $pythonVersion = & $PythonExecutable -c "import platform; print(platform.python_version())"
    if ($LASTEXITCODE -ne 0) { throw "Python could not be executed." }
    if (($pythonVersion -split '\.')[0..1] -join '.' -ne (([string]$metadata.python_version -split '\.')[0..1] -join '.')) {
        throw "Python major/minor version does not match the bundle."
    }
    $sevenZip = @(
        (Join-Path $env:ProgramFiles "7-Zip\7z.exe"),
        (Get-Command 7z.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1)
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -First 1
    if (-not $sevenZip) { throw "7z.exe was not found. Install 7-Zip through an approved mechanism and rerun." }
    Write-Host "7-Zip: $sevenZip"
    if ($PlanOnly) {
        Write-Host "PLAN OK: verification inputs and safe paths validated; no installation actions were performed."
        exit 0
    }

    New-Item -ItemType Directory -Path $install -Force | Out-Null
    $script:SetupLog = Join-Path $install "setup-windows.log"
    Write-SafeLog "bundle=$bundle installation_root=$install"
    Write-SafeLog "PHASE: 1 - Bundle verification completed exit_code=0"
    $venv = Join-Path $install "venv-$($metadata.application_version)"
    $venvPython = Join-Path $venv "Scripts\python.exe"
    $config = Join-Path $install "config\config.yaml"
    $dataPath = Join-Path $install "data"
    $testDownloadsPath = Join-Path $install "test-downloads"
    $logPath = Join-Path $install "logs\nexus-jar-sync.log"
    Write-Host "Operational data: $dataPath"
    Write-Host "Logs: $logPath"
    Write-Host "Test downloads: $testDownloadsPath"

    Write-Phase "2 - Offline installation"
    if (Test-Path -LiteralPath $venv) {
        Write-Host "Existing environment preserved: $venv"
        if (-not (Confirm-Step "Validate and use this existing environment?")) { exit 0 }
        if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) { throw "Existing environment has no Python executable." }
        $installedVersion = & $venvPython -c "import importlib.metadata as m; print(m.version('nexus-jar-sync'))"
        if ($LASTEXITCODE -ne 0 -or $installedVersion -ne $metadata.application_version) { throw "Existing environment package version is incompatible." }
    } else {
        Invoke-Checked $PythonExecutable @("-m", "venv", $venv) "virtual environment creation"
        Invoke-Checked $venvPython @("-m", "pip", "install", "--no-index", "--find-links", $wheelhouse, "nexus-jar-sync") "offline package installation"
    }
    Invoke-Checked $venvPython @("-c", "import importlib.metadata as m; assert m.version('nexus-jar-sync') == '$($metadata.application_version)'") "installed version validation"
    Invoke-Checked $venvPython @("-c", "import importlib.metadata as m; assert any(e.name == 'nexus-jar-sync' and e.value == 'nexus_jar_sync.main:main' for e in m.entry_points(group='console_scripts'))") "entry-point validation"
    Invoke-Checked $venvPython @("-m", "nexus_jar_sync.main", "--help") "installed help"

    Write-Phase "3 - Configuration preparation"
    if (-not (Test-Path -LiteralPath $config)) {
        New-Item -ItemType Directory -Path (Split-Path -Parent $config) -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $bundle "config\config.windows.example.yaml") -Destination $config
        Write-Host "Edit primary Nexus coordinates, companion URLs, destination base, 7z.exe path, and credential variable names."
        Start-Process -FilePath notepad.exe -ArgumentList @($config) -Wait
        if (-not (Confirm-Step "Have you saved and reviewed the configuration?")) { exit 0 }
    } else {
        Write-Host "Existing configuration preserved: $config"
        if (-not (Confirm-Step "Continue with this existing configuration?")) { exit 0 }
    }
    $configuredSevenZip = & $venvPython -c "import yaml,sys; d=yaml.safe_load(open(sys.argv[1],encoding='utf-8')); print(d.get('tools',{}).get('seven_zip_executable',''))" $config
    if ($LASTEXITCODE -ne 0 -or -not $configuredSevenZip -or -not (Test-Path -LiteralPath $configuredSevenZip -PathType Leaf)) {
        throw "The configured tools.seven_zip_executable does not exist."
    }

    Write-Phase "4 - Credential readiness"
    $credentialJson = & $venvPython -c "import json,yaml,sys; d=yaml.safe_load(open(sys.argv[1],encoding='utf-8')); names=[]; add=lambda a: [names.append(a.get(k)) for k in ('username_env','password_env') if a and a.get(k)]; add(d.get('defaults',{}).get('auth')); [(add(t.get('auth')), [add(c.get('auth')) for c in t.get('companions',[])]) for t in d.get('targets',[])]; print(json.dumps(sorted(set(n for n in names if n))))" $config
    if ($LASTEXITCODE -ne 0) { throw "Could not inspect credential variable names." }
    $missing = $false
    foreach ($name in ($credentialJson | ConvertFrom-Json)) {
        $present = [Environment]::GetEnvironmentVariable([string]$name)
        if ($null -eq $present) {
            Write-Host "$name : MISSING"
            $missing = $true
        } else {
            Write-Host "$name : SET"
        }
    }
    if ($missing) {
        Write-Host "Define missing variables through an approved mechanism for this user and the future Scheduled Task account, then rerun."
        exit 3
    }

    Write-Phase "5 - Isolated real test download"
    Write-Host "This performs real Nexus GET requests, downloads JAR/companions, and runs 7-Zip. It never writes Nexus or production state/destinations."
    if (-not (Confirm-Step "Start the isolated real test download?")) { exit 0 }
    $testOutput = Get-UniqueTestOutput $install
    Invoke-Checked $venvPython @("-m", "nexus_jar_sync.main", "--config", $config, "--test-download", "--test-output", $testOutput) "isolated test download" $install
    Write-Host "Test output preserved at: $testOutput"
    if (-not (Confirm-Step "Have you inspected and approved the test output?")) { exit 0 }

    Write-Phase "6 - Production preflight"
    Invoke-Checked $venvPython @("-m", "nexus_jar_sync.main", "--config", $config, "--dry-run") "production dry-run" $install
    Write-Host "The next operation performs real production downloads into configured destinations."
    if (-not (Confirm-Step "Run one active production synchronization?")) { exit 0 }
    Invoke-Checked $venvPython @("-m", "nexus_jar_sync.main", "--config", $config) "active production synchronization" $install
    Write-Host "Production preflight succeeded. Application log: $logPath"

    Write-Phase "7 - Optional Task Scheduler installation"
    if (-not (Confirm-Step "Configure Task Scheduler now?")) {
        Write-Host "Installation is complete but not scheduled."
        exit 0
    }
    $taskName = (Read-Host "Task name [NexusJarSync]").Trim(); if (-not $taskName) { $taskName = "NexusJarSync" }
    $intervalText = (Read-Host "Interval minutes [5]").Trim(); if (-not $intervalText) { $intervalText = "5" }
    $interval = 0
    if (-not [int]::TryParse($intervalText, [ref]$interval) -or $interval -le 0) { throw "Interval must be a positive integer." }
    Write-Host "The task account must have Nexus credential variables plus destination/state/log permissions. Existing tasks are never replaced automatically."
    if (-not (Confirm-Step "Register the exact root task '$taskName'?")) { exit 0 }
    $schedulerScript = Join-Path $bundle "deployment\windows\install-task.ps1"
    try {
        & $schedulerScript -ProjectDirectory $install -ConfigPath $config -PythonExecutable $venvPython -IntervalMinutes $interval -TaskName $taskName
        $code = $LASTEXITCODE
        if ($code -ne 0) { throw "Scheduler installer failed with exit code $code." }
        Write-SafeLog "scheduler_registration task=$taskName exit_code=0"
        Write-Host "Scheduled Task registration succeeded: \$taskName"
    } catch {
        Write-Host "Application installation succeeded, but scheduling did not. Run later:"
        Write-Host "& '$schedulerScript' -ProjectDirectory '$install' -ConfigPath '$config' -PythonExecutable '$venvPython' -IntervalMinutes $interval -TaskName '$taskName'"
        throw
    }
    exit 0
} catch {
    $message = $_.Exception.Message
    Write-Error $message
    Write-SafeLog "FAILED sanitized_message=$message"
    exit 1
}
