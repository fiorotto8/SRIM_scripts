#!/usr/bin/env bash
set -eu
scan_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
run_dir="$scan_root/runs/noble_cf4_mixture_refinement"
case "${1:-status}" in
  start|resume)
    if [ "$(loginctl show-user "$(id -un)" -p Linger --value)" != yes ]; then
      echo "Enable logout persistence first: loginctl enable-linger $(id -un)" >&2; exit 1
    fi
    for unit in srim-staged-scan srim-timing-pilot srim-display-recovery srim-precision-scan srim-low-energy-topup srim-mixture-scan; do
      if systemctl --user is-active --quiet "$unit"; then
        echo "Another SRIM campaign is active: $unit" >&2; exit 1
      fi
    done
    "$scan_root/.venv/bin/python" "$scan_root/scripts/mixture_scan.py" prepare
    mkdir -p "$run_dir/logs"
    scan_cpus=$("$scan_root/.venv/bin/python" -c 'import os; print(",".join(map(str, sorted(os.sched_getaffinity(0))[:15])))')
    systemd-run --user --collect --unit=srim-mixture-scan \
      --property=CPUQuota=1500% --property=MemoryHigh=24G --property=MemoryMax=32G \
      --property=MemorySwapMax=0 --property=OOMPolicy=stop --property=KillMode=mixed \
      --property=TimeoutStopSec=90 --property=RuntimeMaxSec=6h \
      --property="StandardOutput=append:$run_dir/logs/campaign.log" --property=StandardError=inherit \
      /usr/bin/taskset -c "$scan_cpus" "$scan_root/.venv/bin/python" "$scan_root/scripts/mixture_scan.py" run
    echo "Detached mixture scan launched. Monitor: bash scripts/mixture_scan.sh status"
    ;;
  prepare|status) exec "$scan_root/.venv/bin/python" "$scan_root/scripts/mixture_scan.py" "${1:-status}" ;;
  logs) exec tail -n 50 -F "$run_dir/logs/campaign.log" ;;
  stop) exec systemctl --user stop srim-mixture-scan.service ;;
  *) echo "Usage: bash scripts/mixture_scan.sh {prepare|start|status|logs|stop|resume}" >&2; exit 2 ;;
esac
