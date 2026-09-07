#!/bin/sh
set -eu

pysrim_root=/workspace/pysrim
srim_root=/data/srim-volume/srim-2013
wine_prefix=/data/wine-volume/prefix
expected_commit=9b8e1b7e91fe65bf17e9366e1fd9b9406ad125d2
expected_archive=f425c4715d5a849c1f521a8fc14aff58d41066fa4ca773546690a220ff3acecf

# Docker owns each named-volume mount point. Create user-owned directories
# below those roots because Wine rejects a prefix owned by another user.
mkdir -p "$HOME" "$srim_root" "$wine_prefix"

if [ ! -f "$pysrim_root/srim/__init__.py" ] || [ ! -f "$pysrim_root/assets/srim.2013.zip" ]; then
    echo "Expected the fiorotto8/pysrim checkout at $pysrim_root." >&2
    echo "Clone it beside SRIM_scripts before running Docker Compose." >&2
    exit 1
fi

actual_commit=$(git -c "safe.directory=$pysrim_root" -C "$pysrim_root" rev-parse HEAD)
if [ "$actual_commit" != "$expected_commit" ]; then
    echo "PySRIM is at $actual_commit; this branch is pinned to $expected_commit." >&2
    exit 1
fi

actual_archive=$(sha256sum "$pysrim_root/assets/srim.2013.zip" | awk '{print $1}')
if [ "$actual_archive" != "$expected_archive" ]; then
    echo "Unexpected PySRIM SRIM archive hash: $actual_archive" >&2
    exit 1
fi

if [ ! -f "$srim_root/TRIM.exe" ] || [ ! -f "$srim_root/SR Module/SRModule.exe" ]; then
    if [ -n "$(find "$srim_root" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
        echo "Refusing to replace an incomplete, non-empty SRIM volume at $srim_root." >&2
        exit 1
    fi
    extract_root=$(mktemp -d /tmp/srim-extract.XXXXXX)
    trap 'rm -rf -- "$extract_root"' EXIT HUP INT TERM
    unzip -q "$pysrim_root/assets/srim.2013.zip" -d "$extract_root"
    # The named-volume root is writable but owned by Docker. A recursive copy
    # creates user-owned contents without trying to preserve attributes on the
    # volume mount point itself.
    cp -R "$extract_root/srim/." "$srim_root/"
    rm -rf -- "$extract_root"
    trap - EXIT HUP INT TERM
fi

if [ ! -f "$wine_prefix/system.reg" ]; then
    WINEARCH=win32 WINEPREFIX="$wine_prefix" WINEDEBUG=-all \
        xvfb-run -a wineboot --init
    WINEPREFIX="$wine_prefix" wineserver -w
fi

if [ ! -f "$wine_prefix/.srim-components-ok" ]; then
    WINEPREFIX="$wine_prefix" WINEDEBUG=-all \
        xvfb-run -a winetricks -q comdlg32ocx msflxgrd richtx32 comctl32ocx tabctl32
    if ! WINEPREFIX="$wine_prefix" WINEDEBUG=-all xvfb-run -a winetricks -q vb5run; then
        if [ ! -f "$wine_prefix/drive_c/windows/system32/msvbvm50.dll" ]; then
            echo "vb5run setup failed and msvbvm50.dll is absent." >&2
            exit 1
        fi
        echo "vb5run returned nonzero, but msvbvm50.dll is installed; continuing." >&2
    fi
    WINEPREFIX="$wine_prefix" wineserver -w
    touch "$wine_prefix/.srim-components-ok"
fi

# The adapter installs the explicit SafeLoader shim required by modern
# PyYAML before importing this pinned PySRIM revision.
python -W ignore::SyntaxWarning /workspace/SRIM_scripts/pysrim_runner.py --probe
exec "$@"
