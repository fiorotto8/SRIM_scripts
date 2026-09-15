#!/usr/bin/env python3
"""Resumable SRIM scan for Xe/CF4, Kr/CF4, and Ar/CF4 gas mixtures.

The program deliberately separates calculated SRIM quantities from gas inputs
derived with the ideal-gas law.  It never edits the source SRIM installation:
each active worker uses a private runtime copy under the selected run directory.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import gzip
import hashlib
import html
import io
import json
import math
import os
import platform
import re
import signal
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from srim_gas_sweep import (
    Element,
    Gas,
    cumulative_electronic_integral,
    format_float,
    interpolate_series,
    interpolate_stopping,
    load_elements,
    normalize_srim_root_path,
    parse_stopping_output,
    script_dir,
    write_sr_in,
)
from trim_qf_sweep import (
    angstrom_to_mm,
    length_value_to_mm,
    parse_ioniz,
    write_text_crlf,
    write_trim_in,
)


SCHEMA_VERSION = 1
STANDARD_TRIM_OUTPUTS = (
    "TDATA.txt",
    "RANGE.txt",
    "LATERAL.txt",
    "IONIZ.txt",
    "E2RECOIL.txt",
    "PHONON.txt",
    "VACANCY.txt",
    "NOVAC.txt",
)
OPTIONAL_TRIM_OUTPUTS = (
    "BACKSCAT.txt",
    "TRANSMIT.txt",
    "SPUTTER.txt",
    "TRIMOUT.txt",
    "RANGE_3D.txt",
    "EXYZ.txt",
    "COLLISON.txt",
)
ALL_TRIM_OUTPUTS = STANDARD_TRIM_OUTPUTS + OPTIONAL_TRIM_OUTPUTS
NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?")
SHUTDOWN_REQUESTED = threading.Event()


class WorkerInfrastructureError(RuntimeError):
    """A worker runtime is unhealthy; continuing would poison later jobs."""


class SessionBudgetExhausted(RuntimeError):
    """A time-boxed worker reached the hard session deadline."""


@dataclass(frozen=True)
class ScanConfig:
    path: Path
    raw: Dict[str, Any]
    name: str
    families: Tuple[str, ...]
    noble_percent: Tuple[int, ...]
    pressures_atm: Tuple[float, ...]
    temperature_k: float
    energy_min_kev: float
    energy_max_kev: float
    energy_points: int
    gas_constant: float
    avogadro: float
    compound_correction: float
    angle_deg: float
    displacement_energy_ev: float
    lattice_binding_energy_ev: float
    surface_binding_energy_ev: float
    srmodule_timeout_s: int
    trim_ions: int
    trim_energy_points: int
    trim_pressures: Tuple[float, ...]
    derive_full_grid: bool
    trim_cascade_type: int
    trim_timeout_s: int
    workers_default: int
    max_attempts: int
    target_sigma_margin: float
    target_width_factor: float
    minimum_monolayers: float
    edge_fraction: float
    convergence_percent: Tuple[int, ...]
    convergence_pressures: Tuple[float, ...]
    convergence_energies: Tuple[float, ...]
    convergence_ions: int
    convergence_timeout_s: int
    convergence_repeats: int
    convergence_rse: float
    qf_pressure_warning: float
    qf_pressure_failure: float
    srmodule_relative_tolerance: float
    trim_srmodule_range_warning: float
    convergence_rse_severity: str
    campaign_sessions: int
    campaign_session_hours: float
    campaign_stop_margin_minutes: float
    campaign_max_cumulative_hours: float
    execution_backend: str
    srim_root_hint: str
    pysrim_root_hint: str
    pysrim_expected_commit: str
    wine_prefix_hint: str
    headless: bool


@dataclass(frozen=True)
class ExecutionContext:
    """Resolved process-launch settings for this machine."""

    backend: str
    pysrim_root: Path | None = None
    pysrim_expected_commit: str = ""
    wine_prefix: Path | None = None
    headless: bool = False


@dataclass(frozen=True)
class GasCondition:
    condition_id: str
    row_index: int
    family: str
    noble_percent: int
    cf4_percent: int
    pressure_atm: float
    temperature_k: float
    molecular_weight_g_mol: float
    density_g_cm3: float
    molecule_number_density_cm3: float
    atom_number_density_cm3: float
    stoich: Dict[str, float]
    atomic_fractions: Dict[str, float]
    mass_fractions: Dict[str, float]
    element_masses_u: Dict[str, float]
    label: str

    def as_gas(self) -> Gas:
        return Gas(
            row_index=self.row_index,
            label=self.label,
            density_g_cm3=self.density_g_cm3,
            pressure_atm=self.pressure_atm,
            stoich=dict(self.stoich),
            components=[self.family, "CF4"],
            pressure_unit="atm",
            temperature=self.temperature_k,
            temperature_unit="K",
        )


@dataclass(frozen=True)
class JobSpec:
    job_id: str
    stage: str
    payload: Dict[str, Any]


@dataclass(frozen=True)
class RangeSummary:
    projected_range_mm: float | None
    longitudinal_straggling_mm: float | None
    lateral_range_mm: float | None
    lateral_straggling_mm: float | None
    radial_range_mm: float | None
    radial_straggling_mm: float | None
    skewness: float | None
    kurtosis: float | None
    backscattered_ions: int | None
    transmitted_ions: int | None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def stable_id(prefix: str, value: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()[:20]
    return f"{prefix}-{digest}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def logspace(start: float, stop: float, count: int) -> List[float]:
    if start <= 0 or stop <= start or count < 2:
        raise ValueError("Energy grid requires 0 < minimum < maximum and at least two points.")
    lo = math.log10(start)
    hi = math.log10(stop)
    return [10 ** (lo + (hi - lo) * index / (count - 1)) for index in range(count)]


def load_config(path: Path) -> ScanConfig:
    path = path.resolve()
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    scan = raw["scan"]
    physics = raw["physics"]
    srmodule = raw["srmodule"]
    trim = raw["trim"]
    convergence = raw["convergence"]
    validation = raw["validation"]
    campaign = raw.get("campaign", {})
    execution = raw.get("execution", {})
    if scan.get("ions") != "constituents":
        raise ValueError("This canonical scan requires scan.ions = 'constituents'.")
    if physics.get("density_model") != "ideal-gas":
        raise ValueError("This canonical scan supports physics.density_model = 'ideal-gas'.")
    cfg = ScanConfig(
        path=path,
        raw=raw,
        name=str(scan["name"]),
        families=tuple(str(item) for item in scan["families"]),
        noble_percent=tuple(int(item) for item in scan["noble_percent"]),
        pressures_atm=tuple(float(item) for item in scan["pressures_atm"]),
        temperature_k=float(scan["temperature_k"]),
        energy_min_kev=float(scan["energy_min_kev"]),
        energy_max_kev=float(scan["energy_max_kev"]),
        energy_points=int(scan["energy_points"]),
        gas_constant=float(physics["gas_constant_l_atm_mol_k"]),
        avogadro=float(physics["avogadro_mol"]),
        compound_correction=float(physics["compound_correction"]),
        angle_deg=float(physics["incidence_angle_deg"]),
        displacement_energy_ev=float(physics["displacement_energy_ev"]),
        lattice_binding_energy_ev=float(physics["lattice_binding_energy_ev"]),
        surface_binding_energy_ev=float(physics["surface_binding_energy_ev"]),
        srmodule_timeout_s=int(srmodule["timeout_s"]),
        trim_ions=int(trim["ions_per_job"]),
        trim_energy_points=int(trim.get("energy_points", scan["energy_points"])),
        trim_pressures=tuple(
            float(item) for item in trim.get("pressures_atm", scan["pressures_atm"])
        ),
        derive_full_grid=bool(trim.get("derive_full_grid", False)),
        trim_cascade_type=int(trim["cascade_type"]),
        trim_timeout_s=int(trim["timeout_s"]),
        workers_default=int(trim["workers_default"]),
        max_attempts=int(trim["max_attempts"]),
        target_sigma_margin=float(trim["target_sigma_margin"]),
        target_width_factor=float(trim["target_width_factor"]),
        minimum_monolayers=float(trim["minimum_monolayers"]),
        edge_fraction=float(trim["edge_fraction"]),
        convergence_percent=tuple(int(item) for item in convergence["noble_percent"]),
        convergence_pressures=tuple(float(item) for item in convergence["pressures_atm"]),
        convergence_energies=tuple(float(item) for item in convergence["energies_kev"]),
        convergence_ions=int(convergence["ions_per_job"]),
        convergence_timeout_s=int(convergence.get("timeout_s", trim["timeout_s"])),
        convergence_repeats=int(convergence["repeats"]),
        convergence_rse=float(convergence["maximum_relative_standard_error"]),
        qf_pressure_warning=float(validation["qf_absolute_pressure_warning"]),
        qf_pressure_failure=float(validation["qf_absolute_pressure_failure"]),
        srmodule_relative_tolerance=float(validation["srmodule_relative_tolerance"]),
        trim_srmodule_range_warning=float(validation["trim_srmodule_range_warning"]),
        convergence_rse_severity=str(validation.get("convergence_rse_severity", "failure")),
        campaign_sessions=int(campaign.get("sessions", 0)),
        campaign_session_hours=float(campaign.get("session_hours", 0.0)),
        campaign_stop_margin_minutes=float(campaign.get("stop_margin_minutes", 0.0)),
        campaign_max_cumulative_hours=float(campaign.get("max_cumulative_hours", 0.0)),
        execution_backend=str(execution.get("backend", "auto")),
        srim_root_hint=str(execution.get("srim_root", "")),
        pysrim_root_hint=str(execution.get("pysrim_root", "")),
        pysrim_expected_commit=str(execution.get("pysrim_expected_commit", "")),
        wine_prefix_hint=str(execution.get("wine_prefix", "")),
        headless=bool(execution.get("headless", False)),
    )
    validate_config(cfg)
    return cfg


def validate_config(cfg: ScanConfig) -> None:
    profile = cfg.raw.get("scan", {}).get("profile", "standard")
    if profile not in {"standard", "preliminary"}:
        raise ValueError("scan.profile must be standard or preliminary.")
    if profile == "standard" and cfg.families != ("Xe", "Kr", "Ar"):
        raise ValueError("Expected scan families Xe, Kr, Ar in that order.")
    if (not cfg.families or len(set(cfg.families)) != len(cfg.families)
            or any(value not in {"Xe", "Kr", "Ar"} for value in cfg.families)):
        raise ValueError("Scan families must be a nonempty unique subset of Xe, Kr, Ar.")
    if profile == "standard" and cfg.noble_percent != tuple(range(60, 100, 5)):
        raise ValueError("Expected noble percentages 60, 65, ..., 95.")
    if profile == "standard" and cfg.pressures_atm != (0.5, 1.0, 1.5, 2.0, 2.5, 3.0):
        raise ValueError("Expected pressures 0.5, 1.0, ..., 3.0 atm.")
    if (not cfg.noble_percent or len(set(cfg.noble_percent)) != len(cfg.noble_percent)
            or any(value not in range(60, 100, 5) for value in cfg.noble_percent)):
        raise ValueError("Noble percentages must be a nonempty unique subset of 60, 65, ..., 95.")
    if (not cfg.pressures_atm or len(set(cfg.pressures_atm)) != len(cfg.pressures_atm)
            or any(value not in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0) for value in cfg.pressures_atm)):
        raise ValueError("Pressures must be a nonempty unique subset of 0.5, 1.0, ..., 3.0 atm.")
    if cfg.raw.get("trim", {}).get("job_order", "longest-first") not in {"longest-first", "coverage-first"}:
        raise ValueError("trim.job_order must be longest-first or coverage-first.")
    if (
        cfg.temperature_k <= 0
        or cfg.trim_ions <= 0
        or cfg.trim_timeout_s <= 0
        or cfg.convergence_timeout_s <= 0
        or cfg.convergence_repeats < 2
    ):
        raise ValueError("Temperature, ion count, and convergence repeats are invalid.")
    if cfg.trim_cascade_type != 2:
        raise ValueError("The agreed scan requires TRIM full cascades (cascade type 2).")
    logspace(cfg.energy_min_kev, cfg.energy_max_kev, cfg.energy_points)
    logspace(cfg.energy_min_kev, cfg.energy_max_kev, cfg.trim_energy_points)
    if not cfg.trim_pressures or any(
        not any(abs(value - pressure) < 1.0e-12 for pressure in cfg.pressures_atm)
        for value in cfg.trim_pressures
    ):
        raise ValueError("Every trim.pressures_atm value must belong to scan.pressures_atm.")
    if cfg.derive_full_grid and len(cfg.trim_pressures) != 1:
        raise ValueError("Pressure-derived scans require exactly one direct TRIM pressure.")
    if (
        cfg.campaign_sessions < 0
        or cfg.campaign_session_hours < 0
        or cfg.campaign_max_cumulative_hours < 0
    ):
        raise ValueError("Campaign session count and duration cannot be negative.")
    if cfg.campaign_stop_margin_minutes < 0:
        raise ValueError("Campaign stop margin cannot be negative.")
    if (
        cfg.campaign_max_cumulative_hours
        and cfg.campaign_stop_margin_minutes >= cfg.campaign_max_cumulative_hours * 60
    ):
        raise ValueError("Campaign stop margin must be shorter than the cumulative budget.")
    if cfg.convergence_rse_severity not in {"warning", "failure"}:
        raise ValueError("validation.convergence_rse_severity must be warning or failure.")
    if cfg.execution_backend not in {"auto", "native", "pysrim"}:
        raise ValueError("execution.backend must be auto, native, or pysrim.")


def resolve_repo_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (script_dir() / path).resolve()


def resolve_srim_root(explicit: Path | None, cfg: ScanConfig | None = None) -> Path:
    if explicit is not None:
        return normalize_srim_root_path(explicit).resolve()
    if cfg is not None and cfg.srim_root_hint:
        return normalize_srim_root_path(resolve_repo_path(cfg.srim_root_hint)).resolve()
    config = script_dir() / "srim-root.txt"
    if config.exists():
        for line in config.read_text(encoding="utf-8", errors="replace").splitlines():
            value = line.strip()
            if value and not value.startswith("#"):
                return normalize_srim_root_path(value).resolve()
    return script_dir()


def running_under_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/sys/kernel/osrelease").read_text().lower()
    except OSError:
        return False


def resolve_execution_context(
    cfg: ScanConfig,
    backend_override: str | None = None,
    pysrim_root_override: Path | None = None,
    wine_prefix_override: Path | None = None,
    no_headless: bool = False,
) -> ExecutionContext:
    backend = backend_override or cfg.execution_backend
    if backend == "auto":
        backend = "native" if os.name == "nt" or running_under_wsl() else "pysrim"
    pysrim_root: Path | None = None
    wine_prefix: Path | None = None
    headless = False
    if backend == "pysrim":
        if pysrim_root_override is not None:
            pysrim_root = pysrim_root_override.expanduser().resolve()
        elif cfg.pysrim_root_hint:
            pysrim_root = resolve_repo_path(cfg.pysrim_root_hint)
        else:
            pysrim_root = (script_dir().parent / "pysrim").resolve()
        if wine_prefix_override is not None:
            wine_prefix = wine_prefix_override.expanduser().resolve()
        elif cfg.wine_prefix_hint:
            wine_prefix = resolve_repo_path(cfg.wine_prefix_hint)
        else:
            wine_prefix = (script_dir().parent / ".wine-srim").resolve()
        headless = cfg.headless and not no_headless
    return ExecutionContext(
        backend=backend,
        pysrim_root=pysrim_root,
        pysrim_expected_commit=cfg.pysrim_expected_commit,
        wine_prefix=wine_prefix,
        headless=headless,
    )


def generate_conditions(cfg: ScanConfig, elements: Mapping[str, Element]) -> List[GasCondition]:
    for symbol in (*cfg.families, "C", "F"):
        if symbol not in elements:
            raise ValueError(f"SRIM ATOMDATA does not contain required element {symbol}.")
    cf4_mass = elements["C"].mass_u + 4.0 * elements["F"].mass_u
    conditions: List[GasCondition] = []
    row_index = 0
    for family in cfg.families:
        for noble_percent in cfg.noble_percent:
            x = noble_percent / 100.0
            cf4 = 1.0 - x
            stoich = {family: x, "C": cf4, "F": 4.0 * cf4}
            stoich_sum = sum(stoich.values())
            atomic = {key: value / stoich_sum for key, value in stoich.items()}
            mass_terms = {key: value * elements[key].mass_u for key, value in stoich.items()}
            mass_sum = sum(mass_terms.values())
            mass = {key: value / mass_sum for key, value in mass_terms.items()}
            molecular_weight = x * elements[family].mass_u + cf4 * cf4_mass
            for pressure in cfg.pressures_atm:
                row_index += 1
                mol_per_litre = pressure / (cfg.gas_constant * cfg.temperature_k)
                density = mol_per_litre * molecular_weight / 1000.0
                molecules = mol_per_litre * cfg.avogadro / 1000.0
                atoms = molecules * stoich_sum
                pslug = f"{pressure:g}".replace(".", "p")
                condition_id = f"{family.lower()}{noble_percent:02d}_cf4_{100-noble_percent:02d}_p{pslug}atm"
                label = f"{family}CF4({noble_percent}% {family} {100-noble_percent}% CF4)"
                conditions.append(
                    GasCondition(
                        condition_id=condition_id,
                        row_index=row_index,
                        family=family,
                        noble_percent=noble_percent,
                        cf4_percent=100 - noble_percent,
                        pressure_atm=pressure,
                        temperature_k=cfg.temperature_k,
                        molecular_weight_g_mol=molecular_weight,
                        density_g_cm3=density,
                        molecule_number_density_cm3=molecules,
                        atom_number_density_cm3=atoms,
                        stoich=stoich,
                        atomic_fractions=atomic,
                        mass_fractions=mass,
                        element_masses_u={key: elements[key].mass_u for key in stoich},
                        label=label,
                    )
                )
    return conditions


def condition_row(condition: GasCondition) -> Dict[str, Any]:
    return {
        "condition_id": condition.condition_id,
        "family": condition.family,
        "noble_percent": condition.noble_percent,
        "cf4_percent": condition.cf4_percent,
        "pressure_atm": format_float(condition.pressure_atm, 12),
        "temperature_k": format_float(condition.temperature_k, 12),
        "molecular_weight_g_mol": format_float(condition.molecular_weight_g_mol, 12),
        "density_g_cm3": format_float(condition.density_g_cm3, 12),
        "molecule_number_density_cm3": format_float(condition.molecule_number_density_cm3, 12),
        "atom_number_density_cm3": format_float(condition.atom_number_density_cm3, 12),
        "stoichiometry": canonical_json(condition.stoich),
        "atomic_fractions": canonical_json(condition.atomic_fractions),
        "mass_fractions": canonical_json(condition.mass_fractions),
        "element_masses_u": canonical_json(condition.element_masses_u),
        "density_model": "ideal-gas",
        "density_provenance": "p*M/(R*T); masses from installed SRIM ATOMDATA",
        "srim_pressure_model": "SRIM gas stopping assumes STP; pressure changes density/range scaling only",
    }


def write_csv_atomic(path: Path, rows: Sequence[Mapping[str, Any]], fieldnames: Sequence[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        ordered: List[str] = []
        for row in rows:
            for key in row:
                if key not in ordered:
                    ordered.append(key)
        fieldnames = ordered
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def open_database(run_dir: Path) -> sqlite3.Connection:
    run_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(run_dir / "jobs.sqlite", timeout=60)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            stage TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            result_path TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        )
        """
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS campaign_sessions (
            session_id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            started_epoch REAL NOT NULL,
            finished_at TEXT,
            finished_epoch REAL,
            elapsed_seconds REAL
        )
        """
    )
    connection.commit()
    return connection


def campaign_usage(
    connection: sqlite3.Connection,
    maximum_hours: float,
    now_epoch: float | None = None,
) -> Dict[str, float | int]:
    """Return persisted campaign use, conservatively charging open sessions."""
    now = time.time() if now_epoch is None else now_epoch
    elapsed = 0.0
    open_sessions = 0
    for started, finished, duration in connection.execute(
        "SELECT started_epoch,finished_epoch,elapsed_seconds FROM campaign_sessions"
    ):
        if duration is not None:
            elapsed += max(0.0, float(duration))
        elif finished is not None:
            elapsed += max(0.0, float(finished) - float(started))
        else:
            open_sessions += 1
            elapsed += max(0.0, now - float(started))
    maximum_seconds = max(0.0, maximum_hours * 3600.0)
    remaining = max(0.0, maximum_seconds - elapsed) if maximum_seconds else math.inf
    return {
        "elapsed_seconds": elapsed,
        "remaining_seconds": remaining,
        "maximum_seconds": maximum_seconds,
        "open_sessions": open_sessions,
    }


def begin_campaign_session(
    connection: sqlite3.Connection,
    maximum_hours: float,
    now_epoch: float | None = None,
) -> Tuple[int | None, Dict[str, float | int]]:
    """Close abandoned sessions and begin one budget-accounted invocation."""
    now = time.time() if now_epoch is None else now_epoch
    stamp = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")
    abandoned = connection.execute(
        "SELECT session_id,started_epoch FROM campaign_sessions WHERE finished_epoch IS NULL"
    ).fetchall()
    for session_id, started in abandoned:
        elapsed = max(0.0, now - float(started))
        connection.execute(
            """
            UPDATE campaign_sessions
            SET finished_at=?,finished_epoch=?,elapsed_seconds=?
            WHERE session_id=?
            """,
            (stamp, now, elapsed, session_id),
        )
    connection.commit()
    usage = campaign_usage(connection, maximum_hours, now)
    if float(usage["remaining_seconds"]) <= 0.0:
        return None, usage
    cursor = connection.execute(
        "INSERT INTO campaign_sessions(started_at,started_epoch) VALUES(?,?)",
        (stamp, now),
    )
    connection.commit()
    return int(cursor.lastrowid), usage


def finish_campaign_session(
    connection: sqlite3.Connection,
    session_id: int | None,
    now_epoch: float | None = None,
) -> None:
    if session_id is None:
        return
    now = time.time() if now_epoch is None else now_epoch
    row = connection.execute(
        "SELECT started_epoch FROM campaign_sessions WHERE session_id=?", (session_id,)
    ).fetchone()
    if row is None:
        return
    stamp = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")
    connection.execute(
        """
        UPDATE campaign_sessions
        SET finished_at=?,finished_epoch=?,elapsed_seconds=?
        WHERE session_id=?
        """,
        (stamp, now, max(0.0, now - float(row[0])), session_id),
    )
    connection.commit()


def register_jobs(connection: sqlite3.Connection, jobs: Sequence[JobSpec]) -> None:
    now = utc_now()
    connection.executemany(
        "INSERT OR IGNORE INTO jobs(job_id,stage,payload_json,status,created_at) VALUES(?,?,?,?,?)",
        [(job.job_id, job.stage, canonical_json(job.payload), "pending", now) for job in jobs],
    )
    connection.commit()


def set_job_state(
    connection: sqlite3.Connection,
    job_id: str,
    status: str,
    result_path: str = "",
    error: str = "",
) -> None:
    if status == "running":
        connection.execute(
            "UPDATE jobs SET status=?,attempts=attempts+1,started_at=?,error='' WHERE job_id=?",
            (status, utc_now(), job_id),
        )
    else:
        connection.execute(
            "UPDATE jobs SET status=?,result_path=?,error=?,finished_at=? WHERE job_id=?",
            (status, result_path, error, utc_now(), job_id),
        )
    connection.commit()


def recover_interrupted_jobs(connection: sqlite3.Connection) -> None:
    connection.execute("UPDATE jobs SET status='pending',error='interrupted before completion' WHERE status='running'")
    connection.commit()


def job_status(connection: sqlite3.Connection, job_id: str) -> Tuple[str, str]:
    row = connection.execute("SELECT status,COALESCE(result_path,'') FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    return (str(row[0]), str(row[1])) if row else ("missing", "")


def record_run_metadata(
    connection: sqlite3.Connection,
    cfg: ScanConfig,
    srim_root: Path,
    run_dir: Path,
    conditions: Sequence[GasCondition],
    execution_info: Mapping[str, Any],
) -> None:
    config_hash = hashlib.sha256(cfg.path.read_bytes()).hexdigest()
    existing = connection.execute("SELECT value FROM metadata WHERE key='config_sha256'").fetchone()
    if existing and existing[0] != config_hash:
        raise RuntimeError("Run directory belongs to a different configuration; choose another --run-dir.")
    existing_backend = connection.execute(
        "SELECT value FROM metadata WHERE key='execution_backend'"
    ).fetchone()
    if existing_backend and existing_backend[0] != execution_info["backend"]:
        raise RuntimeError(
            "Run directory was created with a different execution backend; choose another --run-dir."
        )
    values = {
        "schema_version": str(SCHEMA_VERSION),
        "config_sha256": config_hash,
        "config_path": str(cfg.path),
        "srim_root": str(srim_root),
        "execution_backend": str(execution_info["backend"]),
        "created_or_opened_at": utc_now(),
    }
    connection.executemany("INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)", values.items())
    connection.commit()
    config_copy = run_dir / "provenance" / "config.toml"
    if not config_copy.exists():
        config_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cfg.path, config_copy)
    atomic_write_json(run_dir / "provenance" / "execution.json", dict(execution_info))
    gas_rows = [condition_row(condition) for condition in conditions]
    write_csv_atomic(run_dir / "gas_conditions.csv", gas_rows)


def required_source_paths(root: Path) -> List[Path]:
    return [
        root / "Data" / "ATOMDATA",
        root / "Data" / "POTCOEF03.DAT",
        root / "Data" / "SCOEF03.DAT",
        root / "Data" / "SNUC03.dat",
        root / "SR Module" / "SRModule.exe",
        root / "SR Module" / "SCOEF03.dat",
        root / "SR Module" / "SNUC03.dat",
        root / "SR Module" / "VERSION",
        root / "TRIM.exe",
    ]


def source_fingerprint(root: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for path in required_source_paths(root):
        if not path.exists():
            raise FileNotFoundError(f"Missing required SRIM file: {path}")
        values[str(path.relative_to(root))] = sha256_file(path)
    for version in (root / "Data" / "VERSION", root / "SR Module" / "VERSION"):
        if version.exists():
            values[str(version.relative_to(root)) + ":text"] = version.read_text(
                encoding="latin-1", errors="replace"
            ).strip()
    return values


def source_control_fingerprint(root: Path) -> Dict[str, str]:
    """Hash mutable installed inputs to prove isolated jobs did not touch them."""
    values: Dict[str, str] = {}
    for path in (root / "SR Module" / "SR.IN", root / "TRIM.IN", root / "TRIMAUTO"):
        key = str(path.relative_to(root))
        values[key] = sha256_file(path) if path.exists() else "<absent>"
    return values


def prepare_runtime(source_root: Path, runtime: Path, fingerprint: Mapping[str, str]) -> None:
    marker = runtime / "runtime_manifest.json"
    if marker.exists():
        data = json.loads(marker.read_text(encoding="utf-8"))
        if data.get("source_fingerprint") != dict(fingerprint):
            raise RuntimeError(f"Existing worker runtime has a different SRIM fingerprint: {runtime}")
        return
    if runtime.exists() and any(runtime.iterdir()):
        raise RuntimeError(f"Refusing to replace non-empty worker runtime: {runtime}")
    runtime.mkdir(parents=True, exist_ok=True)
    for directory in ("Data", "SR Module", "SRIM Tools", "SRIM Restore"):
        source = source_root / directory
        if source.exists():
            shutil.copytree(source, runtime / directory, dirs_exist_ok=False)
    (runtime / "SRIM Outputs").mkdir(exist_ok=True)
    for name in ("TRIM.exe", "TIN.exe", "TRIM.dat", "TRIM 2.dat", "TRIMAUTO.TXT"):
        source = source_root / name
        if source.exists():
            shutil.copy2(source, runtime / name)
    atomic_write_json(
        marker,
        {
            "created_at": utc_now(),
            "source_root": str(source_root),
            "source_fingerprint": dict(fingerprint),
        },
    )


def prepare_worker_wine_prefix(source: Path, target: Path) -> None:
    """Clone the initialized prefix so every worker has an isolated wineserver."""
    marker = target / ".srim-worker-prefix.json"
    if marker.is_file() and (target / "system.reg").is_file():
        return
    if target.exists() and any(target.iterdir()):
        raise RuntimeError(f"Refusing to replace non-empty worker Wine prefix: {target}")
    if not (source / "system.reg").is_file():
        raise FileNotFoundError(f"Source Wine prefix is not initialized: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + f".tmp-{os.getpid()}")
    if temporary.exists():
        raise RuntimeError(f"Refusing to replace incomplete worker Wine prefix: {temporary}")
    shutil.copytree(source, temporary, symlinks=True, dirs_exist_ok=False)
    atomic_write_json(
        temporary / marker.name,
        {
            "created_at": utc_now(),
            "source_prefix": str(source),
            "source_system_reg_sha256": sha256_file(source / "system.reg"),
        },
    )
    os.replace(temporary, target)


def runtime_execution_context(
    context: ExecutionContext, run_dir: Path, runtime: Path
) -> ExecutionContext:
    if context.backend != "pysrim":
        return context
    return replace(context, wine_prefix=run_dir / "wine-prefixes" / runtime.name)


def prepare_runtimes(
    source_root: Path,
    run_dir: Path,
    workers: int,
    execution: ExecutionContext | None = None,
) -> Tuple[List[Path], Dict[str, str]]:
    if workers < 1:
        raise ValueError("--workers must be at least 1.")
    fingerprint = source_fingerprint(source_root)
    runtimes = []
    for index in range(workers):
        runtime = run_dir / "runtime" / f"worker-{index:03d}"
        prepare_runtime(source_root, runtime, fingerprint)
        if execution is not None and execution.backend == "pysrim":
            assert execution.wine_prefix is not None
            worker_context = runtime_execution_context(execution, run_dir, runtime)
            assert worker_context.wine_prefix is not None
            prepare_worker_wine_prefix(execution.wine_prefix, worker_context.wine_prefix)
        runtimes.append(runtime)
    provenance = run_dir / "provenance"
    provenance.mkdir(parents=True, exist_ok=True)
    atomic_write_json(provenance / "srim_fingerprint.json", fingerprint)
    return runtimes, fingerprint


def seed_for(job_id: str, repeat: int = 0) -> int:
    value = int(hashlib.sha256(f"{job_id}:{repeat}".encode("ascii")).hexdigest()[:8], 16)
    return value % 900_000_000 + 1


def condition_payload(condition: GasCondition) -> Dict[str, Any]:
    return asdict(condition)


def condition_from_payload(value: Mapping[str, Any]) -> GasCondition:
    return GasCondition(
        condition_id=str(value["condition_id"]),
        row_index=int(value["row_index"]),
        family=str(value["family"]),
        noble_percent=int(value["noble_percent"]),
        cf4_percent=int(value["cf4_percent"]),
        pressure_atm=float(value["pressure_atm"]),
        temperature_k=float(value["temperature_k"]),
        molecular_weight_g_mol=float(value["molecular_weight_g_mol"]),
        density_g_cm3=float(value["density_g_cm3"]),
        molecule_number_density_cm3=float(value["molecule_number_density_cm3"]),
        atom_number_density_cm3=float(value["atom_number_density_cm3"]),
        stoich={str(k): float(v) for k, v in dict(value["stoich"]).items()},
        atomic_fractions={str(k): float(v) for k, v in dict(value["atomic_fractions"]).items()},
        mass_fractions={str(k): float(v) for k, v in dict(value["mass_fractions"]).items()},
        element_masses_u={str(k): float(v) for k, v in dict(value["element_masses_u"]).items()},
        label=str(value["label"]),
    )


def ion_symbols(condition: GasCondition) -> Tuple[str, str, str]:
    return (condition.family, "C", "F")


def make_srmodule_jobs(conditions: Sequence[GasCondition], cfg: ScanConfig) -> List[JobSpec]:
    jobs: List[JobSpec] = []
    for condition in conditions:
        for ion in ion_symbols(condition):
            payload = {
                "condition": condition_payload(condition),
                "ion": ion,
                "energy_min_kev": cfg.energy_min_kev,
                "energy_max_kev": cfg.energy_max_kev,
                "energy_points": cfg.energy_points,
                "compound_correction": cfg.compound_correction,
            }
            jobs.append(JobSpec(stable_id("sr", payload), "srmodule", payload))
    return jobs


def make_trim_jobs(
    conditions: Sequence[GasCondition],
    cfg: ScanConfig,
    stage: str = "trim",
) -> List[JobSpec]:
    jobs: List[JobSpec] = []
    if stage == "trim":
        energies = logspace(cfg.energy_min_kev, cfg.energy_max_kev, cfg.trim_energy_points)
        selected = [
            condition
            for condition in conditions
            if any(abs(condition.pressure_atm - value) < 1.0e-12 for value in cfg.trim_pressures)
        ]
        repeats = 1
        ions_per_job = cfg.trim_ions
    elif stage == "convergence":
        if not cfg.raw.get("convergence", {}).get("enabled", True):
            return []
        energies = list(cfg.convergence_energies)
        selected = [
            condition
            for condition in conditions
            if condition.noble_percent in cfg.convergence_percent
            and any(abs(condition.pressure_atm - p) < 1e-12 for p in cfg.convergence_pressures)
        ]
        repeats = cfg.convergence_repeats
        ions_per_job = cfg.convergence_ions
    else:
        raise ValueError(f"Unknown TRIM stage {stage!r}")
    for condition in selected:
        for ion in ion_symbols(condition):
            for energy in energies:
                for repeat in range(repeats):
                    payload = {
                        "condition": condition_payload(condition),
                        "ion": ion,
                        "energy_kev": energy,
                        "n_ions": ions_per_job,
                        "cascade_type": cfg.trim_cascade_type,
                        "repeat": repeat,
                        "save_3d_ranges": stage == "convergence",
                    }
                    jobs.append(JobSpec(stable_id("tr" if stage == "trim" else "cv", payload), stage, payload))
    # Benchmarks show that full-cascade cost grows with histories, recoil Z and
    # sublinearly with energy. Longest-job-first prevents a high-Z tail from
    # dominating the end of a multi-worker campaign.
    atomic_number = {"C": 6, "F": 9, "Ar": 18, "Kr": 36, "Xe": 54}
    if cfg.raw.get("trim", {}).get("job_order") == "coverage-first":
        jobs.sort(key=lambda job: (
            float(job.payload["energy_kev"]),
            job.payload["condition"]["noble_percent"],
            job.payload["condition"]["pressure_atm"],
            {"C": 0, "F": 1}.get(str(job.payload["ion"]), 2),
            job.payload["condition"]["family"],
            job.job_id,
        ))
        return jobs
    jobs.sort(
        key=lambda job: (
            int(job.payload["n_ions"])
            * atomic_number[str(job.payload["ion"])]
            * math.sqrt(float(job.payload["energy_kev"])),
            job.job_id,
        ),
        reverse=True,
    )
    return jobs


def stop_process(process: subprocess.Popen[bytes], process_group: bool) -> Tuple[bytes, bytes]:
    """Stop a child and, for Wine jobs, every process in its private group."""
    try:
        if process_group and os.name != "nt":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        pass
    try:
        return process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            if process_group and os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        return process.communicate()


def launch_command(
    command: Sequence[str],
    cwd: Path,
    timeout_s: int,
    expected_output: Path | None = None,
    environment: Mapping[str, str] | None = None,
    process_group: bool = False,
    display_name: str | None = None,
) -> None:
    name = display_name or Path(command[0]).name
    process = subprocess.Popen(
        list(command),
        cwd=str(cwd),
        env=dict(environment) if environment is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=process_group and os.name != "nt",
    )
    deadline = time.monotonic() + timeout_s
    last_size = -1
    stable_since: float | None = None
    try:
        while True:
            if SHUTDOWN_REQUESTED.is_set():
                raise RuntimeError(f"{name} stopped after an interrupt; resume the stage")
            code = process.poll()
            if code is not None:
                stdout, stderr = process.communicate()
                if code != 0:
                    raise RuntimeError(
                        f"{name} failed with exit code {code}. "
                        f"stdout={stdout.decode('latin-1', errors='replace').strip()!r} "
                        f"stderr={stderr.decode('latin-1', errors='replace').strip()!r}"
                    )
                return
            now = time.monotonic()
            if expected_output is not None and expected_output.exists():
                size = expected_output.stat().st_size
                if size > 0 and size == last_size:
                    stable_since = stable_since or now
                    if now - stable_since >= 1.0:
                        stop_process(process, process_group)
                        return
                else:
                    last_size = size
                    stable_since = now
            if now >= deadline:
                stdout, stderr = stop_process(process, process_group)
                time.sleep(1.0)
                if expected_output is not None and expected_output.exists() and expected_output.stat().st_size > 0:
                    return
                raise RuntimeError(
                    f"{name} timed out after {timeout_s}s. "
                    f"stdout={stdout.decode('latin-1', errors='replace').strip()!r} "
                    f"stderr={stderr.decode('latin-1', errors='replace').strip()!r}"
                )
            time.sleep(0.2)
    except BaseException:
        if process.poll() is None:
            stop_process(process, process_group)
        raise


def launch_windows_executable(
    executable: Path,
    cwd: Path,
    timeout_s: int,
    expected_output: Path | None = None,
) -> None:
    # WSL can launch a PE executable directly through its mounted Linux path.
    # Passing ``C:\\...`` as argv[0] makes Linux look for a file with that
    # literal name, while routing through cmd.exe loses the requested cwd for
    # SRIM builds that depend on relative control/data paths.
    launch_command(
        [str(executable.resolve())],
        cwd,
        timeout_s,
        expected_output=expected_output,
        display_name=executable.name,
    )


def git_head(path: Path) -> str:
    completed = subprocess.run(
        ["git", "-c", f"safe.directory={path}", "-C", str(path), "rev-parse", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def pysrim_environment(context: ExecutionContext) -> Dict[str, str]:
    assert context.pysrim_root is not None and context.wine_prefix is not None
    environment = dict(os.environ)
    existing = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = str(context.pysrim_root) + (os.pathsep + existing if existing else "")
    environment["WINEPREFIX"] = str(context.wine_prefix)
    environment.setdefault("WINEDEBUG", "-all")
    return environment


def execution_provenance(context: ExecutionContext, require_runtime: bool = True) -> Dict[str, Any]:
    if context.backend == "native":
        return {
            "backend": "native",
            "wsl": running_under_wsl(),
            "python_executable": sys.executable,
            "python_version": sys.version.split()[0],
        }
    assert context.pysrim_root is not None and context.wine_prefix is not None
    required = (context.pysrim_root / "srim" / "__init__.py", context.pysrim_root / "setup.py")
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Invalid pysrim checkout; missing {', '.join(missing)}")
    commit = git_head(context.pysrim_root)
    if context.pysrim_expected_commit and commit != context.pysrim_expected_commit:
        raise RuntimeError(
            f"pysrim commit mismatch: found {commit or 'unknown'}, "
            f"expected {context.pysrim_expected_commit}"
        )
    dependencies = {name: shutil.which(name) for name in ("wine", "xvfb-run", "wineserver", "xwininfo")}
    needed = ["wine", "wineserver", "xwininfo"] + (["xvfb-run"] if context.headless else [])
    if require_runtime:
        absent = [name for name in needed if not dependencies[name]]
        if absent:
            raise RuntimeError(
                "pysrim execution dependencies are missing: " + ", ".join(absent) +
                ". Run scripts/bootstrap_ubuntu.sh --install-system-deps."
            )
        if not (context.wine_prefix / "system.reg").is_file():
            raise RuntimeError(
                f"Wine prefix is not initialized at {context.wine_prefix}. "
                "Run scripts/bootstrap_ubuntu.sh --install-system-deps."
            )
    runner = script_dir() / "pysrim_runner.py"
    completed = subprocess.run(
        [sys.executable, "-W", "ignore::SyntaxWarning", str(runner), "--probe"],
        cwd=str(script_dir()),
        env=pysrim_environment(context),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Unable to import the configured pysrim fork: "
            + (completed.stderr.strip() or completed.stdout.strip())
        )
    probe = json.loads(completed.stdout)
    return {
        "backend": "pysrim",
        "pysrim_root": str(context.pysrim_root),
        "pysrim_commit": commit,
        "pysrim_probe": probe,
        "wine_prefix": str(context.wine_prefix),
        "wine_prefix_strategy": "private per-worker clone with per-job wineserver teardown",
        "headless": context.headless,
        "dependencies": dependencies,
        "python_executable": sys.executable,
        "python_version": sys.version.split()[0],
        "pyyaml_compatibility_loader": True,
    }


def host_provenance(srim_root: Path, run_dir: Path) -> Dict[str, Any]:
    """Collect reproducibility and capacity facts without optional dependencies."""
    cpu_model = "unknown"
    physical_cores: set[Tuple[str, str]] = set()
    try:
        blocks = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace").split("\n\n")
        for block in blocks:
            processor = {
                key.strip(): value.strip()
                for line in block.splitlines()
                if ":" in line
                for key, value in [line.split(":", 1)]
            }
            cpu_model = processor.get("model name", cpu_model)
            if "physical id" in processor and "core id" in processor:
                physical_cores.add((processor["physical id"], processor["core id"]))
    except OSError:
        pass
    memory: Dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, value = line.split(":", 1)
            if key in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
                memory[key + "_bytes"] = int(value.split()[0]) * 1024
    except (OSError, ValueError):
        pass

    def storage(path: Path) -> Dict[str, Any]:
        existing = path
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        stats = os.statvfs(existing)
        return {
            "path": str(path),
            "filesystem_probe": str(existing),
            "bytes_total": stats.f_blocks * stats.f_frsize,
            "bytes_available": stats.f_bavail * stats.f_frsize,
        }

    wine = (
        subprocess.run(
            ["wine", "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if shutil.which("wine")
        else None
    )
    return {
        "hostname": platform.node(),
        "os": platform.platform(),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "cpu_model": cpu_model,
        "logical_cpus": os.cpu_count(),
        "physical_cores_detected": len(physical_cores) or None,
        "memory": memory,
        "storage": [storage(srim_root), storage(run_dir)],
        "wine_version": wine.stdout.strip() if wine is not None else "unavailable",
    }


def launch_pysrim_job(
    context: ExecutionContext,
    runtime: Path,
    spec: Mapping[str, Any],
    timeout_s: int,
) -> None:
    if context.backend != "pysrim":
        raise ValueError("launch_pysrim_job requires the pysrim backend")
    spec_path = runtime / ".pysrim-job.json"
    atomic_write_json(spec_path, spec)
    command = [
        sys.executable,
        "-W",
        "ignore::SyntaxWarning",
        str(script_dir() / "pysrim_runner.py"),
        "--spec",
        str(spec_path),
    ]
    if context.headless:
        atomic_write_text(runtime / ".xvfb.log", f"job={spec.get('description', '')}\n")
        command = ["xvfb-run", "-a", "-e", str(runtime / ".xvfb.log"),
                   "-s", "-screen 0 1024x768x24", *command]
    environment = pysrim_environment(context)
    try:
        launch_command(
            command,
            runtime,
            timeout_s,
            environment=environment,
            process_group=True,
            display_name=f"pysrim/{spec.get('mode', 'job')}",
        )
    except RuntimeError as exc:
        if "TRIM_BLOCKING_DIALOG" in str(exc) or "TRIM_WATCHDOG_ERROR" in str(exc):
            raise WorkerInfrastructureError(str(exc)) from exc
        raise
    finally:
        # A private prefix makes this safe even while other workers are active.
        # Stopping the per-job wineserver avoids stale GUI processes poisoning
        # the next job assigned to the same runtime after a timeout or exit.
        try:
            subprocess.run(
                ["wineserver", "-k"],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
            subprocess.run(
                ["wineserver", "-w"],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass


def pysrim_element_payload(
    condition: GasCondition,
    cfg: ScanConfig,
    elements: Mapping[str, Element],
) -> List[Dict[str, Any]]:
    return [
        {
            "symbol": symbol,
            "z": elements[symbol].z,
            "mass_u": elements[symbol].mass_u,
            "stoich": condition.stoich[symbol],
            "displacement_energy_ev": cfg.displacement_energy_ev,
            "lattice_binding_energy_ev": cfg.lattice_binding_energy_ev,
            "surface_binding_energy_ev": cfg.surface_binding_energy_ev,
        }
        for symbol in sorted(condition.stoich, key=lambda value: elements[value].z)
    ]


def archive_files_atomic(archive: Path, files: Mapping[str, Path], manifest: Mapping[str, Any]) -> None:
    archive.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive.with_name(archive.name + f".tmp-{os.getpid()}")
    with tarfile.open(temporary, "w:gz") as output:
        manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest_bytes)
        info.mtime = int(time.time())
        output.addfile(info, io.BytesIO(manifest_bytes))
        for arcname, path in sorted(files.items()):
            output.add(path, arcname=arcname, recursive=False)
    os.replace(temporary, archive)


def run_srmodule_job(
    job: JobSpec,
    runtime: Path,
    run_dir: Path,
    cfg: ScanConfig,
    elements: Mapping[str, Element],
    execution: ExecutionContext,
) -> str:
    condition = condition_from_payload(job.payload["condition"])
    ion = elements[str(job.payload["ion"])]
    gas = condition.as_gas()
    module = runtime / "SR Module"
    output_name = "SR_OUTPUT.txt" if execution.backend == "pysrim" else job.job_id.replace("-", "_")
    output_path = module / output_name
    output_path.unlink(missing_ok=True)
    sr_in = module / "SR.IN"
    if execution.backend == "pysrim":
        launch_pysrim_job(
            execution,
            runtime,
            {
                "mode": "srmodule",
                "srim_root": str(runtime),
                "description": job.job_id,
                "layer_name": condition.condition_id,
                "density_g_cm3": condition.density_g_cm3,
                "elements": pysrim_element_payload(condition, cfg, elements),
                "ion_symbol": ion.symbol,
                "ion_mass_u": ion.mass_u,
                "energy_kev": cfg.energy_max_kev,
                "energy_min_kev": cfg.energy_min_kev,
                "output_filename": output_name,
                "compound_correction": cfg.compound_correction,
            },
            cfg.srmodule_timeout_s,
        )
    else:
        write_sr_in(
            sr_in,
            output_name,
            ion,
            gas,
            dict(elements),
            cfg.energy_min_kev,
            cfg.energy_max_kev,
            cfg.compound_correction,
        )
        launch_windows_executable(module / "SRModule.exe", module, cfg.srmodule_timeout_s, output_path)
    if not output_path.exists():
        raise FileNotFoundError(f"SRModule did not create {output_path}")
    raw_rows = parse_stopping_output(output_path)
    integration_energies, integrated_electronic = cumulative_electronic_integral(raw_rows)
    result_rows: List[Dict[str, Any]] = []
    for energy in logspace(cfg.energy_min_kev, cfg.energy_max_kev, cfg.energy_points):
        row = interpolate_stopping(raw_rows, energy)
        total = row.s_elec + row.s_nuc
        electronic_fraction = row.s_elec / total if total > 0 else math.nan
        electronic_energy = interpolate_series(integration_energies, integrated_electronic, energy)
        areal_range = row.projected_range_cm * condition.density_g_cm3 * 1000.0
        linear_scale = condition.density_g_cm3 * 1.0e6
        result_rows.append(
            {
                "job_id": job.job_id,
                "condition_id": condition.condition_id,
                "family": condition.family,
                "noble_percent": condition.noble_percent,
                "cf4_percent": condition.cf4_percent,
                "pressure_atm": condition.pressure_atm,
                "temperature_k": condition.temperature_k,
                "density_g_cm3": condition.density_g_cm3,
                "ion": ion.symbol,
                "ion_z": ion.z,
                "ion_mass_u": ion.mass_u,
                "energy_kev": energy,
                "electronic_stopping_mev_per_mg_cm2": row.s_elec,
                "nuclear_stopping_mev_per_mg_cm2": row.s_nuc,
                "total_stopping_mev_per_mg_cm2": total,
                "electronic_stopping_kev_per_cm": row.s_elec * linear_scale,
                "nuclear_stopping_kev_per_cm": row.s_nuc * linear_scale,
                "total_stopping_kev_per_cm": total * linear_scale,
                "instantaneous_electronic_fraction": electronic_fraction,
                "srmodule_electronic_fraction_proxy": electronic_energy / energy,
                "projected_range_mg_cm2": areal_range,
                "projected_range_cm": row.projected_range_cm,
                "projected_range_mm": row.projected_range_cm * 10.0,
                "longitudinal_straggling_cm": row.longitudinal_straggling_cm,
                "longitudinal_straggling_mm": row.longitudinal_straggling_cm * 10.0,
                "lateral_straggling_cm": row.lateral_straggling_cm,
                "lateral_straggling_mm": row.lateral_straggling_cm * 10.0,
                "compound_correction": cfg.compound_correction,
                "qf_interpretation": "integrated primary-ion electronic stopping fraction; deterministic proxy",
            }
        )
    result_path = run_dir / "job_results" / "srmodule" / f"{job.job_id}.json"
    atomic_write_json(result_path, {"job": job.payload, "rows": result_rows})
    archive = run_dir / "raw" / "srmodule" / f"{job.job_id}.tar.gz"
    archive_files_atomic(
        archive,
        {"SR.IN": sr_in, "SRModule-output.txt": output_path},
        {
            "job_id": job.job_id,
            "stage": job.stage,
            "payload": job.payload,
            "created_at": utc_now(),
            "execution_backend": execution.backend,
            "raw_output_sha256": sha256_file(output_path),
        },
    )
    output_path.unlink(missing_ok=True)
    return str(result_path.relative_to(run_dir))


def parse_range_summary(path: Path) -> RangeSummary:
    text = path.read_text(encoding="latin-1", errors="replace")
    value_unit = r"([-+]?\d+(?:\.\d*)?(?:[Ee][-+]?\d+)?)\s*(A|Ang|um|mm|cm|m)"

    def pair(label: str) -> Tuple[float | None, float | None]:
        match = re.search(
            rf"Ion\s+{label}\s+Range\s*=\s*{value_unit}\s+Straggling\s*=\s*{value_unit}",
            text,
            flags=re.IGNORECASE,
        )
        if not match:
            return None, None
        return (
            length_value_to_mm(float(match.group(1)), match.group(2)),
            length_value_to_mm(float(match.group(3)), match.group(4)),
        )

    projected, longitudinal = pair("Average")
    lateral, lateral_straggling = pair("Lateral")
    radial, radial_straggling = pair(r"Radial\s*")

    def scalar(pattern: str) -> float | None:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        return float(match.group(1)) if match else None

    def ion_count(label: str) -> int | None:
        match = re.search(
            rf"(?:Total\s+)?{label}\s+Ions\s*=\s*([0-9.]*)",
            text,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        # SRIM-2013 prints a blank value before the semicolon when the count is
        # zero (for example, ``Transmitted Ions =;``).
        return int(round(float(match.group(1)))) if match.group(1) else 0

    return RangeSummary(
        projected_range_mm=projected,
        longitudinal_straggling_mm=longitudinal,
        lateral_range_mm=lateral,
        lateral_straggling_mm=lateral_straggling,
        radial_range_mm=radial,
        radial_straggling_mm=radial_straggling,
        skewness=scalar(r"Range\s+Skewne(?:ss|-)?\s*=\s*([-+]?\d+(?:\.\d*)?)"),
        kurtosis=scalar(r"Range\s+Kurtosis\s*=\s*([-+]?\d+(?:\.\d*)?)"),
        backscattered_ions=ion_count("Backscattered"),
        transmitted_ions=ion_count("Transmitted"),
    )


def count_escape_records(path: Path, record_type: str) -> int:
    """Count B/T data records in SRIM BACKSCAT/TRANSMIT output files."""
    marker = record_type.upper()
    if marker not in {"B", "T"}:
        raise ValueError(f"Unsupported escape-record type {record_type!r}")
    count = 0
    for line in path.read_text(encoding="latin-1", errors="replace").splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0].upper() == marker and fields[1].isdigit():
            count += 1
    return count


def longest_numeric_table(path: Path) -> List[List[float]]:
    lines = path.read_text(encoding="latin-1", errors="replace").splitlines()
    groups: List[List[List[float]]] = []
    current: List[List[float]] = []
    for line in lines:
        stripped = line.strip()
        values = NUMBER_RE.findall(stripped)
        numeric_start = bool(re.match(r"^[-+]?(?:\d|\.)", stripped))
        if numeric_start and len(values) >= 2:
            try:
                current.append([float(value) for value in values])
                continue
            except ValueError:
                pass
        if current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return max(groups, key=len) if groups else []


def profile_columns(name: str, width: int) -> List[str]:
    known = {
        "IONIZ.TXT": ["depth_angstrom", "ion_electronic_ev_per_angstrom_ion", "recoil_electronic_ev_per_angstrom_ion"],
        "E2RECOIL.TXT": ["depth_angstrom", "energy_from_ions_ev_per_angstrom_ion", "energy_absorbed_by_recoils_ev_per_angstrom_ion"],
        "PHONON.TXT": ["depth_angstrom", "phonons_by_ion_per_angstrom_ion", "phonons_by_recoils_per_angstrom_ion"],
        "LATERAL.TXT": ["depth_angstrom", "lateral_projected_range_angstrom", "lateral_straggling_angstrom", "radial_range_angstrom", "radial_straggling_angstrom"],
    }
    columns = list(known.get(name.upper(), []))
    if name.upper() == "RANGE.TXT":
        columns = ["depth_angstrom", "ion_distribution"]
    while len(columns) < width:
        columns.append(f"recoil_or_target_column_{len(columns) + 1}")
    return columns[:width]


def normalized_profiles(outputs: Mapping[str, Path]) -> Dict[str, Any]:
    profiles: Dict[str, Any] = {}
    for name in ("IONIZ.txt", "E2RECOIL.txt", "PHONON.txt", "RANGE.txt", "LATERAL.txt"):
        path = outputs.get(name.upper())
        if not path:
            continue
        rows = longest_numeric_table(path)
        width = max((len(row) for row in rows), default=0)
        profiles[name.upper()] = {
            "columns": profile_columns(name, width),
            "rows": rows,
            "source": name,
        }
    return profiles


def clear_runtime_outputs(runtime: Path) -> None:
    names = {name.lower() for name in ALL_TRIM_OUTPUTS}
    for directory in (runtime, runtime / "SRIM Outputs"):
        if not directory.exists():
            continue
        for path in directory.iterdir():
            if path.is_file() and path.name.lower() in names:
                deadline = time.monotonic() + 60.0
                delay = 0.1
                while True:
                    try:
                        path.unlink(missing_ok=True)
                        break
                    except PermissionError as exc:
                        if time.monotonic() >= deadline:
                            raise WorkerInfrastructureError(
                                f"worker output remained locked for 60 s: {path}"
                            ) from exc
                        time.sleep(delay)
                        delay = min(delay * 1.5, 2.0)


def collect_runtime_outputs(runtime: Path) -> Dict[str, Path]:
    found: Dict[str, Path] = {}
    for directory in (runtime, runtime / "SRIM Outputs"):
        if not directory.exists():
            continue
        for path in directory.iterdir():
            key = path.name.upper()
            if path.is_file() and key in {name.upper() for name in ALL_TRIM_OUTPUTS}:
                found[key] = path
    return found


def target_width_angstrom(
    condition: GasCondition,
    prediction: Mapping[str, Any],
    cfg: ScanConfig,
) -> float:
    projected_cm = float(prediction["projected_range_cm"])
    straggling_cm = float(prediction["longitudinal_straggling_cm"])
    physics_width_cm = cfg.target_width_factor * (
        projected_cm + cfg.target_sigma_margin * straggling_cm
    )
    monolayer_cm = condition.atom_number_density_cm3 ** (-1.0 / 3.0)
    minimum_cm = cfg.minimum_monolayers * monolayer_cm
    return max(physics_width_cm, minimum_cm, 1.0e-7) * 1.0e8


def find_output(outputs: Mapping[str, Path], name: str) -> Path:
    key = name.upper()
    if key not in outputs:
        raise FileNotFoundError(f"TRIM did not generate required output {name}")
    return outputs[key]


def trim_result_is_clipped(
    ioniz_max_depth_angstrom: float,
    ranges: RangeSummary,
    width_angstrom: float,
    edge_fraction: float,
) -> bool:
    if ioniz_max_depth_angstrom >= edge_fraction * width_angstrom:
        return True
    if ranges.transmitted_ions not in (None, 0):
        return True
    if ranges.projected_range_mm is not None and ranges.longitudinal_straggling_mm is not None:
        upper_mm = ranges.projected_range_mm + 6.0 * ranges.longitudinal_straggling_mm
        if upper_mm >= edge_fraction * angstrom_to_mm(width_angstrom):
            return True
    return False


def load_srmodule_predictions(run_dir: Path) -> Dict[Tuple[str, str], List[Dict[str, str]]]:
    path = run_dir / "srmodule_results.csv"
    if not path.exists():
        raise FileNotFoundError(
            f"Missing {path}. Complete `run --stage srmodule` before running TRIM."
        )
    grouped: Dict[Tuple[str, str], List[Dict[str, str]]] = {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            grouped.setdefault((row["condition_id"], row["ion"]), []).append(row)
    for rows in grouped.values():
        rows.sort(key=lambda row: float(row["energy_kev"]))
    return grouped


def closest_prediction(
    predictions: Mapping[Tuple[str, str], Sequence[Mapping[str, str]]],
    condition_id: str,
    ion: str,
    energy_kev: float,
) -> Mapping[str, str]:
    rows = predictions.get((condition_id, ion))
    if not rows:
        raise KeyError(f"No SRModule prediction for condition={condition_id}, ion={ion}")
    return min(rows, key=lambda row: abs(math.log(float(row["energy_kev"]) / energy_kev)))


def write_gzip_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    with gzip.open(temporary, "wt", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
    os.replace(temporary, path)


def run_trim_job(
    job: JobSpec,
    runtime: Path,
    run_dir: Path,
    cfg: ScanConfig,
    elements: Mapping[str, Element],
    predictions: Mapping[Tuple[str, str], Sequence[Mapping[str, str]]],
    execution: ExecutionContext,
    hard_deadline: float | None = None,
) -> str:
    condition = condition_from_payload(job.payload["condition"])
    ion = elements[str(job.payload["ion"])]
    energy = float(job.payload["energy_kev"])
    n_ions = int(job.payload["n_ions"])
    repeat = int(job.payload["repeat"])
    prediction = closest_prediction(predictions, condition.condition_id, ion.symbol, energy)
    width = target_width_angstrom(condition, prediction, cfg)
    attempted_widths: List[float] = []
    outputs: Dict[str, Path] = {}
    summary = None
    ranges = None
    clipped = True
    trim_in = runtime / "TRIM.IN"
    trim_auto = runtime / "TRIMAUTO"
    job_seed = seed_for(job.job_id, repeat)
    for width_attempt in range(cfg.max_attempts):
        attempted_widths.append(width)
        clear_runtime_outputs(runtime)
        timeout_s = (
            cfg.convergence_timeout_s if job.stage == "convergence" else cfg.trim_timeout_s
        )
        deadline_limited = False
        if hard_deadline is not None:
            # Reserve one minute for output collection, atomic archives, and
            # checkpoint commits after the Windows process stops.
            remaining = int(hard_deadline - time.monotonic() - 60.0)
            if remaining <= 0:
                raise SessionBudgetExhausted("hard session deadline reached before TRIM launch")
            if remaining < timeout_s:
                timeout_s = remaining
                deadline_limited = True
        try:
            if execution.backend == "pysrim":
                if abs(cfg.compound_correction - 1.0) > 1.0e-12:
                    raise ValueError("fiorotto8/pysrim supports only the configured TRIM compound correction 1.0")
                launch_pysrim_job(
                    execution,
                    runtime,
                    {
                        "mode": "trim",
                        "srim_root": str(runtime),
                        "description": job.job_id,
                        "layer_name": condition.condition_id,
                        "density_g_cm3": condition.density_g_cm3,
                        "elements": pysrim_element_payload(condition, cfg, elements),
                        "ion_symbol": ion.symbol,
                        "ion_mass_u": ion.mass_u,
                        "energy_kev": energy,
                        "n_ions": n_ions,
                        "cascade_type": int(job.payload["cascade_type"]),
                        "target_width_angstrom": width,
                        "compound_correction": cfg.compound_correction,
                        "angle_deg": cfg.angle_deg,
                        "random_seed": job_seed,
                        "save_3d_ranges": bool(job.payload["save_3d_ranges"]),
                    },
                    timeout_s,
                )
            else:
                write_trim_in(
                    trim_in,
                    condition.as_gas(),
                    ion,
                    dict(elements),
                    energy,
                    n_ions,
                    int(job.payload["cascade_type"]),
                    width,
                    condition.density_g_cm3,
                    cfg.compound_correction,
                    angle_deg=cfg.angle_deg,
                    random_seed=job_seed,
                    save_3d_ranges=bool(job.payload["save_3d_ranges"]),
                    save_backscatter=True,
                    save_transmit=True,
                    displacement_energy_ev=cfg.displacement_energy_ev,
                    lattice_binding_energy_ev=cfg.lattice_binding_energy_ev,
                    surface_binding_energy_ev=cfg.surface_binding_energy_ev,
                )
                write_text_crlf(trim_auto, ["1", ""])
                launch_windows_executable(runtime / "TRIM.exe", runtime, timeout_s)
        except RuntimeError as exc:
            if deadline_limited and time.monotonic() >= hard_deadline - 65.0:
                raise SessionBudgetExhausted(
                    "TRIM was stopped at the hard session deadline; the job remains pending"
                ) from exc
            raise
        outputs = collect_runtime_outputs(runtime)
        missing = [name for name in STANDARD_TRIM_OUTPUTS if name.upper() not in outputs]
        if missing:
            raise FileNotFoundError(f"TRIM output set is incomplete; missing {', '.join(missing)}")
        summary = parse_ioniz(find_output(outputs, "IONIZ.txt"), energy)
        ranges = parse_range_summary(find_output(outputs, "RANGE.txt"))
        if "BACKSCAT.TXT" in outputs:
            ranges = replace(
                ranges,
                backscattered_ions=count_escape_records(outputs["BACKSCAT.TXT"], "B"),
            )
        if "TRANSMIT.TXT" in outputs:
            ranges = replace(
                ranges,
                transmitted_ions=count_escape_records(outputs["TRANSMIT.TXT"], "T"),
            )
        clipped = trim_result_is_clipped(
            summary.estimated_max_depth_angstrom, ranges, width, cfg.edge_fraction
        )
        if not clipped:
            break
        width *= 2.0
    assert summary is not None and ranges is not None
    warnings: List[str] = []
    tolerance = max(1.0e-6, energy * 1.0e-6)
    if summary.total_ionization_energy_kev > energy + tolerance:
        warnings.append("integrated IONIZ energy exceeds initial energy")
    if not (0.0 <= summary.qf_ioniz <= 1.0 + 1.0e-6):
        warnings.append("IONIZ quenching fraction lies outside [0,1]")
    if clipped:
        warnings.append("target/viewing window remains clipped after automatic enlargement")
    if job.stage == "convergence" and "RANGE_3D.TXT" not in outputs:
        warnings.append("RANGE_3D endpoint file was requested but not generated by this SRIM build")
    output_hashes = {name: sha256_file(path) for name, path in sorted(outputs.items())}
    manifest = {
        "job_id": job.job_id,
        "stage": job.stage,
        "payload": job.payload,
        "created_at": utc_now(),
        "execution_backend": execution.backend,
        "seed": job_seed,
        "attempted_target_widths_angstrom": attempted_widths,
        "trim_cfg_sha256": sha256_file(runtime / "Data" / "TRIM.cfg")
        if (runtime / "Data" / "TRIM.cfg").is_file() else None,
        "final_target_width_angstrom": attempted_widths[-1],
        "source_srmodule_job_id": prediction["job_id"],
        "output_sha256": output_hashes,
        "scientific_limits": {
            "pressure": "SRIM gas stopping assumes STP; pressure changes density/range scaling only",
            "damage": "vacancy, displacement, and phonon terminology is not a gas material-damage observable",
            "qf": "SRIM electronic-energy fraction, not detector charge or light calibration",
        },
    }
    archive = run_dir / "raw" / job.stage / f"{job.job_id}.tar.gz"
    archive_files_atomic(
        archive,
        {"TRIM.IN": trim_in, "TRIMAUTO": trim_auto,
         **{name: path for name, path in outputs.items()},
         **{name: runtime / name for name in ("Data/TRIM.cfg", ".trim-progress.json", ".trim-process.log", ".xvfb.log")
            if (runtime / name).is_file()}},
        manifest,
    )
    profiles_path = run_dir / "profiles" / job.stage / f"{job.job_id}.json.gz"
    write_gzip_json_atomic(
        profiles_path,
        {
            "job_id": job.job_id,
            "condition_id": condition.condition_id,
            "ion": ion.symbol,
            "energy_kev": energy,
            "profiles": normalized_profiles(outputs),
        },
    )
    range_dict = asdict(ranges)
    result: Dict[str, Any] = {
        "job_id": job.job_id,
        "stage": job.stage,
        "condition_id": condition.condition_id,
        "family": condition.family,
        "noble_percent": condition.noble_percent,
        "cf4_percent": condition.cf4_percent,
        "pressure_atm": condition.pressure_atm,
        "temperature_k": condition.temperature_k,
        "density_g_cm3": condition.density_g_cm3,
        "ion": ion.symbol,
        "ion_z": ion.z,
        "ion_mass_u": ion.mass_u,
        "energy_kev": energy,
        "n_ions": n_ions,
        "repeat": repeat,
        "cascade_type": int(job.payload["cascade_type"]),
        "random_seed": job_seed,
        "target_width_angstrom": attempted_widths[-1],
        "target_width_mm": angstrom_to_mm(attempted_widths[-1]),
        "target_width_attempts": len(attempted_widths),
        "profile_max_depth_mm": angstrom_to_mm(summary.estimated_max_depth_angstrom),
        "qf_trim_ionization": summary.qf_ioniz,
        "total_ionization_energy_kev": summary.total_ionization_energy_kev,
        "primary_ionization_energy_kev": summary.primary_ionization_energy_kev,
        "secondary_recoil_ionization_energy_kev": summary.recoil_ionization_energy_kev,
        "ioniz_bin_width_angstrom": summary.bin_width_angstrom,
        "ioniz_units": summary.units_detected,
        "ioniz_integration": summary.integration_method,
        "srmodule_electronic_fraction_proxy": float(prediction["srmodule_electronic_fraction_proxy"]),
        "srmodule_projected_range_mm": float(prediction["projected_range_mm"]),
        "trim_to_srmodule_projected_range_ratio": (
            ranges.projected_range_mm / float(prediction["projected_range_mm"])
            if ranges.projected_range_mm is not None and float(prediction["projected_range_mm"]) > 0
            else None
        ),
        "clipped": clipped,
        "raw_archive": str(archive.relative_to(run_dir)),
        "normalized_profiles": str(profiles_path.relative_to(run_dir)),
        "standard_outputs_present": canonical_json(
            {name: name.upper() in outputs for name in STANDARD_TRIM_OUTPUTS}
        ),
        "range_3d_present": "RANGE_3D.TXT" in outputs,
        "warnings": "; ".join(warnings),
        "qf_interpretation": "integrated TRIM IONIZ energy from primary ion and secondary recoils divided by initial energy",
        "damage_output_interpretation": "retained as SRIM internal output; not a gas material-damage observable",
    }
    result.update(range_dict)
    result_path = run_dir / "job_results" / job.stage / f"{job.job_id}.json"
    atomic_write_json(result_path, result)
    return str(result_path.relative_to(run_dir))


def aggregate_stage(connection: sqlite3.Connection, run_dir: Path, stage: str) -> Path:
    records = connection.execute(
        "SELECT job_id,result_path FROM jobs WHERE stage=? AND status='complete' ORDER BY job_id",
        (stage,),
    ).fetchall()
    rows: List[Dict[str, Any]] = []
    for _, relative in records:
        path = run_dir / str(relative)
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if stage == "srmodule":
            rows.extend(data["rows"])
        else:
            rows.append(data)
    family_order = {"Xe": 0, "Kr": 1, "Ar": 2}
    rows.sort(
        key=lambda row: (
            family_order.get(str(row.get("family", "")), 99),
            int(row.get("noble_percent", 0)),
            float(row.get("pressure_atm", 0)),
            str(row.get("ion", "")),
            float(row.get("energy_kev", 0)),
            int(row.get("repeat", 0)),
        )
    )
    filename = {
        "srmodule": "srmodule_results.csv",
        "trim": "trim_results.csv",
        "convergence": "convergence_results.csv",
    }[stage]
    path = run_dir / filename
    write_csv_atomic(path, rows)
    return path


def interpolate_anchor_value(
    lower: Mapping[str, Any],
    upper: Mapping[str, Any],
    energy_kev: float,
    field: str,
    logarithmic_value: bool = False,
) -> float | None:
    try:
        low_value = float(lower[field])
        high_value = float(upper[field])
    except (KeyError, TypeError, ValueError):
        return None
    low_energy = float(lower["energy_kev"])
    high_energy = float(upper["energy_kev"])
    if high_energy == low_energy:
        return low_value
    fraction = math.log(energy_kev / low_energy) / math.log(high_energy / low_energy)
    if logarithmic_value and low_value > 0 and high_value > 0:
        return math.exp(math.log(low_value) * (1.0 - fraction) + math.log(high_value) * fraction)
    return low_value * (1.0 - fraction) + high_value * fraction


def derive_trim_grid(
    run_dir: Path,
    conditions: Sequence[GasCondition],
    cfg: ScanConfig,
) -> Path | None:
    """Expand pressure-scaled, energy-interpolated rows from direct TRIM anchors."""
    direct_path = run_dir / "trim_results.csv"
    direct = read_csv_rows(direct_path)
    anchor_path = run_dir / "trim_anchor_results.csv"
    write_csv_atomic(anchor_path, direct)
    expected_direct = (
        len(cfg.families)
        * len(cfg.noble_percent)
        * len(cfg.trim_pressures)
        * 3
        * cfg.trim_energy_points
    )
    if len(direct) != expected_direct:
        print(
            f"TRIM derivation deferred: {len(direct)}/{expected_direct} direct anchor jobs complete.",
            flush=True,
        )
        return None
    sr_predictions = load_srmodule_predictions(run_dir)
    groups: Dict[Tuple[str, int, str], List[Dict[str, str]]] = {}
    for row in direct:
        groups.setdefault((row["family"], int(row["noble_percent"]), row["ion"]), []).append(row)
    for rows in groups.values():
        rows.sort(key=lambda row: float(row["energy_kev"]))
    target_energies = logspace(cfg.energy_min_kev, cfg.energy_max_kev, cfg.energy_points)
    range_fields = (
        "projected_range_mm",
        "longitudinal_straggling_mm",
        "lateral_range_mm",
        "lateral_straggling_mm",
        "radial_range_mm",
        "radial_straggling_mm",
        "profile_max_depth_mm",
        "target_width_mm",
    )
    linear_fields = ("qf_trim_ionization", "skewness", "kurtosis")
    derived: List[Dict[str, Any]] = []
    for condition in conditions:
        for ion in ion_symbols(condition):
            anchors = groups[(condition.family, condition.noble_percent, ion)]
            reference_density = float(anchors[0]["density_g_cm3"])
            depth_scale = reference_density / condition.density_g_cm3
            for energy in target_energies:
                exact = next(
                    (
                        row
                        for row in anchors
                        if abs(math.log(float(row["energy_kev"]) / energy)) < 1.0e-12
                    ),
                    None,
                )
                if exact is not None:
                    lower = upper = exact
                else:
                    upper_index = next(
                        index
                        for index, row in enumerate(anchors)
                        if float(row["energy_kev"]) > energy
                    )
                    lower = anchors[upper_index - 1]
                    upper = anchors[upper_index]
                row: Dict[str, Any] = dict(lower)
                for field in range_fields:
                    value = interpolate_anchor_value(lower, upper, energy, field, True)
                    row[field] = value * depth_scale if value is not None else None
                for field in linear_fields:
                    row[field] = interpolate_anchor_value(lower, upper, energy, field)
                primary_fraction = interpolate_anchor_value(
                    {**lower, "fraction": float(lower["primary_ionization_energy_kev"]) / float(lower["energy_kev"])},
                    {**upper, "fraction": float(upper["primary_ionization_energy_kev"]) / float(upper["energy_kev"])},
                    energy,
                    "fraction",
                )
                secondary_fraction = interpolate_anchor_value(
                    {**lower, "fraction": float(lower["secondary_recoil_ionization_energy_kev"]) / float(lower["energy_kev"])},
                    {**upper, "fraction": float(upper["secondary_recoil_ionization_energy_kev"]) / float(upper["energy_kev"])},
                    energy,
                    "fraction",
                )
                source_jobs = [lower["job_id"]] if lower is upper else [lower["job_id"], upper["job_id"]]
                source_profiles = (
                    [lower["normalized_profiles"]]
                    if lower is upper
                    else [lower["normalized_profiles"], upper["normalized_profiles"]]
                )
                pressure_scaled = abs(condition.pressure_atm - float(lower["pressure_atm"])) > 1.0e-12
                energy_interpolated = lower is not upper
                row.update(
                    {
                        "job_id": lower["job_id"] if not pressure_scaled and not energy_interpolated else "",
                        "condition_id": condition.condition_id,
                        "family": condition.family,
                        "noble_percent": condition.noble_percent,
                        "cf4_percent": condition.cf4_percent,
                        "pressure_atm": condition.pressure_atm,
                        "temperature_k": condition.temperature_k,
                        "density_g_cm3": condition.density_g_cm3,
                        "ion": ion,
                        "energy_kev": energy,
                        "primary_ionization_energy_kev": (primary_fraction or 0.0) * energy,
                        "secondary_recoil_ionization_energy_kev": (secondary_fraction or 0.0) * energy,
                        "total_ionization_energy_kev": float(row["qf_trim_ionization"]) * energy,
                        "target_width_angstrom": (
                            float(row["target_width_mm"]) * 1.0e7
                            if row.get("target_width_mm") not in (None, "")
                            else None
                        ),
                        "projected_range_mg_cm2": (
                            float(row["projected_range_mm"]) / 10.0 * condition.density_g_cm3 * 1000.0
                        ),
                        "srmodule_electronic_fraction_proxy": float(
                            closest_prediction(sr_predictions, condition.condition_id, ion, energy)[
                                "srmodule_electronic_fraction_proxy"
                            ]
                        ),
                        "srmodule_projected_range_mm": float(
                            closest_prediction(sr_predictions, condition.condition_id, ion, energy)[
                                "projected_range_mm"
                            ]
                        ),
                        "backscattered_ions": lower["backscattered_ions"] if lower is upper else "",
                        "transmitted_ions": lower["transmitted_ions"] if lower is upper else "",
                        "raw_archive": lower["raw_archive"] if not energy_interpolated else "",
                        "normalized_profiles": lower["normalized_profiles"] if not energy_interpolated else "",
                        "source_job_ids": canonical_json(source_jobs),
                        "source_profiles": canonical_json(source_profiles),
                        "profile_depth_scale": depth_scale,
                        "direct_trim": not pressure_scaled and not energy_interpolated,
                        "derivation_kind": "+".join(
                            part
                            for part, enabled in (
                                ("pressure_scaled", pressure_scaled),
                                ("energy_interpolated", energy_interpolated),
                            )
                            if enabled
                        )
                        or "direct",
                        "pressure_model": "SRIM STP stopping; spatial coordinates scaled by reference_density/target_density",
                        "energy_interpolation": "linear in log(E) for QF/moments; log-log for positive ranges",
                        "qf_interpretation": (
                            lower.get("qf_interpretation", "direct integrated TRIM IONIZ fraction")
                            if not energy_interpolated
                            else "interpolated in log energy between direct integrated TRIM IONIZ anchors"
                        ),
                        "warnings": "; ".join(
                            value
                            for value in (
                                lower.get("warnings", ""),
                                "derived row; inspect derivation_kind and source_job_ids"
                                if pressure_scaled or energy_interpolated
                                else "",
                            )
                            if value
                        ),
                    }
                )
                sr_range = float(row["srmodule_projected_range_mm"])
                row["trim_to_srmodule_projected_range_ratio"] = (
                    float(row["projected_range_mm"]) / sr_range if sr_range > 0 else None
                )
                derived.append(row)
    write_csv_atomic(direct_path, derived)
    print(
        f"Derived {len(derived)} pressure/energy rows from {len(direct)} direct TRIM anchors; "
        f"direct anchors remain in {anchor_path}.",
        flush=True,
    )
    return direct_path


def execute_stage(
    jobs: Sequence[JobSpec],
    stage: str,
    connection: sqlite3.Connection,
    run_dir: Path,
    runtimes: Sequence[Path],
    worker_function: Any,
    resume: bool,
    max_attempts: int,
    limit: int | None,
    stop_launch_at: float | None = None,
) -> bool:
    SHUTDOWN_REQUESTED.clear()
    register_jobs(connection, jobs)
    recover_interrupted_jobs(connection)
    candidates: List[JobSpec] = []
    completed = 0
    for job in jobs:
        status, relative = job_status(connection, job.job_id)
        result_exists = bool(relative) and (run_dir / relative).exists()
        if status == "complete" and result_exists:
            completed += 1
            continue
        if status == "complete" and not result_exists:
            set_job_state(connection, job.job_id, "pending", error="recorded result is missing")
        candidates.append(job)
    if completed and not resume:
        raise RuntimeError(
            f"Stage {stage} already has {completed} completed jobs. Use --resume or a new --run-dir."
        )
    if limit is not None:
        candidates = candidates[:limit]
    if not candidates:
        print(f"Stage {stage}: all {len(jobs)} jobs are already complete.")
        aggregate_stage(connection, run_dir, stage)
        return False
    print(
        f"Stage {stage}: {len(candidates)} jobs to run, {completed} already complete, "
        f"{len(runtimes)} worker(s).",
        flush=True,
    )
    executors = [concurrent.futures.ThreadPoolExecutor(max_workers=1) for _ in runtimes]
    iterator = iter(candidates)
    inflight: Dict[concurrent.futures.Future[str], Tuple[int, JobSpec]] = {}
    done_this_call = 0
    budget_exhausted = False
    infrastructure_error: Exception | None = None
    interrupted = False

    def may_submit() -> bool:
        nonlocal budget_exhausted
        if infrastructure_error is not None:
            return False
        if stop_launch_at is not None and time.monotonic() >= stop_launch_at:
            budget_exhausted = True
            return False
        return True

    def submit(index: int, job: JobSpec) -> None:
        set_job_state(connection, job.job_id, "running")
        future = executors[index].submit(worker_function, job, runtimes[index])
        inflight[future] = (index, job)

    try:
        for index in range(len(runtimes)):
            if not may_submit():
                break
            try:
                submit(index, next(iterator))
            except StopIteration:
                break
        while inflight:
            ready, _ = concurrent.futures.wait(
                inflight, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in ready:
                index, job = inflight.pop(future)
                try:
                    relative = future.result()
                    set_job_state(connection, job.job_id, "complete", result_path=relative)
                    done_this_call += 1
                    if done_this_call <= 5 or done_this_call % 50 == 0:
                        print(
                            f"{stage}: completed {done_this_call}/{len(candidates)} ({job.job_id})",
                            flush=True,
                        )
                    if may_submit():
                        try:
                            next_job = next(iterator)
                        except StopIteration:
                            continue
                        submit(index, next_job)
                except Exception as exc:
                    if isinstance(exc, SessionBudgetExhausted):
                        set_job_state(connection, job.job_id, "pending", error=str(exc))
                        budget_exhausted = True
                        print(
                            f"{stage}: {job.job_id} returned to pending at the session deadline.",
                            file=sys.stderr,
                            flush=True,
                        )
                        continue
                    if isinstance(exc, WorkerInfrastructureError):
                        set_job_state(connection, job.job_id, "pending", error=str(exc))
                        infrastructure_error = exc
                        print(
                            f"{stage}: stopping before more jobs use unhealthy worker {index}: {exc}",
                            file=sys.stderr,
                            flush=True,
                        )
                        continue
                    attempt_row = connection.execute(
                        "SELECT attempts FROM jobs WHERE job_id=?", (job.job_id,)
                    ).fetchone()
                    attempts = int(attempt_row[0]) if attempt_row else max_attempts
                    if attempts < max_attempts and may_submit():
                        set_job_state(connection, job.job_id, "pending", error=str(exc))
                        print(
                            f"{stage}: retrying {job.job_id} after attempt {attempts}: {exc}",
                            file=sys.stderr,
                            flush=True,
                        )
                        submit(index, job)
                    else:
                        if budget_exhausted:
                            set_job_state(connection, job.job_id, "pending", error=str(exc))
                        else:
                            set_job_state(connection, job.job_id, "failed", error=str(exc))
                            print(
                                f"{stage}: FAILED {job.job_id} after {attempts} attempts: {exc}",
                                file=sys.stderr,
                                flush=True,
                            )
                            if infrastructure_error is None and may_submit():
                                try:
                                    next_job = next(iterator)
                                except StopIteration:
                                    continue
                                submit(index, next_job)
    except KeyboardInterrupt:
        interrupted = True
        SHUTDOWN_REQUESTED.set()
        raise
    finally:
        for executor in executors:
            executor.shutdown(wait=True, cancel_futures=False)
        if interrupted:
            recover_interrupted_jobs(connection)
        SHUTDOWN_REQUESTED.clear()
    output = aggregate_stage(connection, run_dir, stage)
    failures = connection.execute(
        "SELECT COUNT(*) FROM jobs WHERE stage=? AND status='failed'", (stage,)
    ).fetchone()[0]
    print(f"Stage {stage}: wrote completed results to {output}; failures={failures}.")
    if budget_exhausted:
        print(f"Stage {stage}: session launch deadline reached; resume in the next session.")
    if infrastructure_error is not None:
        raise RuntimeError(f"Stage {stage} stopped on a worker infrastructure error: {infrastructure_error}")
    if failures:
        raise RuntimeError(f"Stage {stage} has {failures} failed jobs; inspect status and rerun with --resume.")
    return budget_exhausted


def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def relative_spread(values: Sequence[float]) -> float:
    if not values:
        return math.inf
    mean = statistics.fmean(values)
    return (max(values) - min(values)) / max(abs(mean), 1.0e-300)


def validation_entry(severity: str, check: str, message: str, count: int = 0) -> Dict[str, Any]:
    return {"severity": severity, "check": check, "count": count, "message": message}


def validate_raw_archive(
    archive_path: Path,
    job_id: str,
    stage: str,
    required_members: Sequence[str],
) -> str | None:
    """Return an error string if a raw bundle is corrupt or internally inconsistent."""
    if not archive_path.exists():
        return "archive is missing"
    seen: Dict[str, str] = {}
    digests: Dict[str, str] = {}
    manifest: Dict[str, Any] | None = None
    try:
        with tarfile.open(archive_path, "r:gz") as bundle:
            for member in bundle:
                if not member.isfile():
                    continue
                extracted = bundle.extractfile(member)
                if extracted is None:
                    return f"could not read member {member.name}"
                digest = hashlib.sha256()
                payload = bytearray() if member.name == "manifest.json" else None
                for chunk in iter(lambda: extracted.read(1024 * 1024), b""):
                    digest.update(chunk)
                    if payload is not None:
                        payload.extend(chunk)
                key = Path(member.name).name.upper()
                seen[key] = member.name
                digests[key] = digest.hexdigest()
                if payload is not None:
                    manifest = json.loads(payload.decode("utf-8"))
    except (OSError, tarfile.TarError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return f"cannot read archive: {exc}"
    missing = [name for name in required_members if name.upper() not in seen]
    if missing:
        return f"missing members: {', '.join(missing)}"
    if manifest is None:
        return "manifest.json is missing"
    if manifest.get("job_id") != job_id or manifest.get("stage") != stage:
        return "manifest job_id/stage does not match checkpoint metadata"
    if stage == "srmodule":
        expected = manifest.get("raw_output_sha256")
        if not isinstance(expected, str) or digests.get("SRMODULE-OUTPUT.TXT") != expected:
            return "SRModule output hash does not match its manifest"
    else:
        expected_hashes = manifest.get("output_sha256")
        if not isinstance(expected_hashes, dict):
            return "output hash map is missing from manifest"
        for name in required_members:
            if name.upper() in {"TRIM.IN", "TRIMAUTO"}:
                continue
            expected = expected_hashes.get(name.upper())
            if not isinstance(expected, str) or digests.get(name.upper()) != expected:
                return f"member hash does not match manifest: {name}"
    return None


def validate_profile_artifact(path: Path, job_id: str) -> str | None:
    if not path.exists():
        return "profile is missing"
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return f"cannot read profile: {exc}"
    if data.get("job_id") != job_id or not isinstance(data.get("profiles"), dict):
        return "profile metadata does not match the job"
    return None


def validate_run(run_dir: Path, cfg: ScanConfig) -> Tuple[List[Dict[str, Any]], bool]:
    entries: List[Dict[str, Any]] = []
    database_path = run_dir / "jobs.sqlite"
    if not database_path.exists():
        entries.append(validation_entry("failure", "database", "jobs.sqlite is missing"))
        return entries, False
    connection = sqlite3.connect(database_path)
    metadata = dict(connection.execute("SELECT key,value FROM metadata").fetchall())
    expected = {
        "srmodule": len(cfg.families) * len(cfg.noble_percent) * len(cfg.pressures_atm) * 3,
        "trim": len(cfg.families)
        * len(cfg.noble_percent)
        * len(cfg.trim_pressures)
        * 3
        * cfg.trim_energy_points,
        "convergence": len(cfg.families)
        * len(cfg.convergence_percent)
        * len(cfg.convergence_pressures)
        * 3
        * len(cfg.convergence_energies)
        * cfg.convergence_repeats,
    }
    if not cfg.raw.get("convergence", {}).get("enabled", True):
        expected["convergence"] = 0
    for stage, expected_jobs in expected.items():
        counts = dict(
            connection.execute(
                "SELECT status,COUNT(*) FROM jobs WHERE stage=? GROUP BY status", (stage,)
            ).fetchall()
        )
        complete = int(counts.get("complete", 0))
        if complete == expected_jobs:
            entries.append(
                validation_entry("pass", f"{stage}_completeness", f"{complete}/{expected_jobs} jobs complete")
            )
        else:
            entries.append(
                validation_entry(
                    "failure",
                    f"{stage}_completeness",
                    f"expected {expected_jobs} completed jobs; found {complete}; states={counts}",
                    expected_jobs - complete,
                )
            )
    connection.close()
    config_hash = hashlib.sha256(cfg.path.read_bytes()).hexdigest()
    fingerprint_path = run_dir / "provenance" / "srim_fingerprint.json"
    provenance_error = ""
    try:
        fingerprint = json.loads(fingerprint_path.read_text(encoding="utf-8"))
        required_fingerprints = {
            str(path.relative_to(Path(metadata.get("srim_root", "."))))
            for path in required_source_paths(Path(metadata.get("srim_root", ".")))
        }
        missing_fingerprints = sorted(required_fingerprints - set(fingerprint))
        bad_hashes = [
            key
            for key in required_fingerprints & set(fingerprint)
            if not re.fullmatch(r"[0-9a-f]{64}", str(fingerprint[key]))
        ]
        if metadata.get("config_sha256") != config_hash:
            provenance_error = "frozen configuration hash does not match checkpoint metadata"
        elif missing_fingerprints or bad_hashes:
            provenance_error = (
                f"missing fingerprints={missing_fingerprints}; invalid fingerprints={bad_hashes}"
            )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        provenance_error = f"cannot read provenance: {exc}"
    entries.append(
        validation_entry(
            "failure" if provenance_error else "pass",
            "run_provenance",
            provenance_error or "frozen configuration plus executable/data hashes are present",
            1 if provenance_error else 0,
        )
    )
    gas_rows = read_csv_rows(run_dir / "gas_conditions.csv")
    expected_conditions = len(cfg.families) * len(cfg.noble_percent) * len(cfg.pressures_atm)
    if len(gas_rows) == expected_conditions and len({row["condition_id"] for row in gas_rows}) == expected_conditions:
        entries.append(validation_entry("pass", "gas_grid", f"{expected_conditions} unique gas conditions"))
    else:
        entries.append(validation_entry("failure", "gas_grid", f"found {len(gas_rows)} gas-condition rows"))
    density_groups: Dict[Tuple[str, str], List[Dict[str, str]]] = {}
    for row in gas_rows:
        density_groups.setdefault((row["family"], row["noble_percent"]), []).append(row)
    density_failures = 0
    for rows in density_groups.values():
        for field in (
            "density_g_cm3",
            "molecule_number_density_cm3",
            "atom_number_density_cm3",
        ):
            per_atm = [float(row[field]) / float(row["pressure_atm"]) for row in rows]
            if relative_spread(per_atm) > 1.0e-10:
                density_failures += 1
    entries.append(
        validation_entry(
            "failure" if density_failures else "pass",
            "ideal_gas_pressure_scaling",
            f"{density_failures} density/number-density groups failed" if density_failures else "density and molecular/atomic number densities scale linearly with pressure",
            density_failures,
        )
    )

    sr_rows = read_csv_rows(run_dir / "srmodule_results.csv")
    expected_sr_rows = expected["srmodule"] * cfg.energy_points
    if len(sr_rows) == expected_sr_rows:
        entries.append(validation_entry("pass", "srmodule_rows", f"{len(sr_rows)} result rows"))
    else:
        entries.append(
            validation_entry(
                "failure", "srmodule_rows", f"expected {expected_sr_rows}, found {len(sr_rows)}"
            )
        )
    invalid_sr = 0
    for row in sr_rows:
        numeric = [
            float(row[key])
            for key in (
                "electronic_stopping_mev_per_mg_cm2",
                "nuclear_stopping_mev_per_mg_cm2",
                "projected_range_mm",
                "longitudinal_straggling_mm",
                "lateral_straggling_mm",
            )
        ]
        qf = float(row["srmodule_electronic_fraction_proxy"])
        if any(not math.isfinite(value) or value < 0 for value in numeric) or not (0 <= qf <= 1):
            invalid_sr += 1
    entries.append(
        validation_entry(
            "failure" if invalid_sr else "pass",
            "srmodule_numeric_bounds",
            f"{invalid_sr} invalid rows" if invalid_sr else "all stopping, range, straggling, and proxy values are valid",
            invalid_sr,
        )
    )
    sr_archive_errors = 0
    for job_id in {row["job_id"] for row in sr_rows}:
        error = validate_raw_archive(
            run_dir / "raw" / "srmodule" / f"{job_id}.tar.gz",
            job_id,
            "srmodule",
            ("SR.IN", "SRModule-output.txt"),
        )
        if error:
            sr_archive_errors += 1
    entries.append(
        validation_entry(
            "failure" if sr_archive_errors else "pass",
            "srmodule_raw_provenance",
            f"{sr_archive_errors} invalid raw bundles" if sr_archive_errors else "all SRModule inputs, outputs, manifests, and output hashes are valid",
            sr_archive_errors,
        )
    )
    sr_groups: Dict[Tuple[str, str, str, str], List[Dict[str, str]]] = {}
    for row in sr_rows:
        key = (row["family"], row["noble_percent"], row["ion"], row["energy_kev"])
        sr_groups.setdefault(key, []).append(row)
    pressure_failures = 0
    areal_warnings = 0
    for rows in sr_groups.values():
        if len(rows) != len(cfg.pressures_atm):
            continue
        for field in (
            "electronic_stopping_mev_per_mg_cm2",
            "nuclear_stopping_mev_per_mg_cm2",
            "srmodule_electronic_fraction_proxy",
        ):
            if relative_spread([float(row[field]) for row in rows]) > cfg.srmodule_relative_tolerance:
                pressure_failures += 1
        if relative_spread([float(row["projected_range_mg_cm2"]) for row in rows]) > 0.01:
            areal_warnings += 1
    entries.append(
        validation_entry(
            "failure" if pressure_failures else "pass",
            "srmodule_pressure_invariance",
            f"{pressure_failures} invariant stopping/QF groups failed",
            pressure_failures,
        )
    )
    if areal_warnings:
        entries.append(
            validation_entry(
                "warning",
                "srmodule_inverse_range_pressure_scaling",
                f"{areal_warnings} groups exceed 1% spread, usually indicating SRIM text rounding",
                areal_warnings,
            )
        )
    else:
        entries.append(
            validation_entry(
                "pass",
                "srmodule_inverse_range_pressure_scaling",
                "physical range*density (mass range) is invariant within 1%",
            )
        )

    trim_rows = read_csv_rows(run_dir / "trim_results.csv")
    direct_trim_rows = (
        read_csv_rows(run_dir / "trim_anchor_results.csv") if cfg.derive_full_grid else trim_rows
    )
    expected_trim_rows = (
        len(cfg.families)
        * len(cfg.noble_percent)
        * len(cfg.pressures_atm)
        * 3
        * cfg.energy_points
        if cfg.derive_full_grid
        else expected["trim"]
    )
    if len(trim_rows) == expected_trim_rows:
        entries.append(validation_entry("pass", "trim_rows", f"{len(trim_rows)} result rows"))
    else:
        entries.append(
            validation_entry(
                "failure", "trim_rows", f"expected {expected_trim_rows}, found {len(trim_rows)}"
            )
        )
    qf_invalid = 0
    clipped = 0
    raw_invalid = 0
    range_warnings = 0
    for row in trim_rows:
        try:
            qf = float(row["qf_trim_ionization"])
            energy = float(row["energy_kev"])
            eion = float(row["total_ionization_energy_kev"])
            projected = float(row["projected_range_mm"])
            if not (0 <= qf <= 1.0 + 1.0e-6) or eion > energy + max(1.0e-6, energy * 1.0e-6):
                qf_invalid += 1
            if not math.isfinite(projected) or projected <= 0:
                qf_invalid += 1
            if str(row["clipped"]).lower() == "true":
                clipped += 1
            ratio_text = row.get("trim_to_srmodule_projected_range_ratio", "")
            if ratio_text:
                ratio = float(ratio_text)
                if abs(ratio - 1.0) > cfg.trim_srmodule_range_warning:
                    range_warnings += 1
        except (KeyError, ValueError):
            qf_invalid += 1
    for row in direct_trim_rows:
        try:
            archive_error = validate_raw_archive(
                run_dir / row["raw_archive"],
                row["job_id"],
                "trim",
                ("TRIM.IN", "TRIMAUTO", *STANDARD_TRIM_OUTPUTS),
            )
            profile_error = validate_profile_artifact(
                run_dir / row["normalized_profiles"], row["job_id"]
            )
            if archive_error or profile_error:
                raw_invalid += 1
        except (KeyError, ValueError):
            raw_invalid += 1
    entries.append(
        validation_entry(
            "failure" if qf_invalid else "pass",
            "trim_numeric_bounds",
            f"{qf_invalid} invalid rows" if qf_invalid else "all QF, energy, and range values are valid",
            qf_invalid,
        )
    )
    if cfg.derive_full_grid:
        derivation_invalid = sum(
            1
            for row in trim_rows
            if row.get("derivation_kind")
            not in {"direct", "pressure_scaled", "energy_interpolated", "pressure_scaled+energy_interpolated"}
            or not row.get("source_job_ids")
        )
        entries.append(
            validation_entry(
                "failure" if derivation_invalid else "pass",
                "trim_derivation_provenance",
                f"{derivation_invalid} derived rows lack provenance" if derivation_invalid else "all pressure-scaled/interpolated rows identify their direct source jobs",
                derivation_invalid,
            )
        )
    entries.append(
        validation_entry(
            "failure" if clipped else "pass",
            "trim_target_clipping",
            f"{clipped} clipped jobs" if clipped else "no final target/viewing windows are clipped",
            clipped,
        )
    )
    entries.append(
        validation_entry(
            "failure" if raw_invalid else "pass",
            "trim_raw_provenance",
            f"{raw_invalid} result rows have invalid raw/profile artifacts" if raw_invalid else "all raw archives, manifests, hashes, and normalized profiles are valid",
            raw_invalid,
        )
    )
    if range_warnings:
        entries.append(
            validation_entry(
                "warning",
                "trim_srmodule_range_comparison",
                f"{range_warnings} TRIM ranges differ from the deterministic SRModule value by more than {cfg.trim_srmodule_range_warning:.0%}",
                range_warnings,
            )
        )
    trim_groups: Dict[Tuple[str, str, str, str], List[Dict[str, str]]] = {}
    for row in trim_rows:
        key = (row["family"], row["noble_percent"], row["ion"], row["energy_kev"])
        trim_groups.setdefault(key, []).append(row)
    pressure_warn = 0
    pressure_fail = 0
    range_pressure_warn = 0
    range_pressure_fail = 0
    for rows in trim_groups.values():
        if len(rows) != len(cfg.pressures_atm):
            continue
        values = [float(row["qf_trim_ionization"]) for row in rows]
        spread = max(values) - min(values)
        if spread > cfg.qf_pressure_failure:
            pressure_fail += 1
        elif spread > cfg.qf_pressure_warning:
            pressure_warn += 1
        mass_ranges = [
            float(row["projected_range_mm"]) * float(row["density_g_cm3"])
            for row in rows
        ]
        range_spread = relative_spread(mass_ranges)
        if range_spread > cfg.trim_srmodule_range_warning:
            range_pressure_fail += 1
        elif range_spread > cfg.trim_srmodule_range_warning / 2.0:
            range_pressure_warn += 1
    entries.append(
        validation_entry(
            "failure" if pressure_fail else ("warning" if pressure_warn else "pass"),
            "trim_qf_pressure_invariance",
            f"failures={pressure_fail}, warnings={pressure_warn}",
            pressure_fail + pressure_warn,
        )
    )
    entries.append(
        validation_entry(
            "failure" if range_pressure_fail else ("warning" if range_pressure_warn else "pass"),
            "trim_inverse_range_pressure_scaling",
            f"failures={range_pressure_fail}, warnings={range_pressure_warn}",
            range_pressure_fail + range_pressure_warn,
        )
    )

    convergence = read_csv_rows(run_dir / "convergence_results.csv")
    if len(convergence) == expected["convergence"]:
        entries.append(validation_entry("pass", "convergence_rows", f"{len(convergence)} result rows"))
    else:
        entries.append(
            validation_entry(
                "failure", "convergence_rows", f"expected {expected['convergence']}, found {len(convergence)}"
            )
        )
    convergence_groups: Dict[Tuple[str, str, str, str, str], List[Dict[str, str]]] = {}
    convergence_artifact_failures = 0
    convergence_numeric_failures = 0
    for row in convergence:
        key = (
            row["family"],
            row["noble_percent"],
            row["pressure_atm"],
            row["ion"],
            row["energy_kev"],
        )
        convergence_groups.setdefault(key, []).append(row)
        try:
            qf = float(row["qf_trim_ionization"])
            projected = float(row["projected_range_mm"])
            if not (0 <= qf <= 1.0 + 1.0e-6) or not math.isfinite(projected) or projected <= 0:
                convergence_numeric_failures += 1
            if str(row["clipped"]).lower() == "true" or str(row["range_3d_present"]).lower() != "true":
                convergence_numeric_failures += 1
            archive_error = validate_raw_archive(
                run_dir / row["raw_archive"],
                row["job_id"],
                "convergence",
                ("TRIM.IN", "TRIMAUTO", *STANDARD_TRIM_OUTPUTS, "RANGE_3D.txt"),
            )
            profile_error = validate_profile_artifact(
                run_dir / row["normalized_profiles"], row["job_id"]
            )
            if archive_error or profile_error:
                convergence_artifact_failures += 1
        except (KeyError, ValueError):
            convergence_numeric_failures += 1
    entries.append(
        validation_entry(
            "failure" if convergence_numeric_failures else "pass",
            "convergence_numeric_and_endpoints",
            f"{convergence_numeric_failures} invalid rows" if convergence_numeric_failures else "all convergence ranges/QFs are valid, unclipped, and include 3-D endpoints",
            convergence_numeric_failures,
        )
    )
    entries.append(
        validation_entry(
            "failure" if convergence_artifact_failures else "pass",
            "convergence_raw_provenance",
            f"{convergence_artifact_failures} invalid raw/profile bundles" if convergence_artifact_failures else "all convergence inputs, standard outputs, endpoints, manifests, hashes, and profiles are valid",
            convergence_artifact_failures,
        )
    )
    convergence_failures = 0
    for rows in convergence_groups.values():
        if len(rows) != cfg.convergence_repeats:
            convergence_failures += 1
            continue
        for field in ("qf_trim_ionization", "projected_range_mm"):
            values = [float(row[field]) for row in rows]
            mean = statistics.fmean(values)
            rse = statistics.stdev(values) / math.sqrt(len(values)) / max(abs(mean), 1.0e-300)
            if rse > cfg.convergence_rse:
                convergence_failures += 1
    entries.append(
        validation_entry(
            cfg.convergence_rse_severity if convergence_failures else "pass",
            "convergence_relative_standard_error",
            f"{convergence_failures} quantity groups exceed {cfg.convergence_rse:.0%} RSE",
            convergence_failures,
        )
    )
    pressure_uncertainty_groups: Dict[Tuple[str, str, str, str], Dict[str, List[float]]] = {}
    for row in convergence:
        key = (row["family"], row["noble_percent"], row["ion"], row["energy_kev"])
        pressure_uncertainty_groups.setdefault(key, {}).setdefault(row["pressure_atm"], []).append(
            float(row["qf_trim_ionization"])
        )
    pressure_uncertainty_failures = 0
    for pressures in pressure_uncertainty_groups.values():
        if len(pressures) != len(cfg.convergence_pressures):
            continue
        samples = list(pressures.values())
        if any(len(values) != cfg.convergence_repeats for values in samples):
            continue
        means = [statistics.fmean(values) for values in samples]
        sems = [statistics.stdev(values) / math.sqrt(len(values)) for values in samples]
        combined_sem = math.sqrt(sum(value * value for value in sems))
        difference = abs(means[0] - means[1])
        if (combined_sem == 0.0 and difference > 1.0e-12) or (
            combined_sem > 0.0 and difference / combined_sem > 3.0
        ):
            pressure_uncertainty_failures += 1
    entries.append(
        validation_entry(
            "failure" if pressure_uncertainty_failures else "pass",
            "convergence_qf_pressure_uncertainty",
            f"{pressure_uncertainty_failures} pressure pairs differ by more than 3 combined standard errors",
            pressure_uncertainty_failures,
        )
    )
    passed = not any(entry["severity"] == "failure" for entry in entries)
    atomic_write_json(
        run_dir / "validation.json",
        {"generated_at": utc_now(), "passed": passed, "checks": entries},
    )
    write_csv_atomic(run_dir / "validation.csv", entries, ["severity", "check", "count", "message"])
    return entries, passed


def generate_plots(run_dir: Path) -> List[Path]:
    rows = read_csv_rows(run_dir / "trim_results.csv")
    if not rows:
        return []
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return []
    plots = run_dir / "plots"
    plots.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    for family in ("Xe", "Kr", "Ar"):
        chosen = [
            row
            for row in rows
            if row["family"] == family
            and int(row["noble_percent"]) in {60, 80, 95}
            and abs(float(row["pressure_atm"]) - 1.0) < 1.0e-12
        ]
        if not chosen:
            continue
        fig, axes = plt.subplots(2, 1, figsize=(9, 9), sharex=True)
        for percent in (60, 80, 95):
            for ion in (family, "C", "F"):
                series = [
                    row for row in chosen if int(row["noble_percent"]) == percent and row["ion"] == ion
                ]
                series.sort(key=lambda row: float(row["energy_kev"]))
                if not series:
                    continue
                label = f"{ion}, {percent}/{100-percent}"
                energies = [float(row["energy_kev"]) for row in series]
                axes[0].plot(energies, [float(row["qf_trim_ionization"]) for row in series], label=label)
                axes[1].plot(energies, [float(row["projected_range_mm"]) for row in series], label=label)
        axes[0].set_ylabel("TRIM electronic-energy fraction")
        axes[0].set_ylim(0, 1.05)
        axes[1].set_ylabel("TRIM projected range (mm)")
        axes[1].set_xlabel("Recoil energy (keV)")
        for axis in axes:
            axis.set_xscale("log")
            axis.grid(True, which="both", alpha=0.25)
            axis.legend(fontsize=7, ncol=3)
        axes[1].set_yscale("log")
        fig.suptitle(f"{family}/CF4 at 1 atm (SRIM assumes STP stopping physics)")
        fig.tight_layout()
        path = plots / f"{family.lower()}_cf4_qf_range.png"
        fig.savefig(path, dpi=180, bbox_inches="tight")
        plt.close(fig)
        written.append(path)
    return written


def write_report(run_dir: Path, cfg: ScanConfig, entries: Sequence[Mapping[str, Any]], passed: bool) -> Path:
    plots = generate_plots(run_dir)
    status = "PASS" if passed else "INCOMPLETE / FAILED"
    checks = "\n".join(
        f"<tr><td>{html.escape(str(row['severity']))}</td><td>{html.escape(str(row['check']))}</td>"
        f"<td>{html.escape(str(row['message']))}</td></tr>" for row in entries
    )
    images = "\n".join(
        f'<figure><img src="{html.escape(str(path.relative_to(run_dir)))}" alt="{html.escape(path.stem)}"></figure>'
        for path in plots
    )
    derivation_note = (
        "<p><strong>Time-boxed derivation:</strong> Direct full-cascade values are in "
        "<code>trim_anchor_results.csv</code>. The expanded <code>trim_results.csv</code> "
        "labels pressure-scaled and energy-interpolated rows and identifies their source jobs. "
        "Derived rows are not independent Monte Carlo calculations.</p>"
        if cfg.derive_full_grid
        else ""
    )
    body = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>SRIM noble-gas/CF4 report</title>
<style>body{{font:15px system-ui;max-width:1100px;margin:2rem auto;padding:0 1rem}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #bbb;padding:.4rem;text-align:left}}img{{max-width:100%}}code{{background:#eee;padding:.1rem .25rem}}</style>
</head><body>
<h1>SRIM noble-gas/CF4 scan: {html.escape(status)}</h1>
<p>Grid: Xe/Kr/Ar with CF4, 60/40 through 95/5, 0.5 through 3 atm, 293.15 K, 0.1–500 keV.</p>
<p><strong>Scientific limit:</strong> SRIM always assumes STP gas stopping physics. Pressure changes here enter through ideal-gas density and range scaling; this is not a non-STP stopping correction. The reported QF is an SRIM electronic-energy fraction, not a detector charge/light calibration.</p>
{derivation_note}
<p>Primary tables: <a href="gas_conditions.csv">gas conditions</a>, <a href="srmodule_results.csv">SRModule</a>, <a href="trim_results.csv">TRIM</a>, <a href="convergence_results.csv">convergence</a>.</p>
<h2>Validation</h2><table><thead><tr><th>Severity</th><th>Check</th><th>Result</th></tr></thead><tbody>{checks}</tbody></table>
<h2>Summary plots</h2>{images or '<p>Matplotlib unavailable or no completed TRIM rows.</p>'}
<h2>Interpretation</h2>
<p><code>qf_trim_ionization</code> integrates IONIZ energy from the primary and secondary recoils. <code>srmodule_electronic_fraction_proxy</code> is a separate deterministic primary-ion stopping proxy. Vacancy, displacement, and phonon-named files are retained for provenance but are not gas material-damage observables.</p>
</body></html>"""
    path = run_dir / "report.html"
    atomic_write_text(path, body)
    return path


