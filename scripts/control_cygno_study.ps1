# Interactive Windows control for the existing resumable CYGNO campaign.
[CmdletBinding()]
param(
    [ValidateSet('menu', 'stop', 'resume', 'status')][string]$Action = 'menu',
    [string]$RunDir = '',
    [ValidateRange(1, 2147483647)][int]$Workers = 8,
    [string]$Python = ''
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repo = Split-Path -Parent $PSScriptRoot
if (-not $RunDir) { $RunDir = Join-Path $repo 'runs\cygno_hecf4_900mbar_20261015' }
$RunDir = [IO.Path]::GetFullPath($RunDir)
if (-not [IO.Directory]::Exists($RunDir)) { throw "Study directory is unavailable: $RunDir" }
$stopMarker = Join-Path $RunDir '.stop-requested'
$simulationLock = Join-Path $RunDir '.simulation-lock'
$launchLease = Join-Path $RunDir '.launcher-windows.lease'
$statePath = Join-Path $RunDir 'campaign_state.json'
$state = $null
if (Test-Path -LiteralPath $statePath) {
    $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
}

if ($Action -eq 'menu') {
    Write-Host 'CYGNO scan'
    Write-Host '  1  Stop after current batches finish'
    Write-Host "  2  Resume with $Workers workers"
    Write-Host '  3  Show status'
    Write-Host '  4  Close'
    switch (Read-Host 'Choose 1, 2, 3 or 4') {
        '1' { $Action = 'stop' }
        '2' { $Action = 'resume' }
        '3' { $Action = 'status' }
        '4' { return }
        default { throw 'Please choose 1, 2, 3 or 4.' }
    }
}

switch ($Action) {
    'stop' {
        # OpenOrCreate preserves any existing stop request and its contents.
        $marker = [IO.File]::Open($stopMarker, 'OpenOrCreate', 'Write', 'ReadWrite')
        $marker.Dispose()
        Write-Host 'Stop requested. Each worker will finish and save its current batch.'
        Write-Host 'This can take a while. Check Status for stopped before shutting down.'
    }
    'resume' {
        if ((Test-Path -LiteralPath $simulationLock) -or (Test-Path -LiteralPath $launchLease)) {
            throw 'The scan is running or still stopping. Wait for it to stop; keep its lock files.'
        }
        if ($state -and $state.status -eq 'running') {
            throw 'The recorded scan is still running. Inspect its status before resuming.'
        }
        # Reuse the native Python from the last launch for this study.
        $launcherPath = Join-Path $RunDir 'launcher.json'
        if (-not $Python -and (Test-Path -LiteralPath $launcherPath)) {
            $previous = Get-Content -LiteralPath $launcherPath -Raw | ConvertFrom-Json
            if ($previous.PSObject.Properties['command'] -and @($previous.command).Count -gt 0) {
                $Python = [string]$previous.command[0]
                if (-not [IO.File]::Exists($Python)) {
                    throw "Previous Python is unavailable: $Python. Supply -Python with its new path."
                }
            }
        }
        $launcher = Join-Path $PSScriptRoot 'start_cygno_study.ps1'
        if (-not [IO.File]::Exists($launcher)) { throw "Launcher is unavailable: $launcher" }
        if (Test-Path -LiteralPath $stopMarker) { Remove-Item -LiteralPath $stopMarker }
        $options = @{ RunDir = $RunDir; Workers = $Workers }
        if ($Python) { $options.Python = $Python }
        & $launcher @options
    }
    'status' {
        Write-Host "Study: $RunDir"
        $recorded = if ($state) { $state.status } else { 'not started' }
        Write-Host "Recorded state: $recorded"
        Write-Host ('Stop requested: ' + (Test-Path -LiteralPath $stopMarker))
        Write-Host ('Worker lock present: ' + (Test-Path -LiteralPath $simulationLock))
        if ($recorded -eq 'running' -and (Test-Path -LiteralPath $stopMarker)) {
            Write-Host 'Stopping after the active batches finish.'
        }
    }
}
