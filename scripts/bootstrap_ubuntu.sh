#!/usr/bin/env bash
set -eu

scan_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
workspace_root=$(dirname -- "$scan_root")
pysrim_root="$workspace_root/pysrim"
srim_root="$workspace_root/srim-2013"
wine_prefix="$workspace_root/.wine-srim"
install_system=0

usage() {
    echo "usage: $0 [--install-system-deps] [--pysrim-root PATH] [--srim-root PATH] [--wine-prefix PATH]"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --install-system-deps) install_system=1; shift ;;
        --pysrim-root) pysrim_root=$2; shift 2 ;;
        --srim-root) srim_root=$2; shift 2 ;;
        --wine-prefix) wine_prefix=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

if [ "$install_system" -eq 1 ]; then
    if [ "$(id -u)" -eq 0 ]; then
        elevate=""
    elif command -v sudo >/dev/null 2>&1; then
        elevate="sudo"
    else
        echo "sudo is required to install Ubuntu packages" >&2
        exit 1
    fi
    $elevate dpkg --add-architecture i386
    $elevate apt-get update
    $elevate apt-get install -y git python3 python3-venv python3-pip unzip xvfb wine wine32:i386 winetricks x11-utils
fi

for program in git python3 unzip wine wineserver xvfb-run winetricks xwininfo; do
    if ! command -v "$program" >/dev/null 2>&1; then
        echo "missing $program; rerun with --install-system-deps" >&2
        exit 1
    fi
done

if [ ! -f "$pysrim_root/srim/__init__.py" ] || [ ! -f "$pysrim_root/assets/srim.2013.zip" ]; then
    echo "expected fiorotto8/pysrim at $pysrim_root" >&2
    exit 1
fi

expected_commit=9b8e1b7e91fe65bf17e9366e1fd9b9406ad125d2
actual_commit=$(git -c "safe.directory=$pysrim_root" -C "$pysrim_root" rev-parse HEAD)
if [ "$actual_commit" != "$expected_commit" ]; then
    echo "pysrim is at $actual_commit; expected $expected_commit" >&2
    echo "leave your checkout intact and select the pinned commit before continuing" >&2
    exit 1
fi

expected_archive=f425c4715d5a849c1f521a8fc14aff58d41066fa4ca773546690a220ff3acecf
actual_archive=$(sha256sum "$pysrim_root/assets/srim.2013.zip" | awk '{print $1}')
if [ "$actual_archive" != "$expected_archive" ]; then
    echo "unexpected pysrim SRIM archive hash: $actual_archive" >&2
    exit 1
fi

if [ ! -f "$srim_root/TRIM.exe" ] || [ ! -f "$srim_root/SR Module/SRModule.exe" ]; then
    if [ -e "$srim_root" ] && [ -n "$(find "$srim_root" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
        echo "refusing to replace non-empty incomplete SRIM directory: $srim_root" >&2
        exit 1
    fi
    extract_root=$(mktemp -d "${TMPDIR:-/tmp}/srim-extract.XXXXXX")
    trap 'rm -rf -- "$extract_root"' EXIT HUP INT TERM
    unzip -q "$pysrim_root/assets/srim.2013.zip" -d "$extract_root"
    if [ -d "$srim_root" ]; then
        rmdir "$srim_root"
    fi
    mv "$extract_root/srim" "$srim_root"
fi

python3 -m venv "$scan_root/.venv"
"$scan_root/.venv/bin/python" -m pip install --upgrade pip setuptools wheel
"$scan_root/.venv/bin/python" -m pip install -e "$pysrim_root" matplotlib 'uproot==5.7.6' 'scipy==1.18.1'

if [ ! -f "$wine_prefix/system.reg" ]; then
    mkdir -p "$(dirname -- "$wine_prefix")"
    WINEARCH=win32 WINEPREFIX="$wine_prefix" WINEDEBUG=-all \
        xvfb-run -a wineboot --init
    WINEPREFIX="$wine_prefix" wineserver -w
fi

if [ ! -f "$wine_prefix/.srim-components-ok" ]; then
    WINEPREFIX="$wine_prefix" WINEDEBUG=-all \
        xvfb-run -a winetricks -q comdlg32ocx msflxgrd richtx32 comctl32ocx tabctl32
    if ! WINEPREFIX="$wine_prefix" WINEDEBUG=-all xvfb-run -a winetricks -q vb5run; then
        # Wine 9 can return status 5 after the redistributable has in fact
        # installed.  Accept that case only when the required DLL is present.
        if [ ! -f "$wine_prefix/drive_c/windows/system32/msvbvm50.dll" ]; then
            echo "vb5run setup failed and msvbvm50.dll is absent" >&2
            exit 1
        fi
        echo "vb5run installer returned nonzero, but msvbvm50.dll is installed; continuing" >&2
    fi
    WINEPREFIX="$wine_prefix" wineserver -w
    touch "$wine_prefix/.srim-components-ok"
fi

"$scan_root/.venv/bin/python" "$scan_root/srim_scan.py" preflight \
    --config "$scan_root/configs/noble_cf4_40h_ubuntu.toml" --skip-canary

echo "Ubuntu environment prepared. Run scripts/run_ubuntu_preflight.sh next."