def print_plan(cfg: ScanConfig) -> None:
    gas_conditions = len(cfg.families) * len(cfg.noble_percent) * len(cfg.pressures_atm)
    gas_ions = gas_conditions * 3
    trim_combinations = len(cfg.families) * len(cfg.noble_percent) * len(cfg.trim_pressures) * 3
    trim_jobs = trim_combinations * cfg.trim_energy_points
    convergence_points = (
        len(cfg.families)
        * len(cfg.convergence_percent)
        * len(cfg.convergence_pressures)
        * 3
        * len(cfg.convergence_energies)
    )
    if not cfg.raw.get("convergence", {}).get("enabled", True):
        convergence_points = 0
    convergence_jobs = convergence_points * cfg.convergence_repeats
    print(f"Configuration: {cfg.path}")
    print(f"Gas conditions: {gas_conditions}")
    print(f"Gas/ion combinations: {gas_ions}")
    print(f"SRModule jobs: {gas_ions}; output rows: {gas_ions * cfg.energy_points}")
    trim_label = "Direct TRIM anchor jobs" if cfg.derive_full_grid else "Direct TRIM jobs"
    print(
        f"{trim_label}: {trim_jobs}; primary-ion histories: {trim_jobs * cfg.trim_ions:,}; "
        f"pressures={cfg.trim_pressures}; energies={cfg.trim_energy_points}"
    )
    if cfg.derive_full_grid:
        print(
            f"Derived TRIM table: {gas_ions * cfg.energy_points} rows across all pressures and energies; "
            "direct/scaled/interpolated provenance retained per row."
        )
    print(
        f"Convergence jobs: {convergence_jobs} ({convergence_points} points x {cfg.convergence_repeats} repeats); "
        f"primary-ion histories: {convergence_jobs * cfg.convergence_ions:,}"
    )
    if cfg.campaign_sessions:
        print(
            f"Campaign budget: at most {cfg.campaign_sessions} sessions x "
            f"{cfg.campaign_session_hours:g} h, {cfg.workers_default} workers, "
            f"{cfg.campaign_stop_margin_minutes:g} min drain margin."
        )
    if cfg.campaign_max_cumulative_hours:
        print(
            f"Persistent cumulative runtime limit: {cfg.campaign_max_cumulative_hours:g} h "
            "across restarts."
        )
    print("Pressure caveat: SRIM assumes STP gas stopping; density changes range scaling only.")


