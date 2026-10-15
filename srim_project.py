#!/usr/bin/env python3
"""Small sequential Linux/Wine projects using isolated PySRIM runtimes.

``plan`` only reads a TOML configuration. ``write-input`` makes private runtime
copies and writes inputs without launching Wine. ``run`` additionally clones an
initialized Wine prefix per job, runs TRIM, and retains its raw output files.
"""

from __future__ import annotations

import argparse
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from typing import Any, Mapping, Sequence

from scripts.private_xvfb_noreset import private_display


ROOT = Path(__file__).resolve().parent
REPORT_NAMES = (
    "TDATA.txt", "RANGE.txt", "LATERAL.txt", "IONIZ.txt", "E2RECOIL.txt",
    "PHONON.txt", "VACANCY.txt", "NOVAC.txt", "BACKSCAT.txt", "TRANSMIT.txt",
    "RANGE_3D.txt", "EXYZ.txt", "COLLISON.txt", "SPUTTER.txt", "TRIMOUT.txt",
)


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def positive(value: Any, name: str, *, zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or (result < 0 if zero else result <= 0):
        raise ValueError(f"{name} must be finite and {'nonnegative' if zero else 'positive'}")
    return result


def integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def label(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 32:
        raise ValueError(f"{name} must contain 1–32 printable ASCII characters")
    if any(not 32 <= ord(character) < 127 or character == '"' for character in value):
        raise ValueError(f"{name} must use printable ASCII without double quotes")
    return value


def symbol(value: Any, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z][a-z]?", value):
        raise ValueError(f"{name} must be a chemical symbol, for example He or Si")
    return value


def load_project(path: Path) -> dict[str, Any]:
    """Validate without importing PySRIM, initializing Wine, or writing files."""
    with path.open("rb") as stream:
        raw = tomllib.load(stream)
    try:
        project, ion, target = raw["project"], raw["ion"], raw["target"]
        settings = raw.get("trim", {})
        name = label(project["name"], "project.name")
        energies = project["energies_kev"]
        if not isinstance(energies, list) or not energies:
            raise ValueError("project.energies_kev must be a nonempty list")
        energies = [positive(value, "project.energies_kev") for value in energies]
        if len(set(energies)) != len(energies):
            raise ValueError("project.energies_kev must contain unique energies")
        phase = target["phase"]
        if phase not in ("solid", "gas"):
            raise ValueError("target.phase must be 'solid' or 'gas'")
        elements = target["elements"]
        if not isinstance(elements, list) or not elements:
            raise ValueError("target.elements must contain at least one element")
        normalized_elements = []
        for element in elements:
            normalized_elements.append({
                "symbol": symbol(element["symbol"], "target element symbol"),
                "z": integer(element["z"], "target element z", 1, 118),
                "mass_u": positive(element["mass_u"], "target element mass_u"),
                "stoich": positive(element["stoich"], "target element stoich"),
                "displacement_energy_ev": positive(element["displacement_energy_ev"], "displacement_energy_ev"),
                "lattice_binding_energy_ev": positive(element["lattice_binding_energy_ev"], "lattice_binding_energy_ev", zero=True),
                "surface_binding_energy_ev": positive(element["surface_binding_energy_ev"], "surface_binding_energy_ev", zero=True),
            })
        for field in ("symbol", "z"):
            if len({element[field] for element in normalized_elements}) != len(normalized_elements):
                raise ValueError(f"Target elements must have unique {field} values")
        calculation = integer(settings.get("calculation", 2), "trim.calculation", 1, 2)
        seed = integer(settings.get("random_seed", 12345), "trim.random_seed", 1, 100000)
        if seed + len(energies) - 1 > 100000:
            raise ValueError("Per-energy random seeds would exceed 100000")
        angle = positive(settings.get("angle_deg", 0), "trim.angle_deg", zero=True)
        if angle > 89.9:
            raise ValueError("trim.angle_deg must be at most 89.9")
        ranges = settings.get("save_3d_ranges", True)
        if type(ranges) is not bool:
            raise ValueError("trim.save_3d_ranges must be a boolean")
        base = {
            "mode": "trim", "description": name,
            "layer_name": label(target.get("name", "target layer"), "target.name"),
            "phase": 0 if phase == "solid" else 1,
            "density_g_cm3": positive(target["density_g_cm3"], "target.density_g_cm3"),
            "target_width_angstrom": positive(target["width_angstrom"], "target.width_angstrom"),
            "elements": normalized_elements,
            "ion_symbol": symbol(ion["symbol"], "ion.symbol"),
            "ion_mass_u": positive(ion["mass_u"], "ion.mass_u"),
            "n_ions": integer(settings.get("ions", 10), "trim.ions", 1, 100000),
            "cascade_type": calculation, "compound_correction": 1.0,
            "angle_deg": angle, "random_seed": seed, "save_3d_ranges": ranges,
        }
        timeout = positive(settings.get("timeout_s", 600), "trim.timeout_s")
    except (KeyError, TypeError) as exc:
        raise ValueError(f"Missing or invalid project configuration: {exc}") from exc
    return {"name": name, "energies_kev": energies, "timeout_s": timeout, "base_spec": base}


def planned_jobs(project: Mapping[str, Any]) -> list[dict[str, Any]]:
    jobs = []
    for index, energy in enumerate(project["energies_kev"]):
        jobs.append({
            "job_id": f"e{index:03d}_{energy:g}kev",
            "energy_kev": energy,
            "random_seed": project["base_spec"]["random_seed"] + index,
            "ions": project["base_spec"]["n_ions"],
        })
    return jobs


def plan(project: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "project": project["name"], "status": "PLANNED", "workers": 1,
        "timeout_s_per_job": project["timeout_s"], "jobs": planned_jobs(project),
        "target": {
            "name": project["base_spec"]["layer_name"],
            "phase": "solid" if project["base_spec"]["phase"] == 0 else "gas",
            "density_g_cm3": project["base_spec"]["density_g_cm3"],
            "width_angstrom": project["base_spec"]["target_width_angstrom"],
        },
    }


def overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def validate_paths(source: Path, output: Path, prefix: Path | None = None) -> tuple[Path, Path, Path | None]:
    """Reject unsafe nesting before creating an output directory or copying."""
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"Output already exists; choose a new directory: {output}")
    source, output = source.expanduser().resolve(), output.expanduser().resolve()
    prefix = prefix.expanduser().resolve() if prefix is not None else None
    if not (source / "TRIM.exe").is_file():
        raise ValueError(f"Missing SRIM executable: {source / 'TRIM.exe'}")
    for input_path in (source, prefix):
        if input_path is not None and overlap(input_path, output):
            raise ValueError(f"Output must not overlap an input directory: {input_path}")
    if prefix is not None:
        if not (prefix / "system.reg").is_file() or not (prefix / "drive_c").is_dir():
            raise ValueError(f"Wine prefix is not initialized: {prefix}")
        if overlap(source, prefix):
            raise ValueError("SRIM installation and Wine prefix must be separate directories")
    return source, output, prefix


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def stop_process_group(child: subprocess.Popen[Any]) -> None:
    """Signal descendants even when their immediate parent has already exited."""
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait(timeout=5)


def run_child(command: Sequence[str], runtime: Path, environment: Mapping[str, str],
              timeout_s: float, server: Any = None) -> None:
    started = time.monotonic()
    with (runtime / ".adapter.log").open("wb") as log:
        child = subprocess.Popen(command, cwd=runtime, env=dict(environment), stdout=log,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        try:
            while child.poll() is None:
                if server is not None and server.poll() is not None:
                    raise RuntimeError("Private Xvfb exited during the job")
                remaining = timeout_s - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError(f"Job exceeded {timeout_s:g} seconds; see {runtime / '.adapter.log'}")
                try:
                    child.wait(timeout=min(0.25, remaining))
                except subprocess.TimeoutExpired:
                    pass
            if child.returncode:
                raise RuntimeError(f"Adapter exited with code {child.returncode}; see {runtime / '.adapter.log'}")
        finally:
            stop_process_group(child)


def stop_private_wine(prefix: Path, environment: Mapping[str, str]) -> None:
    if Path(environment.get("WINEPREFIX", "")).resolve() != prefix.resolve():
        raise RuntimeError("Refusing Wine cleanup without the owned private prefix")
    for argument in ("-k", "-w"):
        subprocess.run(["wineserver", argument], env=dict(environment),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=10)


def raw_outputs(runtime: Path) -> dict[str, str]:
    found = {}
    wanted = {name.lower() for name in REPORT_NAMES}
    for directory in (runtime, runtime / "SRIM Outputs"):
        if directory.is_dir():
            for path in directory.iterdir():
                if path.is_file() and path.name.lower() in wanted:
                    found[path.name.upper()] = str(path.relative_to(runtime))
    return found


def execute(project: Mapping[str, Any], config_path: Path, source: Path, output: Path,
            *, write_only: bool, wine_prefix: Path | None = None) -> dict[str, Any]:
    source, output, wine_prefix = validate_paths(source, output, None if write_only else wine_prefix)
    if not write_only and wine_prefix is None:
        raise ValueError("run requires an initialized Wine prefix via --wine-prefix or WINEPREFIX")
    if not write_only:
        missing = [program for program in ("wine", "wineserver", "Xvfb", "xauth", "xwininfo") if not shutil.which(program)]
        if missing:
            raise ValueError(f"Missing Linux runtime tools: {', '.join(missing)}")
    result = plan(project)
    result.update(status="PREPARING", mode="write-input" if write_only else "run", started_at=timestamp(),
                  config_sha256=hashlib.sha256(config_path.read_bytes()).hexdigest(),
                  source_srim=str(source), jobs=[dict(job, status="PENDING") for job in result["jobs"]])
    output.mkdir(parents=True, exist_ok=False)
    shutil.copy2(config_path, output / "config.toml")
    write_json(output / "summary.json", result)
    try:
        for job in result["jobs"]:
            job["status"] = "PREPARING"
            write_json(output / "summary.json", result)
            job_dir = output / job["job_id"]
            job_dir.mkdir()
            runtime = job_dir / "runtime"
            shutil.copytree(source, runtime)
            # Discard only known output files in this new private copy. An old
            # source installation's reports must never count as fresh outputs.
            for directory in (runtime, runtime / "SRIM Outputs"):
                if directory.is_dir():
                    for path in directory.iterdir():
                        if path.is_file() and path.name.lower() in {name.lower() for name in REPORT_NAMES}:
                            path.unlink()
            spec = dict(project["base_spec"], srim_root=str(runtime), energy_kev=job["energy_kev"],
                        random_seed=job["random_seed"])
            write_json(job_dir / "spec.json", spec)
            command = [sys.executable, str(ROOT / "pysrim_runner.py"), "--spec", str(job_dir / "spec.json")]
            environment = dict(os.environ)
            private_prefix = None
            if write_only:
                command.append("--write-only")
                display_context = nullcontext((environment, None, {}))
            else:
                private_prefix = job_dir / "wine-prefix"
                shutil.copytree(wine_prefix, private_prefix, symlinks=True)
                environment.update(WINEPREFIX=str(private_prefix), WINEDEBUG="-all")
                display_context = private_display(runtime)
            job.update(status="WRITING_INPUT" if write_only else "RUNNING", started_at=timestamp())
            result["status"] = job["status"]
            write_json(output / "summary.json", result)
            started = time.monotonic()
            try:
                with display_context as (display_env, server, _):
                    # private_display inherits os.environ, so overlay only its
                    # display variables while retaining our owned Wine prefix.
                    for key in ("DISPLAY", "XAUTHORITY"):
                        if key in display_env:
                            environment[key] = display_env[key]
                    run_child(command, runtime, environment, project["timeout_s"], server)
            finally:
                if private_prefix is not None:
                    stop_private_wine(private_prefix, environment)
            if not (runtime / "TRIM.IN").is_file():
                raise RuntimeError(f"Adapter did not produce {runtime / 'TRIM.IN'}")
            job["raw_files"] = raw_outputs(runtime)
            if not write_only:
                required = {"TDATA.TXT", "RANGE.TXT", "IONIZ.TXT"}
                if not required.issubset(job["raw_files"]):
                    raise RuntimeError(f"Job lacks required outputs: {sorted(required - job['raw_files'].keys())}")
            job.update(status="INPUT_WRITTEN" if write_only else "COMPLETE", ended_at=timestamp(),
                       wall_seconds=time.monotonic() - started)
            write_json(output / "summary.json", result)
        result.update(status="INPUTS_WRITTEN" if write_only else "COMPLETE", ended_at=timestamp())
        write_json(output / "summary.json", result)
        return result
    except BaseException as exc:
        state = "INTERRUPTED" if isinstance(exc, (KeyboardInterrupt, InterruptedError)) else "FAILED"
        for job in result["jobs"]:
            if job["status"] in ("PREPARING", "WRITING_INPUT", "RUNNING"):
                job.update(status=state, error=str(exc), ended_at=timestamp())
        result.update(status=state, error=str(exc), ended_at=timestamp())
        write_json(output / "summary.json", result)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command")
    for command in ("plan", "write-input", "run"):
        child = subparsers.add_parser(command)
        child.add_argument("--config", type=Path, required=True)
        if command != "plan":
            child.add_argument("--srim-root", type=Path,
                               default=Path(os.environ.get("SRIM_ROOT", ROOT.parent / "srim-2013")))
            child.add_argument("--output", type=Path, required=True,
                               help="New output directory; an existing directory is never replaced")
        if command == "run":
            child.add_argument("--wine-prefix", type=Path,
                               default=Path(os.environ.get("WINEPREFIX", ROOT.parent / ".wine-srim")))
    subparsers.add_parser("status").add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "--config":
        arguments.insert(0, "plan")
    parser = build_parser()
    args = parser.parse_args(arguments)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        if args.command == "status":
            value = json.loads((args.output / "summary.json").read_text(encoding="utf-8"))
        else:
            project = load_project(args.config)
            if args.command == "plan":
                value = plan(project)
            else:
                def interrupted(signum: int, _frame: Any) -> None:
                    raise InterruptedError(f"Interrupted by signal {signum}")
                previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM)}
                try:
                    value = execute(project, args.config, args.srim_root, args.output,
                                    write_only=args.command == "write-input",
                                    wine_prefix=getattr(args, "wine_prefix", None))
                finally:
                    for sig, handler in previous.items():
                        signal.signal(sig, handler)
        print(json.dumps(value, indent=2, sort_keys=True))
        return 0
    except (KeyboardInterrupt, InterruptedError) as exc:
        print(f"Interrupted: {exc}", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
