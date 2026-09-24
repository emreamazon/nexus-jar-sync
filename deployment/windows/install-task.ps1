[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectDirectory,

    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,

    [Parameter(Mandatory = $true)]
    [string]$PythonExecutable,

    [Parameter(Mandatory = $false)]
    [ValidateRange(1, 2147483647)]
    [int]$IntervalMinutes = 5,

    [Parameter(Mandatory = $false)]
    [ValidateNotNullOrEmpty()]
    [string]$TaskName = 'NexusJarSync',

    [Parameter(Mandatory = $false)]
    [System.Management.Automation.PSCredential]$TaskCredential,

    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($TaskName)) {
    throw 'TaskName must not be empty.'
}

function Resolve-RequiredPath {
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath,
        [Parameter(Mandatory = $true)][string]$Description,
        [Parameter(Mandatory = $true)][ValidateSet('Container', 'Leaf')][string]$PathType
    )

    if (-not (Test-Path -LiteralPath $LiteralPath -PathType $PathType)) {
        throw "$Description does not exist or has the wrong type: $LiteralPath"
    }
    $resolved = (Resolve-Path -LiteralPath $LiteralPath).Path
    if (-not [System.IO.Path]::IsPathFullyQualified($resolved)) {
        throw "$Description did not resolve to an absolute path."
    }
    return $resolved
}

# Resolve every input before inspecting or modifying Task Scheduler.
$resolvedProject = Resolve-RequiredPath -LiteralPath $ProjectDirectory -Description 'ProjectDirectory' -PathType Container
$resolvedConfig = Resolve-RequiredPath -LiteralPath $ConfigPath -Description 'ConfigPath' -PathType Leaf
$resolvedPython = Resolve-RequiredPath -LiteralPath $PythonExecutable -Description 'PythonExecutable' -PathType Leaf

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $existingTask -and -not $Force) {
    throw "Scheduled task '$TaskName' already exists. Re-run with -Force to replace only this task."
}

$arguments = '-m nexus_jar_sync.main --config "{0}"' -f $resolvedConfig
$action = New-ScheduledTaskAction `
    -Execute $resolvedPython `
    -Argument $arguments `
    -WorkingDirectory $resolvedProject
$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At ((Get-Date).AddMinutes(1)) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew
$description = 'Runs one nexus-jar-sync synchronization pass. Nexus credentials are supplied through the task account environment.'

if ($PSCmdlet.ShouldProcess($TaskName, 'Register scheduled task')) {
    if ($null -ne $TaskCredential) {
        # The ScheduledTasks API requires the account password as a string. It exists
        # only in memory for this registration call and is never printed or persisted here.
        $accountName = $TaskCredential.UserName
        $accountPassword = $TaskCredential.GetNetworkCredential().Password
        try {
            Register-ScheduledTask `
                -TaskName $TaskName `
                -Action $action `
                -Trigger $trigger `
                -Settings $settings `
                -Description $description `
                -User $accountName `
                -Password $accountPassword `
                -Force:$Force | Out-Null
        }
        finally {
            $accountPassword = $null
        }
    }
    else {
        $currentAccount = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $principal = New-ScheduledTaskPrincipal `
            -UserId $currentAccount `
            -LogonType Interactive `
            -RunLevel Limited
        Register-ScheduledTask `
            -TaskName $TaskName `
            -Action $action `
            -Trigger $trigger `
            -Settings $settings `
            -Description $description `
            -Principal $principal `
            -Force:$Force | Out-Null
    }

    if ($null -ne $existingTask) {
        Write-Host "Replaced scheduled task '$TaskName'."
    }
    else {
        Write-Host "Installed scheduled task '$TaskName'."
    }
}
