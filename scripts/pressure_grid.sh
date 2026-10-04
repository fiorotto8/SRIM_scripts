#!/usr/bin/env bash
set -eu
scan_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
run_dir="$scan_root/runs/noble_cf4_pressure_grid"
case "${1:-status}" in
 start|resume)
  test "$(loginctl show-user "$(id -un)" -p Linger --value)" = yes
  if systemctl --user list-units --type=service --state=active,activating --no-legend 'srim-*' | rg -q 'srim-'; then
   echo 'Another SRIM service is active.' >&2; exit 1
  fi
  "$scan_root/.venv/bin/python" "$scan_root/scripts/pressure_grid.py" prepare
  mkdir -p "$run_dir/logs"
  scan_cpus=$("$scan_root/.venv/bin/python" -c 'import os; print(",".join(map(str, sorted(os.sched_getaffinity(0))[:15])))')
  systemd-run --user --collect --unit=srim-pressure-grid \
   --property=CPUQuota=1500% --property=MemoryHigh=24G --property=MemoryMax=32G --property=MemorySwapMax=0 \
   --property=OOMPolicy=stop --property=KillMode=mixed --property=TimeoutStopSec=90 --property=RuntimeMaxSec=24h \
   --property="StandardOutput=append:$run_dir/logs/campaign.log" --property=StandardError=inherit \
   /usr/bin/taskset -c "$scan_cpus" "$scan_root/.venv/bin/python" "$scan_root/scripts/pressure_grid.py" run
  ;;
 prepare|status) exec "$scan_root/.venv/bin/python" "$scan_root/scripts/pressure_grid.py" "${1:-status}" ;;
 logs) exec tail -n 30 -F "$run_dir/logs/campaign.log" ;;
 stop) exec systemctl --user stop srim-pressure-grid ;;
 *) echo 'Usage: bash scripts/pressure_grid.sh {prepare|start|status|logs|stop|resume}' >&2; exit 2 ;;
esac
