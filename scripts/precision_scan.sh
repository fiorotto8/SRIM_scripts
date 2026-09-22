#!/usr/bin/env bash
set -eu
scan_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
run_dir="$scan_root/runs/noble_cf4_precision"
action=${1:-status}
case "$action" in
  start|resume)
    if [ "$(loginctl show-user "$(id -un)" -p Linger --value)" != yes ]; then
      echo "Enable logout persistence first: loginctl enable-linger $(id -un)" >&2; exit 1
    fi
    for unit in srim-staged-scan srim-timing-pilot srim-display-recovery srim-low-energy-topup srim-mixture-scan; do
      if systemctl --user is-active --quiet "$unit"; then
        echo "Another SRIM campaign is active: $unit. Stop it before starting this one." >&2; exit 1
      fi
    done
    mkdir -p "$run_dir/logs"
    scan_cpus=$("$scan_root/.venv/bin/python" -c 'import os; print(",".join(map(str, sorted(os.sched_getaffinity(0))[:15])))')
    systemd-run --user --collect --unit=srim-precision-scan \
      --property=CPUQuota=1500% --property=MemoryHigh=24G --property=MemoryMax=32G \
      --property=MemorySwapMax=0 --property=OOMPolicy=stop --property=KillMode=mixed \
      --property=TimeoutStopSec=90 --property=RuntimeMaxSec=73h \
      --property="StandardOutput=append:$run_dir/logs/campaign.log" --property=StandardError=inherit \
      /usr/bin/taskset -c "$scan_cpus" "$scan_root/.venv/bin/python" "$scan_root/scripts/precision_scan.py" run
    echo "Detached precision campaign launched. Monitor: bash $scan_root/scripts/precision_scan.sh status"
    ;;
  status|report) exec "$scan_root/.venv/bin/python" "$scan_root/scripts/precision_scan.py" "$action" ;;
  logs) exec tail -n 50 -F "$run_dir/logs/campaign.log" ;;
  stop) exec systemctl --user stop srim-precision-scan.service ;;
  *) echo "Usage: bash scripts/precision_scan.sh {start|status|logs|stop|resume|report}" >&2; exit 2 ;;
esac
