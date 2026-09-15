#!/usr/bin/env python3
"""Process-isolated adapter for fiorotto8/pysrim on Linux/Wine.

PySRIM changes the process working directory while writing inputs and running
SRIM.  The scan therefore invokes this adapter in a separate process for every
job; the main process can safely run several private SRIM worker directories.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Mapping


# SRIM-2013 Data/TRIM.cfg controls allocation, not collision physics. The
# bundled 200k limit overflows on heavy 500-keV gas recoils. Record the actual
# allocation alongside every input and retain the config in the raw archive.
MAX_SECONDARY_RECOILS = 2_000_000


def configure_recoil_capacity(runtime: Path, capacity: int = MAX_SECONDARY_RECOILS) -> None:
    if not 200_000 <= capacity <= 10_000_000:
        raise ValueError("Secondary recoil capacity must be between 200,000 and 10,000,000")
    path = runtime / "Data" / "TRIM.cfg"
    original = path.read_bytes()
    pattern = rb"(Maximum number of secondary recoils[^\r\n]*\r?\n)[ \t]*[0-9]+[ \t]*"
    updated, count = re.subn(pattern, lambda m: m[1] + f" {capacity} ".encode(), original)
    if count != 1:
        raise RuntimeError(f"Cannot identify secondary recoil capacity in {path}")
    temporary = path.with_suffix(".cfg.tmp")
    temporary.write_bytes(updated)
    os.replace(temporary, path)


def dialog_candidates(tree: str) -> list[str]:
    """Recognize TRIM's error MessageBox, excluding its invisible 1x1 helper."""
    return [
        match[1]
        for match in re.finditer(
            r'(0x[0-9a-f]+) "(?:TRIM|[^"\n]*[Ee]rror[^"\n]*)"[^\n]*?\s(\d+)x(\d+)[+-]',
            tree,
        )
        if int(match[2]) > 20 and int(match[3]) > 20
    ]


def check_trim_dialogs() -> list[str]:
    # XQueryTree walks a changing window tree: a window can disappear between
    # enumeration and inspection. Retry boundedly, never treat a failed probe
    # as an empty/safe display. Persistent display loss still fails closed.
    for attempt in range(3):
        try:
            return _check_trim_dialogs()
        except (OSError, subprocess.SubprocessError) as exc:
            detail = str(getattr(exc, "stderr", "") or "")[-2000:]
            message = f"display={os.environ.get('DISPLAY', '')}; {exc}; stderr={detail!r}"
            print(f"TRIM watchdog probe {attempt + 1}/3 failed: {message}", file=sys.stderr, flush=True)
            if attempt == 2:
                raise RuntimeError(f"TRIM_WATCHDOG_ERROR: cannot inspect the worker display: {message}") from exc
            time.sleep(0.25 * (attempt + 1))
    raise AssertionError("unreachable")


def _check_trim_dialogs() -> list[str]:
    tree = subprocess.run(
        ["xwininfo", "-root", "-tree"], capture_output=True, text=True, timeout=5, check=True
    ).stdout
    dialogs = []
    for window in dialog_candidates(tree):
        details = subprocess.run(
            ["xwininfo", "-id", window], capture_output=True, text=True, timeout=5
        )
        if details.returncode:
            raise subprocess.CalledProcessError(details.returncode, details.args,
                                                output=details.stdout, stderr=details.stderr)
        if "Map State: IsViewable" in details.stdout:
            dialogs.append(window)
    return dialogs


