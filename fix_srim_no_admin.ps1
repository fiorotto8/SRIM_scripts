param(
    [string]$Root = "C:\SRIM-2013",
    [switch]$CheckOnly,
    [switch]$SkipExecutionTest,
    [switch]$ClearBatchCompatibility
)

$ErrorActionPreference = "Stop"

function Test-IsAdmin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Resolve-SafeSrimRoot {
    param([string]$PathText)
    $resolved = (Resolve-Path -LiteralPath $PathText).Path
    $blockedRoots = @(
        $env:ProgramFiles,
        ${env:ProgramFiles(x86)},
        $env:WINDIR
    ) | Where-Object { $_ }
    foreach ($blocked in $blockedRoots) {
        if ($resolved.StartsWith($blocked, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to modify SRIM under protected folder: $resolved"
        }
    }
    return $resolved
}

function Get-CompatValue {
    param([string]$RegistryPath, [string]$ExePath)
    if (-not (Test-Path -LiteralPath $RegistryPath)) {
        return $null
    }
    $props = Get-ItemProperty -Path $RegistryPath
    $prop = $props.PSObject.Properties[$ExePath]
    if ($prop) {
        return [string]$prop.Value
    }
    return $null
}

function Set-CompatWithoutRunAsAdmin {
    param([string]$RegistryPath, [string]$ExePath, [string]$Value)
    $tokens = $Value -split "\s+" | Where-Object { $_ -and ($_ -ne "RUNASADMIN") }
    $newValue = ($tokens -join " ").Trim()
    if ($newValue -eq "" -or $newValue -eq "~") {
        Remove-ItemProperty -Path $RegistryPath -Name $ExePath -ErrorAction SilentlyContinue
        return "<removed>"
    }
    Set-ItemProperty -Path $RegistryPath -Name $ExePath -Value $newValue
    return $newValue
}

function Remove-CompatValue {
    param([string]$RegistryPath, [string]$ExePath)
    Remove-ItemProperty -Path $RegistryPath -Name $ExePath -ErrorAction SilentlyContinue
    return "<removed>"
}

function Get-ManifestElevationHint {
    param([string]$ExePath)
    try {
        $bytes = [IO.File]::ReadAllBytes($ExePath)
        $text = [Text.Encoding]::ASCII.GetString($bytes)
    } catch {
        return "unreadable: $($_.Exception.Message)"
    }
    if ($text -match "requireAdministrator") {
        return "requires elevation: embedded requireAdministrator string found"
    }
    if ($text -match "highestAvailable") {
        return "highestAvailable string found"
    }
    if ($text -match "asInvoker") {
        return "asInvoker string found"
    }
    return "no embedded elevation manifest string found"
}

function Get-IcaclsPath {
    $candidates = @()
    if ($env:SystemRoot) {
        $candidates += (Join-Path $env:SystemRoot "System32\icacls.exe")
        $candidates += (Join-Path $env:SystemRoot "Sysnative\icacls.exe")
    }
    if ($env:WINDIR) {
        $candidates += (Join-Path $env:WINDIR "System32\icacls.exe")
        $candidates += (Join-Path $env:WINDIR "Sysnative\icacls.exe")
    }
    $cmd = Get-Command "icacls.exe" -ErrorAction SilentlyContinue
    if ($cmd) {
        $candidates += $cmd.Source
    }
    foreach ($candidate in ($candidates | Where-Object { $_ } | Select-Object -Unique)) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }
    throw "Could not find icacls.exe. Run this script from Windows PowerShell or repair the Windows System32 PATH."
}

