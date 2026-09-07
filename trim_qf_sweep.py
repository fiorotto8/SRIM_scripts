#!/usr/bin/env python3
"""
TRIM/IONIZ quenching-factor sweep for CYGNO-like gas recoil studies.

This script complements srim_gas_sweep.py. The SRModule script is the fast
stopping/range table path; this script drives TRIM batch jobs when possible and
parses IONIZ.TXT to estimate the ionization quenching factor:

    QF(E) = integrated ionization energy / initial recoil energy
    Eee   = QF(E) * Eion

The implementation is intentionally transparent. Every generated TRIM.IN and
manifest is kept under work/ when requested, and parse-only mode can rebuild the
CSV from those raw files.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

from srim_gas_sweep import (
    CompoundDecision,
    Element,
    Gas,
    compound_decisions_for_gas,
    default_srim_root,
    filter_gases,
    format_float,
    is_wsl,
    load_compound_dictionary,
    load_elements,
    load_gases,
    normalize_name,
    normalize_srim_root_path,
    resolve_inside_root,
    sanitize_filename,
    select_ions,
    script_dir,
    to_windows_path,
)


THESIS_ENERGIES_KEV = [
    1,
    2,
    3,
    4,
    5,
    6,
    7,
    8,
    9,
    10,
    20,
    30,
    40,
    50,
    60,
    70,
    80,
    90,
    100,
    200,
    300,
    400,
    500,
]

KNOWN_TRIM_OUTPUTS = [
    "IONIZ.txt",
    "IONIZ.TXT",
    "RANGE.txt",
    "RANGE.TXT",
    "VACANCY.txt",
    "VACANCY.TXT",
    "PHONON.txt",
    "PHONON.TXT",
    "E2RECOIL.txt",
    "E2RECOIL.TXT",
    "BACKSCAT.txt",
    "BACKSCAT.TXT",
    "TRANSMIT.txt",
    "TRANSMIT.TXT",
    "SPUTTER.txt",
    "SPUTTER.TXT",
    "COLLISON.txt",
    "COLLISON.TXT",
    "EXYZ.txt",
    "EXYZ.TXT",
]

NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?")
MIX_PART_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*%\s*([A-Za-z][A-Za-z0-9]*)")


@dataclass
class IonizBin:
    depth_angstrom: float
    primary_ev_per_angstrom: float
    recoil_ev_per_angstrom: float


@dataclass
class IonizSummary:
    units_detected: str
    bin_width_angstrom: float
    integration_method: str
    estimated_max_depth_angstrom: float
    primary_ionization_energy_kev: float
    recoil_ionization_energy_kev: float
    total_ionization_energy_kev: float
    qf_ioniz: float
    electron_equivalent_energy_kev: float
    warnings: List[str]


@dataclass
class TrimJob:
    gas: Gas
    ion: Element
    energy_kev: float
    n_ions: int
    trim_mode: str
    cascade_type: int
    target_width_angstrom: float
    job_dir: Path
    compound: CompoundDecision
    density_g_cm3: float
    warnings: List[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run or parse TRIM IONIZ-based QF simulations for gases in GasDensities.csv."
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
    parser.add_argument("--out", type=Path, default=Path("results/trim_qf_results.csv"))
    parser.add_argument("--plots-dir", type=Path, default=Path("plots/trim_qf"))
    parser.add_argument("--workdir", type=Path, default=Path("work/trim_qf"))
    parser.add_argument(
        "--gases",
        default="",
        help="Optional comma-separated gas selectors. Default is all rows in GasDensities.csv.",
    )
    parser.add_argument(
        "--ions",
        default="gas",
        help=(
            "Ion symbols to run, comma-separated, or 'gas' to use each gas's constituent elements. "
            "Requested ions that are not present in a selected gas are skipped."
        ),
    )
    parser.add_argument(
        "--energies-kev",
        default="",
        help=(
            "Comma-separated recoil energies in keV. If omitted, use the default "
            "log grid from --emin-kev to --emax-kev with --points samples."
        ),
    )
    parser.add_argument("--emin-kev", type=float, default=0.1)
    parser.add_argument("--emax-kev", type=float, default=500.0)
    parser.add_argument("--points", type=int, default=100)
    parser.add_argument("--n-ions", type=int, default=100)
    parser.add_argument("--quick-n-ions", type=int, default=100)
    parser.add_argument(
        "--mode",
        choices=("quick-depth-and-qf", "quick-depth-scan", "qf-production", "full-cascade-validation"),
        default="quick-depth-and-qf",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--parse-only", action="store_true")
    parser.add_argument("--keep-work", action="store_true")
    parser.add_argument("--no-plots", action="store_true")
    parser.add_argument("--plot-only", action="store_true")
    parser.add_argument("--timeout-s", type=int, default=300)
    parser.add_argument("--depth-scan-width-mm", type=float, default=50.0)
    parser.add_argument("--fallback-target-width-mm", type=float, default=10.0)
    parser.add_argument("--target-width-margin", type=float, default=2.0)
    parser.add_argument(
        "--force-target-width-mm",
        type=float,
        default=0.0,
        help="Use this target width for production instead of the fitted depth.",
    )
    parser.add_argument(
        "--density-mode",
        choices=("csv", "ideal-gas"),
        default="csv",
        help="Use CSV density by default. ideal-gas recomputes from pressure, temperature, and mixture molecular weight.",
    )
    parser.add_argument(
        "--srmodule-csv",
        type=Path,
        default=Path("results/srim_gas_sweep_compare.csv"),
        help="Optional SRModule CSV used for QF comparison plots if it exists.",
    )
    return parser.parse_args()


def parse_energy_list(text: str) -> List[float]:
    values = [float(part.strip()) for part in text.split(",") if part.strip()]
    if not values:
        raise ValueError("At least one energy is required.")
    if any(value <= 0 for value in values):
        raise ValueError("TRIM energies must be positive.")
    return values


def logspace(start: float, stop: float, count: int) -> List[float]:
    if start <= 0 or stop <= 0:
        raise ValueError("Log-spaced energies must be positive.")
    if stop <= start:
        raise ValueError("Require --emax-kev > --emin-kev for log-spaced energies.")
    if count < 2:
        raise ValueError("--points must be at least 2 for log-spaced energies.")
    lo = math.log10(start)
    hi = math.log10(stop)
    return [10 ** (lo + (hi - lo) * idx / (count - 1)) for idx in range(count)]


def parse_energy_grid(args: argparse.Namespace) -> List[float]:
    if args.energies_kev.strip():
        return parse_energy_list(args.energies_kev)
    return logspace(args.emin_kev, args.emax_kev, args.points)


def gas_aliases(gas: Gas) -> List[str]:
    aliases = [gas.label, *gas.components]
    parts = MIX_PART_RE.findall(gas.label)
    if parts:
        formulas = "".join(formula for _, formula in parts)
        percent_labels = []
        for percent, _ in parts:
            value = float(percent)
            percent_labels.append(str(int(value)) if value.is_integer() else f"{value:g}")
        percents = "_".join(percent_labels)
        aliases.append(f"{formulas}_{percents}")
    return aliases


def filter_gases_with_aliases(gases: Sequence[Gas], selectors_text: str) -> List[Gas]:
    if not selectors_text.strip():
        return list(gases)
    selected = filter_gases(gases, selectors_text)
    selected_ids = {gas.row_index for gas in selected}
    selectors = [normalize_name(part.strip()) for part in selectors_text.split(",") if part.strip()]
    for gas in gases:
        if gas.row_index in selected_ids:
            continue
        aliases = [normalize_name(alias) for alias in gas_aliases(gas)]
        if any(selector == alias or selector in alias for selector in selectors for alias in aliases):
            selected.append(gas)
            selected_ids.add(gas.row_index)
    return selected


def stoich_text(gas: Gas, elements: Dict[str, Element]) -> str:
    return ";".join(f"{symbol}:{gas.stoich[symbol]:.12g}" for symbol in sorted(gas.stoich, key=lambda s: elements[s].z))


def pressure_warning(gas: Gas, density_mode: str) -> str:
    warnings: List[str] = []
    if density_mode == "csv":
        warnings.append("Using density from GasDensities.csv.")
    if abs(gas.pressure_atm - 1.0) > 1e-9 or abs(gas.temperature - 293.0) > 1e-9:
        warnings.append(
            f"Gas row records pressure={gas.pressure_atm:g} {gas.pressure_unit}, "
            f"temperature={gas.temperature:g} {gas.temperature_unit}; no STP normalization was applied."
        )
    return " ".join(warnings)


def formula_mass_u(formula: str, elements: Dict[str, Element]) -> float:
    from srim_gas_sweep import parse_formula

    return sum(elements[symbol].mass_u * count for symbol, count in parse_formula(formula).items())


def gas_molecular_weight_u(gas: Gas, elements: Dict[str, Element]) -> float:
    parts = MIX_PART_RE.findall(gas.label)
    if parts:
        total = 0.0
        frac_sum = 0.0
        for percent_text, formula in parts:
            fraction = float(percent_text) / 100.0
            total += fraction * formula_mass_u(formula, elements)
            frac_sum += fraction
        if frac_sum > 0:
            return total / frac_sum
    return sum(elements[symbol].mass_u * amount for symbol, amount in gas.stoich.items())


def effective_density(gas: Gas, elements: Dict[str, Element], density_mode: str) -> Tuple[float, str]:
    if density_mode == "csv":
        return gas.density_g_cm3, ""
    if gas.pressure_unit.lower() != "atm" or gas.temperature_unit.lower() != "k":
        raise ValueError("ideal-gas density recomputation currently requires pressure in atm and temperature in K.")
    gas_constant_l_atm = 0.082057366080960
    mw = gas_molecular_weight_u(gas, elements)
    density = gas.pressure_atm * mw / (gas_constant_l_atm * gas.temperature) / 1000.0
    return density, f"Density recomputed with ideal gas law using molecular weight {mw:.6g} g/mol."


def angstrom_to_mm(value: float) -> float:
    return value * 1e-7


def mm_to_angstrom(value: float) -> float:
    return value * 1e7


def write_text_crlf(path: Path, lines: Sequence[str]) -> None:
    path.write_text("\r\n".join(lines) + "\r\n", encoding="ascii", errors="strict")


def write_trim_auto(path: Path, mode: int = 1) -> None:
    write_text_crlf(path, [str(mode), ""])


def write_trim_in(
    path: Path,
    gas: Gas,
    ion: Element,
    elements: Dict[str, Element],
    energy_kev: float,
    n_ions: int,
    cascade_type: int,
    target_width_angstrom: float,
    density_g_cm3: float,
    compound_correction: float,
    angle_deg: float = 0.0,
    random_seed: int = 0,
    save_3d_ranges: bool = False,
    save_backscatter: bool = False,
    save_transmit: bool = False,
    displacement_energy_ev: float = 21.0,
    lattice_binding_energy_ev: float = 0.0,
    surface_binding_energy_ev: float = 0.0,
) -> None:
    target_symbols = sorted(gas.stoich, key=lambda symbol: elements[symbol].z)
    title = f"{ion.symbol} {energy_kev:g} keV in {gas.label}"
    title = title[:38].ljust(38)
    layer_name = sanitize_filename(gas.label).replace("_", " ")[:14].ljust(14)
    element_header = "  ".join(f"{symbol}({elements[symbol].z})" for symbol in target_symbols)
    stoich_values = " ".join(f"{gas.stoich[symbol]:.8g}" for symbol in target_symbols)
    displacement = " ".join(f"{displacement_energy_ev:.8g}" for _ in target_symbols)
    lattice = " ".join(f"{lattice_binding_energy_ev:.8g}" for _ in target_symbols)
    surface = " ".join(f"{surface_binding_energy_ev:.8g}" for _ in target_symbols)

    lines = [
        "==> SRIM-2011.00 This file controls TRIM Calculations.",
        "Ion: Z1 ,  M1,  Energy (keV), Angle,Number,Bragg Corr,AutoSave Number.",
        f"{ion.z:6d} {ion.mass_u:10.5f} {energy_kev:12.6g} {angle_deg:7.3g} {n_ions:7d} {0:8d} {min(n_ions, 10000):8d}",
        "Cascade Type[1=None;2=Full;3=Mono/Sputt;4-5=Ions(TRIM.dat);6-7=Recoils(TRIM.dat)], Random Number Seed, Reminders",
        f"{cascade_type:23d} {random_seed:35d} {0:7d}",
        "Diskfiles (0=no,1=yes): Ranges, Backscatt, Transmit, Sputtered, Collisions(1=Ion;2=Ion+Recoils), Special EXYZ.txt file",
        (
            f"                          {int(save_3d_ranges)}       {int(save_backscatter)}"
            f"           {int(save_transmit)}       0               0                               0"
        ),
        "Target material : Number of Elements & Layers",
        f"\"{title}\" {len(target_symbols):7d} {1:15d}",
        "PlotType (0-5); Plot Depths: Xmin, Xmax(Ang.) [=0 0 for Viewing Full Target]",
        f"{5:8d} {0:25d} {target_width_angstrom:15.6g}",
        "Target Elements:    Z   Mass(amu)",
    ]
    for idx, symbol in enumerate(target_symbols, start=1):
        elem = elements[symbol]
        lines.append(f"Atom {idx:d} = {symbol:>2s} = {elem.z:8d} {elem.mass_u:10.5f}")
    lines.extend(
        [
            f"Layer   Layer Name /               Width Density   {element_header}",
            "Numb.   Description                (Ang) (g/cm3)  Stoich",
            f" 1      \"{layer_name}\" {target_width_angstrom:15.6g} {density_g_cm3:10.6g} {stoich_values}",
            "1  Target layer phases (0=Solid, 1=Gas)",
            "       1",
            "Target Compound Corrections (Bragg)",
            f"       {compound_correction:.8g}",
            "Individual target atom displacement energies (eV)",
            f"      {displacement}",
            "Individual target atom lattice binding energies (eV)",
            f"      {lattice}",
            "Individual target atom surface binding energies (eV)",
            f"      {surface}",
            "Stopping Power Version (1=2008, 0=2008)",
            " 0 ",
        ]
    )
    write_text_crlf(path, lines)


def trim_job_name(gas: Gas, ion: Element, energy_kev: float, trim_mode: str) -> str:
    energy = f"{energy_kev:g}".replace(".", "p")
    return f"gas{gas.row_index:03d}_{sanitize_filename(gas.label)}_{ion.symbol}_{energy}keV_{trim_mode}"


def job_manifest(job: TrimJob, elements: Dict[str, Element]) -> Dict[str, object]:
    return {
        "gas_label": job.gas.label,
        "gas_row": job.gas.row_index,
        "gas_density_g_cm3": job.density_g_cm3,
        "csv_density_g_cm3": job.gas.density_g_cm3,
        "pressure": job.gas.pressure_atm,
        "pressure_unit": job.gas.pressure_unit,
        "temperature": job.gas.temperature,
        "temperature_unit": job.gas.temperature_unit,
        "parsed_stoichiometry": stoich_text(job.gas, elements),
        "ion": job.ion.symbol,
        "ion_z": job.ion.z,
        "ion_mass_u": job.ion.mass_u,
        "energy_kev": job.energy_kev,
        "n_ions": job.n_ions,
        "trim_mode": job.trim_mode,
        "cascade_type": job.cascade_type,
        "target_width_angstrom": job.target_width_angstrom,
        "target_width_mm": angstrom_to_mm(job.target_width_angstrom),
        "compound_match_status": job.compound.match_status,
        "compound_match_name": job.compound.match_name,
        "compound_match_id": job.compound.match_id,
        "compound_correction_used": job.compound.correction,
        "warnings": " ".join(job.warnings),
    }


def write_job_inputs(job: TrimJob, elements: Dict[str, Element]) -> None:
    job.job_dir.mkdir(parents=True, exist_ok=True)
    write_trim_in(
        job.job_dir / "TRIM.IN",
        job.gas,
        job.ion,
        elements,
        job.energy_kev,
        job.n_ions,
        job.cascade_type,
        job.target_width_angstrom,
        job.density_g_cm3,
        job.compound.correction,
    )
    write_trim_auto(job.job_dir / "TRIMAUTO", 1)
    (job.job_dir / "manifest.json").write_text(
        json.dumps(job_manifest(job, elements), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def clear_previous_outputs(root: Path) -> None:
    for name in KNOWN_TRIM_OUTPUTS:
        path = root / name
        if path.exists():
            path.unlink()


def collect_outputs(root: Path, job_dir: Path) -> None:
    for name in KNOWN_TRIM_OUTPUTS:
        path = root / name
        if path.exists():
            shutil.copy2(path, job_dir / path.name.upper())


def stop_trim_processes() -> None:
    command = [
        "powershell.exe",
        "-NoProfile",
        "-Command",
        "Get-Process TRIM -ErrorAction SilentlyContinue | Stop-Process -Force",
    ]
    subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def run_trim_exe(root: Path, timeout_s: int) -> None:
    exe = root / "TRIM.exe"
    if not exe.exists():
        raise FileNotFoundError(f"Missing TRIM executable: {exe}")
    # Direct WSL PE launch keeps SRIM's working directory intact; invoking via
    # cmd.exe can make relative control/output paths resolve elsewhere.
    cmd = [str(exe.resolve())]
    cwd = str(root)
    try:
        completed = subprocess.run(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stop_trim_processes()
        raise RuntimeError(
            "TRIM.exe timed out. If a UAC dialog was visible, run fix_srim_no_admin.ps1 once "
            "and retry from a normal shell."
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            "TRIM.exe could not be launched. If Windows reports that elevation is required, "
            "run fix_srim_no_admin.ps1 once and check compatibility flags for RUNASADMIN."
        ) from exc
    if completed.returncode != 0:
        stdout = completed.stdout.decode("latin-1", errors="replace").strip()
        stderr = completed.stderr.decode("latin-1", errors="replace").strip()
        raise RuntimeError(
            f"TRIM.exe failed with exit code {completed.returncode}. "
            "This often means RUNASADMIN compatibility is still set or TRIM cannot batch-run here.\n"
            f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"
        )


def numeric_columns_from_line(line: str) -> Tuple[float, float, float] | None:
    fixed = [line[0:11], line[12:24], line[25:37]]
    try:
        values = [float(part.strip()) for part in fixed]
        if values[0] > 0:
            return values[0], values[1], values[2]
    except ValueError:
        pass
    if not re.match(r"^\s*[-+]?(?:\d|\.)", line):
        return None
    numbers = NUMBER_RE.findall(line)
    if len(numbers) >= 3:
        try:
            depth, primary, recoil = (float(numbers[0]), float(numbers[1]), float(numbers[2]))
        except ValueError:
            return None
        if depth > 0:
            return depth, primary, recoil
    return None


def detect_ioniz_units(text: str) -> Tuple[str, str, str]:
    match = re.search(r"Ionization\s+Energy\s+Units\s+are\s+>+\s*(.*?)\s*<+", text, flags=re.IGNORECASE)
    units = re.sub(r"\s+", " ", match.group(1).strip()) if match else "unknown"
    normalized = units.lower().replace(" ", "")
    if "ev" in normalized and "angstrom" in normalized and "ion" in normalized:
        return (
            units,
            "differential_ev_per_angstrom_per_ion",
            "sum((ionization_by_ions + ionization_by_recoils) * bin_width_angstrom) / 1000",
        )
    if "ev" in normalized and "ion" in normalized and "angstrom" not in normalized:
        return (
            units,
            "bin_ev_per_ion",
            "sum(ionization_by_ions + ionization_by_recoils) / 1000",
        )
    return (
        units,
        "assumed_differential_ev_per_angstrom_per_ion",
        "sum((ionization_by_ions + ionization_by_recoils) * bin_width_angstrom) / 1000",
    )


def parse_ioniz(path: Path, energy_kev: float) -> IonizSummary:
    if not path.exists():
        raise FileNotFoundError(f"Missing TRIM ionization output {path}")
    text = path.read_text(encoding="latin-1", errors="replace")
    units_detected, units_kind, integration_method = detect_ioniz_units(text)
    bins: List[IonizBin] = []
    for line in text.splitlines():
        parsed = numeric_columns_from_line(line)
        if parsed is None:
            continue
        depth, primary, recoil = parsed
        bins.append(IonizBin(depth, primary, recoil))
    if not bins:
        raise ValueError(f"No numeric ionization bins parsed from {path}")
    bins.sort(key=lambda item: item.depth_angstrom)
    deltas = [
        b.depth_angstrom - a.depth_angstrom
        for a, b in zip(bins, bins[1:])
        if b.depth_angstrom > a.depth_angstrom
    ]
    bin_width = statistics.median(deltas) if deltas else 0.0
    if bin_width <= 0:
        raise ValueError(f"Could not determine IONIZ bin width from {path}")
    warnings: List[str] = []
    if units_kind.startswith("assumed_"):
        warnings.append(
            "IONIZ.TXT unit header was not recognized; assuming differential eV/(Angstrom-Ion) "
            "and multiplying by bin width."
        )
    total_profile = [item.primary_ev_per_angstrom + item.recoil_ev_per_angstrom for item in bins]
    max_profile = max(total_profile)
    threshold = max(max_profile * 1e-9, 0.0)
    positive_indices = [idx for idx, value in enumerate(total_profile) if value > threshold]
    if positive_indices:
        last_positive = positive_indices[-1]
        next_index = min(last_positive + 1, len(bins) - 1)
        estimated_max_depth = bins[next_index].depth_angstrom
    else:
        estimated_max_depth = 0.0

    if units_kind == "bin_ev_per_ion":
        primary_kev = sum(item.primary_ev_per_angstrom for item in bins) / 1000.0
        recoil_kev = sum(item.recoil_ev_per_angstrom for item in bins) / 1000.0
    else:
        primary_kev = sum(item.primary_ev_per_angstrom for item in bins) * bin_width / 1000.0
        recoil_kev = sum(item.recoil_ev_per_angstrom for item in bins) * bin_width / 1000.0
    total_kev = primary_kev + recoil_kev
    qf = total_kev / energy_kev if energy_kev > 0 else math.nan
    return IonizSummary(
        units_detected,
        bin_width,
        integration_method,
        estimated_max_depth,
        primary_kev,
        recoil_kev,
        total_kev,
        qf,
        qf * energy_kev,
        warnings,
    )


def length_value_to_mm(value: float, unit: str) -> float:
    unit = unit.lower()
    factors = {
        "a": 1e-7,
        "ang": 1e-7,
        "angstrom": 1e-7,
        "um": 1e-3,
        "mm": 1.0,
        "cm": 10.0,
        "m": 1000.0,
    }
    return value * factors[unit]


def parse_range_file(path: Path) -> Tuple[str, str, str]:
    if not path.exists():
        return "", "", ""
    text = path.read_text(encoding="latin-1", errors="replace")
    value_unit = r"([-+]?\d+(?:\.\d*)?(?:[Ee][-+]?\d+)?)\s*(A|Ang|um|mm|cm|m)"

    # SRIM-2013 TRIM RANGE.TXT commonly reports the ion distribution as:
    # "Ion Average Range = ... A   Straggling = ... A"
    # "Ion Lateral Range = ... A   Straggling = ... A"
    average_match = re.search(
        rf"Ion\s+Average\s+Range\s*=\s*{value_unit}\s+Straggling\s*=\s*{value_unit}",
        text,
        flags=re.IGNORECASE,
    )
    lateral_match = re.search(
        rf"Ion\s+Lateral\s+Range\s*=\s*{value_unit}\s+Straggling\s*=\s*{value_unit}",
        text,
        flags=re.IGNORECASE,
    )
    if average_match:
        projected = format_float(length_value_to_mm(float(average_match.group(1)), average_match.group(2)), 12)
        longitudinal = format_float(length_value_to_mm(float(average_match.group(3)), average_match.group(4)), 12)
        lateral = ""
        if lateral_match:
            lateral = format_float(length_value_to_mm(float(lateral_match.group(3)), lateral_match.group(4)), 12)
        return projected, longitudinal, lateral

    patterns = {
        "projected": rf"Projected\s+Range[^\d+-]*{value_unit}",
        "longitudinal": rf"Longitudinal\s+Straggling[^\d+-]*{value_unit}",
        "lateral": rf"Lateral\s+Straggling[^\d+-]*{value_unit}",
    }
    values: Dict[str, str] = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            values[key] = format_float(length_value_to_mm(float(match.group(1)), match.group(2)), 12)
        else:
            values[key] = ""
    return values["projected"], values["longitudinal"], values["lateral"]


def ioniz_sanity_warnings(summary: IonizSummary, energy_kev: float) -> List[str]:
    warnings = list(summary.warnings)
    tolerance_kev = max(1e-6, abs(energy_kev) * 1e-6)
    if summary.total_ionization_energy_kev > energy_kev + tolerance_kev:
        warnings.append(
            "Integrated IONIZ energy exceeds the initial ion energy beyond numerical tolerance; "
            "check IONIZ normalization, target width, and TRIM mode."
        )
    if summary.qf_ioniz < -1e-6 or summary.qf_ioniz > 1.05:
        warnings.append(
            "Integrated IONIZ energy gives QF outside the expected [0,1.05] sanity interval; "
            "check IONIZ normalization, target width, and TRIM mode."
        )
    elif summary.qf_ioniz > 1.0:
        warnings.append(
            "Integrated IONIZ energy gives QF above 1.0; this may be statistical/normalization noise "
            "but should be inspected."
        )
    return warnings


def row_from_job(job: TrimJob, elements: Dict[str, Element], summary: IonizSummary) -> Dict[str, str]:
    range_path = next((path for path in [job.job_dir / "RANGE.TXT", job.job_dir / "RANGE.txt"] if path.exists()), None)
    projected, longitudinal, lateral = parse_range_file(range_path) if range_path else ("", "", "")
    manifest = job_manifest(job, elements)
    warnings = [item for item in job.warnings if item]
    warnings.extend(ioniz_sanity_warnings(summary, job.energy_kev))
    if job.target_width_angstrom > 0 and summary.estimated_max_depth_angstrom >= 0.98 * job.target_width_angstrom:
        warnings.append(
            "Ionization profile reaches the target/plot window edge; rerun with a larger target width."
        )
    return {
        "gas_label": str(manifest["gas_label"]),
        "gas_density_g_cm3": format_float(job.density_g_cm3, 12),
        "pressure": format_float(job.gas.pressure_atm, 12),
        "pressure_unit": job.gas.pressure_unit,
        "temperature": format_float(job.gas.temperature, 12),
        "temperature_unit": job.gas.temperature_unit,
        "parsed_stoichiometry": stoich_text(job.gas, elements),
        "ion": job.ion.symbol,
        "ion_z": str(job.ion.z),
        "ion_mass_u": format_float(job.ion.mass_u, 12),
        "energy_kev": format_float(job.energy_kev, 12),
        "n_ions": str(job.n_ions),
        "trim_mode": job.trim_mode,
        "target_width_angstrom": format_float(job.target_width_angstrom, 12),
        "target_width_mm": format_float(angstrom_to_mm(job.target_width_angstrom), 12),
        "estimated_max_depth_mm": format_float(angstrom_to_mm(summary.estimated_max_depth_angstrom), 12),
        "ioniz_units_detected": summary.units_detected,
        "ioniz_bin_width_angstrom": format_float(summary.bin_width_angstrom, 12),
        "ioniz_integration_method": summary.integration_method,
        "qf_ioniz": format_float(summary.qf_ioniz, 12),
        "electron_equivalent_energy_kev": format_float(summary.electron_equivalent_energy_kev, 12),
        "primary_ionization_energy_kev": format_float(summary.primary_ionization_energy_kev, 12),
        "recoil_or_secondary_ionization_energy_kev": format_float(summary.recoil_ionization_energy_kev, 12),
        "total_ionization_energy_kev": format_float(summary.total_ionization_energy_kev, 12),
        "projected_range_mm": projected,
        "longitudinal_straggling_mm": longitudinal,
        "lateral_straggling_mm": lateral,
        "compound_match_status": job.compound.match_status,
        "compound_match_name": job.compound.match_name,
        "compound_match_id": job.compound.match_id,
        "compound_correction_used": format_float(job.compound.correction, 12),
        "warnings": " ".join(warnings),
    }


def output_fieldnames() -> List[str]:
    return [
        "gas_label",
        "gas_density_g_cm3",
        "pressure",
        "pressure_unit",
        "temperature",
        "temperature_unit",
        "parsed_stoichiometry",
        "ion",
        "ion_z",
        "ion_mass_u",
        "energy_kev",
        "n_ions",
        "trim_mode",
        "target_width_angstrom",
        "target_width_mm",
        "estimated_max_depth_mm",
        "ioniz_units_detected",
        "ioniz_bin_width_angstrom",
        "ioniz_integration_method",
        "qf_ioniz",
        "electron_equivalent_energy_kev",
        "primary_ionization_energy_kev",
        "recoil_or_secondary_ionization_energy_kev",
        "total_ionization_energy_kev",
        "projected_range_mm",
        "longitudinal_straggling_mm",
        "lateral_straggling_mm",
        "compound_match_status",
        "compound_match_name",
        "compound_match_id",
        "compound_correction_used",
        "warnings",
    ]


def run_job(root: Path, job: TrimJob, elements: Dict[str, Element], timeout_s: int, dry_run: bool) -> Dict[str, str] | None:
    write_job_inputs(job, elements)
    if dry_run:
        return None

    trim_in = root / "TRIM.IN"
    trim_auto = root / "TRIMAUTO"
    original_trim_in = trim_in.read_bytes() if trim_in.exists() else b""
    original_trim_auto = trim_auto.read_bytes() if trim_auto.exists() else b""
    try:
        shutil.copy2(job.job_dir / "TRIM.IN", trim_in)
        write_trim_auto(trim_auto, 1)
        clear_previous_outputs(root)
        run_trim_exe(root, timeout_s)
        collect_outputs(root, job.job_dir)
    finally:
        if original_trim_in:
            trim_in.write_bytes(original_trim_in)
        else:
            trim_in.unlink(missing_ok=True)
        if original_trim_auto:
            trim_auto.write_bytes(original_trim_auto)
        else:
            trim_auto.unlink(missing_ok=True)

    ioniz_path = next((path for path in [job.job_dir / "IONIZ.TXT", job.job_dir / "IONIZ.txt"] if path.exists()), None)
    if ioniz_path is None:
        raise FileNotFoundError(
            f"TRIM finished but no IONIZ.TXT was found for {job.job_dir}. "
            "Batch automation may not be producing plot TXT files on this installation."
        )
    summary = parse_ioniz(ioniz_path, job.energy_kev)
    return row_from_job(job, elements, summary)


def fit_power_law(depth_rows: Sequence[Dict[str, str]]) -> Tuple[float, float] | None:
    pairs = [
        (float(row["energy_kev"]), float(row["estimated_max_depth_mm"]))
        for row in depth_rows
        if float(row["energy_kev"]) > 0 and float(row["estimated_max_depth_mm"]) > 0
    ]
    if len(pairs) < 2:
        return None
    xs = [math.log(item[0]) for item in pairs]
    ys = [math.log(item[1]) for item in pairs]
    xbar = sum(xs) / len(xs)
    ybar = sum(ys) / len(ys)
    denom = sum((x - xbar) ** 2 for x in xs)
    if denom <= 0:
        return None
    p1 = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys)) / denom
    p0 = math.exp(ybar - p1 * xbar)
    return p0, p1


def estimated_width_angstrom(
    energy_kev: float,
    fit: Tuple[float, float] | None,
    args: argparse.Namespace,
    depth_rows: Sequence[Dict[str, str]],
) -> float:
    if args.force_target_width_mm > 0:
        return mm_to_angstrom(args.force_target_width_mm)
    estimate_mm = 0.0
    if fit:
        p0, p1 = fit
        estimate_mm = p0 * (energy_kev ** p1)
    if estimate_mm <= 0:
        for row in depth_rows:
            if abs(float(row["energy_kev"]) - energy_kev) <= max(energy_kev, 1.0) * 1e-9:
                estimate_mm = float(row["estimated_max_depth_mm"])
                break
    if estimate_mm <= 0:
        estimate_mm = args.fallback_target_width_mm
    return max(mm_to_angstrom(estimate_mm * args.target_width_margin), 1000.0)


def prepare_launcher(root: Path, workdir: Path) -> None:
    win_root = to_windows_path(root) if is_wsl() else str(root)
    win_workdir = to_windows_path(workdir) if is_wsl() else str(workdir)
    lines = [
        "@echo off",
        "setlocal enabledelayedexpansion",
        f"set SRIM_ROOT={win_root}",
        f"set WORKDIR={win_workdir}",
        "if not exist \"%SRIM_ROOT%\\TRIM.exe\" (",
        "  echo Missing TRIM.exe",
        "  exit /b 1",
        ")",
        "for /f \"usebackq delims=\" %%J in (\"%WORKDIR%\\trim_run_list.txt\") do (",
        "  echo Running %%J",
        "  copy /Y \"%%J\\TRIM.IN\" \"%SRIM_ROOT%\\TRIM.IN\" >nul",
        "  if exist \"%%J\\TRIMAUTO\" (",
        "    copy /Y \"%%J\\TRIMAUTO\" \"%SRIM_ROOT%\\TRIMAUTO\" >nul",
        "  ) else (",
        "    > \"%SRIM_ROOT%\\TRIMAUTO\" echo 1",
        "    >> \"%SRIM_ROOT%\\TRIMAUTO\" echo(",
        "  )",
        "  pushd \"%SRIM_ROOT%\"",
        "  TRIM.exe",
        "  popd",
        "  for %%F in (IONIZ.TXT RANGE.TXT VACANCY.TXT PHONON.TXT E2RECOIL.TXT BACKSCAT.TXT TRANSMIT.TXT SPUTTER.TXT COLLISON.TXT EXYZ.TXT) do (",
        "    if exist \"%SRIM_ROOT%\\%%F\" copy /Y \"%SRIM_ROOT%\\%%F\" \"%%J\\%%F\" >nul",
        "  )",
        ")",
        "echo Done. Re-run trim_qf_sweep.py --parse-only --workdir %WORKDIR%",
    ]
    write_text_crlf(workdir / "run_trim_jobs.bat", lines)


def append_run_list(workdir: Path, jobs: Sequence[TrimJob]) -> None:
    with (workdir / "trim_run_list.txt").open("a", encoding="utf-8", newline="") as handle:
        for job in jobs:
            job_path = to_windows_path(job.job_dir) if is_wsl() else str(job.job_dir)
            handle.write(job_path + "\n")


def import_pyplot():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def read_rows(csv_path: Path) -> List[Dict[str, str]]:
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def save_figure(fig, path_base: Path) -> List[Path]:
    paths = [path_base.with_suffix(".png"), path_base.with_suffix(".svg")]
    for path in paths:
        fig.savefig(path, dpi=180, bbox_inches="tight")
    return paths


def rows_by_gas(rows: Sequence[Dict[str, str]]) -> Dict[str, List[Dict[str, str]]]:
    grouped: Dict[str, List[Dict[str, str]]] = {}
    for row in rows:
        grouped.setdefault(gas_pressure_key(row), []).append(row)
    return grouped


def pressure_slug(value_text: str, unit: str) -> str:
    try:
        value = float(value_text)
        value_part = f"{value:g}".replace(".", "p")
    except (TypeError, ValueError):
        value_part = sanitize_filename(str(value_text))
    unit_part = sanitize_filename(unit or "pressure")
    return f"{value_part}{unit_part}"


def gas_pressure_slug_from_values(label: str, pressure: str, pressure_unit: str) -> str:
    return f"{sanitize_filename(label)}_{pressure_slug(pressure, pressure_unit)}"


def gas_pressure_key(row: Dict[str, str]) -> str:
    return "|".join(
        [
            row.get("gas_label", ""),
            row.get("pressure", ""),
            row.get("pressure_unit", ""),
            row.get("temperature", ""),
            row.get("temperature_unit", ""),
        ]
    )


def same_gas_pressure(trim_row: Dict[str, str], sr_row: Dict[str, str]) -> bool:
    if sr_row.get("gas") != trim_row.get("gas_label"):
        return False
    try:
        pressure_matches = abs(float(sr_row.get("pressure", "nan")) - float(trim_row.get("pressure", "nan"))) < 1e-9
    except ValueError:
        pressure_matches = sr_row.get("pressure") == trim_row.get("pressure")
    return pressure_matches and sr_row.get("pressure_unit", "") == trim_row.get("pressure_unit", "")


def report_qualitative_qf_order(rows: Sequence[Dict[str, str]]) -> None:
    production_rows = [
        row
        for row in rows
        if row.get("trim_mode") in {"qf_production", "full_cascade_validation"} and row.get("qf_ioniz")
    ]
    for _, gas_rows in rows_by_gas(production_rows).items():
        gas_label = gas_rows[0]["gas_label"]
        medians: Dict[str, float] = {}
        for ion in ["H", "He", "C", "F"]:
            values = [float(row["qf_ioniz"]) for row in gas_rows if row.get("ion") == ion]
            if values:
                medians[ion] = statistics.median(values)
        if not medians:
            continue
        present = " > ".join(f"{ion}:{medians[ion]:.4g}" for ion in ["H", "He", "C", "F"] if ion in medians)
        print(f"Qualitative QF median check for {gas_label}: {present}", flush=True)
        if all(ion in medians for ion in ["H", "He", "C", "F"]):
            if not (medians["H"] >= medians["He"] >= medians["C"] >= medians["F"]):
                print(
                    "warning: Median QF ordering does not follow the broad expected H >= He >= C >= F trend; "
                    "inspect the CSV/plots before using these runs as physics results.",
                    flush=True,
                )


def generate_trim_plots(csv_path: Path, plots_dir: Path, srmodule_csv: Path | None) -> List[Path]:
    rows = [row for row in read_rows(csv_path) if row.get("trim_mode") in {"qf_production", "full_cascade_validation"}]
    if not rows:
        return []
    if plots_dir.exists():
        shutil.rmtree(plots_dir)
    plots_dir.mkdir(parents=True, exist_ok=True)
    plt = import_pyplot()
    written: List[Path] = []

    sr_rows: List[Dict[str, str]] = []
    if srmodule_csv and srmodule_csv.exists():
        sr_rows = read_rows(srmodule_csv)

    for _, gas_rows in rows_by_gas(rows).items():
        first = gas_rows[0]
        gas_label = first["gas_label"]
        gas_dir = plots_dir / gas_pressure_slug_from_values(gas_label, first.get("pressure", ""), first.get("pressure_unit", ""))
        gas_dir.mkdir(parents=True, exist_ok=True)
        by_ion: Dict[str, List[Dict[str, str]]] = {}
        for row in gas_rows:
            by_ion.setdefault(row["ion"], []).append(row)

        fig, axes = plt.subplots(3, 1, figsize=(10.5, 11.0), sharex=True)
        qf_ax, eee_ax, depth_ax = axes
        for ion, ion_rows in sorted(by_ion.items()):
            ion_rows = sorted(ion_rows, key=lambda item: float(item["energy_kev"]))
            energies = [float(item["energy_kev"]) for item in ion_rows]
            qf = [float(item["qf_ioniz"]) for item in ion_rows]
            eee = [float(item["electron_equivalent_energy_kev"]) for item in ion_rows]
            depth = [float(item["estimated_max_depth_mm"]) for item in ion_rows]
            qf_ax.plot(energies, qf, marker="o", linewidth=1.7, label=ion)
            eee_ax.plot(energies, eee, marker="o", linewidth=1.7, label=ion)
            depth_ax.plot(energies, depth, marker="o", linewidth=1.7, label=ion)

        qf_ax.set_title(f"TRIM IONIZ QF: {gas_label}")
        qf_ax.set_ylabel("QF = Eioniz / Eion")
        qf_ax.set_ylim(0, 1.05)
        eee_ax.set_ylabel("Eee (keV)")
        depth_ax.set_ylabel("Depth/range proxy (mm)")
        depth_ax.set_xlabel("Ion energy (keV)")
        depth_ax.set_yscale("log")
        for ax in axes:
            ax.set_xscale("log")
            ax.grid(True, which="both", alpha=0.25)
            ax.legend(title="Ion", loc="best")
        fig.tight_layout()
        written.extend(save_figure(fig, gas_dir / "trim_qf"))
        plt.close(fig)

        if sr_rows:
            fig, axes = plt.subplots(2, 1, figsize=(10.5, 9.0), sharex=True)
            qf_cmp_ax, range_cmp_ax = axes
            plotted_range = False
            for ion, ion_rows in sorted(by_ion.items()):
                ion_rows = sorted(ion_rows, key=lambda item: float(item["energy_kev"]))
                qf_cmp_ax.plot(
                    [float(item["energy_kev"]) for item in ion_rows],
                    [float(item["qf_ioniz"]) for item in ion_rows],
                    marker="o",
                    linewidth=1.8,
                    label=f"TRIM {ion}",
                )
                trim_range_rows = [item for item in ion_rows if item.get("projected_range_mm")]
                if trim_range_rows:
                    plotted_range = True
                    range_cmp_ax.plot(
                        [float(item["energy_kev"]) for item in trim_range_rows],
                        [float(item["projected_range_mm"]) for item in trim_range_rows],
                        marker="o",
                        linewidth=1.8,
                        label=f"TRIM {ion}",
                    )
                sr_match = [
                    item
                    for item in sr_rows
                    if same_gas_pressure(first, item)
                    and item.get("ion") == ion
                    and item.get("compound_mode", "none") in {"none", ""}
                ]
                if sr_match:
                    sr_match = sorted(sr_match, key=lambda item: float(item["energy_kev"]))
                    qf_cmp_ax.plot(
                        [float(item["energy_kev"]) for item in sr_match],
                        [float(item["quenching_factor_electronic_integral"]) for item in sr_match],
                        linestyle="--",
                        linewidth=1.3,
                        label=f"SRModule {ion}",
                    )
                    sr_range_rows = [item for item in sr_match if item.get("projected_range_mm")]
                    if sr_range_rows:
                        plotted_range = True
                        range_cmp_ax.plot(
                            [float(item["energy_kev"]) for item in sr_range_rows],
                            [float(item["projected_range_mm"]) for item in sr_range_rows],
                            linestyle="--",
                            linewidth=1.3,
                            label=f"SRModule {ion}",
                        )
            qf_cmp_ax.set_title(f"TRIM IONIZ QF vs SRModule electronic proxy: {gas_label}")
            qf_cmp_ax.set_ylabel("Electronic fraction")
            qf_cmp_ax.set_xscale("log")
            qf_cmp_ax.set_ylim(0, 1.05)
            qf_cmp_ax.grid(True, which="both", alpha=0.25)
            qf_cmp_ax.legend(loc="best")
            range_cmp_ax.set_xlabel("Ion energy (keV)")
            range_cmp_ax.set_ylabel("Projected range (mm)")
            range_cmp_ax.set_xscale("log")
            if plotted_range:
                range_cmp_ax.set_yscale("log")
            range_cmp_ax.grid(True, which="both", alpha=0.25)
            range_cmp_ax.legend(loc="best")
            fig.tight_layout()
            written.extend(save_figure(fig, gas_dir / "trim_vs_srmodule"))
            plt.close(fig)

    return written


def parse_only(workdir: Path, output_csv: Path, elements: Dict[str, Element]) -> int:
    manifests = sorted(workdir.rglob("manifest.json"))
    if not manifests:
        raise FileNotFoundError(f"No manifest.json files found under {workdir}")
    rows: List[Dict[str, str]] = []
    for manifest_path in manifests:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        ioniz_path = next(
            (path for path in [manifest_path.parent / "IONIZ.TXT", manifest_path.parent / "IONIZ.txt"] if path.exists()),
            None,
        )
        if ioniz_path is None:
            continue
        summary = parse_ioniz(ioniz_path, float(data["energy_kev"]))
        range_path = next(
            (path for path in [manifest_path.parent / "RANGE.TXT", manifest_path.parent / "RANGE.txt"] if path.exists()),
            None,
        )
        projected, longitudinal, lateral = parse_range_file(range_path) if range_path else ("", "", "")
        warning_items = [str(data.get("warnings", "")).strip(), *ioniz_sanity_warnings(summary, float(data["energy_kev"]))]
        target_width_angstrom = float(data["target_width_angstrom"])
        if target_width_angstrom > 0 and summary.estimated_max_depth_angstrom >= 0.98 * target_width_angstrom:
            warning_items.append("Ionization profile reaches the target/plot window edge; rerun with a larger target width.")
        warnings = " ".join(item for item in warning_items if item)
        rows.append(
            {
                "gas_label": str(data["gas_label"]),
                "gas_density_g_cm3": format_float(float(data["gas_density_g_cm3"]), 12),
                "pressure": format_float(float(data["pressure"]), 12),
                "pressure_unit": str(data["pressure_unit"]),
                "temperature": format_float(float(data["temperature"]), 12),
                "temperature_unit": str(data["temperature_unit"]),
                "parsed_stoichiometry": str(data["parsed_stoichiometry"]),
                "ion": str(data["ion"]),
                "ion_z": str(data["ion_z"]),
                "ion_mass_u": format_float(float(data["ion_mass_u"]), 12),
                "energy_kev": format_float(float(data["energy_kev"]), 12),
                "n_ions": str(data["n_ions"]),
                "trim_mode": str(data["trim_mode"]),
                "target_width_angstrom": format_float(float(data["target_width_angstrom"]), 12),
                "target_width_mm": format_float(float(data["target_width_mm"]), 12),
                "estimated_max_depth_mm": format_float(angstrom_to_mm(summary.estimated_max_depth_angstrom), 12),
                "ioniz_units_detected": summary.units_detected,
                "ioniz_bin_width_angstrom": format_float(summary.bin_width_angstrom, 12),
                "ioniz_integration_method": summary.integration_method,
                "qf_ioniz": format_float(summary.qf_ioniz, 12),
                "electron_equivalent_energy_kev": format_float(summary.electron_equivalent_energy_kev, 12),
                "primary_ionization_energy_kev": format_float(summary.primary_ionization_energy_kev, 12),
                "recoil_or_secondary_ionization_energy_kev": format_float(summary.recoil_ionization_energy_kev, 12),
                "total_ionization_energy_kev": format_float(summary.total_ionization_energy_kev, 12),
                "projected_range_mm": projected,
                "longitudinal_straggling_mm": longitudinal,
                "lateral_straggling_mm": lateral,
                "compound_match_status": str(data.get("compound_match_status", "")),
                "compound_match_name": str(data.get("compound_match_name", "")),
                "compound_match_id": str(data.get("compound_match_id", "")),
                "compound_correction_used": format_float(float(data.get("compound_correction_used", 1.0)), 12),
                "warnings": warnings,
            }
        )
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=output_fieldnames())
        writer.writeheader()
        writer.writerows(rows)
    print(f"Parsed {len(rows)} TRIM result rows from {workdir} into {output_csv}")
    return len(rows)


def print_plan(gases: Sequence[Gas], ions_arg: str, energies: Sequence[float], elements: Dict[str, Element], args: argparse.Namespace) -> None:
    print(f"Loaded {len(gases)} gas row(s).")
    print(f"Mode: {args.mode}; energies: {len(energies)}; n_ions={args.n_ions}; quick_n_ions={args.quick_n_ions}")
    for gas in gases:
        ions = select_ions(gas, ions_arg, elements)
        print(
            f"gas#{gas.row_index}: {gas.label} density={gas.density_g_cm3:g} g/cm3 "
            f"pressure={gas.pressure_atm:g} {gas.pressure_unit} temp={gas.temperature:g} {gas.temperature_unit} "
            f"stoich=({stoich_text(gas, elements)}) ions={','.join(ion.symbol for ion in ions)}"
        )
    if args.mode == "quick-depth-and-qf":
        print("Planned TRIM jobs: quick depth scan plus production QF for each gas/ion/energy.")
    else:
        print("Planned TRIM jobs: one pass for each gas/ion/energy.")


def main() -> int:
    args = parse_args()
    project_dir = script_dir()
    root = normalize_srim_root_path(args.srim_root).resolve()
    gas_csv = resolve_inside_root(root, args.gas_csv, project_dir, prefer_existing=True)
    output_csv = resolve_inside_root(root, args.out, project_dir)
    plots_dir = resolve_inside_root(root, args.plots_dir, project_dir)
    workdir = resolve_inside_root(root, args.workdir, project_dir)
    srmodule_csv = resolve_inside_root(root, args.srmodule_csv, project_dir, prefer_existing=True)

    if args.plot_only:
        report_qualitative_qf_order(read_rows(output_csv))
        paths = generate_trim_plots(output_csv, plots_dir, srmodule_csv)
        print(f"Wrote {len(paths)} TRIM plot files to {plots_dir}")
        return 0

    elements, _ = load_elements(root / "Data" / "ATOMDATA")

    if args.parse_only:
        count = parse_only(workdir, output_csv, elements)
        if count and not args.no_plots:
            report_qualitative_qf_order(read_rows(output_csv))
            paths = generate_trim_plots(output_csv, plots_dir, srmodule_csv)
            print(f"Wrote {len(paths)} TRIM plot files to {plots_dir}")
        elif count:
            report_qualitative_qf_order(read_rows(output_csv))
        return 0

    energies = parse_energy_grid(args)
    gases = filter_gases_with_aliases(load_gases(gas_csv, elements), args.gases)
    if not gases:
        raise ValueError(f"No gas rows matched --gases {args.gases!r}")
    compounds = load_compound_dictionary(root / "Data" / "Compound.dat")
    print_plan(gases, args.ions, energies, elements, args)
    if args.dry_run:
        print("Dry run only: no TRIM.exe calls will be made.")

    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "trim_run_list.txt").write_text("", encoding="utf-8")
    prepare_launcher(root, workdir)

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    rows_written = 0
    with output_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=output_fieldnames())
        writer.writeheader()
        for gas in gases:
            density, density_note = effective_density(gas, elements, args.density_mode)
            base_warning = pressure_warning(gas, args.density_mode)
            if density_note:
                base_warning = f"{base_warning} {density_note}".strip()
            compound = compound_decisions_for_gas(gas, "dictionary", compounds, elements)[0]
            ions = select_ions(gas, args.ions, elements)
            for ion in ions:
                depth_rows: List[Dict[str, str]] = []
                if args.mode in {"quick-depth-and-qf", "quick-depth-scan"}:
                    quick_jobs: List[TrimJob] = []
                    for energy in energies:
                        job_dir = workdir / trim_job_name(gas, ion, energy, "quick_depth_scan")
                        quick_jobs.append(
                            TrimJob(
                                gas,
                                ion,
                                energy,
                                args.quick_n_ions,
                                "quick_depth_scan",
                                1,
                                mm_to_angstrom(args.depth_scan_width_mm),
                                job_dir,
                                compound,
                                density,
                                [base_warning, compound.warning],
                            )
                        )
                    append_run_list(workdir, quick_jobs)
                    for job in quick_jobs:
                        row = run_job(root, job, elements, args.timeout_s, args.dry_run)
                        if row:
                            writer.writerow(row)
                            handle.flush()
                            depth_rows.append(row)
                            rows_written += 1
                            print(
                                f"done {job.trim_mode} gas={gas.label} ion={ion.symbol} E={job.energy_kev:g} keV "
                                f"QF={row['qf_ioniz']}",
                                flush=True,
                            )
                    if args.mode == "quick-depth-scan":
                        continue

                fit = fit_power_law(depth_rows)
                if args.mode == "qf-production":
                    fit = None
                if fit:
                    p0, p1 = fit
                    print(f"depth fit gas={gas.label} ion={ion.symbol}: L_mm = {p0:.6g} * E_keV^{p1:.6g}", flush=True)
                elif args.mode == "quick-depth-and-qf" and args.dry_run:
                    print(
                        f"dry-run gas={gas.label} ion={ion.symbol}: production target widths use fallback "
                        "because quick-scan depths are not available until TRIM has run.",
                        flush=True,
                    )

                if args.mode in {"quick-depth-and-qf", "qf-production", "full-cascade-validation"}:
                    production_mode = "full_cascade_validation" if args.mode == "full-cascade-validation" else "qf_production"
                    production_cascade_type = 2 if production_mode == "full_cascade_validation" else 1
                    production_jobs: List[TrimJob] = []
                    for energy in energies:
                        if args.mode == "qf-production" and args.force_target_width_mm <= 0:
                            width = mm_to_angstrom(args.fallback_target_width_mm)
                        else:
                            width = estimated_width_angstrom(energy, fit, args, depth_rows)
                        job_dir = workdir / trim_job_name(gas, ion, energy, production_mode)
                        production_jobs.append(
                            TrimJob(
                                gas,
                                ion,
                                energy,
                                args.n_ions,
                                production_mode,
                                production_cascade_type,
                                width,
                                job_dir,
                                compound,
                                density,
                                [base_warning, compound.warning],
                            )
                        )
                    append_run_list(workdir, production_jobs)
                    for job in production_jobs:
                        row = run_job(root, job, elements, args.timeout_s, args.dry_run)
                        if row:
                            writer.writerow(row)
                            handle.flush()
                            rows_written += 1
                            print(
                                f"done {job.trim_mode} gas={gas.label} ion={ion.symbol} E={job.energy_kev:g} keV "
                                f"QF={row['qf_ioniz']}",
                                flush=True,
                            )

    if args.dry_run:
        print(f"Wrote TRIM.IN templates, manifests, run list, and launcher under {workdir}")
        return 0

    print(f"Wrote {rows_written} TRIM rows to {output_csv}")
    if not args.keep_work:
        # Keep only the run list and launcher if the user did not request raw files.
        launcher = (workdir / "run_trim_jobs.bat").read_bytes()
        run_list = (workdir / "trim_run_list.txt").read_bytes()
        shutil.rmtree(workdir, ignore_errors=True)
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "run_trim_jobs.bat").write_bytes(launcher)
        (workdir / "trim_run_list.txt").write_bytes(run_list)
    if not args.no_plots and rows_written:
        report_qualitative_qf_order(read_rows(output_csv))
        paths = generate_trim_plots(output_csv, plots_dir, srmodule_csv)
        print(f"Wrote {len(paths)} TRIM plot files to {plots_dir}")
    elif rows_written:
        report_qualitative_qf_order(read_rows(output_csv))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
