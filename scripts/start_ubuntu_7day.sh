#!/usr/bin/env bash
set -eu

scan_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
run_dir="$scan_root/runs/noble_cf4_7day_ubuntu"
arguments=("$@")

index=0
while [ "$index" -lt "${#arguments[@]}" ]; do
    if [ "${arguments[$index]}" = "--run-dir" ]; then
        index=$((index + 1))
        if [ "$index" -ge "${#arguments[@]}" ]; then
            echo "--run-dir requires a path" >&2
            exit 2
        fi
        run_dir=${arguments[$index]}
    fi
    index=$((index + 1))
done
run_dir=$(realpath -m -- "$run_dir")
mkdir -p "$run_dir/logs"

pid_file="$run_dir/campaign.pid"
if [ -f "$pid_file" ]; then
    existing_pid=$(sed -n '1p' "$pid_file")
    case "$existing_pid" in
        ''|*[!0-9]*) ;;
        *)
            if kill -0 "$existing_pid" 2>/dev/null; then
                echo "campaign is already running with PID $existing_pid" >&2
                exit 73
            fi
            ;;
    esac
    mv -- "$pid_file" "$pid_file.stale-$(date -u +%Y%m%dT%H%M%SZ)"
fi

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
log_file="$run_dir/logs/campaign-$timestamp.log"
ln -sfn -- "$(basename -- "$log_file")" "$run_dir/logs/latest.log"
nohup setsid "$scan_root/scripts/run_ubuntu_7day.sh" "$run_dir" "${arguments[@]}" \
    </dev/null >>"$log_file" 2>&1 &
launcher_pid=$!

attempt=0
while [ "$attempt" -lt 50 ]; do
    if [ -s "$pid_file" ]; then
        running_pid=$(sed -n '1p' "$pid_file")
        if [ "$running_pid" = "$launcher_pid" ] && kill -0 "$running_pid" 2>/dev/null; then
            echo "started SRIM campaign PID $running_pid"
            echo "log: $log_file"
            exit 0
        fi
    fi
    if ! kill -0 "$launcher_pid" 2>/dev/null; then
        echo "campaign launcher exited before startup; log follows:" >&2
        tail -n 20 "$log_file" >&2 || true
        exit 1
    fi
    sleep 0.1
    attempt=$((attempt + 1))
done

echo "campaign process $launcher_pid started but did not publish its PID file" >&2
echo "inspect $log_file" >&2
exit 1
