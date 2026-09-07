#!/usr/bin/env python3
"""Run a disposable, representative Wine/PySRIM concurrency benchmark."""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import srim_scan  # noqa: E402
from srim_gas_sweep import load_elements  # noqa: E402


CASES = (
    ("Xe", 60, 0.5, "Xe", 500.0, 0),
    ("Xe", 95, 3.0, "F", 500.0, 0),
    ("Ar", 60, 0.5, "Ar", 500.0, 0),
    ("Ar", 95, 3.0, "F", 500.0, 0),
    ("Kr", 60, 0.5, "Kr", 100.0, 0),
    ("Kr", 95, 3.0, "C", 10.0, 0),
    ("Xe", 60, 3.0, "C", 0.1, 0),
    ("Ar", 95, 0.5, "F", 0.1, 0),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=ROOT / "configs" / "noble_cf4_7day_ubuntu.toml"
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 2, 4, 8])
    parser.add_argument("--ions", type=int, default=10)
    parser.add_argument("--timeout-s", type=int, default=600)
    return parser.parse_args()


def selected_jobs(
    conditions: list[srim_scan.GasCondition],
    config: srim_scan.ScanConfig,
    ions: int,
) -> tuple[list[srim_scan.JobSpec], list[srim_scan.JobSpec]]:
    srmodule: dict[tuple[str, str], srim_scan.JobSpec] = {}
    trim: list[srim_scan.JobSpec] = []
    for family, percent, pressure, ion, energy, repeat in CASES:
        condition = next(
            item
            for item in conditions
            if item.family == family
            and item.noble_percent == percent
            and abs(item.pressure_atm - pressure) < 1.0e-12
        )
        sr_payload = {
            "condition": srim_scan.condition_payload(condition),
            "ion": ion,
            "energy_min_kev": config.energy_min_kev,
            "energy_max_kev": config.energy_max_kev,
            "energy_points": config.energy_points,
            "compound_correction": config.compound_correction,
        }
        sr_job = srim_scan.JobSpec(srim_scan.stable_id("bm-sr", sr_payload), "srmodule", sr_payload)
        srmodule[(condition.condition_id, ion)] = sr_job
        trim_payload = {
            "condition": srim_scan.condition_payload(condition),
            "ion": ion,
            "energy_kev": energy,
            "n_ions": ions,
            "cascade_type": config.trim_cascade_type,
            "repeat": repeat,
            "save_3d_ranges": False,
        }
        trim.append(
            srim_scan.JobSpec(srim_scan.stable_id("bm-tr", trim_payload), "trim", trim_payload)
        )
    trim.sort(
        key=lambda job: (float(job.payload["energy_kev"]), str(job.payload["ion"])),
        reverse=True,
    )
    return list(srmodule.values()), trim


def validate_result(run_dir: Path, relative: str) -> list[str]:
    result = json.loads((run_dir / relative).read_text(encoding="utf-8"))
    errors: list[str] = []
    energy = float(result["energy_kev"])
    qf = float(result["qf_trim_ionization"])
    total = float(result["total_ionization_energy_kev"])
    projected_range = float(result["projected_range_mm"])
    if not math.isfinite(qf) or not 0.0 <= qf <= 1.0:
        errors.append(f"QF outside [0,1]: {qf}")
    # A single escaping ion can legitimately yield a zero sample mean. The
    # real preflight uses ten ions and enforces a positive finite range; keep
    # this one-history scaling benchmark focused on infrastructure stability.
    if not math.isfinite(projected_range) or (
        projected_range <= 0.0 and int(result["n_ions"]) > 1
    ):
        errors.append(f"projected range is not positive and finite: {projected_range}")
    if not math.isfinite(total) or total < 0.0 or total > energy * 1.000001 + 1.0e-6:
        errors.append(f"IONIZ integral is inconsistent with initial energy: {total}/{energy}")
    if bool(result["clipped"]):
        errors.append("target/viewing window remained clipped")
    raw_error = srim_scan.validate_raw_archive(
        run_dir / result["raw_archive"],
        result["job_id"],
        "trim",
        ("TRIM.IN", "TRIMAUTO", *srim_scan.STANDARD_TRIM_OUTPUTS),
    )
    profile_error = srim_scan.validate_profile_artifact(
        run_dir / result["normalized_profiles"], result["job_id"]
    )
    if raw_error:
        errors.append(f"raw capture: {raw_error}")
    if profile_error:
        errors.append(f"profile capture: {profile_error}")
    return errors


