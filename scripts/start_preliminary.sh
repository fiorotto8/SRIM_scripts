#!/usr/bin/env bash
set -eu
scan_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$scan_root"
run_dir="$scan_root/runs/noble_cf4_preliminary"
"$scan_root/scripts/start_ubuntu_7day.sh" --run-dir "$run_dir" \
    --config "$scan_root/configs/noble_cf4_preliminary.toml" "$@"
nohup setsid flock -n "$run_dir/report.lock" "$scan_root/.venv/bin/python" \
    "$scan_root/scripts/preliminary_report.py" --run-dir "$run_dir" --watch \
    </dev/null >>"$run_dir/logs/report.log" 2>&1 &
echo "Open $run_dir/report.html for automatically updated plots and CSV downloads."
