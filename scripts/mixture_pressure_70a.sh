#!/usr/bin/env bash
set -eu
scan_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
run_dir="$scan_root/runs/noble_cf4_mixture_pressure_70a"
case "${1:-status}" in
 start)
  test "$(loginctl show-user "$(id -un)" -p Linger --value)" = yes
  test ! -f "$run_dir/status.json"
  test ! -f "$run_dir/thermal-status.json"
  if systemctl --user list-units --type=service --state=active,activating --no-legend 'srim-*' | rg -q 'srim-'; then
   echo 'Another SRIM service is active.' >&2; exit 1
  fi
  "$scan_root/.venv/bin/python" "$scan_root/scripts/mixture_pressure_70a.py" prepare
  mkdir -p "$run_dir/logs"
  scan_cpus=$("$scan_root/.venv/bin/python" -c 'import os; print(",".join(map(str, sorted(os.sched_getaffinity(0))[:10])))')
  scan_remaining=$("$scan_root/.venv/bin/python" -c 'import json,sys; p=json.load(open(sys.argv[1])); print(int(p["limit_seconds"]-p["used_seconds"]))' "$run_dir/budget.json")
  systemd-run --user --collect --unit=srim-mixture-pressure-70a \
   --property="WorkingDirectory=$scan_root" \
   --property=CPUQuota=1000% --property=CPUQuotaPeriodSec=20ms \
   --property=MemoryHigh=24G --property=MemoryMax=32G --property=MemorySwapMax=0 \
   --property=OOMPolicy=stop --property=KillMode=control-group --property=TimeoutStopSec=30 --property="RuntimeMaxSec=$scan_remaining" \
   --setenv=OPENBLAS_NUM_THREADS=1 --setenv=OMP_NUM_THREADS=1 --setenv=MKL_NUM_THREADS=1 \
   --setenv=NUMEXPR_NUM_THREADS=1 --setenv=VECLIB_MAXIMUM_THREADS=1 --setenv=BLIS_NUM_THREADS=1 \
   --property="ExecStopPost=$scan_root/.venv/bin/python -X faulthandler $scan_root/scripts/mixture_pressure_70a.py finalize" \
   --property="StandardOutput=append:$run_dir/logs/campaign.log" --property=StandardError=inherit \
   /usr/bin/taskset -c "$scan_cpus" "$scan_root/.venv/bin/python" -X faulthandler "$scan_root/scripts/mixture_pressure_70a.py" guard
  ;;
 prepare|status) exec "$scan_root/.venv/bin/python" "$scan_root/scripts/mixture_pressure_70a.py" "${1:-status}" ;;
 logs) exec tail -n 30 -F "$run_dir/logs/campaign.log" ;;
 stop) exec systemctl --user stop srim-mixture-pressure-70a ;;
 *) echo 'Usage: bash scripts/mixture_pressure_70a.sh {prepare|start|status|logs|stop}' >&2; exit 2 ;;
esac
