#!/usr/bin/env bash
set -eu

scan_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
run_dir=${1:-"$scan_root/runs/noble_cf4_7day_ubuntu"}
run_dir=$(realpath -m -- "$run_dir")
pid_file="$run_dir/campaign.pid"

if [ ! -s "$pid_file" ]; then
    echo "campaign is not running (no PID file at $pid_file)"
    exit 0
fi
pid=$(sed -n '1p' "$pid_file")
case "$pid" in
    ''|*[!0-9]*) echo "invalid campaign PID file: $pid_file" >&2; exit 1 ;;
esac
if ! kill -0 "$pid" 2>/dev/null; then
    echo "campaign is not running (stale PID $pid)"
    exit 0
fi

echo "requesting graceful checkpoint stop for campaign PID $pid"
kill -TERM "$pid"
attempt=0
while kill -0 "$pid" 2>/dev/null && [ "$attempt" -lt 120 ]; do
    sleep 0.5
    attempt=$((attempt + 1))
done
if kill -0 "$pid" 2>/dev/null; then
    echo "campaign is still draining; check status before sending any stronger signal" >&2
    exit 1
fi
echo "campaign stopped; rerun start_ubuntu_7day.sh to resume"
