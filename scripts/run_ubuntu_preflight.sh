#!/usr/bin/env bash
set -eu

scan_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec "$scan_root/.venv/bin/python" "$scan_root/srim_scan.py" preflight \
    --config "$scan_root/configs/noble_cf4_40h_ubuntu.toml" "$@"