def wait_for_wine(context: srim_scan.ExecutionContext) -> bool:
    environment = srim_scan.pysrim_environment(context)
    try:
        completed = subprocess.run(
            ["wineserver", "-w"],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        return completed.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def main() -> int:
    args = parse_args()
    if args.ions < 1 or any(value < 1 for value in args.workers):
        raise ValueError("ion and worker counts must be positive")
    output_dir = args.output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty benchmark directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    config = replace(srim_scan.load_config(args.config), trim_timeout_s=args.timeout_s)
    srim_root = srim_scan.resolve_srim_root(None, config)
    context = srim_scan.resolve_execution_context(config)
    execution = srim_scan.execution_provenance(context)
    elements, _ = load_elements(srim_root / "Data" / "ATOMDATA")
    conditions = srim_scan.generate_conditions(config, elements)
    sr_jobs, trim_jobs = selected_jobs(conditions, config, args.ions)
    source_before = srim_scan.source_fingerprint(srim_root)
    controls_before = srim_scan.source_control_fingerprint(srim_root)
    trials: list[dict[str, Any]] = []
    try:
        for workers in args.workers:
            run_dir = output_dir / f"workers-{workers}"
            connection = srim_scan.open_database(run_dir)
            runtimes, _ = srim_scan.prepare_runtimes(
                srim_root, run_dir, workers, context
            )
            try:
                def sr_worker(job: srim_scan.JobSpec, runtime: Path) -> str:
                    worker_context = srim_scan.runtime_execution_context(
                        context, run_dir, runtime
                    )
                    return srim_scan.run_srmodule_job(
                        job, runtime, run_dir, config, elements, worker_context
                    )

                srim_scan.execute_stage(
                    sr_jobs, "srmodule", connection, run_dir, runtimes, sr_worker, True, 1, None
                )
                predictions = srim_scan.load_srmodule_predictions(run_dir)
                job_timings: dict[str, float] = {}

                def trim_worker(job: srim_scan.JobSpec, runtime: Path) -> str:
                    started = time.monotonic()
                    try:
                        worker_context = srim_scan.runtime_execution_context(
                            context, run_dir, runtime
                        )
                        return srim_scan.run_trim_job(
                            job, runtime, run_dir, config, elements, predictions, worker_context
                        )
                    finally:
                        job_timings[job.job_id] = time.monotonic() - started

                started = time.monotonic()
                srim_scan.execute_stage(
                    trim_jobs, "trim", connection, run_dir, runtimes, trim_worker, True, 1, None
                )
                elapsed = time.monotonic() - started
                result_rows = connection.execute(
                    "SELECT job_id,result_path FROM jobs WHERE stage='trim' AND status='complete'"
                ).fetchall()
                errors = [
                    f"{job_id}: {error}"
                    for job_id, relative in result_rows
                    for error in validate_result(run_dir, str(relative))
                ]
                raw_bytes = sum(
                    path.stat().st_size for path in (run_dir / "raw" / "trim").glob("*.tar.gz")
                )
                histories = len(trim_jobs) * args.ions
                trial = {
                    "workers": workers,
                    "jobs": len(trim_jobs),
                    "histories": histories,
                    "wall_seconds": elapsed,
                    "jobs_per_hour": len(trim_jobs) / elapsed * 3600.0,
                    "histories_per_second": histories / elapsed,
                    "raw_bytes": raw_bytes,
                    "raw_bytes_per_second": raw_bytes / elapsed,
                    "job_seconds": job_timings,
                    "validation_errors": errors,
                    "wine_cleanup_ok": all(
                        wait_for_wine(
                            srim_scan.runtime_execution_context(context, run_dir, runtime)
                        )
                        for runtime in runtimes
                    ),
                }
                trials.append(trial)
                print(json.dumps(trial, sort_keys=True), flush=True)
            finally:
                connection.close()
    finally:
        wait_for_wine(context)

    source_after = srim_scan.source_fingerprint(srim_root)
    controls_after = srim_scan.source_control_fingerprint(srim_root)
    stable = [
        row for row in trials if not row["validation_errors"] and row["wine_cleanup_ok"]
    ]
    if not stable:
        raise RuntimeError("no worker count completed the validated benchmark stably")
    selected = max(stable, key=lambda row: float(row["histories_per_second"]))
    total_histories = 43_200_000 + 3_240_000
    retry_factor = 1.10
    safety_factor = 1.25
    fixed_hours = config.campaign_stop_margin_minutes / 60.0 + 1.0
    estimate_hours = (
        total_histories
        / float(selected["histories_per_second"])
        / 3600.0
        * retry_factor
        * safety_factor
        + fixed_hours
    )
    summary = {
        "created_at": srim_scan.utc_now(),
        "benchmark_ions_per_job": args.ions,
        "cases": [
            {
                "family": family,
                "noble_percent": percent,
                "pressure_atm": pressure,
                "ion": ion,
                "energy_kev": energy,
            }
            for family, percent, pressure, ion, energy, _repeat in CASES
        ],
        "execution": execution,
        "host": srim_scan.host_provenance(srim_root, output_dir),
        "source_fingerprint": source_after,
        "source_control_unchanged": controls_before == controls_after,
        "source_fingerprint_unchanged": source_before == source_after,
        "trials": trials,
        "selected_worker_count": int(selected["workers"]),
        "selected_histories_per_second": float(selected["histories_per_second"]),
        "runtime_estimate": {
            "baseline_histories": 43_200_000,
            "convergence_histories": 3_240_000,
            "retry_allowance_factor": retry_factor,
            "conservative_safety_factor": safety_factor,
            "drain_validation_report_hours": fixed_hours,
            "estimated_total_hours": estimate_hours,
            "seven_day_completion_credible": estimate_hours <= 168.0,
            "note": (
                "Conservative history-throughput extrapolation from a bundle weighted toward "
                "500 keV full-cascade jobs; production jobs retain 1000/10000-ion statistics."
            ),
        },
    }
    srim_scan.atomic_write_json(output_dir / "benchmark_summary.json", summary)
    print(f"selected workers: {summary['selected_worker_count']}")
    print(f"conservative estimate: {estimate_hours:.3f} h")
    print(f"summary: {output_dir / 'benchmark_summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