def run_preflight(
    cfg: ScanConfig,
    srim_root: Path,
    execution: ExecutionContext,
    skip_canary: bool,
    canary_ions: int,
) -> int:
    execution_info = execution_provenance(execution)
    fingerprint_before = source_fingerprint(srim_root)
    controls_before = source_control_fingerprint(srim_root)
    elements, _ = load_elements(srim_root / "Data" / "ATOMDATA")
    conditions = generate_conditions(cfg, elements)
    expected_conditions = len(cfg.families) * len(cfg.noble_percent) * len(cfg.pressures_atm)
    if len(conditions) != expected_conditions:
        raise RuntimeError(f"Grid generation produced {len(conditions)} conditions, expected {expected_conditions}.")
    print(f"SRIM root: {srim_root}")
    print(f"Execution backend: {execution.backend}")
    if execution.backend == "pysrim":
        print(f"PySRIM root: {execution.pysrim_root}")
        print(f"PySRIM commit: {execution_info['pysrim_commit']}")
        print(f"Wine prefix: {execution.wine_prefix}")
    print(f"Version: {fingerprint_before.get('Data/VERSION:text', 'unknown')}")
    print(f"SRModule version: {fingerprint_before.get('SR Module/VERSION:text', 'unknown')}")
    print_plan(cfg)
    if skip_canary:
        print("Preflight file/configuration checks passed; executable canaries were skipped.")
        return 0
    temporary = Path(tempfile.mkdtemp(prefix=".srim-preflight-", dir=script_dir()))
    keep = True
    try:
        runtime = temporary / "runtime" / "worker-000"
        prepare_runtime(srim_root, runtime, fingerprint_before)
        condition = next(
            condition
            for condition in conditions
            if condition.family == "Xe" and condition.noble_percent == 60 and condition.pressure_atm == 1.0
        )
        sr_payload = {
            "condition": condition_payload(condition),
            "ion": "Xe",
            "energy_min_kev": cfg.energy_min_kev,
            "energy_max_kev": cfg.energy_max_kev,
            "energy_points": cfg.energy_points,
            "compound_correction": cfg.compound_correction,
        }
        sr_job = JobSpec(stable_id("canary-sr", sr_payload), "srmodule", sr_payload)
        relative = run_srmodule_job(sr_job, runtime, temporary, cfg, elements, execution)
        sr_data = json.loads((temporary / relative).read_text(encoding="utf-8"))
        write_csv_atomic(temporary / "srmodule_results.csv", sr_data["rows"])
        trim_payload = {
            "condition": condition_payload(condition),
            "ion": "Xe",
            "energy_kev": 10.0,
            "n_ions": canary_ions,
            "cascade_type": 2,
            "repeat": 0,
            "save_3d_ranges": True,
        }
        trim_job = JobSpec(stable_id("canary-tr", trim_payload), "convergence", trim_payload)
        predictions = load_srmodule_predictions(temporary)
        trim_relative = run_trim_job(
            trim_job, runtime, temporary, cfg, elements, predictions, execution
        )
        trim_result = json.loads((temporary / trim_relative).read_text(encoding="utf-8"))
        if trim_result["clipped"]:
            raise RuntimeError("TRIM canary remained clipped after target enlargement.")
        qf = float(trim_result["qf_trim_ionization"])
        if not 0 <= qf <= 1:
            raise RuntimeError(f"TRIM canary QF is outside [0,1]: {qf}")
        if not trim_result["range_3d_present"]:
            raise RuntimeError("TRIM canary did not generate the requested RANGE_3D endpoint file.")
        raw_error = validate_raw_archive(
            temporary / trim_result["raw_archive"],
            trim_result["job_id"],
            "convergence",
            ("TRIM.IN", "TRIMAUTO", *STANDARD_TRIM_OUTPUTS, "RANGE_3D.txt"),
        )
        profile_error = validate_profile_artifact(
            temporary / trim_result["normalized_profiles"], trim_result["job_id"]
        )
        if raw_error or profile_error:
            raise RuntimeError(
                f"TRIM canary artifact validation failed: raw={raw_error}, profile={profile_error}"
            )
        if trim_result["backscattered_ions"] is None or trim_result["transmitted_ions"] is None:
            raise RuntimeError("TRIM canary did not yield explicit escape-ion counts.")
        fingerprint_after = source_fingerprint(srim_root)
        controls_after = source_control_fingerprint(srim_root)
        if fingerprint_before != fingerprint_after or controls_before != controls_after:
            raise RuntimeError("Installed SRIM executables, data, or control inputs changed during canaries.")
        print(
            f"Executable canaries passed: Xe 10 keV in Xe/CF4 60/40 at 1 atm, "
            f"n={canary_ions}, QF={qf:.6g}, range={trim_result['projected_range_mm']:.6g} mm."
        )
        print("Installed SRIM executables, data, and control inputs remained byte-for-byte unchanged.")
        keep = False
        return 0
    finally:
        if keep:
            print(f"Failed canary artifacts retained at {temporary}", file=sys.stderr)
        else:
            shutil.rmtree(temporary)


