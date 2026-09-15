#!/usr/bin/env bash
set -eu
scan_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if systemctl --user is-active --quiet srim-display-recovery.service; then
    echo "The previous scan's recovery is still running; wait for it to finish." >&2
    exit 1
fi
scan_cpus=$("$scan_root/.venv/bin/python" -c 'import os; print(",".join(map(str, sorted(os.sched_getaffinity(0))[:15])))')
systemd-run --user --collect --unit=srim-timing-pilot \
    --property=CPUQuota=1500% --property=MemoryHigh=24G --property=MemoryMax=32G \
    --property=MemorySwapMax=0 --property=OOMPolicy=stop --property=KillMode=mixed \
    --property=TimeoutStopSec=90 \
    /usr/bin/taskset -c "$scan_cpus" "$scan_root/.venv/bin/python" "$scan_root/scripts/timing_pilot.py"
echo "Report: $scan_root/runs/noble_cf4_timing_pilot/report.html"
echo "Status: systemctl --user status srim-timing-pilot"
echo "Stop safely: systemctl --user stop srim-timing-pilot"
