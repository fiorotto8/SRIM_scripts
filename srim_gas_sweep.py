#!/usr/bin/env python3
"""
Batch SRIM/SRModule sweep for gas mixtures.

This script is designed to run from WSL or Windows Python inside a SRIM-2013
installation directory. It uses SRIM's headless "SR Module" executable to create
stopping/range tables, then interpolates SRIM's projected ranges onto a
log-spaced energy grid. It also derives an electronic energy-fraction quenching
proxy from SRIM's electronic and nuclear stopping powers.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple


NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?")
FORMULA_TOKEN_RE = re.compile(r"([A-Z][a-z]?)(\d*(?:\.\d+)?)")
MIX_PART_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*%\s*([A-Za-z][A-Za-z0-9]*)")
SRIM_ROOT_CONFIG = "srim-root.txt"


@dataclass(frozen=True)
class Element:
    z: int
    symbol: str
    name: str
    mass_u: float


@dataclass
class Gas:
    row_index: int
    label: str
    density_g_cm3: float
    pressure_atm: float
    stoich: Dict[str, float]
    components: List[str]
    pressure_unit: str = "atm"
    temperature: float = 293.0
    temperature_unit: str = "K"


@dataclass
class CompoundEntry:
    dictionary_name: str
    display_name: str
    density_g_cm3: float
    stoich: Dict[int, float]
    bond_line: str


@dataclass
class CompoundDecision:
    mode: str
    correction: float
    match_name: str
    match_status: str
    warning: str
    match_id: str = ""


@dataclass
class StoppingRow:
    energy_kev: float
    s_elec: float
    s_nuc: float
    projected_range_cm: float
    longitudinal_straggling_cm: float
    lateral_straggling_cm: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run SRIM SRModule for each gas and ion in GasDensities.csv."
    )
    parser.add_argument(
        "--srim-root",
        type=Path,
        default=default_srim_root(),
        help=(
            "SRIM installation directory. Defaults to SRIM_ROOT, then srim-root.txt next to "
            "this script, then this script's directory."
        ),
    )
    parser.add_argument("--gas-csv", type=Path, default=Path("GasDensities.csv"))
    parser.add_argument("--out", type=Path, default=Path("results/srim_gas_sweep_results.csv"))
    parser.add_argument("--plots-dir", type=Path, default=Path("plots"))
    parser.add_argument("--workdir", type=Path, default=Path("work/srim_gas_sweep"))
    parser.add_argument("--emin-kev", type=float, default=0.1)
    parser.add_argument("--emax-kev", type=float, default=500.0)
    parser.add_argument("--points", type=int, default=100)
    parser.add_argument(
        "--ions",
        default="gas",
        help=(
            "Ion symbols to run, comma-separated, or 'gas' to use each gas's constituent elements. "
            "Requested ions that are not present in a selected gas are skipped."
        ),
    )
    parser.add_argument(
        "--compound-correction",
        type=float,
        default=1.0,
        help="Manual SRIM compound correction for legacy/manual use. 1.0 means no correction.",
    )
    parser.add_argument(
        "--compound-mode",
        choices=("none", "dictionary", "compare"),
        default="none",
        help="Compound correction mode: none, dictionary metadata, or compare both.",
    )
    parser.add_argument(
        "--gases",
        default="",
        help="Optional comma-separated gas selectors, e.g. CF4,CH4,He. Matches CSV labels or mixture components.",
    )
    parser.add_argument(
        "--keep-work",
        action="store_true",
        help="Keep per-job SR.IN and raw SRModule output files.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse inputs and print planned jobs without running SRModule.",
    )
    parser.add_argument(
        "--no-plots",
        action="store_true",
        help="Do not generate Matplotlib plots after writing the CSV.",
    )
    parser.add_argument(
        "--plot-only",
        action="store_true",
        help="Generate plots from the existing output CSV and exit without running SRIM.",
    )
    return parser.parse_args()


def script_dir() -> Path:
    return Path(__file__).resolve().parent


def normalize_srim_root_path(path: Path | str) -> Path:
    text = os.path.expandvars(os.path.expanduser(str(path).strip().strip('"').strip("'")))
    if is_wsl() and re.match(r"^[A-Za-z]:[\\/]", text):
        completed = subprocess.run(
            ["wslpath", "-u", text],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"wslpath failed for SRIM root {text}: {completed.stderr.strip()}")
        return Path(completed.stdout.strip())
    return Path(text)


def default_srim_root() -> Path:
    env_root = os.environ.get("SRIM_ROOT")
    if env_root:
        return normalize_srim_root_path(env_root)
    config_path = script_dir() / SRIM_ROOT_CONFIG
    if config_path.exists():
        for line in config_path.read_text(encoding="utf-8", errors="replace").splitlines():
            value = line.strip()
            if value and not value.startswith("#"):
                return normalize_srim_root_path(value)
    return script_dir()


def resolve_inside_root(root: Path, path: Path, base_dir: Path | None = None, prefer_existing: bool = False) -> Path:
    if path.is_absolute():
        return path
    if base_dir is not None:
        candidate = base_dir / path
        if not prefer_existing or candidate.exists():
            return candidate
    return root / path


def load_elements(atomdata_path: Path) -> Tuple[Dict[str, Element], Dict[int, Element]]:
    by_symbol: Dict[str, Element] = {}
    by_z: Dict[int, Element] = {}
    with atomdata_path.open("r", encoding="latin-1") as handle:
        for line in handle:
            fields = line.split()
            if not fields or not fields[0].isdigit():
                continue
            z = int(fields[0])
            symbol = fields[1]
            name_parts: List[str] = []
            idx = 2
            while idx < len(fields) and not _looks_like_number(fields[idx]):
                name_parts.append(fields[idx])
                idx += 1
            if idx + 2 >= len(fields):
                continue
            mass_u = float(fields[idx + 2])
            elem = Element(z=z, symbol=symbol, name=" ".join(name_parts), mass_u=mass_u)
            by_symbol[symbol] = elem
            by_z[z] = elem
    return by_symbol, by_z


def _looks_like_number(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


def parse_float(text: str) -> float:
    return float(str(text).strip())


def parse_header_unit(text: str, default: str = "") -> str:
    matches = re.findall(r"\(([^()]+)\)", text)
    return matches[-1].strip() if matches else default


def parse_temperature_from_headers(fieldnames: Sequence[str] | None) -> Tuple[float, str]:
    for field in fieldnames or []:
        match = re.search(r"@\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)", field)
        if match:
            return float(match.group(1)), match.group(2)
    return 293.0, "K"


def parse_formula(formula: str) -> Dict[str, float]:
    pos = 0
    result: Dict[str, float] = {}
    for match in FORMULA_TOKEN_RE.finditer(formula):
        if match.start() != pos:
            raise ValueError(f"Cannot parse formula segment in {formula!r} near {formula[pos:]!r}")
        symbol, count_text = match.groups()
        count = float(count_text) if count_text else 1.0
        result[symbol] = result.get(symbol, 0.0) + count
        pos = match.end()
    if pos != len(formula) or not result:
        raise ValueError(f"Cannot parse chemical formula {formula!r}")
    return result


def add_scaled(target: Dict[str, float], source: Dict[str, float], scale: float) -> None:
    for symbol, amount in source.items():
        target[symbol] = target.get(symbol, 0.0) + amount * scale


def parse_gas_composition(label: str) -> Dict[str, float]:
    label = label.strip()
    match = re.search(r"\((.*)\)", label)
    if match:
        contents = match.group(1)
        parts = MIX_PART_RE.findall(contents)
        if not parts:
            raise ValueError(f"Found parentheses but no percentages in gas label {label!r}")
        stoich: Dict[str, float] = {}
        for percent_text, formula in parts:
            fraction = float(percent_text) / 100.0
            add_scaled(stoich, parse_formula(formula), fraction)
        return stoich
    return parse_formula(label)


def parse_gas_components(label: str) -> List[str]:
    label = label.strip()
    match = re.search(r"\((.*)\)", label)
    if match:
        parts = MIX_PART_RE.findall(match.group(1))
        return [formula for _, formula in parts] if parts else [label]
    return [label]


def validate_composition(stoich: Dict[str, float], elements: Dict[str, Element], label: str) -> None:
    unknown = sorted(symbol for symbol in stoich if symbol not in elements)
    if unknown:
        raise ValueError(f"Gas {label!r} uses unknown element symbols: {', '.join(unknown)}")


def load_gases(csv_path: Path, elements: Dict[str, Element]) -> List[Gas]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        temperature, temperature_unit = parse_temperature_from_headers(fieldnames)
        rows = [{str(k).strip(): v for k, v in row.items()} for row in reader]
        return _load_gases_from_rows(rows, elements, temperature, temperature_unit, fieldnames)


def _load_gases_from_rows(
    rows: Sequence[Dict[str, str]],
    elements: Dict[str, Element],
    temperature: float = 293.0,
    temperature_unit: str = "K",
    original_fieldnames: Sequence[str] | None = None,
) -> List[Gas]:
    gases: List[Gas] = []
    if not rows:
        return gases
    headers = list(rows[0])
    density_key = next((key for key in headers if "density" in key.lower()), "density @ 293 K (g/cm3)")
    pressure_key = next((key for key in headers if "pressure" in key.lower()), "Pressure (atm)")
    pressure_unit = parse_header_unit(
        next((field for field in original_fieldnames or [] if "pressure" in field.lower()), pressure_key),
        "atm",
    )
    for row_index, row in enumerate(rows, start=1):
        label = row["Gas"].strip()
        density = parse_float(row[density_key])
        pressure = parse_float(row[pressure_key])
        stoich = parse_gas_composition(label)
        validate_composition(stoich, elements, label)
        gases.append(
            Gas(
                row_index,
                label,
                density,
                pressure,
                stoich,
                parse_gas_components(label),
                pressure_unit,
                temperature,
                temperature_unit,
            )
        )
    return gases


def normalize_name(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def parse_compound_entry_data(line: str) -> Tuple[str, float, Dict[int, float]] | None:
    try:
        fields = next(csv.reader([line], skipinitialspace=True))
    except csv.Error:
        return None
    if len(fields) < 5:
        return None
    try:
        name = fields[0].strip()
        density = float(fields[1])
        count = int(float(fields[2]))
        raw_pairs = fields[3 : 3 + 2 * count]
        stoich = {
            int(float(raw_pairs[idx])): float(raw_pairs[idx + 1])
            for idx in range(0, len(raw_pairs), 2)
        }
    except (ValueError, IndexError):
        return None
    return name, density, stoich


def load_compound_dictionary(path: Path) -> Dict[str, CompoundEntry]:
    entries: Dict[str, CompoundEntry] = {}
    if not path.exists():
        return entries
    lines = path.read_text(encoding="latin-1", errors="replace").splitlines()
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith('"'):
            continue
        parsed = parse_compound_entry_data(stripped)
        if not parsed:
            continue
        name, density, stoich = parsed
        display = ""
        if idx > 0 and lines[idx - 1].lstrip().startswith("*"):
            display = lines[idx - 1].lstrip()[1:].strip()
            display = re.sub(r"\s{2,}.*$", "", display).strip()
        display_name = display or name
        bond_line = lines[idx + 1].strip() if idx + 1 < len(lines) else ""
        entry = CompoundEntry(name, display_name, density, stoich, bond_line)
        for key in {name, display_name}:
            entries.setdefault(normalize_name(key), entry)

    alias_targets = {
        "CF4": "Carbon Tetrafluoride (ICRU-370)",
        "CH4": "Methane (ICRU-197)",
        "He": "Helium",
        "Helium Gas": "Helium",
    }
    for alias, target in alias_targets.items():
        target_entry = entries.get(normalize_name(target))
        if target_entry:
            entries[normalize_name(alias)] = target_entry
    return entries


def compound_entry_label(formula: str, entry: CompoundEntry) -> str:
    if normalize_name(formula) == normalize_name("He"):
        return "Helium Gas"
    return entry.display_name or entry.dictionary_name


def compound_entry_id(entry: CompoundEntry) -> str:
    match = re.search(r"\b(ICRU-\d+)\b", f"{entry.display_name} {entry.dictionary_name}")
    return match.group(1) if match else ""


def dictionary_decision_for_gas(
    gas: Gas,
    compound_entries: Dict[str, CompoundEntry],
    elements_by_symbol: Dict[str, Element],
) -> CompoundDecision:
    component_matches: List[Tuple[str, CompoundEntry]] = []
    missing: List[str] = []
    for component in gas.components:
        entry = compound_entries.get(normalize_name(component))
        if entry:
            component_matches.append((component, entry))
        else:
            missing.append(component)

    if not component_matches:
        return CompoundDecision(
            "dictionary",
            1.0,
            "",
            "none",
            "No matching COMPOUND.DAT entry found; using compound correction 1.0.",
            "",
        )

    match_names = [compound_entry_label(component, entry) for component, entry in component_matches]
    if missing:
        return CompoundDecision(
            "dictionary",
            1.0,
            "; ".join(match_names),
            "partial",
            f"Matched {', '.join(match_names)} but no entries for {', '.join(missing)}; using correction 1.0.",
            "; ".join(filter(None, (compound_entry_id(entry) for _, entry in component_matches))),
        )

    if len(component_matches) == 1 and len(gas.components) == 1:
        component, entry = component_matches[0]
        match_name = compound_entry_label(component, entry)
        status = "exact" if normalize_name(gas.label) in {
            normalize_name(entry.dictionary_name),
            normalize_name(entry.display_name),
        } else "mapped"
        if normalize_name(component) == normalize_name("He"):
            warning = (
                "Helium Gas is an elemental noble-gas dictionary match. "
                "No molecular/bonding correction is applicable; using correction 1.0."
            )
        else:
            warning = (
                "COMPOUND.DAT contains stoichiometry/bonding metadata, but no directly usable scalar "
                "for SR.IN compound correction was extracted; using correction 1.0."
            )
        return CompoundDecision("dictionary", 1.0, match_name, status, warning, compound_entry_id(entry))

    return CompoundDecision(
        "dictionary",
        1.0,
        "; ".join(match_names),
        "mapped_components",
        (
            "Mixture components were found in COMPOUND.DAT, but SR.IN accepts one scalar correction "
            "and no directly usable mixture scalar was identified; using correction 1.0."
        ),
        "; ".join(filter(None, (compound_entry_id(entry) for _, entry in component_matches))),
    )


def compound_decisions_for_gas(
    gas: Gas,
    mode: str,
    compound_entries: Dict[str, CompoundEntry],
    elements_by_symbol: Dict[str, Element],
) -> List[CompoundDecision]:
    none_decision = CompoundDecision(
        "none",
        1.0,
        "",
        "disabled",
        "Compound dictionary disabled by --compound-mode none; using correction 1.0.",
        "",
    )
    if mode == "none":
        return [none_decision]
    dictionary_decision = dictionary_decision_for_gas(gas, compound_entries, elements_by_symbol)
    if mode == "dictionary":
        return [dictionary_decision]
    return [none_decision, dictionary_decision]


def gas_aliases(gas: Gas) -> List[str]:
    aliases = [gas.label, *gas.components]
    parts = MIX_PART_RE.findall(gas.label)
    if parts:
        formulas = "".join(formula for _, formula in parts)
        percent_labels = []
        for percent, _ in parts:
            value = float(percent)
            percent_labels.append(str(int(value)) if value.is_integer() else f"{value:g}")
        aliases.append(f"{formulas}_{'_'.join(percent_labels)}")
    return aliases


def filter_gases(gases: Sequence[Gas], selectors_text: str) -> List[Gas]:
    selectors = [item.strip() for item in selectors_text.split(",") if item.strip()]
    if not selectors:
        return list(gases)
    normalized_selectors = [normalize_name(item) for item in selectors]
    filtered: List[Gas] = []
    for gas in gases:
        candidates = gas_aliases(gas)
        normalized_candidates = [normalize_name(item) for item in candidates]
        if any(
            selector == candidate or selector in candidate
            for selector in normalized_selectors
            for candidate in normalized_candidates
        ):
            filtered.append(gas)
    return filtered


def logspace(start: float, stop: float, count: int) -> List[float]:
    if start <= 0 or stop <= 0:
        raise ValueError("Log-spaced energies must be positive.")
    if count < 2:
        return [start]
    lo = math.log10(start)
    hi = math.log10(stop)
    return [10 ** (lo + (hi - lo) * i / (count - 1)) for i in range(count)]


def sr_output_name(gas: Gas, ion: Element) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", gas.label).strip("_")
    return f"gas{gas.row_index:03d}_{safe}_{ion.symbol}"


def format_float(value: float, precision: int = 10) -> str:
    return f"{value:.{precision}g}"


def format_decimal_float(value: float, precision: int = 10) -> str:
    text = format_float(value, precision)
    if "e" not in text.lower() and "." not in text:
        text += ".0"
    return text


def write_sr_in(
    path: Path,
    output_name: str,
    ion: Element,
    gas: Gas,
    elements: Dict[str, Element],
    emin_kev: float,
    emax_kev: float,
    compound_correction: float,
) -> None:
    lines = [
        "---Stopping/Range Input Data (Number-format: Period = Decimal Point)",
        "---Output File Name",
        f'"{output_name}"',
        "---Ion(Z), Ion Mass(u)",
        f"{ion.z}   {format_float(ion.mass_u)}",
        "---Target Data: (Solid=0,Gas=1), Density(g/cm3), Compound Corr.",
        f"1    {format_float(gas.density_g_cm3)}    {format_decimal_float(compound_correction)}",
        "---Number of Target Elements",
        f" {len(gas.stoich)} ",
        "---Target Elements: (Z), Target name, Stoich, Target Mass(u)",
    ]
    for symbol, amount in sorted(gas.stoich.items(), key=lambda item: elements[item[0]].z):
        elem = elements[symbol]
        name_padding = " " * max(1, 23 - len(elem.name))
        lines.append(
            f'{elem.z:<4d}"{elem.name}"{name_padding}{format_float(amount)}             {format_float(elem.mass_u)}'
        )
    lines.extend(
        [
            "---Output Stopping Units (1-8)",
            " 5",
            "---Ion Energy : E-Min(keV), E-Max(keV)",
            f" {format_float(emin_kev, 12)}    {format_float(emax_kev, 12)}",
        ]
    )
    path.write_text("\r\n".join(lines) + "\r\n", encoding="ascii")


def prepare_raw_workdir(workdir: Path, clean: bool = True) -> Path:
    if clean and workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    raw_dir = workdir / "raw"
    raw_dir.mkdir(exist_ok=True)
    return raw_dir


def srmodule_dir(root: Path) -> Path:
    module_dir = root / "SR Module"
    required = ["SRModule.exe", "SCOEF03.dat", "SNUC03.dat", "VERSION"]
    missing = [name for name in required if not (module_dir / name).exists()]
    if missing:
        raise FileNotFoundError(f"Missing SR Module files: {', '.join(missing)}")
    return module_dir


def is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    return "microsoft" in platform.uname().release.lower()


def to_windows_path(path: Path) -> str:
    if not is_wsl():
        return str(path)
    completed = subprocess.run(
        ["wslpath", "-w", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"wslpath failed for {path}: {completed.stderr.strip()}")
    return completed.stdout.strip()


def stop_srmodule_processes() -> None:
    if is_wsl():
        command = [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "Get-Process SRModule -ErrorAction SilentlyContinue | Stop-Process -Force",
        ]
    else:
        command = [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "Get-Process SRModule -ErrorAction SilentlyContinue | Stop-Process -Force",
        ]
    subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def unlink_with_retries(path: Path, attempts: int = 20, delay_s: float = 0.25) -> None:
    for attempt in range(attempts):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise PermissionError(
                    f"Could not remove locked SRModule output {path}. A previous SRModule.exe may still be "
                    "running with elevated or incompatible Windows compatibility settings. Close it from "
                    "Task Manager, then run fix_srim_no_admin.ps1; if XP compatibility is still set on "
                    "batch executables, rerun with -ClearBatchCompatibility."
                )
            stop_srmodule_processes()
            time.sleep(delay_s)


def prepare_srmodule_output_path(module_dir: Path, output_name: str) -> Tuple[str, Path]:
    raw_output = module_dir / output_name
    try:
        unlink_with_retries(raw_output)
        return output_name, raw_output
    except PermissionError as exc:
        for attempt in range(1, 100):
            suffix = f"_alt{os.getpid()}_{attempt}"
            trimmed = output_name[: max(1, 120 - len(suffix))]
            alternate_name = f"{trimmed}{suffix}"
            alternate_output = module_dir / alternate_name
            try:
                unlink_with_retries(alternate_output, attempts=2, delay_s=0.1)
            except PermissionError:
                continue
            print(
                f"warning: {exc} Using alternate SRModule output name {alternate_name!r}.",
                flush=True,
            )
            return alternate_name, alternate_output
        raise


def run_srmodule(module_dir: Path, timeout_s: int = 60, expected_output: Path | None = None) -> None:
    # WSL launches Windows PE files directly from /mnt/<drive>/... paths and
    # preserves cwd.  cmd.exe does not reliably preserve the Linux Popen cwd,
    # which breaks SRModule's relative control and data-file lookups.
    exe = module_dir / "SRModule.exe"
    proc = subprocess.Popen(
        [str(exe.resolve())],
        cwd=str(module_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    deadline = time.monotonic() + timeout_s
    last_size = -1
    stable_since = 0.0
    while True:
        returncode = proc.poll()
        if returncode is not None:
            stdout, stderr = proc.communicate()
            if returncode != 0:
                raise RuntimeError(
                    f"SRModule failed with exit code {returncode}\nSTDOUT:\n"
                    f"{stdout.decode('latin-1', errors='replace').strip()}\nSTDERR:\n"
                    f"{stderr.decode('latin-1', errors='replace').strip()}"
                )
            return

        if expected_output is not None and expected_output.exists():
            size = expected_output.stat().st_size
            now = time.monotonic()
            if size > 0 and size == last_size:
                if stable_since and now - stable_since >= 0.8:
                    stop_srmodule_processes()
                    time.sleep(0.5)
                    proc.terminate()
                    try:
                        proc.communicate(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    return
                if not stable_since:
                    stable_since = now
            else:
                stable_since = now
                last_size = size

        if time.monotonic() > deadline:
            stop_srmodule_processes()
            try:
                stdout, stderr = proc.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
            if expected_output is not None and expected_output.exists() and expected_output.stat().st_size > 0:
                return
            stdout_text = stdout.decode("latin-1", errors="replace").strip()
            stderr_text = stderr.decode("latin-1", errors="replace").strip()
            raise subprocess.TimeoutExpired(str(proc.args), timeout_s, output=stdout_text, stderr=stderr_text)

        time.sleep(0.2)


def run_srmodule_legacy(module_dir: Path, timeout_s: int = 60) -> None:
    try:
        return run_srmodule(module_dir, timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"SRModule timed out after {timeout_s} s.\nSTDOUT:\n{exc.output}\nSTDERR:\n{exc.stderr}"
        ) from exc


def length_to_cm(value: float, unit: str) -> float:
    unit = unit.strip().lower()
    factors = {
        "a": 1e-8,
        "ang": 1e-8,
        "angstrom": 1e-8,
        "um": 1e-4,
        "µm": 1e-4,
        "mm": 1e-1,
        "cm": 1.0,
        "m": 100.0,
    }
    if unit not in factors:
        raise ValueError(f"Unknown length unit {unit!r}")
    return value * factors[unit]


def energy_to_kev(value: float, unit: str) -> float:
    unit = unit.strip().lower()
    factors = {
        "ev": 1e-3,
        "kev": 1.0,
        "mev": 1e3,
        "gev": 1e6,
    }
    if unit not in factors:
        raise ValueError(f"Unknown energy unit {unit!r}")
    return value * factors[unit]


TABLE_ROW_RE = re.compile(
    r"^\s*"
    r"(?P<energy>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?)\s*"
    r"(?P<energy_unit>eV|keV|MeV|GeV)\s+"
    r"(?P<selec>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?)\s+"
    r"(?P<snuc>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?)\s+"
    r"(?P<range>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?)\s*"
    r"(?P<range_unit>A|um|µm|mm|cm|m)\s+"
    r"(?P<long>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?)\s*"
    r"(?P<long_unit>A|um|µm|mm|cm|m)\s+"
    r"(?P<lat>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?)\s*"
    r"(?P<lat_unit>A|um|µm|mm|cm|m)\s*$",
    re.IGNORECASE,
)


def parse_stopping_output(path: Path) -> List[StoppingRow]:
    rows: List[StoppingRow] = []
    with path.open("r", encoding="latin-1") as handle:
        for line in handle:
            match = TABLE_ROW_RE.match(line)
            if not match:
                continue
            data = match.groupdict()
            energy = energy_to_kev(float(data["energy"]), data["energy_unit"])
            s_elec = float(data["selec"])
            s_nuc = float(data["snuc"])
            projected = length_to_cm(float(data["range"]), data["range_unit"])
            long = length_to_cm(float(data["long"]), data["long_unit"])
            lat = length_to_cm(float(data["lat"]), data["lat_unit"])
            if energy > 0 and s_elec >= 0 and s_nuc >= 0:
                rows.append(StoppingRow(energy, s_elec, s_nuc, projected, long, lat))
    if not rows:
        raise ValueError(f"No stopping rows parsed from {path}")
    rows.sort(key=lambda row: row.energy_kev)
    return rows


def interpolate_stopping(rows: Sequence[StoppingRow], energy_kev: float) -> StoppingRow:
    if energy_kev <= rows[0].energy_kev:
        return rows[0]
    if energy_kev >= rows[-1].energy_kev:
        return rows[-1]
    lo = 0
    hi = len(rows) - 1
    while hi - lo > 1:
        mid = (hi + lo) // 2
        if rows[mid].energy_kev < energy_kev:
            lo = mid
        else:
            hi = mid
    a = rows[lo]
    b = rows[hi]
    x = (math.log(energy_kev) - math.log(a.energy_kev)) / (
        math.log(b.energy_kev) - math.log(a.energy_kev)
    )
    s_e = math.exp(math.log(max(a.s_elec, 1e-300)) * (1 - x) + math.log(max(b.s_elec, 1e-300)) * x)
    s_n = math.exp(math.log(max(a.s_nuc, 1e-300)) * (1 - x) + math.log(max(b.s_nuc, 1e-300)) * x)
    projected = math.exp(
        math.log(max(a.projected_range_cm, 1e-300)) * (1 - x)
        + math.log(max(b.projected_range_cm, 1e-300)) * x
    )
    long = math.exp(
        math.log(max(a.longitudinal_straggling_cm, 1e-300)) * (1 - x)
        + math.log(max(b.longitudinal_straggling_cm, 1e-300)) * x
    )
    lat = math.exp(
        math.log(max(a.lateral_straggling_cm, 1e-300)) * (1 - x)
        + math.log(max(b.lateral_straggling_cm, 1e-300)) * x
    )
    return StoppingRow(energy_kev, s_e, s_n, projected, long, lat)


def cumulative_electronic_integral(rows: Sequence[StoppingRow]) -> Tuple[List[float], List[float]]:
    energies = [row.energy_kev for row in rows]
    first_total = rows[0].s_elec + rows[0].s_nuc
    first_fraction = rows[0].s_elec / first_total if first_total > 0 else 0.0
    electronic_energy_kev = [rows[0].energy_kev * first_fraction]
    for prev, cur in zip(rows, rows[1:]):
        e0 = prev.energy_kev
        e1 = cur.energy_kev
        de_kev = e1 - e0
        st0 = prev.s_elec + prev.s_nuc
        st1 = cur.s_elec + cur.s_nuc
        if st0 <= 0 or st1 <= 0:
            raise ValueError("Encountered non-positive total stopping power.")
        frac0 = prev.s_elec / st0
        frac1 = cur.s_elec / st1
        d_elec_kev = de_kev * 0.5 * (frac0 + frac1)
        electronic_energy_kev.append(electronic_energy_kev[-1] + d_elec_kev)
    return energies, electronic_energy_kev


def interpolate_series(xs: Sequence[float], ys: Sequence[float], x: float) -> float:
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    lo = 0
    hi = len(xs) - 1
    while hi - lo > 1:
        mid = (hi + lo) // 2
        if xs[mid] < x:
            lo = mid
        else:
            hi = mid
    t = (x - xs[lo]) / (xs[hi] - xs[lo])
    return ys[lo] * (1 - t) + ys[hi] * t


def select_ions(gas: Gas, ions_arg: str, elements: Dict[str, Element]) -> List[Element]:
    if ions_arg.strip().lower() == "gas":
        symbols = sorted(gas.stoich, key=lambda symbol: elements[symbol].z)
    else:
        symbols = [part.strip() for part in ions_arg.split(",") if part.strip()]
    unknown = [symbol for symbol in symbols if symbol not in elements]
    if unknown:
        raise ValueError(f"Unknown ion symbols: {', '.join(unknown)}")
    present_symbols = [symbol for symbol in symbols if symbol in gas.stoich]
    if not present_symbols:
        available = ", ".join(sorted(gas.stoich, key=lambda symbol: elements[symbol].z))
        raise ValueError(
            f"No requested ions are present in gas {gas.label!r}. "
            f"Requested: {', '.join(symbols)}; available gas ions: {available}."
        )
    return [elements[symbol] for symbol in present_symbols]


def print_plan(gases: Sequence[Gas], ions_arg: str, elements: Dict[str, Element]) -> None:
    print(f"Loaded {len(gases)} gas rows.")
    for gas in gases:
        ions = select_ions(gas, ions_arg, elements)
        comp = ", ".join(f"{k}:{v:g}" for k, v in sorted(gas.stoich.items(), key=lambda x: elements[x[0]].z))
        ion_names = ", ".join(ion.symbol for ion in ions)
        print(
            f"gas#{gas.row_index}: {gas.label} density={gas.density_g_cm3:g} g/cm3 "
            f"pressure={gas.pressure_atm:g} {gas.pressure_unit} temp={gas.temperature:g} {gas.temperature_unit} "
            f"comp=({comp}) ions=({ion_names})"
        )


def sanitize_filename(text: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_")
    return safe[:120] or "plot"


def pressure_slug(value_text: str, unit: str) -> str:
    try:
        value = float(value_text)
        value_part = f"{value:g}".replace(".", "p")
    except (TypeError, ValueError):
        value_part = sanitize_filename(str(value_text))
    unit_part = sanitize_filename(unit or "pressure")
    return f"{value_part}{unit_part}"


def gas_pressure_slug(label: str, pressure: str, pressure_unit: str, gas_row: str | int | None = None) -> str:
    prefix = f"gas_{int(gas_row):03d}_" if gas_row not in (None, "") else ""
    return f"{prefix}{sanitize_filename(label)}_{pressure_slug(str(pressure), pressure_unit)}"


def result_float(row: Dict[str, str], key: str) -> float:
    return float(row[key])


def read_result_rows(csv_path: Path) -> List[Dict[str, str]]:
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def grouped_result_rows(rows: Sequence[Dict[str, str]]) -> Dict[int, List[Dict[str, str]]]:
    grouped: Dict[int, List[Dict[str, str]]] = {}
    for row in rows:
        grouped.setdefault(int(row["gas_row"]), []).append(row)
    return grouped


def import_pyplot():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def save_figure(fig, path_base: Path) -> List[Path]:
    paths = [path_base.with_suffix(".png"), path_base.with_suffix(".svg")]
    for path in paths:
        fig.savefig(path, bbox_inches="tight", dpi=180)
    return paths


def generate_gas_plot(gas_rows: Sequence[Dict[str, str]], plots_dir: Path) -> List[Path]:
    plt = import_pyplot()
    first = gas_rows[0]
    gas_row = int(first["gas_row"])
    gas_dir = plots_dir / gas_pressure_slug(first["gas"], first.get("pressure", first.get("pressure_atm", "")), first.get("pressure_unit", ""), gas_row)
    gas_dir.mkdir(parents=True, exist_ok=True)
    title = f"Gas {gas_row}: {first['gas']} ({first['pressure_atm']} atm, {first['density_g_cm3']} g/cm3)"
    by_ion: Dict[str, List[Dict[str, str]]] = {}
    multiple_modes = len({row.get("compound_mode", "") for row in gas_rows}) > 1
    for row in gas_rows:
        label = row["ion"]
        if multiple_modes:
            label = f"{row['ion']} ({row.get('compound_mode', '')})"
        by_ion.setdefault(label, []).append(row)
    fig, axes = plt.subplots(2, 1, figsize=(10.5, 8.0), sharex=True)
    range_ax, qf_ax = axes
    for ion, rows in sorted(by_ion.items()):
        rows = sorted(rows, key=lambda row: result_float(row, "energy_kev"))
        energies = [result_float(row, "energy_kev") for row in rows]
        ranges = [result_float(row, "projected_range_mm") for row in rows]
        quenching = [result_float(row, "quenching_factor_electronic_integral") for row in rows]
        range_ax.plot(energies, ranges, marker=".", linewidth=1.8, label=ion)
        qf_ax.plot(energies, quenching, marker=".", linewidth=1.8, label=ion)

    range_ax.set_title(title)
    range_ax.set_xscale("log")
    range_ax.set_yscale("log")
    range_ax.set_ylabel("Projected range (mm)")
    range_ax.grid(True, which="both", alpha=0.25)
    range_ax.legend(title="Ion", loc="best")

    qf_ax.set_xscale("log")
    qf_ax.set_ylim(0.0, 1.05)
    qf_ax.set_xlabel("Energy (keV)")
    qf_ax.set_ylabel("Electronic quenching proxy")
    qf_ax.grid(True, which="both", alpha=0.25)
    qf_ax.legend(title="Ion", loc="best")
    fig.tight_layout()

    paths = save_figure(fig, gas_dir / "srmodule_range_qf")
    plt.close(fig)
    return paths


def generate_summary_plot(rows: Sequence[Dict[str, str]], plots_dir: Path) -> List[Path]:
    plt = import_pyplot()
    max_energy = max(result_float(row, "energy_kev") for row in rows)
    final_rows = [row for row in rows if abs(result_float(row, "energy_kev") - max_energy) <= max_energy * 1e-9]
    final_rows.sort(key=lambda row: (int(row["gas_row"]), row["ion_z"]))
    multiple_modes = len({row.get("compound_mode", "") for row in final_rows}) > 1
    labels = [
        f"g{row['gas_row']} {row['ion']}" + (f" {row.get('compound_mode', '')}" if multiple_modes else "")
        for row in final_rows
    ]
    values = [result_float(row, "projected_range_mm") for row in final_rows]
    colors = [f"C{idx % 10}" for idx, _ in enumerate(final_rows)]

    fig, ax = plt.subplots(figsize=(13.5, 6.8))
    ax.bar(range(len(values)), values, color=colors)
    ax.set_yscale("log")
    ax.set_ylabel("Projected range at Emax (mm)")
    ax.set_title(f"Summary: Projected Range at {max_energy:g} keV")
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=55, ha="right")
    ax.grid(True, axis="y", which="both", alpha=0.25)
    fig.tight_layout()

    paths = save_figure(fig, plots_dir / "summary_projected_range_at_emax")
    plt.close(fig)
    return paths


def generate_plots(csv_path: Path, plots_dir: Path) -> List[Path]:
    rows = read_result_rows(csv_path)
    if not rows:
        return []
    if plots_dir.exists():
        shutil.rmtree(plots_dir)
    plots_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    for _, gas_rows in sorted(grouped_result_rows(rows).items()):
        written.extend(generate_gas_plot(gas_rows, plots_dir))
    written.extend(generate_summary_plot(rows, plots_dir))
    return written


def main() -> int:
    args = parse_args()
    project_dir = script_dir()
    root = normalize_srim_root_path(args.srim_root).resolve()
    gas_csv = resolve_inside_root(root, args.gas_csv, project_dir, prefer_existing=True)
    output_csv = resolve_inside_root(root, args.out, project_dir)
    plots_dir = resolve_inside_root(root, args.plots_dir, project_dir)
    work_root = resolve_inside_root(root, args.workdir, project_dir)

    if args.plot_only:
        plot_paths = generate_plots(output_csv, plots_dir)
        print(f"Wrote {len(plot_paths)} Matplotlib plot files to {plots_dir}")
        return 0

    if args.points < 2:
        raise ValueError("--points must be at least 2")
    if args.emin_kev <= 0 or args.emax_kev <= args.emin_kev:
        raise ValueError("Require 0 < --emin-kev < --emax-kev")

    elements, _ = load_elements(root / "Data" / "ATOMDATA")
    gases = filter_gases(load_gases(gas_csv, elements), args.gases)
    if not gases:
        raise ValueError(f"No gas rows matched --gases {args.gases!r}")
    compound_entries = load_compound_dictionary(root / "Data" / "Compound.dat")
    print_plan(gases, args.ions, elements)

    output_grid = logspace(args.emin_kev, args.emax_kev, args.points)
    print(f"Energy grid: {args.points} log-spaced output points.")

    if args.dry_run:
        return 0

    module_dir = srmodule_dir(root)
    sr_in_path = module_dir / "SR.IN"
    original_sr_in = sr_in_path.read_bytes()
    raw_dir = prepare_raw_workdir(work_root, clean=True)

    fieldnames = [
        "gas_row",
        "gas",
        "density_g_cm3",
        "pressure",
        "pressure_unit",
        "temperature",
        "temperature_unit",
        "pressure_atm",
        "gas_components",
        "parsed_stoichiometry",
        "target_stoich",
        "compound_mode",
        "compound_match_name",
        "compound_match_status",
        "compound_match_id",
        "compound_correction_used",
        "compound_warning",
        "ion",
        "ion_z",
        "ion_mass_u",
        "energy_kev",
        "energy_mev",
        "s_elec_MeV_per_mg_cm2",
        "s_nuc_MeV_per_mg_cm2",
        "s_total_MeV_per_mg_cm2",
        "electronic_fraction_instant",
        "projected_range_mg_cm2",
        "projected_range_cm",
        "projected_range_mm",
        "longitudinal_straggling_cm",
        "longitudinal_straggling_mm",
        "lateral_straggling_cm",
        "lateral_straggling_mm",
        "quenching_factor_electronic_integral",
    ]

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    total_jobs = 0
    total_rows = 0
    try:
        with output_csv.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for gas in gases:
                target_stoich = ";".join(
                    f"{symbol}:{gas.stoich[symbol]:.12g}"
                    for symbol in sorted(gas.stoich, key=lambda s: elements[s].z)
                )
                gas_components = ";".join(gas.components)
                decisions = compound_decisions_for_gas(gas, args.compound_mode, compound_entries, elements)
                for decision in decisions:
                    for ion in select_ions(gas, args.ions, elements):
                        total_jobs += 1
                        output_name = f"{sr_output_name(gas, ion)}_{decision.mode}"
                        output_name, raw_output = prepare_srmodule_output_path(module_dir, output_name)
                        write_sr_in(
                            sr_in_path,
                            output_name,
                            ion,
                            gas,
                            elements,
                            args.emin_kev,
                            args.emax_kev,
                            decision.correction,
                        )
                        run_srmodule(module_dir, expected_output=raw_output)
                        if not raw_output.exists():
                            raise FileNotFoundError(f"SRModule did not create expected output {raw_output}")
                        rows = parse_stopping_output(raw_output)
                        if args.keep_work:
                            shutil.copy2(sr_in_path, raw_dir / f"{output_name}.SR.IN")
                            shutil.copy2(raw_output, raw_dir / f"{output_name}.srout")
                        unlink_with_retries(raw_output)

                        energies, elec_energy_kev = cumulative_electronic_integral(rows)
                        for energy in output_grid:
                            stopping = interpolate_stopping(rows, energy)
                            s_total = stopping.s_elec + stopping.s_nuc
                            electronic_fraction = stopping.s_elec / s_total if s_total > 0 else math.nan
                            projected_mg = stopping.projected_range_cm * gas.density_g_cm3 * 1000.0
                            elec_integral = interpolate_series(energies, elec_energy_kev, energy)
                            qf_integral = elec_integral / energy if energy > 0 else math.nan
                            writer.writerow(
                                {
                                    "gas_row": gas.row_index,
                                    "gas": gas.label,
                                    "density_g_cm3": format_float(gas.density_g_cm3, 12),
                                    "pressure": format_float(gas.pressure_atm, 12),
                                    "pressure_unit": gas.pressure_unit,
                                    "temperature": format_float(gas.temperature, 12),
                                    "temperature_unit": gas.temperature_unit,
                                    "pressure_atm": format_float(gas.pressure_atm, 12),
                                    "gas_components": gas_components,
                                    "parsed_stoichiometry": target_stoich,
                                    "target_stoich": target_stoich,
                                    "compound_mode": decision.mode,
                                    "compound_match_name": decision.match_name,
                                    "compound_match_status": decision.match_status,
                                    "compound_match_id": decision.match_id,
                                    "compound_correction_used": format_float(decision.correction, 12),
                                    "compound_warning": decision.warning,
                                    "ion": ion.symbol,
                                    "ion_z": ion.z,
                                    "ion_mass_u": format_float(ion.mass_u, 12),
                                    "energy_kev": format_float(energy, 12),
                                    "energy_mev": format_float(energy / 1000.0, 12),
                                    "s_elec_MeV_per_mg_cm2": format_float(stopping.s_elec, 12),
                                    "s_nuc_MeV_per_mg_cm2": format_float(stopping.s_nuc, 12),
                                    "s_total_MeV_per_mg_cm2": format_float(s_total, 12),
                                    "electronic_fraction_instant": format_float(electronic_fraction, 12),
                                    "projected_range_mg_cm2": format_float(projected_mg, 12),
                                    "projected_range_cm": format_float(stopping.projected_range_cm, 12),
                                    "projected_range_mm": format_float(stopping.projected_range_cm * 10.0, 12),
                                    "longitudinal_straggling_cm": format_float(
                                        stopping.longitudinal_straggling_cm, 12
                                    ),
                                    "longitudinal_straggling_mm": format_float(
                                        stopping.longitudinal_straggling_cm * 10.0, 12
                                    ),
                                    "lateral_straggling_cm": format_float(stopping.lateral_straggling_cm, 12),
                                    "lateral_straggling_mm": format_float(
                                        stopping.lateral_straggling_cm * 10.0, 12
                                    ),
                                    "quenching_factor_electronic_integral": format_float(qf_integral, 12),
                                }
                            )
                            total_rows += 1
                        print(
                            f"done gas#{gas.row_index} {gas.label} mode={decision.mode} ion={ion.symbol}: "
                            f"{len(output_grid)} rows",
                            flush=True,
                        )
    finally:
        sr_in_path.write_bytes(original_sr_in)
        if not args.keep_work:
            shutil.rmtree(work_root, ignore_errors=True)
    print(f"Wrote {total_rows} rows from {total_jobs} SRModule jobs to {output_csv}")
    if not args.no_plots:
        plot_paths = generate_plots(output_csv, plots_dir)
        print(f"Wrote {len(plot_paths)} Matplotlib plot files to {plots_dir}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
