#!/usr/bin/env bash
set -eu
scan_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
run_dir="$scan_root/runs/xvfb_reset_gate"
case "${1:-status}" in
 start)
  test "$(loginctl show-user "$(id -un)" -p Linger --value)" = yes
  test ! -f "$run_dir/status.json"
  if systemctl --user list-units --type=service --state=active,activating --no-legend 'srim-*' | rg -q 'srim-'; then
   echo 'Another SRIM service is active.' >&2; exit 1
  fi
  "$scan_root/.venv/bin/python" "$scan_root/scripts/xvfb_reset_gate.py" prepare
  mkdir -p "$run_dir/logs"
  scan_cpus=$("$scan_root/.venv/bin/python" -c 'import os; print(",".join(map(str, sorted(os.sched_getaffinity(0))[:15])))')
  systemd-run --user --collect --unit=srim-xvfb-reset-gate \
   --property=CPUQuota=1500% --property=MemoryHigh=4G --property=MemoryMax=8G --property=MemorySwapMax=0 \
   --property=OOMPolicy=stop --property=KillMode=mixed --property=TimeoutStopSec=30 --property=RuntimeMaxSec=15min \
   --property="StandardOutput=append:$run_dir/logs/campaign.log" --property=StandardError=inherit \
   /usr/bin/taskset -c "$scan_cpus" "$scan_root/.venv/bin/python" "$scan_root/scripts/xvfb_reset_gate.py" run
  ;;
 prepare|status) exec "$scan_root/.venv/bin/python" "$scan_root/scripts/xvfb_reset_gate.py" "${1:-status}" ;;
 logs) exec tail -n 30 -F "$run_dir/logs/campaign.log" ;;
 stop) exec systemctl --user stop srim-xvfb-reset-gate ;;
 *) echo 'Usage: bash scripts/xvfb_reset_gate.sh {prepare|start|status|logs|stop}' >&2; exit 2 ;;
esac
