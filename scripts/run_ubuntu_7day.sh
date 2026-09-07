#!/usr/bin/env bash
set -u

scan_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
run_dir=$1
shift
lock_file="$run_dir/campaign.lock"
pid_file="$run_dir/campaign.pid"

mkdir -p "$run_dir/logs"
exec 9>"$lock_file"
if ! flock -n 9; then
    echo "another campaign launcher holds $lock_file" >&2
    exit 73
fi

temporary_pid="$pid_file.tmp-$$"
printf '%s\n' "$$" >"$temporary_pid"
mv -f -- "$temporary_pid" "$pid_file"

python_pid=""
cleanup() {
    if [ -f "$pid_file" ] && [ "$(sed -n '1p' "$pid_file")" = "$$" ]; then
        rm -f -- "$pid_file"
    fi
}
forward_stop() {
    if [ -n "$python_pid" ] && kill -0 "$python_pid" 2>/dev/null; then
        kill -TERM "$python_pid" 2>/dev/null || true
        wait "$python_pid"
    fi
    exit 130
}
trap cleanup EXIT
trap forward_stop HUP INT TERM

echo "campaign wrapper PID $$ started at $(date --iso-8601=seconds)"
"$scan_root/.venv/bin/python" "$scan_root/srim_scan.py" run \
    --config "$scan_root/configs/noble_cf4_7day_ubuntu.toml" \
    --run-dir "$run_dir" \
    --stage all --resume "$@" &
python_pid=$!
wait "$python_pid"
status=$?
python_pid=""
echo "campaign wrapper finished with status $status at $(date --iso-8601=seconds)"
exit "$status"