def command_run(args: argparse.Namespace) -> int:
    if getattr(args, "job_id", None) and args.stage == "all":
        raise ValueError("--job-id requires a single --stage.")
    cfg = load_config(args.config)
    srim_root = resolve_srim_root(args.srim_root, cfg)
    execution = resolve_execution_context(
        cfg,
        args.backend,
        args.pysrim_root,
        args.wine_prefix,
        args.no_headless,
    )
    run_dir = args.run_dir.resolve()
    worker_count = args.workers if args.workers is not None else cfg.workers_default
    execution_info = execution_provenance(execution)
    execution_info["selected_worker_count"] = worker_count
    execution_info["host"] = host_provenance(srim_root, run_dir)
    elements, _ = load_elements(srim_root / "Data" / "ATOMDATA")
    conditions = generate_conditions(cfg, elements)
    wall_hours = args.wall_hours
    if wall_hours is None and cfg.campaign_session_hours > 0:
        wall_hours = cfg.campaign_session_hours
    stop_margin_minutes = args.stop_margin_minutes
    if stop_margin_minutes is None:
        stop_margin_minutes = cfg.campaign_stop_margin_minutes
    if wall_hours is not None and wall_hours <= 0:
        raise ValueError("--wall-hours must be positive.")
    if stop_margin_minutes < 0:
        raise ValueError("The stop margin must be nonnegative.")
    connection = open_database(run_dir)
    campaign_session_id: int | None = None
    try:
        record_run_metadata(connection, cfg, srim_root, run_dir, conditions, execution_info)
        if cfg.campaign_max_cumulative_hours:
            campaign_session_id, usage = begin_campaign_session(
                connection, cfg.campaign_max_cumulative_hours
            )
            remaining_hours = float(usage["remaining_seconds"]) / 3600.0
            used_hours = float(usage["elapsed_seconds"]) / 3600.0
            print(
                f"Cumulative campaign budget: {used_hours:.6g} h used; "
                f"{remaining_hours:.6g} h remain of {cfg.campaign_max_cumulative_hours:g} h.",
                flush=True,
            )
            if campaign_session_id is None:
                print("Cumulative campaign runtime is exhausted; no work was launched.")
                return 0
            wall_hours = min(wall_hours or remaining_hours, remaining_hours)
        stop_launch_at: float | None = None
        hard_deadline: float | None = None
        if wall_hours is not None:
            hard_deadline = time.monotonic() + wall_hours * 3600.0
            stop_launch_at = hard_deadline - stop_margin_minutes * 60.0
            print(
                f"Session budget: {wall_hours:g} h; stop launching new jobs "
                f"{stop_margin_minutes:g} min before the deadline.",
                flush=True,
            )
            if stop_launch_at <= time.monotonic():
                print("Remaining runtime is inside the drain margin; no new work was launched.")
                return 0
        stages = ["srmodule", "trim", "convergence"] if args.stage == "all" else [args.stage]
        for stage in stages:
            if stop_launch_at is not None and time.monotonic() >= stop_launch_at:
                print("Session launch deadline reached before the next stage; resume next session.")
                break
            runtimes, _ = prepare_runtimes(srim_root, run_dir, worker_count, execution)
            if stage == "srmodule":
                jobs = make_srmodule_jobs(conditions, cfg)

                def worker(job: JobSpec, runtime: Path) -> str:
                    worker_execution = runtime_execution_context(execution, run_dir, runtime)
                    return run_srmodule_job(
                        job, runtime, run_dir, cfg, elements, worker_execution
                    )

            else:
                predictions = load_srmodule_predictions(run_dir)
                jobs = make_trim_jobs(conditions, cfg, stage)

                def worker(
                    job: JobSpec,
                    runtime: Path,
                    predictions: Any = predictions,
                    hard_deadline: float | None = hard_deadline,
                ) -> str:
                    worker_execution = runtime_execution_context(execution, run_dir, runtime)
                    return run_trim_job(
                        job,
                        runtime,
                        run_dir,
                        cfg,
                        elements,
                        predictions,
                        worker_execution,
                        hard_deadline,
                    )

            if getattr(args, "job_id", None):
                requested = set(args.job_id)
                jobs = [job for job in jobs if job.job_id in requested]
                missing = requested - {job.job_id for job in jobs}
                if missing:
                    raise ValueError(f"Requested job IDs are not in the configured stage: {sorted(missing)}")
            exhausted = execute_stage(
                jobs,
                stage,
                connection,
                run_dir,
                runtimes,
                worker,
                args.resume,
                cfg.max_attempts,
                args.limit,
                stop_launch_at,
            )
            if stage == "trim" and cfg.derive_full_grid:
                incomplete = connection.execute(
                    "SELECT COUNT(*) FROM jobs WHERE stage='trim' AND status!='complete'"
                ).fetchone()[0]
                if incomplete:
                    print(
                        f"Derived TRIM grid deferred until all anchors complete; {incomplete} remain.",
                        flush=True,
                    )
                else:
                    derive_trim_grid(run_dir, conditions, cfg)
            if exhausted:
                break
        return 0
    finally:
        finish_campaign_session(connection, campaign_session_id)
        connection.close()


