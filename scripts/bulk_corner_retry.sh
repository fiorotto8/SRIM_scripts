#!/usr/bin/env bash
set -eu
scan_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
run_dir="$scan_root/runs/noble_cf4_bulk_corners_retry"
case "${1:-status}" in
 start|resume)
  test "$(loginctl show-user "$(id -un)" -p Linger --value)" = yes
  if systemctl --user list-units --type=service --state=active,activating --no-legend 'srim-*' | rg -q 'srim-'; then
   echo 'Another SRIM service is active; do not overlap campaigns.' >&2; exit 1
  fi
  "$scan_root/.venv/bin/python" "$scan_root/scripts/bulk_corner_retry.py" prepare
  mkdir -p "$run_dir/logs"
  scan_cpus=$("$scan_root/.venv/bin/python" -c 'import os; print(",".join(map(str, sorted(os.sched_getaffinity(0))[:15])))')
  systemd-run --user --collect --unit=srim-bulk-corners-retry \
   --property=CPUQuota=1500% --property=MemoryHigh=24G --property=MemoryMax=32G \
   --property=MemorySwapMax=0 --property=OOMPolicy=stop --property=KillMode=mixed \
   --property=TimeoutStopSec=90 --property=RuntimeMaxSec=6h \
   --property="StandardOutput=append:$run_dir/logs/campaign.log" --property=StandardError=inherit \
   /usr/bin/taskset -c "$scan_cpus" "$scan_root/.venv/bin/python" "$scan_root/scripts/bulk_corner_retry.py" run
  ;;
 prepare|status) exec "$scan_root/.venv/bin/python" "$scan_root/scripts/bulk_corner_retry.py" "${1:-status}" ;;
 logs) exec tail -n 40 -F "$run_dir/logs/campaign.log" ;;
 stop) exec systemctl --user stop srim-bulk-corners-retry ;;
 *) echo 'Usage: bash scripts/bulk_corner_retry.sh {prepare|start|status|logs|stop|resume}' >&2; exit 2 ;;
esac
