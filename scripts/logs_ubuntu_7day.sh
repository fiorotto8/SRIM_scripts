#!/usr/bin/env bash
set -eu

scan_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
run_dir=${1:-"$scan_root/runs/noble_cf4_7day_ubuntu"}
latest="$run_dir/logs/latest.log"
if [ ! -e "$latest" ]; then
    echo "no campaign log at $latest" >&2
    exit 1
fi
exec tail -n 100 -F "$latest"
