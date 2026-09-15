#!/usr/bin/env bash
set -eu
scan_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
run_dir="$scan_root/runs/noble_cf4_staged"
action=${1:-status}
case "$action" in
  start|resume)
    if [ "$(loginctl show-user "$(id -un)" -p Linger --value)" != yes ]; then
      echo "Enable logout persistence first: loginctl enable-linger $(id -un)" >&2
      exit 1
    fi
    mkdir -p "$run_dir/logs"
    scan_cpus=$("$scan_root/.venv/bin/python" -c 'import os; print(",".join(map(str, sorted(os.sched_getaffinity(0))[:15])))')
    systemd-run --user --collect --unit=srim-staged-scan \
      --property=CPUQuota=1500% --property=MemoryHigh=24G --property=MemoryMax=32G \
      --property=MemorySwapMax=0 --property=OOMPolicy=stop --property=KillMode=mixed \
      --property=TimeoutStopSec=90 --property=RuntimeMaxSec=96h \
      --property="StandardOutput=append:$run_dir/logs/campaign.log" --property=StandardError=inherit \
      /usr/bin/taskset -c "$scan_cpus" "$scan_root/.venv/bin/python" "$scan_root/scripts/staged_scan.py" run
    echo "Detached staged scan launched. Status: bash $scan_root/scripts/staged_scan.sh status"
    ;;
  status|report) exec "$scan_root/.venv/bin/python" "$scan_root/scripts/staged_scan.py" "$action" ;;
  logs) exec tail -n 50 -F "$run_dir/logs/campaign.log" ;;
  stop) exec systemctl --user stop srim-staged-scan.service ;;
  *) echo "Usage: bash scripts/staged_scan.sh {start|status|logs|stop|resume|report}" >&2; exit 2 ;;
esac
