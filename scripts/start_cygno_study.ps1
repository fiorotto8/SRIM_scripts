# Native Windows SRIM coordinator; WSL supplies optional analysis/reporting only.
[CmdletBinding()]
param(
    [string]$Config = '',
    [string]$RunDir = '',
    [string]$SrimRoot = '',
    [ValidateSet('campaign', 'extend')][string]$Mode = 'campaign',
    [ValidateRange(0, 2147483647)][int]$Workers = 0,
    [string]$Python = ''
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repo = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
if (-not $Config) { $Config = Join-Path $repo 'configs\cygno_hecf4_900mbar.toml' }
if (-not $RunDir) { $RunDir = Join-Path $repo 'runs\cygno_hecf4_900mbar_20261015' }
if (-not $SrimRoot) { $SrimRoot = $repo }
$Config = [IO.Path]::GetFullPath($Config)
$RunDir = [IO.Path]::GetFullPath($RunDir).TrimEnd('\', '/')
$SrimRoot = [IO.Path]::GetFullPath($SrimRoot).TrimEnd('\', '/')
if ((Test-Path -LiteralPath (Join-Path $RunDir 'extension_plan.json')) -and $Mode -ne 'extend') {
    throw 'This is a bounded extension. Use -Mode extend; a full adaptive campaign is not permitted here.'
}
$driver = Join-Path $repo 'scripts\cygno_study.py'

function Quote-NativeArgument([string]$Value) {
    # Windows CRT argv rules, including literal quotes and trailing backslashes.
    $quoted = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $quoted = [regex]::Replace($quoted, '(\\+)$', '$1$1')
    return '"' + $quoted + '"'
}
function Quote-PowerShellLiteral([string]$Value) { return "'" + $Value.Replace("'", "''") + "'" }
function Write-AtomicText([string]$Path, [string]$Value) {
    $temporary = $Path + '.tmp-' + [guid]::NewGuid().ToString('N')
    [IO.File]::WriteAllText($temporary, $Value, (New-Object Text.UTF8Encoding($false)))
    if ([IO.File]::Exists($Path)) {
        [IO.File]::Replace($temporary, $Path, [System.Management.Automation.Language.NullString]::Value)
    }
    else { [IO.File]::Move($temporary, $Path) }
}
function Read-Json([string]$Path) {
    if (-not [IO.File]::Exists($Path)) { return $null }
    return ([IO.File]::ReadAllText($Path) | ConvertFrom-Json)
}
function Test-OwnedCoordinator($Process) {
    if (-not $Process -or -not $Process.CommandLine) { return $false }
    $text = $Process.CommandLine.Replace('/', '\')
    $driverPath = [regex]::Escape($driver)
    $runPath = [regex]::Escape($RunDir)
    $driverPattern = '(?i)(?:^|\s)(?:"' + $driverPath + '"|' + $driverPath + ')(?=\s|$)'
    $directoryPattern = '(?i)(?:^|\s)(?:"--run-dir"|--run-dir)\s+(?:"' + $runPath + '"|' + $runPath + ')(?=\s|$)'
    return ($text -match $driverPattern -and $text -match $directoryPattern -and
            $text -match ('(?i)(?:^|\s)"?' + $Mode + '"?(?=\s|$)'))
}

foreach ($required in @($Config, $driver, (Join-Path $SrimRoot 'TRIM.exe'),
        (Join-Path $SrimRoot 'SR Module\SRModule.exe'), (Join-Path $SrimRoot 'Data\ATOMDATA'))) {
    if (-not [IO.File]::Exists($required)) { throw "Required file is unavailable: $required" }
}
if ($RunDir -eq $repo -or $RunDir -eq $SrimRoot) { throw 'Use a separate study output directory.' }
if (-not $Python) {
    $uvRoot = Join-Path $env:APPDATA 'uv\python'
    $candidates = @(Get-ChildItem -LiteralPath $uvRoot -Directory | ForEach-Object {
        if ($_.Name -match '^cpython-(\d+\.\d+\.\d+)-windows-[^-]+-none$') {
            $version = [version]$Matches[1]
            $executable = Join-Path $_.FullName 'python.exe'
            if ($version -ge [version]'3.11' -and [IO.File]::Exists($executable)) {
                [pscustomobject]@{ Version = $version; Executable = $executable }
            }
        }
    } | Sort-Object Version -Descending)
    if (-not $candidates.Count) { throw 'No native uv Python 3.11+ found. Supply -Python with its executable path.' }
    $Python = $candidates[0].Executable
}
$Python = [IO.Path]::GetFullPath($Python)
if (-not [IO.File]::Exists($Python)) { throw "Python executable is unavailable: $Python" }
$probeInfo = New-Object Diagnostics.ProcessStartInfo
$probeInfo.FileName = $Python
$probeArguments = @('-B', '-c', 'import os,sys; print(os.name); print(sys.version_info.major); print(sys.version_info.minor)')
$probeInfo.Arguments = ($probeArguments | ForEach-Object { Quote-NativeArgument $_ }) -join ' '
$probeInfo.UseShellExecute = $false
$probeInfo.CreateNoWindow = $true
$probeInfo.RedirectStandardOutput = $true
$probeInfo.RedirectStandardError = $true
$probeProcess = [Diagnostics.Process]::Start($probeInfo)
try {
    $probeOutput = $probeProcess.StandardOutput.ReadToEndAsync()
    $probeError = $probeProcess.StandardError.ReadToEndAsync()
    if (-not $probeProcess.WaitForExit(10000)) {
        $probeProcess.Kill()
        $probeProcess.WaitForExit()
        throw 'Native Python qualification timed out.'
    }
    if ($probeProcess.ExitCode -ne 0) { throw "Native Python qualification failed: $($probeError.Result)" }
    $probe = @($probeOutput.Result.Trim() -split '\r?\n')
} finally { $probeProcess.Dispose() }
if ($probe.Count -ne 3 -or $probe[0] -ne 'nt' -or
        [int]$probe[1] -ne 3 -or [int]$probe[2] -lt 11) {
    throw 'The coordinator requires native Windows Python 3.11 or newer.'
}

[IO.Directory]::CreateDirectory($RunDir) | Out-Null
$leasePath = Join-Path $RunDir '.launcher-windows.lease'
$lease = $null
$leaseId = [guid]::NewGuid().ToString('N')
try {
    try { $lease = [IO.File]::Open($leasePath, 'CreateNew', 'Write', 'Read') }
    catch { throw "Launch lease exists or cannot be acquired: $leasePath. Inspect its owner; it is never removed automatically." }
    $leaseRecord = @{ owner_pid = $PID; host = $env:COMPUTERNAME; lease_id = $leaseId;
        created_at = [DateTime]::UtcNow.ToString('o') } | ConvertTo-Json -Compress
    $leaseBytes = [Text.Encoding]::UTF8.GetBytes($leaseRecord)
    $lease.Write($leaseBytes, 0, $leaseBytes.Length)
    $lease.Flush()
    $simulationLock = Join-Path $RunDir '.simulation-lock'
    $stopMarker = Join-Path $RunDir '.stop-requested'
    if (Test-Path -LiteralPath $simulationLock) { throw "Study lock exists: $simulationLock. Verify its owner before any recovery." }
    if (Test-Path -LiteralPath $stopMarker) { throw "A stop request is present: $stopMarker. Clear it explicitly before resuming." }
    $pythonName = [IO.Path]::GetFileName($Python).Replace("'", "''")
    $processes = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe' OR Name = '$pythonName'")
    foreach ($existing in $processes) {
        if (Test-OwnedCoordinator $existing) { throw "Campaign already running with Windows PID $($existing.ProcessId)." }
    }
    foreach ($recordPath in @((Join-Path $RunDir 'launcher.json'), (Join-Path $RunDir 'campaign_state.json'))) {
        $previous = Read-Json $recordPath
        if ($previous -and $previous.PSObject.Properties['pid']) {
            $existing = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$previous.pid)"
            if ($existing -and -not $existing.CommandLine) {
                throw "Cannot verify command line of recorded Windows PID $($previous.pid); inspect it before launching."
            }
        }
    }

    $logs = Join-Path $RunDir 'logs'
    [IO.Directory]::CreateDirectory($logs) | Out-Null
    $tag = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ') + '.' + [guid]::NewGuid().ToString('N').Substring(0, 8)
    $stdoutLog = Join-Path $logs "campaign-native-$tag.out.log"
    $stderrLog = Join-Path $logs "campaign-native-$tag.err.log"
    $stdinFile = Join-Path $logs "campaign-native-$tag.stdin"
    [IO.File]::WriteAllBytes($stdinFile, [byte[]]@())
    $arguments = @('-u', '-B', $driver, $Mode, '--config', $Config,
        '--run-dir', $RunDir, '--srim-root', $SrimRoot, '--defer-report')
    if ($Workers -gt 0) { $arguments += @('--workers', [string]$Workers) }
    $argumentLine = ($arguments | ForEach-Object { Quote-NativeArgument $_ }) -join ' '
    # Windows owns this child; all standard handles are files, without WSL pipes.
    $child = Start-Process -FilePath $Python -ArgumentList $argumentLine -WorkingDirectory $SrimRoot `
        -RedirectStandardInput $stdinFile -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog `
        -WindowStyle Hidden -PassThru
    $launcherPath = Join-Path $RunDir 'launcher.json'
    $record = @{ pid = $child.Id; platform = 'nt'; coordinator_platform = 'nt'; status = 'starting';
        command = @($Python) + $arguments; log = $stdoutLog; stdout_log = $stdoutLog; stderr_log = $stderrLog;
        stdin = $stdinFile; backend = 'native Windows SRIM executables'; environment = 'native Windows coordinator';
        report_deferred = $true; updated_at = [DateTime]::UtcNow.ToString('o') }
    Write-AtomicText (Join-Path $RunDir 'campaign.pid') ([string]$child.Id + "`n")
    Write-AtomicText $launcherPath (($record | ConvertTo-Json -Depth 6) + "`n")
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    $ready = $false
    while ([DateTime]::UtcNow -lt $deadline) {
        $child.Refresh()
        if ($child.HasExited) {
            $record.status = 'exited_before_ready'
            $record.exit_code = $child.ExitCode
            break
        }
        $state = $null; $owner = $null
        try {
            $state = Read-Json (Join-Path $RunDir 'campaign_state.json')
            $owner = Read-Json (Join-Path $simulationLock 'owner.json')
        } catch [IO.IOException] { }
        if ($state -and $owner -and $state.pid -eq $child.Id -and $owner.pid -eq $child.Id -and
                $state.status -eq 'running' -and $state.coordinator_platform -eq 'nt') {
            $live = Get-CimInstance Win32_Process -Filter "ProcessId = $($child.Id)"
            if (Test-OwnedCoordinator $live) { $ready = $true; break }
        }
        Start-Sleep -Milliseconds 250
    }
    if ($ready) { $record.status = 'running' }
    elseif ($record.status -eq 'starting') { $record.status = 'startup_unconfirmed' }
    $record.updated_at = [DateTime]::UtcNow.ToString('o')
    Write-AtomicText $launcherPath (($record | ConvertTo-Json -Depth 6) + "`n")
    if (-not $ready) { throw "Coordinator startup is $($record.status). Inspect Windows PID $($child.Id) and logs before retrying: $stderrLog" }
    Write-Output "Native Windows coordinator PID: $($child.Id)"
    Write-Output "Output log: $stdoutLog"
    Write-Output "Error log: $stderrLog"
    Write-Output "Status: & $(Quote-PowerShellLiteral $Python) -B $(Quote-PowerShellLiteral $driver) status --config $(Quote-PowerShellLiteral $Config) --run-dir $(Quote-PowerShellLiteral $RunDir) --srim-root $(Quote-PowerShellLiteral $SrimRoot)"
    Write-Output "Follow log: Get-Content -LiteralPath $(Quote-PowerShellLiteral $stdoutLog) -Wait"
    Write-Output "Graceful stop after active jobs: New-Item -ItemType File -Path $(Quote-PowerShellLiteral $stopMarker)"
    Write-Output 'Render the final report separately with WSL scientific Python and the same study paths.'
} finally {
    if ($lease) {
        $lease.Dispose()
        $owner = Read-Json $leasePath
        if ($owner -and $owner.lease_id -eq $leaseId) { [IO.File]::Delete($leasePath) }
    }
}
