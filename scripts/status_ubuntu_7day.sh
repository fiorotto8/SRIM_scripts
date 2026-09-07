#!/usr/bin/env bash
set -eu

scan_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
run_dir=${1:-"$scan_root/runs/noble_cf4_7day_ubuntu"}
run_dir=$(realpath -m -- "$run_dir")
pid_file="$run_dir/campaign.pid"

if [ -s "$pid_file" ]; then
    pid=$(sed -n '1p' "$pid_file")
    if kill -0 "$pid" 2>/dev/null; then
        echo "launcher: running (PID $pid)"
    else
        echo "launcher: stopped (stale PID $pid)"
    fi
else
    echo "launcher: stopped"
fi
if [ -e "$run_dir/logs/latest.log" ]; then
    echo "latest log: $(realpath -- "$run_dir/logs/latest.log")"
fi
if [ -f "$run_dir/jobs.sqlite" ]; then
    "$scan_root/.venv/bin/python" "$scan_root/srim_scan.py" status --run-dir "$run_dir"
else
    echo "checkpoint: not initialized"
fi
