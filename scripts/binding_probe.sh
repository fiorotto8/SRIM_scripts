#!/usr/bin/env bash
set -eu
scan_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
suite=${2:-format}
case "$suite" in
 format) run_dir="$scan_root/runs/binding_format_probe" ;;
 zero-handling) run_dir="$scan_root/runs/binding_zero_probe" ;;
 *) echo 'Suite must be format or zero-handling' >&2; exit 2 ;;
esac
case "${1:-status}" in
 start|resume)
  test "$(loginctl show-user "$(id -un)" -p Linger --value)" = yes
  for unit in srim-staged-scan srim-timing-pilot srim-display-recovery srim-precision-scan srim-low-energy-topup srim-mixture-scan srim-repro-check srim-binding-probe; do
   if systemctl --user is-active --quiet "$unit"; then echo "Another campaign is active: $unit" >&2; exit 1; fi
  done
  "$scan_root/.venv/bin/python" "$scan_root/scripts/binding_probe.py" prepare --suite "$suite"
  mkdir -p "$run_dir/logs"
  scan_cpus=$("$scan_root/.venv/bin/python" -c 'import os; print(",".join(map(str,sorted(os.sched_getaffinity(0))[:8])))')
  systemd-run --user --collect --unit=srim-binding-probe \
   --property=CPUQuota=800% --property=MemoryHigh=24G --property=MemoryMax=32G \
   --property=MemorySwapMax=0 --property=OOMPolicy=stop --property=KillMode=control-group \
   --property=TimeoutStopSec=30 --property=RuntimeMaxSec=30min \
   --property="StandardOutput=append:$run_dir/logs/campaign.log" --property=StandardError=inherit \
   /usr/bin/taskset -c "$scan_cpus" "$scan_root/.venv/bin/python" "$scan_root/scripts/binding_probe.py" run --suite "$suite"
  ;;
 prepare|status) exec "$scan_root/.venv/bin/python" "$scan_root/scripts/binding_probe.py" "${1:-status}" --suite "$suite" ;;
 logs) exec tail -n 30 -F "$run_dir/logs/campaign.log" ;;
 stop) exec systemctl --user stop srim-binding-probe ;;
 *) echo 'Usage: bash scripts/binding_probe.sh {prepare|start|status|logs|stop|resume} [format|zero-handling]' >&2; exit 2 ;;
esac
