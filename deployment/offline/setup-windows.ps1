[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$BundleRoot,
    [string]$InstallationRoot=(Join-Path $env:USERPROFILE "NexusJarSync"),
    [string]$PythonExecutable="python",
    [string]$SevenZipExecutable,
    [switch]$PlanOnly
)
$ErrorActionPreference="Stop"; $script:SetupLog=$null

function Write-Phase([string]$Name){ Write-Host "`n=== $Name ==="; Write-SafeLog "PHASE: $Name" }
function Write-SafeLog([string]$Message){ if($script:SetupLog){ Add-Content -LiteralPath $script:SetupLog -Value "[$([DateTime]::UtcNow.ToString('o'))] $Message" -Encoding UTF8 } }
function Confirm-Step([string]$Prompt){ if($PlanOnly){return $false}; (Read-Host "$Prompt [y/N]").Trim().ToUpperInvariant() -eq "Y" }
function Resolve-FullPathField([object]$Value,[string]$Field){
    if($Value -isnot [string] -or [string]::IsNullOrWhiteSpace($Value) -or $Value -match '[\x00-\x1f]'){throw "$Field is not a valid single path value."}
    try{return [IO.Path]::GetFullPath([string]$Value)}catch{throw "$Field is not a valid path."}
}
function Resolve-SafeRoot([string]$Value){
    $candidate=Resolve-FullPathField $Value 'Installation root'; $trimmed=$candidate.TrimEnd('\')
    $profile=(Resolve-FullPathField $env:USERPROFILE 'User profile').TrimEnd('\'); $bundle=(Resolve-FullPathField $BundleRoot 'Bundle root').TrimEnd('\')
    $root=[IO.Path]::GetPathRoot($candidate).TrimEnd('\')
    if($trimmed -eq $root -or $trimmed -eq $profile -or $trimmed -eq $bundle){throw "Installation root is too broad or conflicts with the bundle/user profile."}
    if($trimmed.StartsWith($bundle+'\',[StringComparison]::OrdinalIgnoreCase)){throw "Installation root must be outside the extracted bundle."}; $trimmed
}
function Invoke-Checked([string]$Executable,[string[]]$Arguments,[string]$Description,[string]$WorkingDirectory=""){
    $code=1; if($WorkingDirectory){Push-Location -LiteralPath $WorkingDirectory}
    try{ & $Executable @Arguments; $code=$LASTEXITCODE } finally {if($WorkingDirectory){Pop-Location}}
    Write-SafeLog "$Description exit_code=$code"; if($code -ne 0){throw "$Description failed with exit code $code."}
}
function ConvertTo-NormalizedArchitecture([string]$Value){
    switch($Value.Trim().ToLowerInvariant()){
        {$_ -in @('amd64','x86_64','x64')}{'x86_64';break}
        {$_ -in @('arm64','aarch64')}{'arm64';break}
        {$_ -in @('x86','i386','i486','i586','i686')}{'x86';break}
        default{throw "Unsupported or missing architecture metadata."}
    }
}
function Get-PythonRuntime([string]$Executable){
    $json=& $Executable -c "import json,platform,struct; print(json.dumps({'implementation':platform.python_implementation(),'version':platform.python_version(),'bits':struct.calcsize('P')*8,'machine':platform.machine()}))"
    if($LASTEXITCODE -ne 0){throw "Python could not be executed."}; try{$r=$json|ConvertFrom-Json}catch{throw "Selected Python returned malformed compatibility information."}
    if(-not $r.implementation -or -not $r.version -or -not $r.machine -or $r.bits -notin @(32,64)){throw "Selected Python returned incomplete compatibility information."}
    $r|Add-Member normalized_architecture (ConvertTo-NormalizedArchitecture ([string]$r.machine)); $r
}
function Get-ConfiguredSevenZip([string]$ConfigPath){
    if(-not(Test-Path -LiteralPath $ConfigPath -PathType Leaf)){return $null}
    foreach($line in Get-Content -LiteralPath $ConfigPath){if($line -match '^\s*seven_zip_executable\s*:\s*["'']?([^"''#]+?)["'']?\s*(?:#.*)?$'){return $Matches[1].Trim()}}
}
function Resolve-SevenZip([string]$Override,[string]$ConfigPath){
    $fromPath=Get-Command 7z.exe -ErrorAction SilentlyContinue|Select-Object -ExpandProperty Source -First 1
    foreach($item in @($Override,(Get-ConfiguredSevenZip $ConfigPath),(Join-Path $env:ProgramFiles '7-Zip\7z.exe'),$fromPath)|Where-Object{$_}){
        if([string]$item -match '[\x00-\x1f;|&><]'){if($item -eq $Override){throw "The 7-Zip executable path is unsafe."};continue}
        try{$full=Resolve-FullPathField $item '7-Zip executable'}catch{if($item -eq $Override){throw};continue}
        if(-not[IO.Path]::IsPathFullyQualified($full)-or-not(Test-Path -LiteralPath $full -PathType Leaf)){if($item -eq $Override){throw "The specified 7-Zip executable does not exist."};continue}
        $file=Get-Item -LiteralPath $full -Force; if(($file.Attributes-band[IO.FileAttributes]::ReparsePoint)-ne 0){if($item -eq $Override){throw "The 7-Zip executable must not be a reparse point."};continue}
        & $file.FullName @('i') *> $null; if($LASTEXITCODE -eq 0){return $file.FullName}; if($item -eq $Override){throw "The specified 7-Zip executable failed its version check."}
    }
    if(-not$PlanOnly){$entered=Read-Host "Absolute path to approved 7z.exe";if($entered){return Resolve-SevenZip $entered $ConfigPath}}
    throw "7z.exe was not found. Supply -SevenZipExecutable or configure an approved absolute path."
}
function Test-Environment([string]$Python,[string]$Version){
    if(-not(Test-Path -LiteralPath $Python -PathType Leaf)){return $false}; & $Python -c "import importlib.metadata as m; assert m.version('nexus-jar-sync')=='$Version'; assert any(e.name=='nexus-jar-sync' and e.value=='nexus_jar_sync.main:main' for e in m.entry_points(group='console_scripts'))" *> $null; $LASTEXITCODE -eq 0
}
function Get-UniqueTestOutput([string]$Root){$p=Join-Path $Root 'test-downloads';New-Item -ItemType Directory -Path $p -Force|Out-Null;do{$c=Join-Path $p "run-$([DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss'))-$([Guid]::NewGuid().ToString('N').Substring(0,8))"}while(Test-Path -LiteralPath $c);$c}

try{
    $bundle=(Resolve-FullPathField $BundleRoot 'Bundle root').TrimEnd('\')
    if(-not$PlanOnly){$rootInput=Read-Host "Installation root [$InstallationRoot]";if($rootInput){$InstallationRoot=$rootInput}}
    $install=Resolve-SafeRoot $InstallationRoot
    $manifest=Join-Path $bundle 'SHA256SUMS.json';$metadataPath=Join-Path $bundle 'BUILD-METADATA.json';$verifier=Join-Path $bundle 'tools\verify_manifest.py';$wheelhouse=Join-Path $bundle 'wheelhouse'
    if(-not(Test-Path -LiteralPath $manifest -PathType Leaf)-or-not(Test-Path -LiteralPath $verifier -PathType Leaf)){throw "Bundle manifest or verifier is missing."}
    Write-Phase "1 - Bundle verification";Invoke-Checked $PythonExecutable @($verifier,$bundle) "manifest verification"
    try{$metadata=Get-Content -LiteralPath $metadataPath -Raw|ConvertFrom-Json}catch{throw "Bundle provenance metadata is missing or malformed."}
    foreach($field in @('application_version','source_commit','built_at_utc','python_implementation','python_version','platform','architecture')){if(-not[string]$metadata.$field){throw "Bundle provenance metadata is incomplete."}}
    Write-Host "Application version: $($metadata.application_version)";Write-Host "Source commit: $($metadata.source_commit)";Write-Host "Built at UTC: $($metadata.built_at_utc)";Write-Host "Bundle Python: $($metadata.python_implementation) $($metadata.python_version)";Write-Host "Bundle platform: $($metadata.platform) $($metadata.architecture)"
    if($metadata.platform-ne'Windows'){throw "Bundle platform does not match Windows."};$runtime=Get-PythonRuntime $PythonExecutable;$bundleArch=ConvertTo-NormalizedArchitecture ([string]$metadata.architecture)
    if($bundleArch-ne$runtime.normalized_architecture){throw "Bundle architecture does not match the selected Python interpreter."};$expectedBits=if($bundleArch-eq'x86'){32}else{64};if($runtime.bits-ne$expectedBits){throw "Python interpreter bitness does not match the bundle wheelhouse."}
    if([string]$runtime.implementation-ne[string]$metadata.python_implementation){throw "Python implementation does not match the bundle."};if(($runtime.version-split'\.')[0..1]-join'.'-ne(([string]$metadata.python_version-split'\.')[0..1]-join'.')){throw "Python major/minor version does not match the bundle."}
    $config=Join-Path $install 'config\config.yaml';$sevenZip=Resolve-SevenZip $SevenZipExecutable $config;Write-Host "7-Zip: $sevenZip"
    if($PlanOnly){Write-Host "PLAN OK: integrity, compatibility, 7-Zip, and safe paths validated; no installation actions were performed.";exit 0}
    New-Item -ItemType Directory -Path $install -Force|Out-Null;$script:SetupLog=Join-Path $install 'setup-windows.log';Write-SafeLog "bundle=$bundle installation_root=$install"
    $venv=Join-Path $install "venv-$($metadata.application_version)";$venvPython=Join-Path $venv 'Scripts\python.exe';$dataPath=Join-Path $install 'data';$testDownloadsPath=Join-Path $install 'test-downloads';$logPath=Join-Path $install 'logs\nexus-jar-sync.log'
    Write-Host "Operational data: $dataPath";Write-Host "Logs: $logPath";Write-Host "Test downloads: $testDownloadsPath"
    Write-Phase "2 - Offline installation"
    if(Test-Path -LiteralPath $venv){Write-Host "Existing environment preserved: $venv";if(-not(Test-Environment $venvPython $metadata.application_version)){throw "Existing environment is not a validated compatible installation and will not be overwritten."};if(-not(Confirm-Step "Use this validated existing environment?")){exit 0}}
    else{
        $privateVenv=Join-Path $install ".njs-venv-$([Guid]::NewGuid().ToString('N').Substring(0,12))";New-Item -ItemType Directory -Path $privateVenv|Out-Null;Set-Content -LiteralPath (Join-Path $privateVenv '.njs-setup-owned') -Value 'nexus-jar-sync guided setup' -Encoding ASCII
        try{Invoke-Checked $PythonExecutable @('-m','venv',$privateVenv) "virtual environment creation";$privatePython=Join-Path $privateVenv 'Scripts\python.exe';Invoke-Checked $privatePython @('-m','pip','install','--no-index','--find-links',$wheelhouse,'nexus-jar-sync') "offline package installation";if(-not(Test-Environment $privatePython $metadata.application_version)){throw "Private environment validation failed."};Invoke-Checked $privatePython @('-m','nexus_jar_sync.main','--help') "installed help";Set-Content -LiteralPath (Join-Path $privateVenv '.njs-environment-complete.json') -Value ('{"application_version":"'+$metadata.application_version+'"}') -Encoding ASCII;if(Test-Path -LiteralPath $venv){throw "Final environment appeared during installation and was not overwritten."};Move-Item -LiteralPath $privateVenv -Destination $venv}
        catch{if((Test-Path -LiteralPath $privateVenv)-and(Test-Path -LiteralPath (Join-Path $privateVenv '.njs-setup-owned') -PathType Leaf)){Remove-Item -LiteralPath $privateVenv -Recurse -Force};throw}
    }
    Write-Phase "3 - Configuration preparation"
    $configOutput=$config;$replaceConfig=$false
    if(Test-Path -LiteralPath $config){
        Write-Host "Existing configuration preserved: $config"
        Invoke-Checked $venvPython @('-c','from nexus_jar_sync.config import load_config;import sys;load_config(sys.argv[1])',$config) "existing configuration validation"
        if(-not(Confirm-Step "Back up and replace the existing configuration?")){Write-Host "Using the validated existing configuration."}
        else{$replaceConfig=$true;$configOutput="$config.new-$([Guid]::NewGuid().ToString('N').Substring(0,8))"}
    }
    if(-not(Test-Path -LiteralPath $config)-or$replaceConfig){
        $nexusUrl=Read-Host "Nexus base URL";$repository=Read-Host "Repository";$groupId=Read-Host "Group ID";$primaryId=Read-Host "Primary artifact ID [windows-versions]";if(-not$primaryId){$primaryId='windows-versions'}
        $windowsObs=Read-Host "Windows obfuscated artifact ID [windows-obs]";if(-not$windowsObs){$windowsObs='windows-obs'};$linuxVersions=Read-Host "Linux artifact ID [linux-versions]";if(-not$linuxVersions){$linuxVersions='linux-versions'};$linuxObs=Read-Host "Linux obfuscated artifact ID [linux-obs]";if(-not$linuxObs){$linuxObs='linux-obs'}
        $destination=Read-Host "Absolute release destination";$dependenciesUrl=Read-Host "Dependencies direct URL";$licenseUrl=Read-Host "License direct URL";$caBundle=Read-Host "Optional absolute CA bundle path (blank for none)"
        $allowHttp=$false;if(@($nexusUrl,$dependenciesUrl,$licenseUrl)|Where-Object{$_ -match '^http://'}){$allowHttp=Confirm-Step "HTTP has no transport integrity. Explicitly accept HTTP for this trusted internal environment?";if(-not$allowHttp){throw "HTTP use was not accepted."}}
        $credentialArgs=@();if(Confirm-Step "Configure credential environment-variable names? (anonymous is the default)"){$userEnv=Read-Host "Username environment-variable name";$passwordEnv=Read-Host "Credential secret environment-variable name";$credentialArgs=@('--username-env',$userEnv,'--password-env',$passwordEnv)}
        $generator=Join-Path $bundle 'tools\generate_windows_config.py';$generatorArgs=@($generator,'--output',$configOutput,'--nexus-url',$nexusUrl,'--repository',$repository,'--group-id',$groupId,'--primary-artifact-id',$primaryId,'--windows-obs',$windowsObs,'--linux-versions',$linuxVersions,'--linux-obs',$linuxObs,'--destination',$destination,'--dependencies-url',$dependenciesUrl,'--license-url',$licenseUrl,'--seven-zip',$sevenZip,'--log-file',$logPath,'--state-directory',(Join-Path $install 'data\state'))+$credentialArgs
        if($caBundle){$generatorArgs+=@('--ca-bundle',$caBundle)};if($allowHttp){$generatorArgs+='--allow-http'}
        Invoke-Checked $venvPython $generatorArgs "configuration generation"
        if($replaceConfig){$backup="$config.backup-$([DateTime]::UtcNow.ToString('yyyyMMddHHmmss'))";[IO.File]::Replace($configOutput,$config,$backup);Write-Host "Original configuration backed up: $backup"}
    }
    $configuredSevenZip=Get-ConfiguredSevenZip $config;if(-not$configuredSevenZip-or((Resolve-FullPathField $configuredSevenZip 'Configured 7-Zip path')-ne(Resolve-FullPathField $sevenZip 'Reviewed 7-Zip path'))){throw "Configuration must reference the reviewed absolute 7-Zip executable path."}
    Write-Phase "4 - Credential readiness"
    $credentialJson=& $venvPython -c "import json,yaml,sys;d=yaml.safe_load(open(sys.argv[1],encoding='utf-8'));n=[];a=lambda x:[n.append(x.get(k)) for k in ('username_env','password_env') if x and x.get(k)];a(d.get('defaults',{}).get('auth'));[(a(t.get('auth')),[a(c.get('auth')) for c in t.get('companions',[])]) for t in d.get('targets',[])];print(json.dumps(sorted(set(x for x in n if x))))" $config;if($LASTEXITCODE-ne0){throw "Could not inspect credential variable names."}
    $missing=$false;$schedulerReady=$true;foreach($name in($credentialJson|ConvertFrom-Json)){$process=$null-ne[Environment]::GetEnvironmentVariable([string]$name,'Process');$user=$null-ne[Environment]::GetEnvironmentVariable([string]$name,'User');$machine=$null-ne[Environment]::GetEnvironmentVariable([string]$name,'Machine');Write-Host "$name : PROCESS=$(if($process){'SET'}else{'MISSING'}) USER=$(if($user){'SET'}else{'MISSING'}) MACHINE=$(if($machine){'SET'}else{'MISSING'})";if(-not$process){$missing=$true};if(-not($user-or$machine)){$schedulerReady=$false}}
    if($missing){Write-Host "Current-process credentials are incomplete. Configure them through an approved mechanism and rerun.";exit 3};Invoke-Checked $venvPython @('-c','from nexus_jar_sync.config import load_config;import sys;load_config(sys.argv[1])',$config) "authoritative configuration validation";if(-not$schedulerReady){Write-Host "Current-process-only credentials are not scheduler-ready for the current user."}
    Write-Phase "5 - Isolated real test download";Write-Host "This performs real Nexus GET requests, downloads files, and runs 7-Zip. It never writes Nexus or production state/destinations.";if(-not(Confirm-Step "Start the isolated real test download?")){exit 0};$testOutput=Get-UniqueTestOutput $install;Invoke-Checked $venvPython @('-m','nexus_jar_sync.main','--config',$config,'--test-download','--test-output',$testOutput) "isolated test download" $install;Write-Host "Test output preserved at: $testOutput";if(-not(Confirm-Step "Have you inspected and approved the test output?")){exit 0}
    Write-Phase "6 - Production preflight";Invoke-Checked $venvPython @('-m','nexus_jar_sync.main','--config',$config,'--dry-run') "production dry-run" $install;Write-Host "The next operation performs real production downloads into configured destinations.";if(-not(Confirm-Step "Run one active production synchronization?")){exit 0};Invoke-Checked $venvPython @('-m','nexus_jar_sync.main','--config',$config) "active production synchronization" $install;Write-Host "Production preflight succeeded. Application log: $logPath"
    Write-Phase "7 - Optional Task Scheduler installation";Write-Host "Choose scheduler identity: [I] current-user logged-on-only, [H] credential-backed headless, [D] defer.";$identity=(Read-Host "Scheduler identity [D]").Trim().ToUpperInvariant();if(-not$identity){$identity='D'};if($identity-notin@('I','H','D')){throw "Scheduler identity must be I, H, or D."};if($identity-eq'D'){Write-Host "Installation is complete but not scheduled.";exit 0};if(-not(Confirm-Step "Configure Task Scheduler now?")){exit 0}
    $credential=$null;if($identity-eq'I'){Write-Host "The task will run only while the current user is logged on.";Write-SafeLog "scheduler_identity=current-user-interactive logged_on_only=true"}else{Write-Host "Headless task credentials are Windows account credentials, not Nexus credentials.";$credential=Get-Credential -Message "Windows account for the headless scheduled task"}
    Write-Host "The selected account needs Nexus network access; config/CA read; destination/state/log read-write; credential variables; and 7-Zip access. A different account was not validated by this setup user's preflight."
    $taskName=(Read-Host "Task name [NexusJarSync]").Trim();if(-not$taskName){$taskName='NexusJarSync'};$intervalText=(Read-Host "Interval minutes [5]").Trim();if(-not$intervalText){$intervalText='5'};$interval=0;if(-not[int]::TryParse($intervalText,[ref]$interval)-or$interval-le0){throw "Interval must be a positive integer."};if(-not(Confirm-Step "Register the exact root task '$taskName'?")){exit 0}
    $schedulerScript=Join-Path $bundle 'deployment\windows\install-task.ps1';$schedulerArgs=@{ProjectDirectory=$install;ConfigPath=$config;PythonExecutable=$venvPython;IntervalMinutes=$interval;TaskName=$taskName};if($credential){$schedulerArgs.TaskCredential=$credential};& $schedulerScript @schedulerArgs;if($LASTEXITCODE-ne0){throw "Application installation succeeded, but scheduler registration failed."};Write-SafeLog "scheduler_registration task=$taskName identity=$identity exit_code=0";Write-Host "Scheduled Task registration succeeded: \$taskName";exit 0
}catch{$message=$_.Exception.Message;Write-Error $message;Write-SafeLog "FAILED sanitized_message=$message";exit 1}
