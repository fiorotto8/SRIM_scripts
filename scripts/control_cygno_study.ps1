# Interactive control; process ownership and recovery remain in native Python.
[CmdletBinding()]
param(
    [ValidateSet('menu', 'stop', 'resume', 'status', 'recover')][string]$Action = 'menu',
    [string]$RunDir = '',
    [ValidateRange(0, 2147483647)][int]$Workers = 0,
    [string]$Config = '',
    [string]$SrimRoot = '',
    [string]$Python = ''
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($Action -eq 'menu') {
    Write-Host 'CYGNO scan'
    Write-Host '  1  Stop after current batches finish'
    Write-Host '  2  Resume from completed checkpoints'
    Write-Host '  3  Show status'
    Write-Host '  4  Recover verified stale locks'
    Write-Host '  5  Close'
    switch (Read-Host 'Choose 1, 2, 3, 4 or 5') {
        '1' { $Action = 'stop' }
        '2' { $Action = 'resume' }
        '3' { $Action = 'status' }
        '4' { $Action = 'recover' }
        '5' { return }
        default { throw 'Please choose 1, 2, 3, 4 or 5.' }
    }
}
$driver = Join-Path $PSScriptRoot 'cygno_campaign.py'
$options = @('-B', $driver, $Action)
if ($RunDir) { $options += @('--run-dir', $RunDir) }
if ($Workers) { $options += @('--workers', [string]$Workers) }
if ($Config) { $options += @('--config', $Config) }
if ($SrimRoot) { $options += @('--srim-root', $SrimRoot) }
if ($Python) { & $Python @options } else { & py -3.15 @options }
exit $LASTEXITCODE
