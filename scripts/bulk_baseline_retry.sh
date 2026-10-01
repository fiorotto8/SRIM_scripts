#!/usr/bin/env bash
set -eu
scan_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
run_dir="$scan_root/runs/noble_cf4_bulk_baseline_retry"
case "${1:-status}" in
 start|resume)
  test "$(loginctl show-user "$(id -un)" -p Linger --value)" = yes
  if systemctl --user list-units --type=service --state=active,activating --no-legend 'srim-*' | rg -q 'srim-'; then
   echo 'Another SRIM service is active; do not overlap campaigns.' >&2; exit 1
  fi
  "$scan_root/.venv/bin/python" "$scan_root/scripts/bulk_baseline_retry.py" prepare
  mkdir -p "$run_dir/logs"
  scan_seconds=$("$scan_root/.venv/bin/python" -c 'import json,sys; from pathlib import Path; b=json.loads(Path(sys.argv[1]).read_text()); print(max(1,int(b["limit_seconds"]-b["used_seconds"])))' "$run_dir/budget.json")
  scan_cpus=$("$scan_root/.venv/bin/python" -c 'import os; print(",".join(map(str, sorted(os.sched_getaffinity(0))[:15])))')
  systemd-run --user --collect --unit=srim-bulk-baseline-retry \
   --property=CPUQuota=1500% --property=MemoryHigh=24G --property=MemoryMax=32G \
   --property=MemorySwapMax=0 --property=OOMPolicy=stop --property=KillMode=mixed \
   --property=TimeoutStopSec=90 --property="RuntimeMaxSec=$scan_seconds" \
   --property="StandardOutput=append:$run_dir/logs/campaign.log" --property=StandardError=inherit \
   /usr/bin/taskset -c "$scan_cpus" "$scan_root/.venv/bin/python" "$scan_root/scripts/bulk_baseline_retry.py" run
  ;;
 prepare|status) exec "$scan_root/.venv/bin/python" "$scan_root/scripts/bulk_baseline_retry.py" "${1:-status}" ;;
 logs) exec tail -n 30 -F "$run_dir/logs/campaign.log" ;;
 stop) exec systemctl --user stop srim-bulk-baseline-retry ;;
 *) echo 'Usage: bash scripts/bulk_baseline_retry.sh {prepare|start|status|logs|stop|resume}' >&2; exit 2 ;;
esac