def command_status(run_dir: Path) -> int:
    database = run_dir / "jobs.sqlite"
    if not database.exists():
        raise FileNotFoundError(f"No scan database at {database}")
    connection = sqlite3.connect(database)
    rows = connection.execute(
        "SELECT stage,status,COUNT(*) FROM jobs GROUP BY stage,status ORDER BY stage,status"
    ).fetchall()
    for stage, status, count in rows:
        print(f"{stage:12s} {status:10s} {count:8d}")
    active_ids = {row[0] for row in connection.execute("SELECT job_id FROM jobs WHERE status='running'")}
    for path in sorted((run_dir / "runtime").glob("worker-*/.trim-progress.json")):
        progress = json.loads(path.read_text(encoding="utf-8"))
        if progress.get("job_id") in active_ids:
            age = time.time() - float(progress["updated_epoch"])
            print(
                f"{path.parent.name}: {progress['status']}; heartbeat {age:.0f}s ago; "
                f"TRIM CPU {progress.get('cpu_seconds', 0):.1f}s; "
                f"recoil capacity {progress['secondary_recoil_capacity']:,}"
            )
    cfg_path = run_dir / "provenance" / "config.toml"
    if cfg_path.exists():
        cfg = load_config(cfg_path)
        if cfg.campaign_max_cumulative_hours:
            usage = campaign_usage(connection, cfg.campaign_max_cumulative_hours)
            print(
                f"campaign     runtime    {float(usage['elapsed_seconds']) / 3600.0:8.3f} h used; "
                f"{float(usage['remaining_seconds']) / 3600.0:.3f} h remaining; "
                f"open sessions={usage['open_sessions']}"
            )
    connection.close()
    return 0


