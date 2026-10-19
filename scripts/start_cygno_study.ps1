# Thin entry point for the native detached coordinator.
[CmdletBinding()]
param(
    [string]$Config = '',
    [string]$RunDir = '',
    [string]$SrimRoot = '',
    [ValidateSet('campaign', 'extend')][string]$Mode = 'campaign',
    [ValidateRange(0, 2147483647)][int]$Workers = 0,
    [string]$Python = '',
    [switch]$DeferReport
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$driver = Join-Path $PSScriptRoot 'cygno_campaign.py'
$options = @('-B', $driver, 'start', '--mode', $Mode)
if ($Config) { $options += @('--config', $Config) }
if ($RunDir) { $options += @('--run-dir', $RunDir) }
if ($SrimRoot) { $options += @('--srim-root', $SrimRoot) }
if ($Workers) { $options += @('--workers', [string]$Workers) }
if ($DeferReport) { $options += '--defer-report' }
if ($Python) { & $Python @options } else { & py -3.15 @options }
exit $LASTEXITCODE
