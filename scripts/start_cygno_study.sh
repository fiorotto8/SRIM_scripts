#!/usr/bin/env bash
# Native Windows SRIM backend. WSL supplies orchestration and analysis only;
# this launcher does not use Wine or PySRIM.
set -euo pipefail

scan_root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
config="$scan_root/configs/cygno_hecf4_900mbar.toml"
run_dir="$scan_root/runs/cygno_hecf4_900mbar_20261015"
srim_root="$scan_root"
workers=""

usage() {
    cat <<'USAGE'
Usage: scripts/start_cygno_study.sh [--config FILE] [--run-dir DIR]
       [--srim-root DIR] [--workers N]

Detach the native Windows SRIM study campaign from a WSL shell.
Worker count defaults to the study configuration. Existing logs are preserved.
USAGE
}

while (( $# )); do
    case "$1" in
        --config|--run-dir|--srim-root|--workers)
            if (( $# < 2 )) || [[ -z "$2" || "$2" == --* ]]; then
                printf '%s requires a value\n' "$1" >&2
                exit 2
            fi
            case "$1" in
                --config) config=$2 ;;
                --run-dir) run_dir=$2 ;;
                --srim-root) srim_root=$2 ;;
                --workers) workers=$2 ;;
            esac
            shift 2
            ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'Unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ -n "$workers" && ! "$workers" =~ ^[1-9][0-9]*$ ]]; then
    printf '%s\n' '--workers must be a positive integer' >&2
    exit 2
fi
if [[ -z "${WSL_DISTRO_NAME:-}" && "$(</proc/sys/kernel/osrelease)" != *microsoft* ]]; then
    printf '%s\n' 'Run this launcher in WSL; the simulation backend is native Windows SRIM.' >&2
    exit 2
fi
for program in python3 flock setsid nohup realpath mktemp date; do
    if ! command -v "$program" >/dev/null 2>&1; then
        printf 'Required WSL command is unavailable: %s\n' "$program" >&2
        exit 2
    fi
done

config=$(realpath -e -- "$config")
srim_root=$(realpath -e -- "$srim_root")
run_dir=$(realpath -m -- "$run_dir")
driver="$scan_root/scripts/cygno_study.py"
if [[ ! -f "$config" || ! -f "$driver" || ! -d "$srim_root" ]]; then
    printf '%s\n' 'Configuration, study driver, or SRIM source directory is unavailable.' >&2
    exit 2
fi
for required in TRIM.exe 'SR Module/SRModule.exe' Data/ATOMDATA; do
    if [[ ! -f "$srim_root/$required" ]]; then
        printf 'Required native SRIM file is unavailable: %s\n' "$srim_root/$required" >&2
        exit 2
    fi
done
if [[ "$run_dir" == "$srim_root" || "$run_dir" == "$scan_root" ]]; then
    printf '%s\n' 'Select a separate run directory for campaign state and logs.' >&2
    exit 2
fi

mkdir -p -- "$run_dir"
# Serialize launch attempts only. The campaign owns its .simulation-lock;
# the detached child must not inherit this launcher lock descriptor.
exec 9>"$run_dir/.launcher.lock"
if ! flock -n 9; then
    printf 'Another launcher is inspecting this study: %s\n' "$run_dir" >&2
    exit 73
fi
if [[ -e "$run_dir/.simulation-lock" ]]; then
    printf 'Study lock already exists: %s\n' "$run_dir/.simulation-lock" >&2
    if [[ -f "$run_dir/.simulation-lock/owner.json" ]]; then
        head -c 4096 -- "$run_dir/.simulation-lock/owner.json" >&2
        printf '\n' >&2
    fi
    printf '%s\n' 'Verify the owner and native SRIM processes before resolving a stale study lock.' >&2
    exit 73
fi
if [[ -e "$run_dir/.stop-requested" ]]; then
    printf 'A stop request is present: %s\n' "$run_dir/.stop-requested" >&2
    printf '%s\n' 'Clear it explicitly before resuming this campaign.' >&2
    exit 73
fi

pid_file="$run_dir/campaign.pid"
if [[ -s "$pid_file" ]]; then
    existing_pid=$(head -n 1 -- "$pid_file")
    if [[ "$existing_pid" =~ ^[1-9][0-9]*$ ]] && kill -0 "$existing_pid" 2>/dev/null; then
        if python3 -B - "$existing_pid" "$driver" "$run_dir" <<'PY'
from pathlib import Path
import os
import sys
try:
    argv = [os.fsdecode(value) for value in Path(f'/proc/{sys.argv[1]}/cmdline').read_bytes().split(b'\0') if value]
except OSError:
    sys.exit(1)
run_arguments = [argv[i + 1] for i, value in enumerate(argv[:-1]) if value == '--run-dir']
sys.exit(0 if sys.argv[2] in argv and 'campaign' in argv and sys.argv[3] in run_arguments else 1)
PY
        then
            printf 'Campaign already running with PID %s\n' "$existing_pid" >&2
            exit 73
        fi
    fi
fi

mkdir -p -- "$run_dir/logs"
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
log_file=$(mktemp "$run_dir/logs/campaign-$timestamp.XXXXXX.log")
campaign_command=(python3 -u "$driver" campaign --config "$config" --run-dir "$run_dir" --srim-root "$srim_root")
if [[ -n "$workers" ]]; then
    campaign_command+=(--workers "$workers")
fi
cd -- "$scan_root"
setsid nohup "${campaign_command[@]}" </dev/null >>"$log_file" 2>&1 9>&- &
campaign_pid=$!
printf '%s\n' "$campaign_pid" >"$pid_file.tmp.$$"
mv -- "$pid_file.tmp.$$" "$pid_file"

write_launcher_record() {
    python3 -B - "$run_dir/launcher.json" "$campaign_pid" "$log_file" "$1" "${campaign_command[@]}" <<'PY'
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import sys
path = Path(sys.argv[1])
record = {'pid': int(sys.argv[2]), 'log': sys.argv[3], 'status': sys.argv[4],
          'updated_at': datetime.now(timezone.utc).isoformat(), 'command': sys.argv[5:],
          'backend': 'native Windows SRIM executables', 'environment': 'WSL orchestration and analysis'}
temporary = path.with_name(path.name + f'.tmp.{os.getpid()}')
temporary.write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
temporary.replace(path)
PY
}
write_launcher_record starting
sleep 1
if ! kill -0 "$campaign_pid" 2>/dev/null; then
    exit_code=0
    wait "$campaign_pid" || exit_code=$?
    write_launcher_record exited_before_ready
    printf 'Campaign exited during startup (exit %s). Log: %s\n' "$exit_code" "$log_file" >&2
    tail -n 25 -- "$log_file" >&2 || true
    exit 1
fi
write_launcher_record running
printf 'Native Windows SRIM campaign PID: %s\nLog: %s\n' "$campaign_pid" "$log_file"
printf 'Status: python3 -u %q status --config %q --run-dir %q --srim-root %q\n' "$driver" "$config" "$run_dir" "$srim_root"
printf 'Log: tail -f -- %q\n' "$log_file"
printf 'Stop after the active job(s): touch -- %q\n' "$run_dir/.stop-requested"