def command_validate(run_dir: Path) -> int:
    cfg_path = run_dir / "provenance" / "config.toml"
    if not cfg_path.exists():
        raise FileNotFoundError(f"Missing run configuration {cfg_path}")
    cfg = load_config(cfg_path)
    entries, passed = validate_run(run_dir, cfg)
    for entry in entries:
        print(f"{entry['severity'].upper():7s} {entry['check']}: {entry['message']}")
    print(f"Validation {'PASSED' if passed else 'FAILED'}; details: {run_dir / 'validation.json'}")
    return 0 if passed else 1


def command_report(run_dir: Path) -> int:
    cfg_path = run_dir / "provenance" / "config.toml"
    cfg = load_config(cfg_path)
    entries, passed = validate_run(run_dir, cfg)
    report = write_report(run_dir, cfg, entries, passed)
    print(f"Wrote report: {report}")
    return 0 if passed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resumable SRIM noble-gas/CF4 stopping, range, and electronic-energy scan."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    default_config = script_dir() / "configs" / "noble_cf4_standard.toml"

    def add_execution_arguments(command: argparse.ArgumentParser) -> None:
        command.add_argument("--srim-root", type=Path)
        command.add_argument("--backend", choices=("auto", "native", "pysrim"))
        command.add_argument("--pysrim-root", type=Path)
        command.add_argument("--wine-prefix", type=Path)
        command.add_argument(
            "--no-headless",
            action="store_true",
            help="Use the current DISPLAY instead of starting an isolated Xvfb display.",
        )

    preflight = subparsers.add_parser("preflight", help="Validate the installation and run isolated canaries.")
    preflight.add_argument("--config", type=Path, default=default_config)
    add_execution_arguments(preflight)
    preflight.add_argument("--skip-canary", action="store_true")
    preflight.add_argument("--canary-ions", type=int, default=10)

    plan = subparsers.add_parser("plan", help="Print the exact grid and workload without writing a run.")
    plan.add_argument("--config", type=Path, default=default_config)

    run = subparsers.add_parser("run", help="Execute one or all resumable scan stages.")
    run.add_argument("--config", type=Path, default=default_config)
    add_execution_arguments(run)
    run.add_argument("--run-dir", type=Path, required=True)
    run.add_argument("--stage", choices=("srmodule", "trim", "convergence", "all"), required=True)
    run.add_argument("--workers", type=int)
    run.add_argument("--resume", action="store_true")
    run.add_argument("--limit", type=int, help="Run at most this many pending jobs; intended for commissioning.")
    run.add_argument("--job-id", action="append", help="Select an exact job for recovery; repeatable, single stage only.")
    run.add_argument(
        "--wall-hours",
        type=float,
        help="Maximum session duration; defaults to campaign.session_hours when configured.",
    )
    run.add_argument(
        "--stop-margin-minutes",
        type=float,
        help="Stop launching new jobs this long before the wall deadline so active jobs can drain.",
    )

    status = subparsers.add_parser("status", help="Show checkpoint counts for a run.")
    status.add_argument("--run-dir", type=Path, required=True)

    validate = subparsers.add_parser("validate", help="Run completeness and scientific consistency checks.")
    validate.add_argument("--run-dir", type=Path, required=True)

    report = subparsers.add_parser("report", help="Regenerate plots and the HTML validation report.")
    report.add_argument("--run-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    def handle_shutdown(_signum: int, _frame: Any) -> None:
        SHUTDOWN_REQUESTED.set()
        raise KeyboardInterrupt

    if os.name != "nt":
        signal.signal(signal.SIGTERM, handle_shutdown)
        signal.signal(signal.SIGHUP, handle_shutdown)
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "preflight":
        cfg = load_config(args.config)
        if args.canary_ions < 1:
            parser.error("--canary-ions must be positive")
        execution = resolve_execution_context(
            cfg,
            args.backend,
            args.pysrim_root,
            args.wine_prefix,
            args.no_headless,
        )
        return run_preflight(
            cfg,
            resolve_srim_root(args.srim_root, cfg),
            execution,
            args.skip_canary,
            args.canary_ions,
        )
    if args.command == "plan":
        print_plan(load_config(args.config))
        return 0
    if args.command == "run":
        if args.workers is not None and args.workers < 1:
            parser.error("--workers must be positive")
        if args.limit is not None and args.limit < 1:
            parser.error("--limit must be positive")
        return command_run(args)
    if args.command == "status":
        return command_status(args.run_dir.resolve())
    if args.command == "validate":
        return command_validate(args.run_dir.resolve())
    if args.command == "report":
        return command_report(args.run_dir.resolve())
    parser.error(f"Unknown command {args.command}")
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