def write_progress(runtime: Path, values: Mapping[str, Any]) -> None:
    path = runtime / ".trim-progress.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(dict(values), indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def monitored_trim_run(trim: Any, runtime: Path, spec: Mapping[str, Any]) -> None:
    if not os.environ.get("DISPLAY") or not shutil.which("xwininfo"):
        raise RuntimeError("TRIM_WATCHDOG_ERROR: requires DISPLAY and xwininfo (Ubuntu package x11-utils)")
    # Verify the display is accessible before launching an expensive job.
    check_trim_dialogs()
    previous = Path.cwd()
    try:
        os.chdir(runtime)
        trim._write_input_files()
    finally:
        os.chdir(previous)
    with (runtime / ".trim-process.log").open("wb") as log:
        process = subprocess.Popen(["wine", "./TRIM.exe"], cwd=runtime, stdout=log, stderr=log)
        state = {
            "job_id": spec["description"], "pid": process.pid,
            "started_epoch": time.time(), "secondary_recoil_capacity": MAX_SECONDARY_RECOILS,
        }
        try:
            while True:
                code = process.poll()
                if code is not None:
                    state.update(updated_epoch=time.time(), status="complete" if code == 0 else "failed")
                    write_progress(runtime, state)
                    if code:
                        raise RuntimeError(f"TRIM exited with code {code}; see {runtime / '.trim-process.log'}")
                    return
                dialogs = check_trim_dialogs()
                state.update(updated_epoch=time.time(), status="running", dialogs=dialogs)
                try:
                    fields = Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()
                    state["cpu_seconds"] = (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
                except (FileNotFoundError, IndexError, ValueError):
                    pass
                try:
                    memory = dict(line.split(":", 1) for line in Path(f"/proc/{process.pid}/status").read_text().splitlines())
                    state["rss_bytes"] = int(memory["VmRSS"].split()[0]) * 1024
                    state["peak_rss_bytes_observed"] = max(state.get("peak_rss_bytes_observed", 0),
                                                          int(memory["VmHWM"].split()[0]) * 1024)
                except (FileNotFoundError, KeyError, ValueError):
                    pass
                if dialogs:
                    state["status"] = "blocked_dialog"
                    try:
                        from PIL import ImageGrab
                        ImageGrab.grab(xdisplay=os.environ["DISPLAY"]).save(runtime / ".trim-dialog.png")
                    except Exception as exc:
                        state["screenshot_error"] = str(exc)
                    write_progress(runtime, state)
                    raise RuntimeError(
                        f"TRIM_BLOCKING_DIALOG: windows={dialogs}; see {runtime / '.trim-dialog.png'}. "
                        "Job rejected; no dialog was dismissed or truncated result accepted."
                    )
                write_progress(runtime, state)
                time.sleep(2)
        except RuntimeError as exc:
            if "TRIM_WATCHDOG_ERROR" in str(exc):
                state.update(updated_epoch=time.time(), status="watchdog_error", error=str(exc))
                write_progress(runtime, state)
            raise
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def import_pysrim() -> Dict[str, Any]:
    # fiorotto8/pysrim 0.5.10 predates PyYAML 6, where yaml.load requires an
    # explicit Loader.  Patch only this child process and retain safe loading.
    import yaml

    original_load = yaml.load

    def compatible_load(stream: Any, Loader: Any = None, *args: Any, **kwargs: Any) -> Any:
        return original_load(stream, Loader=Loader or yaml.SafeLoader, *args, **kwargs)

    yaml.load = compatible_load
    import srim
    from srim import Element, Ion, Layer, SR, TRIM, Target
    from srim.output import SRIMOutputParseError

    return {
        "module": srim,
        "Element": Element,
        "Ion": Ion,
        "Layer": Layer,
        "SR": SR,
        "TRIM": TRIM,
        "Target": Target,
        "SRIMOutputParseError": SRIMOutputParseError,
    }


def target_layer(api: Mapping[str, Any], spec: Mapping[str, Any]) -> Any:
    Element = api["Element"]
    Layer = api["Layer"]
    element_data = []
    for item in spec["elements"]:
        element = Element(str(item["symbol"]), mass=float(item["mass_u"]))
        element_data.append(
            (
                int(item["z"]),
                element,
                {
                    "stoich": float(item["stoich"]),
                    "E_d": float(item["displacement_energy_ev"]),
                    "lattice": float(item["lattice_binding_energy_ev"]),
                    "surface": float(item["surface_binding_energy_ev"]),
                },
            )
        )
    element_data.sort(key=lambda value: value[0])
    return Layer(
        {element: properties for _, element, properties in element_data},
        density=float(spec["density_g_cm3"]),
        width=float(spec.get("target_width_angstrom", 1.0)),
        phase=1,
        name=str(spec.get("layer_name", "noble CF4 gas"))[:32],
    )


def execute_spec(spec: Mapping[str, Any], write_only: bool = False) -> None:
    api = import_pysrim()
    runtime = Path(str(spec["srim_root"])).expanduser().resolve()
    if not (runtime / "TRIM.exe").is_file():
        raise FileNotFoundError(f"Missing TRIM.exe in worker runtime {runtime}")
    layer = target_layer(api, spec)
    Ion = api["Ion"]
    ion = Ion(
        str(spec["ion_symbol"]),
        energy=float(spec["energy_kev"]) * 1000.0,
        mass=float(spec["ion_mass_u"]),
    )
    mode = str(spec["mode"])
    if mode == "trim":
        trim = api["TRIM"](
            api["Target"]([layer]),
            ion,
            calculation=int(spec["cascade_type"]),
            number_ions=int(spec["n_ions"]),
            description=str(spec["description"]),
            reminders=0,
            autosave=0,
            plot_mode=5,
            plot_xmin=0.0,
            plot_xmax=float(spec["target_width_angstrom"]),
            ranges=int(bool(spec["save_3d_ranges"])),
            backscattered=1,
            transmit=1,
            sputtered=0,
            collisions=0,
            exyz=0,
            angle_ions=float(spec["angle_deg"]),
            # Zero here matches SRIM-2013's generated TRIM.IN.  The actual
            # configured 1.0 compound correction is written in the target
            # compound-correction section by PySRIM.
            bragg_correction=0.0,
            random_seed=int(spec["random_seed"]),
            version=0,
        )
        # PySRIM 0.5.10 accidentally derives `sputtered` from `ranges`.
        # Correct the in-memory setting so convergence endpoint output does not
        # also request an unrelated sputtering file.
        trim.settings._settings["sputtered"] = 0
        if write_only:
            previous = Path.cwd()
            try:
                os.chdir(runtime)
                trim._write_input_files()
            finally:
                os.chdir(previous)
        else:
            configure_recoil_capacity(runtime)
            # Keep PySRIM's input generation, but supervise the executable.
            # srim_scan independently parses and validates all required outputs.
            monitored_trim_run(trim, runtime, spec)
    elif mode == "srmodule":
        sr = api["SR"](
            layer,
            ion,
            energy_min=float(spec["energy_min_kev"]) * 1000.0,
            output_type=5,
            output_filename=str(spec["output_filename"]),
            correction=float(spec["compound_correction"]),
        )
        if write_only:
            previous = Path.cwd()
            try:
                os.chdir(runtime / "SR Module")
                sr._write_input_file()
            finally:
                os.chdir(previous)
        else:
            sr.run(str(runtime))
    else:
        raise ValueError(f"Unknown PySRIM job mode {mode!r}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Isolated fiorotto8/pysrim job adapter")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--probe", action="store_true")
    group.add_argument("--spec", type=Path)
    parser.add_argument("--write-only", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.probe:
        api = import_pysrim()
        module = api["module"]
        print(
            json.dumps(
                {
                    "module_file": str(Path(module.__file__).resolve()),
                    "python": sys.version.split()[0],
                    "yaml_loader_compatibility": True,
                },
                sort_keys=True,
            )
        )
        return 0
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    execute_spec(spec, write_only=args.write_only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