function Invoke-Icacls {
    param(
        [string]$Path,
        [string[]]$Arguments
    )
    $stdoutPath = [IO.Path]::GetTempFileName()
    $stderrPath = [IO.Path]::GetTempFileName()
    try {
        $proc = Start-Process -FilePath $Path -ArgumentList $Arguments -Wait -PassThru `
            -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
        $script:LastIcaclsExitCode = $proc.ExitCode
        $output = @()
        if (Test-Path -LiteralPath $stdoutPath) {
            $output += Get-Content -LiteralPath $stdoutPath -ErrorAction SilentlyContinue
        }
        if (Test-Path -LiteralPath $stderrPath) {
            $output += Get-Content -LiteralPath $stderrPath -ErrorAction SilentlyContinue
        }
        return $output
    } finally {
        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    }
}

$srimRoot = Resolve-SafeSrimRoot $Root
$isAdmin = Test-IsAdmin
$icacls = Get-IcaclsPath

Write-Host "SRIM root: $srimRoot"
Write-Host "Current user: $env:USERDOMAIN\$env:USERNAME"
Write-Host "PowerShell elevated: $isAdmin"
Write-Host "icacls path: $icacls"
Write-Host ""

$primaryExePaths = @(
    Join-Path $srimRoot "SRIM.exe"
    Join-Path $srimRoot "TRIM.exe"
    Join-Path $srimRoot "SR.exe"
    Join-Path $srimRoot "TIN.exe"
    Join-Path $srimRoot "SR Module\SRModule.exe"
)
$exePaths = @($primaryExePaths)
$exePaths += Get-ChildItem -LiteralPath $srimRoot -Filter "*.exe" -File -Recurse -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName }
$exePaths = $exePaths | Where-Object { Test-Path -LiteralPath $_ } | Sort-Object -Unique
$primaryExePaths = $primaryExePaths | Where-Object { Test-Path -LiteralPath $_ } | Sort-Object -Unique
$batchExePaths = @(
    Join-Path $srimRoot "TRIM.exe"
    Join-Path $srimRoot "SR Module\SRModule.exe"
) | Where-Object { Test-Path -LiteralPath $_ } | Sort-Object -Unique

Write-Host "Current root permissions:"
$aclOutput = Invoke-Icacls $icacls @($srimRoot)
$aclOutput | Select-Object -First 20
Write-Host ""

if (-not $CheckOnly) {
    Write-Host "Granting current user full control under $srimRoot ..."
    $account = "$env:USERDOMAIN\$env:USERNAME"
    $grantOutput = Invoke-Icacls $icacls @($srimRoot, "/grant", "${account}:(OI)(CI)F", "/T", "/C")
    $grantOutput | Out-Host
    if ($script:LastIcaclsExitCode -ne 0) {
        Write-Host "  Some ACL updates failed. Re-run this script once from an elevated PowerShell if those files need repair."
    }
    Write-Host ""
} else {
    Write-Host "CheckOnly: not changing permissions."
    Write-Host ""
}

Write-Host "Embedded manifest/elevation string scan:"
foreach ($exePath in $exePaths) {
    $hint = Get-ManifestElevationHint $exePath
    if (($primaryExePaths -contains $exePath) -or ($hint -match "requireAdministrator|highestAvailable|asInvoker")) {
        Write-Host "  $exePath : $hint"
    }
}
Write-Host ""

$registryPaths = @(
    "HKCU:\Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers",
    "HKLM:\Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"
)

foreach ($registryPath in $registryPaths) {
    Write-Host "Compatibility flags in ${registryPath}:"
    if ($registryPath.StartsWith("HKLM:") -and -not $isAdmin) {
        Write-Host "  HKLM check is read-only or may be unavailable without elevation."
    }
    if (-not (Test-Path -LiteralPath $registryPath)) {
        Write-Host "  <registry key missing>"
        continue
    }
    foreach ($exePath in $exePaths) {
        $value = Get-CompatValue $registryPath $exePath
        if ($null -eq $value) {
            continue
        }
        Write-Host "  $exePath = $value"
        $isBatchExe = $batchExePaths -contains $exePath
        if ($ClearBatchCompatibility -and $isBatchExe) {
            if ($CheckOnly) {
                Write-Host "    CheckOnly: all compatibility flags would be removed for this batch executable."
            } elseif ($registryPath.StartsWith("HKLM:") -and -not $isAdmin) {
                Write-Host "    Skipped HKLM removal because PowerShell is not elevated."
            } else {
                $newValue = Remove-CompatValue $registryPath $exePath
                Write-Host "    Batch compatibility flags removed; new value: $newValue"
            }
        } elseif ($value -match "\bRUNASADMIN\b") {
            if ($CheckOnly) {
                Write-Host "    CheckOnly: RUNASADMIN would be removed."
            } elseif ($registryPath.StartsWith("HKLM:") -and -not $isAdmin) {
                Write-Host "    Skipped HKLM removal because PowerShell is not elevated."
            } else {
                $newValue = Set-CompatWithoutRunAsAdmin $registryPath $exePath $value
                Write-Host "    RUNASADMIN removed; new value: $newValue"
            }
        }
    }
    Write-Host ""
}

if (-not $SkipExecutionTest) {
    $srModule = Join-Path $srimRoot "SR Module\SRModule.exe"
    if (Test-Path -LiteralPath $srModule) {
        Write-Host "Testing SRModule launch from this PowerShell context..."
        if ($isAdmin) {
            Write-Host "  This shell is elevated. Re-run with -CheckOnly -SkipExecutionTest omitted from a normal shell to confirm non-elevated launch."
        }
        $work = Split-Path -Parent $srModule
        $proc = Start-Process -FilePath $srModule -WorkingDirectory $work -WindowStyle Hidden -PassThru -Wait
        Write-Host "  SRModule exit code: $($proc.ExitCode)"
        if ($proc.ExitCode -eq 0) {
            Write-Host "  SRModule execution succeeded in this shell."
        } else {
            Write-Host "  SRModule returned a non-zero exit code."
        }
    }
} else {
    Write-Host "Skipping execution test by request."
}

Write-Host ""
Write-Host "Done. This script does not unregister OCX files and does not modify Windows system directories."
