#!/usr/bin/env bash
# Native Windows SRIM coordinator; WSL delegates once, then Windows owns execution.
set -euo pipefail

scan_root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
usage() {
    cat <<'USAGE'
Usage: scripts/start_cygno_study.sh [--config FILE] [--run-dir DIR]
       [--srim-root DIR] [--workers N]

Launch a detached native Windows coordinator through PowerShell.
Reports are rendered separately with the WSL scientific Python environment.
CYGNO_WINDOWS_PYTHON optionally selects a native Windows Python 3.11+ executable.
USAGE
}
if [[ -z "${WSL_DISTRO_NAME:-}" && "$(</proc/sys/kernel/osrelease)" != *[Mm]icrosoft* ]]; then
    printf '%s\n' 'Run this wrapper in WSL, or use scripts/start_cygno_study.ps1 in Windows.' >&2
    exit 2
fi
command -v wslpath >/dev/null || { printf '%s\n' 'Required WSL command unavailable: wslpath' >&2; exit 2; }
powershell=/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe
if [[ ! -f "$powershell" ]]; then
    printf '%s\n' 'Windows PowerShell executable is unavailable.' >&2
    exit 2
fi
arguments=()
while (( $# )); do
    case "$1" in
        --config|--run-dir|--srim-root|--workers)
            if (( $# < 2 )) || [[ -z "$2" || "$2" == --* ]]; then
                printf '%s requires a value\n' "$1" >&2; exit 2
            fi
            case "$1" in
                --config) arguments+=(-Config "$(wslpath -aw -- "$2")") ;;
                --run-dir) arguments+=(-RunDir "$(wslpath -aw -- "$2")") ;;
                --srim-root) arguments+=(-SrimRoot "$(wslpath -aw -- "$2")") ;;
                --workers)
                    if [[ ! "$2" =~ ^[1-9][0-9]*$ ]]; then
                        printf '%s\n' '--workers must be a positive integer' >&2; exit 2
                    fi
                    arguments+=(-Workers "$2") ;;
            esac
            shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'Unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done
if [[ -n "${CYGNO_WINDOWS_PYTHON:-}" ]]; then
    if [[ "$CYGNO_WINDOWS_PYTHON" =~ ^[A-Za-z]:[\\/] ]]; then
        arguments+=(-Python "$CYGNO_WINDOWS_PYTHON")
    else
        arguments+=(-Python "$(wslpath -aw -- "$CYGNO_WINDOWS_PYTHON")")
    fi
fi
exec "$powershell" -NoProfile -ExecutionPolicy Bypass -File \
    "$(wslpath -aw -- "$scan_root/scripts/start_cygno_study.ps1")" "${arguments[@]}"
