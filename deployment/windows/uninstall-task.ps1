[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [Parameter(Mandatory = $false)]
    [ValidateNotNullOrEmpty()]
    [string]$TaskName = 'NexusJarSync'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($TaskName)) {
    throw 'TaskName must not be empty.'
}
if ($TaskName.IndexOfAny([char[]]'*?[]/\') -ge 0) {
    throw 'TaskName must not contain wildcard characters, forward slashes, or backslashes.'
}
if ($null -ne ($TaskName.ToCharArray() | Where-Object { [char]::IsControl($_) } | Select-Object -First 1)) {
    throw 'TaskName must not contain control characters.'
}

$TaskPath = '\'

$task = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $task) {
    Write-Host "Scheduled task '$TaskName' is not installed; nothing to remove."
    return
}

if ($PSCmdlet.ShouldProcess("$TaskPath$TaskName", 'Unregister exact scheduled task')) {
    Unregister-ScheduledTask -InputObject $task -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'. Project files and data were not changed."
}
