#!/usr/bin/env bash
set -eu
scan_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
run_dir="$scan_root/runs/supervisor_crash_gate"
case "${1:-status}" in
 start)
  test "$(loginctl show-user "$(id -un)" -p Linger --value)" = yes
  test ! -f "$run_dir/status.json"
  if systemctl --user list-units --type=service --state=active,activating --no-legend 'srim-*' | rg -q 'srim-'; then
   echo 'Another SRIM service is active.' >&2; exit 1
  fi
  "$scan_root/.venv/bin/python" "$scan_root/scripts/supervisor_crash_gate.py" prepare
  "$scan_root/.venv/bin/python" "$scan_root/scripts/crash_gate_thermal_guard.py" prepare
  mkdir -p "$run_dir/logs"
  scan_cpus=$("$scan_root/.venv/bin/python" -c 'import os; print(",".join(map(str, sorted(os.sched_getaffinity(0))[:15])))')
  systemd-run --user --collect --unit=srim-supervisor-crash-gate \
   --property=CPUQuota=1500% --property=MemoryHigh=24G --property=MemoryMax=32G --property=MemorySwapMax=0 \
   --property=OOMPolicy=stop --property=KillMode=control-group --property=TimeoutStopSec=90 --property=RuntimeMaxSec=15min \
   --property="ExecStopPost=$scan_root/.venv/bin/python -X faulthandler $scan_root/scripts/supervisor_crash_gate.py finalize" \
   --property="StandardOutput=append:$run_dir/logs/campaign.log" --property=StandardError=inherit \
   /usr/bin/taskset -c "$scan_cpus" "$scan_root/.venv/bin/python" -X faulthandler "$scan_root/scripts/crash_gate_thermal_guard.py" run
  ;;
 status) exec "$scan_root/.venv/bin/python" "$scan_root/scripts/crash_gate_thermal_guard.py" status ;;
 logs|stop) exec bash "$scan_root/scripts/supervisor_crash_gate.sh" "$1" ;;
 *) echo 'Usage: bash scripts/supervisor_crash_gate_thermal.sh {start|status|logs|stop}' >&2; exit 2 ;;
esac
