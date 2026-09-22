#!/usr/bin/env bash
set -eu
scan_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
run_dir="$scan_root/runs/noble_cf4_low_energy_topup"
case "${1:-status}" in
  start|resume)
    if [ "$(loginctl show-user "$(id -un)" -p Linger --value)" != yes ]; then
      echo "Enable logout persistence first: loginctl enable-linger $(id -un)" >&2; exit 1
    fi
    for unit in srim-staged-scan srim-timing-pilot srim-display-recovery srim-precision-scan srim-low-energy-topup; do
      if systemctl --user is-active --quiet "$unit"; then
        echo "Another SRIM campaign is active: $unit" >&2; exit 1
      fi
    done
    "$scan_root/.venv/bin/python" "$scan_root/scripts/low_energy_topup.py" prepare
    mkdir -p "$run_dir/logs"
    scan_cpus=$("$scan_root/.venv/bin/python" -c 'import os; print(",".join(map(str, sorted(os.sched_getaffinity(0))[:15])))')
    systemd-run --user --collect --unit=srim-low-energy-topup \
      --property=CPUQuota=1500% --property=MemoryHigh=24G --property=MemoryMax=32G \
      --property=MemorySwapMax=0 --property=OOMPolicy=stop --property=KillMode=mixed \
      --property=TimeoutStopSec=90 --property=RuntimeMaxSec=2h \
      --property="StandardOutput=append:$run_dir/logs/campaign.log" --property=StandardError=inherit \
      /usr/bin/taskset -c "$scan_cpus" "$scan_root/.venv/bin/python" "$scan_root/scripts/low_energy_topup.py" run
    echo "Detached follow-up launched. Monitor: bash scripts/low_energy_topup.sh status"
    ;;
  prepare|status) exec "$scan_root/.venv/bin/python" "$scan_root/scripts/low_energy_topup.py" "${1:-status}" ;;
  logs) exec tail -n 50 -F "$run_dir/logs/campaign.log" ;;
  stop) exec systemctl --user stop srim-low-energy-topup.service ;;
  *) echo "Usage: bash scripts/low_energy_topup.sh {prepare|start|status|logs|stop|resume}" >&2; exit 2 ;;
esac
