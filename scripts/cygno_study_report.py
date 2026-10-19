#!/usr/bin/env python3
"""Portable plots and reports for a completed or partial CYGNO SRIM study.

This module only reads the study tables. It never starts SRIM or fills missing
Monte Carlo points with interpolated values. Call ``build_report(study_dir)``
or run this file with the study directory as its sole argument.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import html
import json
import math
import re
import textwrap
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.backends.backend_pdf import PdfPages


SPECIES = ("H", "He", "C", "F", "O")
COLORS = dict(zip(SPECIES, ("#0072B2", "#E69F00", "#009E73", "#CC79A7", "#D55E00")))
MEDIA = ("baseline", "contaminated")
CALIBRATION_NOTE = (
    "Eee_Srim is the SRIM electronic-energy fraction times recoil energy. "
    "It is an energy-only surrogate, not charge-calibrated physical keVee. "
    "Physical electron-recoil/charge calibration remains unresolved."
)
LENGTH_NOTE = (
    "Projected range is the signed displacement along the incident direction; "
    "endpoint radius is the displacement magnitude. The sampled primary path "
    "joins recorded primary trajectory points and excludes secondary tracks. "
    "Complete primary and cascade arc lengths have not been established here. "
    "Detailed COLLISON output offers additional 3D collision information, whose "
    "filtering and recoil-history semantics require separate qualification."
)
ENERGY_NOTE = (
    "Plotted energy channels are primary electronic energy, secondary electronic energy, "
    "and SRIM phonon energy. E2RECOIL is an intermediate recoil-energy transfer "
    "and is not added to these channels. Escape, residual, threshold and binding "
    "energy balances must be assessed separately in validation; the displayed "
    "channels are not assumed to exhaust the initial energy. SRIM phonon output in a gas is a model "
    "bookkeeping channel, not a measured gas heat or material-damage observable."
)
UNCERTAINTY_NOTE = (
    "Error bars show supplied standard errors: QF and Eee_Srim use independent "
    "batch means; range and sampled-path errors use the supplied endpoint/track "
    "sample estimator. Contamination QF differences use the recorded paired-batch estimator. "
    "Missing uncertainties remain missing; correlations between recoil-derived "
    "coordinates are not modeled. Lines only connect reported simulation points. "
    "Only positive x-values appear on logarithmic axes; zero or missing values "
    "remain in the numerical export."
)
IMPURITY_NOTE = (
    "Relative changes are dimensionless; multiply by 100 for percent. Missing "
    "comparison values are unavailable, not zero. A small mean shift with broad "
    "uncertainty does not establish equivalence. QF classification uses paired linear contrasts to assess "
    "the configured relative-change band while retaining baseline uncertainty. "
    "Their Student-t intervals are nominal pointwise/per-contrast intervals; "
    "adaptive repeated checks do not guarantee sequential or simultaneous 95% coverage. "
    "These comparisons concern SRIM "
    "transport and energy loss; impurity-driven electron attachment, charge "
    "collection and optical response are outside this calculation."
)
STOPPING_NOTE = (
    "Se/(Se+Sn) and Sn/(Se+Sn) are instantaneous electronic and nuclear stopping "
    "fractions for the moving primary projectile in the SRModule table. They "
    "are not full-cascade QFs: energy transferred to nuclear recoils can later "
    "feed secondary electronic deposition. Invalid or zero total stopping "
    "leaves both fractions null."
)
EVENT_NOTE = (
    "The event CSV contains endpoint and sampled-primary-path records. It does "
    "not contain per-event charge or electronic-energy resolution. Aggregate "
    "IONIZ profiles cannot determine an eventwise QF or its intrinsic eventwise "
    "distribution; batch-mean uncertainties are a separate quantity."
)
SYSTEMATICS_NOTE = (
    "Planned recoil-cutoff tests compare 10/21/30 eV transport thresholds; "
    "these are model settings, not measured gas charge thresholds. Sub-threshold "
    "recoils enter phonon bookkeeping; energy closure does not establish the "
    "physical low-energy charge partition or zero-cutoff convergence. Same-seed "
    "coarse/fine EXYZ tests assess sampled-primary-polyline sensitivity. Equal "
    "actual increments at the 1 eV output floor provide no refinement test. "
    "Even a passed sampling diagnostic does not establish an exact primary or "
    "total-cascade path length. Availability and qualification follow the "
    "recorded validation checks, not the existence of a planned test."
)
ARCHIVE_NOTE = (
    "Raw-archive checks compare member content hashes with the recorded batch "
    "and archive-manifest SHA256 maps. Integrity checks establish preservation "
    "of the recorded files; they do not establish detector calibration or "
    "physical model validity. The numerical export links raw archive manifests "
    "and available source-revision manifests. Historical batches without a "
    "recorded source revision retain an unknown revision; their raw inputs "
    "remain archived."
)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if str(value).strip().lower() in {"true", "1", "yes"}:
        return True
    if str(value).strip().lower() in {"false", "0", "no"}:
        return False
    return None


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _load_json(path: Path, default: Any, problems: list[str]) -> Any:
    if not path.is_file():
        problems.append(f"Missing input: {path.name}.")
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        problems.append(f"Could not read {path.name}: {exc}.")
        return default


def _provenance_references(study_dir: Path, problems: list[str]) -> dict[str, Any]:
    """Link existing provenance without assigning current code to old batches."""
    manifests = {
        path.parent.name: f"../{path.relative_to(study_dir).as_posix()}"
        for path in sorted((study_dir / "provenance" / "source_revisions").glob("*/manifest.json"))
        if path.is_file()
    }
    batches = []
    for path in sorted((study_dir / "batches").glob("*.json")):
        batch = _load_json(path, {}, problems)
        if not isinstance(batch, dict):
            problems.append(f"Batch metadata is not an object: {path.name}.")
            continue
        revision = batch.get("source_revision") or None
        archive = batch.get("raw_archive")
        archive_reference = "../" + archive.replace("\\", "/") if isinstance(archive, str) and archive else None
        batches.append({
            "job_id": batch.get("job", {}).get("job_id"),
            "batch_metadata": f"../{path.relative_to(study_dir).as_posix()}",
            "raw_archive": archive_reference,
            "raw_manifest": {"archive": archive_reference, "member": "manifest.json"} if archive_reference else None,
            "source_revision": revision,
            "source_revision_status": "recorded" if revision else "unknown",
            "source_revision_manifest": manifests.get(revision),
        })
    current = study_dir / "provenance" / "current_source_revision.json"
    return {
        "current_source_revision": f"../{current.relative_to(study_dir).as_posix()}" if current.is_file() else None,
        "source_revision_manifests": [{"revision": revision, "path": path} for revision, path in manifests.items()],
        "raw_batch_manifests": batches,
        "srmodule_manifests": [f"../{path.relative_to(study_dir).as_posix()}"
                               for path in sorted((study_dir / "raw" / "srmodule").glob("*/manifest.json"))
                               if path.is_file()],
        "unknown_source_revision_batches": sum(row["source_revision"] is None for row in batches),
        "interpretation": "References only. A current source snapshot does not identify historical batch code; "
                          "batches predating recorded source revisions retain null revisions and their archived raw inputs.",
    }


def _read_table(path: Path, problems: list[str]) -> list[dict[str, Any]]:
    if not path.is_file():
        problems.append(f"Missing input: {path.name}.")
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for raw in csv.DictReader(handle):
                row: dict[str, Any] = {}
                for key, value in raw.items():
                    if key is None:
                        continue
                    if value is None or value.strip().lower() in {"", "null", "none", "nan", "inf", "+inf", "-inf", "infinity", "-infinity"}:
                        row[key] = None
                    elif value.strip().lower() in {"true", "false"}:
                        row[key] = value.strip().lower() == "true"
                    else:
                        number = _number(value)
                        row[key] = number if number is not None else value
                rows.append(row)
    except (OSError, csv.Error, UnicodeError) as exc:
        problems.append(f"Could not completely read {path.name}: {exc}.")
    return rows


def _validation_entries(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        for key in ("entries", "checks", "results", "validation"):
            if isinstance(value.get(key), list):
                return _validation_entries(value[key])
        if any(key in value for key in ("severity", "check", "message")):
            return [value]
    return []


def _status(metadata: Any, summary: list[dict[str, Any]], validation: Any,
            problems: list[str]) -> tuple[str, list[str]]:
    notes = list(problems)
    numeric = [row for row in summary if _number(row.get("qf_mean")) is not None]
    expected = _number(metadata.get("expected_summary_rows")) if isinstance(metadata, dict) else None
    production_expected = _number(validation.get("production_expected_batches")) if isinstance(validation, dict) else None
    production_completed = _number(validation.get("production_completed_batches")) if isinstance(validation, dict) else None
    pending = (expected is not None and len(numeric) < expected) or (
        isinstance(validation, dict) and validation.get("status") == "incomplete"
        and production_expected is not None and production_completed is not None
        and production_completed < production_expected)
    pending_status = "INCOMPLETE — SIMULATIONS / VALIDATION PENDING"
    if not numeric:
        return pending_status if pending else "NO TRIM RESULTS", notes + ["No numerical TRIM QF rows are available."]
    entries = _validation_entries(validation)
    failed = [row for row in entries
              if str(row.get("severity", row.get("status", ""))).lower() in
              {"failure", "failed", "error", "fail"} or
              (row.get("passed") is False and str(row.get("severity", "")).lower()
               not in {"limitation", "warning", "info", "informational"})]
    limitations = [row for row in entries if str(row.get("severity", "")).lower() == "limitation"]
    warnings = [row for row in entries if str(row.get("severity", "")).lower() == "warning"]
    if isinstance(validation, dict) and validation.get("passed") is False:
        failed.append(validation)
    clipped = sum(_boolean(row.get("clipped")) is True for row in numeric)
    unconverged = sum(_boolean(row.get("statistical_converged")) is not True for row in numeric)
    if clipped:
        notes.append(f"{clipped} TRIM rows are flagged as clipped.")
    if unconverged:
        notes.append(f"{unconverged} TRIM rows do not yet have confirmed statistical convergence.")
    if failed:
        label = "pending or failed checks" if pending else "validation failure entries"
        notes.append(f"{len(failed)} {label} are recorded; see individual check details.")
    complete = isinstance(metadata, dict) and (
        metadata.get("complete") is True or metadata.get("run_complete") is True
        or str(metadata.get("status", "")).lower() in {"complete", "completed"}
    )
    validation_complete = isinstance(validation, dict) and validation.get("status") == "complete"
    complete = complete or validation_complete
    if expected is not None:
        complete = len(numeric) == int(expected)
        if not complete:
            notes.append(f"Numerical TRIM rows: {len(numeric)} of {int(expected)} expected.")
    if clipped:
        return "VALIDATION NEEDS REVIEW", notes
    if pending:
        return pending_status, notes
    if failed:
        return "VALIDATION NEEDS REVIEW", notes
    if unconverged or problems:
        return "PARTIAL / NOT STATISTICALLY QUALIFIED", notes
    if not complete:
        notes.append("Full-grid completeness is not confirmed by the supplied metadata.")
        return "PARTIAL / COMPLETENESS UNCONFIRMED", notes
    if not isinstance(validation, dict) or not (validation.get("passed") is True or validation_complete):
        notes.append("An explicit overall validation pass is not recorded.")
        return "RESULTS AVAILABLE / VALIDATION UNCONFIRMED", notes
    if limitations:
        notes.append(f"{len(limitations)} model or calibration limitations remain explicitly unresolved.")
        return "SIMULATION COMPLETE / MODEL LIMITATIONS REMAIN", notes
    if warnings:
        notes.append(f"{len(warnings)} validation warnings remain.")
        return "SIMULATION COMPLETE / VALIDATION WARNINGS", notes
    return "COMPLETE WITH RECORDED VALIDATION", notes


def _groups(rows: Iterable[dict[str, Any]]) -> list[tuple[tuple[str, str], list[dict[str, Any]]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row.get("medium", "unknown")), str(row.get("species", "unknown")))].append(row)
    order = {value: index for index, value in enumerate(SPECIES)}
    return sorted(grouped.items(), key=lambda item: (order.get(item[0][1], 99), item[0][0]))


def _style(medium: str, species: str) -> dict[str, Any]:
    return {"color": COLORS.get(species, "#555555"),
            "linestyle": "--" if medium == "contaminated" else "-",
            "marker": "s" if medium == "contaminated" else "o",
            "markersize": 3.5, "linewidth": 1.1,
            "label": f"{species} · {medium}"}


def _empty(ax: Any, text: str = "No numerical data available") -> None:
    ax.text(0.5, 0.5, text, ha="center", va="center", wrap=True, transform=ax.transAxes)


def _finish_axes(ax: Any) -> None:
    ax.grid(True, which="both", alpha=0.22)
    handles, labels = ax.get_legend_handles_labels()
    if handles:
        ax.legend(handles, labels, fontsize=7, ncol=2, loc="best")


def _curve_figure(summary: list[dict[str, Any]], xkey: str, ykey: str,
                  xlabel: str, ylabel: str, title: str, sem: str | None = None) -> Any:
    fig, ax = plt.subplots(figsize=(9.0, 5.4), layout="constrained")
    plotted = False
    positive_y: list[float] = []
    for (medium, species), rows in _groups(summary):
        points = [(float(row[xkey]), float(row[ykey]), row) for row in rows
                  if _number(row.get(xkey)) is not None and _number(row.get(ykey)) is not None
                  and float(row[xkey]) > 0]
        points.sort(key=lambda item: item[0])
        if not points:
            continue
        x, y = [p[0] for p in points], [p[1] for p in points]
        ax.plot(x, y, **_style(medium, species))
        for xx, yy, row in points:
            error = _number(row.get(sem)) if sem else None
            xerror = _number(row.get("eee_srim_kev_sem")) if xkey == "eee_srim_kev_mean" else None
            if (error is not None and error >= 0) or (xerror is not None and xerror >= 0):
                ax.errorbar(xx, yy, yerr=error if error is not None and error >= 0 else None,
                            xerr=xerror if xerror is not None and xerror >= 0 else None,
                            fmt="none", color=COLORS.get(species, "#555555"), alpha=0.65,
                            linewidth=0.8, capsize=2)
            if _boolean(row.get("clipped")) is True:
                ax.scatter([xx], [yy], marker="x", s=65, color="#B00020", zorder=5)
        positive_y.extend(y)
        plotted = True
    ax.set(xlabel=xlabel, ylabel=ylabel, title=title)
    if plotted:
        ax.set_xscale("log")
        if ykey == "projected_range_mm_mean" and positive_y and min(positive_y) > 0:
            ax.set_yscale("log")
    else:
        _empty(ax)
    _finish_axes(ax)
    return fig


def _event_field_units(fields: list[str]) -> dict[str, str | None]:
    """Keep native absolute coordinates distinct from converted displacements."""
    return {field: ("angstrom" if field.endswith("_angstrom") else "mm" if field.endswith("_mm")
                    else "keV" if field == "energy_kevnr" else "1" if field in {"batch", "ion_id"}
                    else None) for field in fields}


def _event_data(path: Path, problems: list[str]) -> tuple[dict[tuple[str, str, float], dict[str, list[float]]], int, list[str]]:
    result: dict[tuple[str, str, float], dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    if not path.is_file():
        problems.append("Missing input: events.csv.gz; event distributions are unavailable.")
        return result, 0, []
    count = 0
    fields: list[str] = []
    aliases = {
        "radius": ("endpoint_displacement_3d_mm", "endpoint_radius_mm", "endpoint_r_mm", "radial_endpoint_mm", "endpoint_radius_mm_mean"),
        "path": ("primary_sampled_path_mm", "sampled_primary_path_mm", "sampled_path_mm", "primary_path_mm"),
    }
    try:
        with gzip.open(path, "rt", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            fields = list(reader.fieldnames or [])
            for row in reader:
                count += 1
                energy = _number(row.get("energy_kevnr", row.get("energy_kev")))
                if energy is None:
                    continue
                key = (row.get("medium", "unknown"), row.get("species", row.get("ion", "unknown")), energy)
                for name, candidates in aliases.items():
                    value = next((_number(row.get(field)) for field in candidates
                                  if _number(row.get(field)) is not None), None)
                    if value is not None and value >= 0:
                        result[key][name].append(value)
    except (OSError, EOFError, csv.Error, UnicodeError) as exc:
        problems.append(f"Could not completely read events.csv.gz: {exc}.")
    return result, count, fields


def _distribution_figure(events: dict[Any, Any], summary: list[dict[str, Any]]) -> tuple[Any, str]:
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.8), layout="constrained")
    available: dict[float, set[tuple[str, str]]] = defaultdict(set)
    for (medium, species, energy), values in events.items():
        if values.get("radius") or values.get("path"):
            available[energy].add((medium, species))
    energy = max(available, key=lambda value: (len(available[value]), value)) if available else None
    description = (
        f"Event distributions at {energy:g} keVnr (the highest energy among those with widest species/media coverage). "
        if energy is not None else "Event distributions are unavailable. "
    )
    for ax, quantity, field, xlabel in (
        (axes[0], "radius", "endpoint_radius_mm_mean", "Endpoint displacement magnitude [mm]"),
        (axes[1], "path", "primary_sampled_path_mm_mean", "Sampled primary trajectory length [mm]"),
    ):
        plotted = False
        if energy is not None:
            selected = [(key, value[quantity]) for key, value in events.items()
                        if key[2] == energy and value.get(quantity)]
            for (medium, species, _), values in selected:
                if len(values) < 2:
                    continue
                style = _style(medium, species)
                ax.hist(values, bins="auto", density=True, histtype="step", linewidth=1.4,
                        color=style["color"], linestyle=style["linestyle"],
                        label=f"{style['label']} (n={len(values)})")
                plotted = True
        if plotted:
            ax.set(xlabel=xlabel, ylabel="Probability density [1/mm]", title=f"Recorded events · {energy:g} keVnr")
        else:
            for (medium, species), rows in _groups(summary):
                points = sorted((float(row["energy_kevnr"]), float(row[field])) for row in rows
                                if _number(row.get("energy_kevnr")) is not None
                                and _number(row.get(field)) is not None and float(row["energy_kevnr"]) > 0)
                if points:
                    ax.plot([p[0] for p in points], [p[1] for p in points], **_style(medium, species))
                    plotted = True
            ax.set(xlabel="Recoil energy [keVnr]", ylabel=xlabel,
                   title="Reported means · event distribution unavailable")
            if plotted:
                ax.set_xscale("log")
            else:
                _empty(ax, "No event distribution or reported mean available")
        _finish_axes(ax)
    return fig, description + LENGTH_NOTE


def _energy_figure(summary: list[dict[str, Any]]) -> Any:
    fig, axes = plt.subplots(1, 3, figsize=(13, 4.6), sharey=True, layout="constrained")
    for ax, field, title in zip(axes,
            ("primary_ionization_kev_mean", "secondary_ionization_kev_mean", "phonon_kev_mean"),
            ("Primary electronic", "Secondary electronic", "SRIM phonon bookkeeping")):
        plotted = False
        for (medium, species), rows in _groups(summary):
            points = sorted((float(row["energy_kevnr"]), float(row[field]) / float(row["energy_kevnr"]), row)
                            for row in rows if _number(row.get("energy_kevnr")) is not None
                            and float(row["energy_kevnr"]) > 0 and _number(row.get(field)) is not None)
            if points:
                ax.plot([p[0] for p in points], [p[1] for p in points], **_style(medium, species))
                for energy, fraction, row in points:
                    sem = _number(row.get(field.removesuffix("_mean") + "_sem"))
                    if sem is not None and sem >= 0:
                        ax.errorbar(energy, fraction, yerr=sem / energy, fmt="none",
                                    color=COLORS.get(species, "#555555"), alpha=0.6, capsize=2, linewidth=0.8)
                plotted = True
        ax.set(title=title, xlabel="Recoil energy [keVnr]")
        if plotted:
            ax.set_xscale("log")
        else:
            _empty(ax)
        _finish_axes(ax)
    axes[0].set_ylabel("Mean channel energy / initial recoil energy")
    return fig


def _stopping_partitions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        electronic = _number(row.get("electronic_stopping_mev_mg_cm2"))
        nuclear = _number(row.get("nuclear_stopping_mev_mg_cm2"))
        valid = electronic is not None and nuclear is not None and electronic >= 0 and nuclear >= 0
        total = electronic + nuclear if valid else None
        valid = valid and total is not None and math.isfinite(total) and total > 0
        result.append({"medium": row.get("medium"), "species": row.get("species"),
                       "energy_kevnr": row.get("energy_kevnr"),
                       "electronic_stopping_fraction": electronic / total if valid else None,
                       "nuclear_stopping_fraction": nuclear / total if valid else None,
                       "method": "Instantaneous primary SRModule stopping partition; not cascade QF"})
    return result


def _stopping_figure(partitions: list[dict[str, Any]]) -> Any:
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.8), sharey=True, layout="constrained")
    for ax, field, title in (
        (axes[0], "electronic_stopping_fraction", "Primary electronic stopping: Se / (Se + Sn)"),
        (axes[1], "nuclear_stopping_fraction", "Primary nuclear stopping: Sn / (Se + Sn)"),
    ):
        plotted = False
        for (medium, species), rows in _groups(partitions):
            points = sorted((float(row["energy_kevnr"]), float(row[field])) for row in rows
                            if _number(row.get("energy_kevnr")) is not None and float(row["energy_kevnr"]) > 0
                            and _number(row.get(field)) is not None)
            if points:
                ax.plot([p[0] for p in points], [p[1] for p in points], **_style(medium, species))
                plotted = True
        ax.set(xlabel="Primary projectile energy [keV]", title=title, ylim=(-0.02, 1.02))
        if plotted:
            ax.set_xscale("log")
        else:
            _empty(ax, "No finite, nonnegative stopping table available")
        _finish_axes(ax)
    axes[0].set_ylabel("Instantaneous primary stopping fraction")
    return fig


def _impurity_figure(comparison: list[dict[str, Any]]) -> Any:
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 5.3), layout="constrained")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in comparison:
        grouped[str(row.get("species", "unknown"))].append(row)
    for ax, value_field, error_field, scale, title, ylabel in (
        (axes[0], "qf_difference", "qf_difference_sem", 1.0,
         "Paired QF difference", "QF(contaminated) − QF(baseline)"),
        (axes[1], "range_relative_change", "range_relative_change_sem", 100.0,
         "Relative projected-range change", "100 × [range(contaminated) / range(baseline) − 1] [%]"),
    ):
        plotted = False
        for species, rows in sorted(grouped.items(), key=lambda item: SPECIES.index(item[0]) if item[0] in SPECIES else 99):
            points = sorted((float(row["energy_kevnr"]), row) for row in rows
                            if _number(row.get("energy_kevnr")) is not None and float(row["energy_kevnr"]) > 0
                            and _number(row.get(value_field)) is not None)
            for index, (energy, row) in enumerate(points):
                value = float(row[value_field]) * scale
                error = _number(row.get(error_field))
                has_error = error is not None and error >= 0
                classification_resolved = (row.get("impurity_classification") in (
                    "equivalent_within_material_boundary", "material_increase", "material_decrease")
                    if "impurity_classification" in row
                    else _boolean(row.get("impurity_equivalence_resolved")) is True)
                resolved = has_error and (classification_resolved if value_field == "qf_difference" else True)
                color = COLORS.get(species, "#555555")
                ax.errorbar(energy, value, yerr=error * scale if has_error else None,
                            fmt="o", markersize=4, markerfacecolor=color if resolved else "none",
                            markeredgecolor=color, color=color, capsize=2, elinewidth=0.9,
                            label=species if index == 0 else None)
                plotted = True
        ax.axhline(0, color="#777777", linewidth=0.8)
        ax.set(xlabel="Recoil energy [keVnr]", ylabel=ylabel, title=title)
        if plotted:
            ax.set_xscale("log")
        else:
            _empty(ax, "No paired contamination comparison available")
        _finish_axes(ax)
    fig.supxlabel("Error bars: supplied ±1 SEM. Hollow QF markers: classification unresolved or unavailable.\n"
                  "Hollow range markers: uncertainty unavailable; the plotted shift is not proof of a negligible effect.",
                  fontsize=8)
    return fig


def _fmt(value: Any) -> str:
    number = _number(value)
    if number is not None:
        return f"{number:.6g}"
    if value is None:
        return "unavailable"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _markdown_table(rows: list[dict[str, Any]], fields: list[str], limit: int = 60) -> str:
    if not rows:
        return "No rows available.\n"
    output = ["| " + " | ".join(fields) + " |", "| " + " | ".join("---" for _ in fields) + " |"]
    for row in rows[:limit]:
        output.append("| " + " | ".join(_fmt(row.get(field)).replace("|", "\\|").replace("\n", " ")
                                         for field in fields) + " |")
    if len(rows) > limit:
        output += ["", f"Showing {limit} of {len(rows)} rows; the numerical export retains every row."]
    return "\n".join(output) + "\n"


def _html_table(rows: list[dict[str, Any]], fields: list[str], limit: int = 100) -> str:
    if not rows:
        return "<p>No rows available.</p>"
    header = "".join(f"<th scope='col'>{html.escape(field)}</th>" for field in fields)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(_fmt(row.get(field)))}</td>" for field in fields)
                   + "</tr>" for row in rows[:limit])
    note = f"<p>Showing {limit} of {len(rows)} rows; numerical export retains every row.</p>" if len(rows) > limit else ""
    return f"<div class='table-scroll'><table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table></div>{note}"


def _pdf_text(pdf: PdfPages, title: str, paragraphs: list[str]) -> None:
    lines: list[str] = []
    for paragraph in paragraphs:
        lines.extend(textwrap.wrap(paragraph, width=100, break_long_words=True) or [""])
        lines.append("")
    for page in range(0, max(len(lines), 1), 55):
        fig = plt.figure(figsize=(8.27, 11.69))
        fig.text(0.07, 0.95, title, fontsize=15, weight="bold", va="top")
        fig.text(0.07, 0.91, "\n".join(lines[page:page + 55]), fontsize=9.2, va="top", linespacing=1.45)
        pdf.savefig(fig)
        plt.close(fig)


def build_report(study_dir: Path) -> dict[str, Any]:
    """Write self-contained study reports, preserving missing data as null.

    Returned artifact paths are absolute. Links inside the HTML, Markdown, and
    JSON export are relative to the report directory so the study is portable.
    """
    study_dir = Path(study_dir).resolve()
    report_dir = study_dir / "report"
    figures_dir = report_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    metadata = _load_json(study_dir / "metadata.json", {}, problems)
    validation = _load_json(study_dir / "validation.json", {}, problems)
    summary = _read_table(study_dir / "summary.csv", problems)
    srmodule = _read_table(study_dir / "srmodule.csv", problems)
    stopping_partitions = _stopping_partitions(srmodule)
    comparison = _read_table(study_dir / "comparison.csv", problems)
    optional_notes: list[str] = []
    systematics_csv = (_read_table(study_dir / "systematics.csv", optional_notes)
                       if (study_dir / "systematics.csv").is_file() else None)
    systematics = (_load_json(study_dir / "systematics.json", None, optional_notes)
                   if (study_dir / "systematics.json").is_file() else None)
    archive_validation = (_load_json(study_dir / "archive_validation.json", None, optional_notes)
                          if (study_dir / "archive_validation.json").is_file() else None)
    stopping_comparison = (_load_json(study_dir / "srmodule_medium_comparison.json", None, optional_notes)
                           if (study_dir / "srmodule_medium_comparison.json").is_file() else None)
    impurity_diagnostics = (_load_json(study_dir / "impurity_diagnostics.json", None, optional_notes)
                            if (study_dir / "impurity_diagnostics.json").is_file() else None)
    stopping_summary = stopping_comparison.get("species_summary", []) if isinstance(stopping_comparison, dict) else []
    stopping_note = ("Exact native SRModule energy pairs in the configured recoil-energy interval. "
                     "Relative changes are contaminated/baseline minus one. Values are deterministic predictions "
                     "with unassigned model uncertainty and finite printed precision; they do not establish "
                     "full-cascade QF or physical charge equivalence.")
    stopping_summary_fields = ["species", "paired_native_points", "electronic_stopping_mev_mg_cm2_max_abs_relative_change",
                               "nuclear_stopping_mev_mg_cm2_max_abs_relative_change", "projected_range_mm_max_abs_relative_change",
                               "longitudinal_straggling_mm_max_abs_relative_change", "lateral_straggling_mm_max_abs_relative_change",
                               "srmodule_screen_passed"]
    provenance_references = _provenance_references(study_dir, optional_notes)
    systematics_rows = systematics_csv if systematics_csv is not None else (
        systematics.get("rows", []) if isinstance(systematics, dict) else [])
    if not isinstance(systematics_rows, list):
        optional_notes.append("Systematics rows are not a table; inspect the original JSON.")
        systematics_rows = []
    systematics_rows = [row for row in systematics_rows if isinstance(row, dict)]
    systematics_checks = _validation_entries(systematics)
    archive_checks = _validation_entries(archive_validation)
    optional_files = [name for name in ("systematics.csv", "systematics.json", "archive_validation.json", "impurity_diagnostics.json")
                      if (study_dir / name).is_file()]
    systematics_inventory = (
        f"Supplied systematics rows: {len(systematics_rows)}; recorded systematics checks: {len(systematics_checks)}. "
        + ("Archive-validation data supplied." if archive_validation is not None
           else "Raw-archive validation has not been supplied.")
    )
    systematics_views = [
        ("Cutoff comparisons", [row for row in systematics_rows if row.get("comparison") == "threshold_vs_nominal"],
         ["species", "energy_kevnr", "displacement_energy_ev", "reference_displacement_energy_ev",
          "qf_difference", "qf_difference_sem", "qf_relative_change", "complete"]),
        ("EXYZ sampling comparisons", [row for row in systematics_rows if row.get("comparison") == "track_refinement"],
         ["species", "energy_kevnr", "coarse_exyz_steps_ev", "fine_exyz_steps_ev", "n_paired_batches",
          "paired_relative_path_change_mean", "paired_relative_path_change_sem", "actual_sampling_refinement",
          "sampled_polyline_convergence_resolved"]),
        ("Systematic batch summaries", [row for row in systematics_rows if row.get("comparison") == "group"],
         ["stage", "species", "energy_kevnr", "displacement_energy_ev", "exyz_steps_ev",
          "n_valid_batches", "expected_batches", "qf_mean", "qf_sem", "complete"]),
    ]
    events, event_count, event_fields = _event_data(study_dir / "events.csv.gz", problems)
    status, notes = _status(metadata, summary, validation, problems)
    validation_rows = _validation_entries(validation)
    gas_conditions = metadata.get("gas_conditions", []) if isinstance(metadata, dict) else []
    if isinstance(gas_conditions, dict):
        gas_conditions = [{"medium": key, **value} if isinstance(value, dict)
                          else {"medium": key, "condition": value} for key, value in gas_conditions.items()]
    gas_conditions = [row for row in gas_conditions if isinstance(row, dict)] if isinstance(gas_conditions, list) else []

    paths = {"html": report_dir / "index.html", "markdown": report_dir / "technical_report.md",
             "pdf": report_dir / "technical_report.pdf", "json": report_dir / "numerical_export.json"}
    methodology = ""
    for candidate in (study_dir / "research" / "methodology.md", study_dir / "methodology.md",
                      Path(__file__).resolve().parents[1] / "docs" / "cygno_srim_methodology.md",
                      Path(__file__).resolve().parents[1] / "research" / "methodology.md"):
        if candidate.is_file():
            methodology = candidate.read_text(encoding="utf-8")
            (report_dir / "methodology.md").write_text(methodology, encoding="utf-8")
            break

    inventory = f"Available inputs: {len(summary)} TRIM summary rows, {len(srmodule)} SRModule rows, {len(comparison)} comparison rows, {event_count} event records."
    intro = [f"Status: {status}.", inventory, *notes, CALIBRATION_NOTE, LENGTH_NOTE, ENERGY_NOTE,
             STOPPING_NOTE, EVENT_NOTE,
             UNCERTAINTY_NOTE, "Historical data and new simulation results must retain their recorded provenance. "
             "No missing Monte Carlo point is replaced or extrapolated by this reporter."]
    specs = [
        ("qf_vs_recoil", "QF versus recoil energy", "energy_kevnr", "qf_mean", "Recoil energy [keVnr]", "SRIM electronic-energy fraction QF", "qf_sem"),
        ("qf_vs_eee_srim", "QF versus electronic-energy surrogate", "eee_srim_kev_mean", "qf_mean", "Eee_Srim [keV; energy-only surrogate]", "SRIM electronic-energy fraction QF", "qf_sem"),
        ("projected_range_vs_recoil", "Projected range versus recoil energy", "energy_kevnr", "projected_range_mm_mean", "Recoil energy [keVnr]", "Mean projected range [mm]", "projected_range_mm_sem"),
        ("projected_range_vs_eee_srim", "Projected range versus electronic-energy surrogate", "eee_srim_kev_mean", "projected_range_mm_mean", "Eee_Srim [keV; energy-only surrogate]", "Mean projected range [mm]", "projected_range_mm_sem"),
    ]
    figures: list[dict[str, Any]] = []
    with plt.rc_context({"font.family": "DejaVu Sans", "font.size": 10, "savefig.dpi": 170}), PdfPages(paths["pdf"]) as pdf:
        _pdf_text(pdf, "CYGNO gas-recoil SRIM study", intro)

        def save_figure(fig: Any, stem: str, title: str, caption: str) -> None:
            fig.suptitle(f"{status}\n{title}", fontsize=11)
            png, vector = figures_dir / f"{stem}.png", figures_dir / f"{stem}.pdf"
            fig.savefig(png, bbox_inches="tight")
            fig.savefig(vector, bbox_inches="tight")
            pdf.savefig(fig, bbox_inches="tight")
            plt.close(fig)
            figures.append({"title": title, "png": f"figures/{png.name}", "pdf": f"figures/{vector.name}", "caption": caption})

        for stem, title, xkey, ykey, xlabel, ylabel, sem in specs:
            fig = _curve_figure(summary, xkey, ykey, xlabel, ylabel, "", sem)
            save_figure(fig, stem, title, UNCERTAINTY_NOTE + " " + CALIBRATION_NOTE
                        + (" Red crosses mark clipped points." if any(_boolean(row.get("clipped")) is True for row in summary) else ""))
        fig, caption = _distribution_figure(events, summary)
        save_figure(fig, "endpoint_and_sampled_path", "Endpoint displacement and sampled primary trajectory", caption)
        save_figure(_energy_figure(summary), "energy_channels", "Recorded energy partition", ENERGY_NOTE)
        save_figure(_stopping_figure(stopping_partitions), "instantaneous_stopping_fractions",
                    "Primary stopping fractions · distinct from full-cascade QF", STOPPING_NOTE)
        save_figure(_impurity_figure(comparison), "impurity_comparison",
                    "Contamination comparison with recorded uncertainty",
                    IMPURITY_NOTE + " QF bars show paired-batch ±1 SEM, not a 95% confidence interval. "
                    "Hollow QF markers identify unresolved or unavailable classification against the configured criterion. "
                    "Range uncertainties are drawn only when explicitly supplied.")
        numerical_notes = ["The complete numerical tables, configuration, gas fractions, and input hashes are retained in numerical_export.json."]
        for condition in gas_conditions:
            numerical_notes.append("Gas condition: " + "; ".join(f"{key}={_fmt(value)}" for key, value in condition.items()))
        if stopping_summary:
            numerical_notes.append("SRModule stopping-medium impurity comparison: " + stopping_note)
            for row in stopping_summary:
                numerical_notes.append("; ".join(f"{key}={_fmt(row.get(key))}" for key in stopping_summary_fields))
        if comparison:
            numerical_notes.extend(["Contamination comparisons:", IMPURITY_NOTE])
            for row in comparison[:24]:
                numerical_notes.append(
                    f"{_fmt(row.get('species'))}, {_fmt(row.get('energy_kevnr'))} keVnr: "
                    f"QF change={_fmt(row.get('qf_relative_change'))}; "
                    f"QF difference={_fmt(row.get('qf_difference'))} +/- {_fmt(row.get('qf_difference_sem'))}; "
                    f"range change={_fmt(row.get('range_relative_change'))}."
                )
            if len(comparison) > 24:
                numerical_notes.append(f"Showing 24 of {len(comparison)} comparison rows. Every row remains in the JSON and CSV tables.")
        else:
            numerical_notes.append("No numerical contamination comparison is available.")
        numerical_notes.extend(["Systematics and archive integrity:", SYSTEMATICS_NOTE, systematics_inventory,
                                ARCHIVE_NOTE, *optional_notes])
        for check in [*systematics_checks, *archive_checks][:8]:
            numerical_notes.append(f"{_fmt(check.get('check'))}: {_fmt(check.get('severity'))}; "
                                   f"{_fmt(check.get('detail', check.get('message')))}")
        if len(systematics_checks) + len(archive_checks) > 8:
            numerical_notes.append("Only the first eight systematic/archive checks are listed here; the complete checks and rows are in numerical_export.json and the original JSON files.")
        _pdf_text(pdf, "Numerical inputs and comparison", numerical_notes)
        if methodology:
            # Keep the full sourced note in the portable Markdown copy; the PDF
            # includes a bounded readable excerpt and points to that full copy.
            excerpt = re.sub(r"\[([^]]+)\]\(([^)]+)\)", r"\1 (\2)", methodology)
            _pdf_text(pdf, "Methodology and source notes", [
                "Source: methodology.md, included alongside this PDF. Historical benchmark or detector statements retain the qualification of that source.",
                excerpt[:2000],
                "See the complete methodology.md for the full notes and source list."
            ])

    comparison_fields = [field for field in ("medium", "species", "energy_kevnr", "qf_relative_change",
                         "qf_difference", "qf_difference_sem", "range_relative_change", "n_pairs", "impurity_classification")
                         if any(field in row for row in comparison)]
    summary_fields = ["medium", "species", "energy_kevnr", "qf_mean", "qf_sem", "eee_srim_kev_mean",
                      "projected_range_mm_mean", "n_ions_total", "n_batches", "clipped", "statistical_converged"]
    gas_fields = list(dict.fromkeys(key for row in gas_conditions for key in row))
    validation_fields = list(dict.fromkeys(key for row in validation_rows for key in row))
    systematic_check_fields = list(dict.fromkeys(key for row in systematics_checks for key in row))
    archive_check_fields = list(dict.fromkeys(key for row in archive_checks for key in row))
    markdown = ["# CYGNO gas-recoil SRIM study", f"**Status: {status}.**", *intro[1:],
                "## Recorded gas conditions", _markdown_table(gas_conditions, gas_fields),
                "## Numerical summary", _markdown_table(summary, summary_fields),
                "## SRModule stopping-medium comparison", stopping_note, _markdown_table(stopping_summary, stopping_summary_fields),
                "## Contamination comparison", IMPURITY_NOTE, _markdown_table(comparison, comparison_fields),
                "## Validation", _markdown_table(validation_rows, validation_fields),
                "## Cutoff and trajectory-sampling systematics", SYSTEMATICS_NOTE, systematics_inventory]
    if optional_files:
        markdown.append(" · ".join(f"[{name}](../{name})" for name in optional_files))
    else:
        markdown.append("Optional systematic/archive files are not yet available.")
    markdown.extend(optional_notes)
    for title, rows, fields in systematics_views:
        if rows:
            markdown.extend([f"### {title}", _markdown_table(rows, fields, limit=20)])
    markdown.extend(["### Recorded systematics checks", _markdown_table(systematics_checks, systematic_check_fields, limit=30),
                     "### Raw-archive integrity", ARCHIVE_NOTE, _markdown_table(archive_checks, archive_check_fields),
                     "## Figures"])
    for figure in figures:
        markdown.extend([f"### {figure['title']}", f"![{figure['title']}]({figure['png']})", figure["caption"],
                         f"[Vector PDF]({figure['pdf']})"])
    markdown.extend(["## Portable numerical data", "[Numerical JSON](numerical_export.json) preserves every supplied table row and metadata field. "
                     "The original event table remains at [events.csv.gz](../events.csv.gz).", EVENT_NOTE])
    if methodology:
        markdown.extend(["## Methodology and sources", "Full sourced notes, equations and primary references: [methodology.md](methodology.md)."])
    else:
        markdown.extend(["## Methodology and sources", "No research/methodology.md was available when this report was built. "
                         "A literature comparison and detector-response qualification are not supplied by this reporter."])
    paths["markdown"].write_text("\n\n".join(markdown) + "\n", encoding="utf-8")

    figures_html = "".join(f"<figure><h3>{html.escape(figure['title'])}</h3>"
                           f"<a href='{figure['pdf']}'><img src='{figure['png']}' alt='{html.escape(figure['title'], quote=True)}'></a>"
                           f"<figcaption>{html.escape(figure['caption'])} <a href='{figure['pdf']}'>Vector PDF</a></figcaption></figure>"
                           for figure in figures)
    sections = "".join(f"<p>{html.escape(paragraph)}</p>" for paragraph in intro[1:])
    source_html = ("<p><a href='methodology.md'>Full methodology and source notes</a></p>"
                   f"<details><summary>Read sourced methodology notes</summary><div class='source-notes'>{html.escape(methodology)}</div></details>" if methodology
                   else "<p>No research/methodology.md was supplied. Literature and detector qualification remain unresolved.</p>")
    optional_links_html = (" · ".join(f"<a href='../{name}'>{name}</a>" for name in optional_files)
                           if optional_files else "Optional systematic/archive files are not yet available.")
    systematic_tables_html = "".join(f"<h3>{html.escape(title)}</h3>{_html_table(rows, fields, limit=20)}"
                                    for title, rows, fields in systematics_views if rows)
    optional_notes_html = "".join(f"<p>{html.escape(note)}</p>" for note in optional_notes)
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CYGNO SRIM study — {html.escape(status)}</title><style>
body{{font:16px/1.55 system-ui,sans-serif;color:#182838;background:#f5f7fa;margin:0}}main{{max-width:1160px;margin:auto;padding:28px}}
h1,h2,h3{{line-height:1.2}}.status{{padding:16px;background:#fff0cf;border-left:6px solid #a85b00;font-weight:700}}
nav a{{display:inline-block;margin:0 16px 8px 0}}figure{{margin:24px 0;padding:18px;background:white;border:1px solid #d8dfe6}}
img{{display:block;width:100%;height:auto}}figcaption{{font-size:14px;color:#34495e}}.table-scroll{{overflow-x:auto}}
table{{border-collapse:collapse;background:white;font-size:13px;width:100%}}th,td{{padding:7px;border:1px solid #d4dce4;text-align:left;vertical-align:top}}
th{{background:#e8eef4}}.source-notes{{white-space:pre-wrap;font-size:14px;background:white;padding:16px;overflow-wrap:anywhere}}
@media print{{main{{padding:0}}figure{{break-inside:avoid}}}}a{{color:#075893}}
</style></head><body><main><h1>CYGNO gas-recoil SRIM study</h1><p class="status">{html.escape(status)}</p>
<nav aria-label="Downloads"><a href="technical_report.pdf">Technical PDF</a><a href="technical_report.md">Technical Markdown</a>
<a href="numerical_export.json">Numerical JSON</a><a href="../events.csv.gz">Event table</a></nav>{sections}
<h2>Recorded gas conditions</h2>{_html_table(gas_conditions, gas_fields)}
<h2>Numerical summary</h2>{_html_table(summary, summary_fields)}
<h2>SRModule stopping-medium comparison</h2><p>{html.escape(stopping_note)}</p>
{_html_table(stopping_summary, stopping_summary_fields)}
<h2>Contamination comparison</h2><p>{html.escape(IMPURITY_NOTE)}</p>
{_html_table(comparison, comparison_fields)}<h2>Validation</h2>{_html_table(validation_rows, validation_fields)}
<h2>Cutoff and trajectory-sampling systematics</h2><p>{html.escape(SYSTEMATICS_NOTE)}</p>
<p>{html.escape(systematics_inventory)}</p><p>{optional_links_html}</p>{optional_notes_html}{systematic_tables_html}
<h3>Recorded systematics checks</h3>{_html_table(systematics_checks, systematic_check_fields, limit=30)}
<h3>Raw-archive integrity</h3><p>{html.escape(ARCHIVE_NOTE)}</p>{_html_table(archive_checks, archive_check_fields)}
<h2>Figures</h2>{figures_html}<h2>Methodology and source notes</h2>{source_html}</main></body></html>
"""
    paths["html"].write_text(document, encoding="utf-8")
    input_manifest: dict[str, Any] = {}
    for name in ("metadata.json", "srmodule.csv", "summary.csv", "comparison.csv", "validation.json", "events.csv.gz",
                 "systematics.csv", "systematics.json", "archive_validation.json", "srmodule_medium_comparison.csv", "srmodule_medium_comparison.json", "impurity_diagnostics.json"):
        path = study_dir / name
        if path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            input_manifest[name] = {"path": f"../{name}", "sha256": digest.hexdigest(), "bytes": path.stat().st_size}
        else:
            input_manifest[name] = None
    if methodology:
        input_manifest["methodology.md"] = {
            "path": "methodology.md", "sha256": hashlib.sha256(methodology.encode("utf-8")).hexdigest(),
            "bytes": len(methodology.encode("utf-8")),
        }
    export = {"schema_version": 1, "status": status, "status_notes": notes,
              "interpretation": {"eee_srim": CALIBRATION_NOTE, "lengths": LENGTH_NOTE,
                                 "energy_channels": ENERGY_NOTE, "uncertainties": UNCERTAINTY_NOTE,
                                 "contamination": IMPURITY_NOTE, "primary_stopping_partitions": STOPPING_NOTE,
                                 "event_records": EVENT_NOTE, "systematics": SYSTEMATICS_NOTE,
                                 "archive_validation": ARCHIVE_NOTE},
              "metadata": metadata, "summary": summary, "srmodule": srmodule,
              "srmodule_instantaneous_partitions": stopping_partitions, "comparison": comparison,
              "systematics_csv": systematics_csv, "systematics": systematics,
              "srmodule_medium_comparison": stopping_comparison,
              "impurity_diagnostics": impurity_diagnostics,
              "archive_validation": archive_validation, "provenance_references": provenance_references,
              "optional_input_notes": optional_notes,
              "validation": validation, "event_table": {"path": "../events.csv.gz", "rows": event_count,
                                                        "fields": event_fields, "interpretation": EVENT_NOTE,
                                                        "field_units": _event_field_units(event_fields),
                                                        "coordinate_note": "Absolute X retains native Angstrom units; displacements and sampled paths use mm. One Angstrom equals 1e-7 mm. Source jitter is unresolved.",
                                                        "event_identity": ["medium", "species", "energy_kevnr", "batch", "ion_id"],
                                                        "per_event_charge_available": False,
                                                        "per_event_electronic_energy_available": False,
                                                        "per_event_qf_available": False},
              "input_manifest": input_manifest, "figures": figures,
              "methodology": "methodology.md" if methodology else None,
              "units": {"energy_kevnr": "keV nuclear-recoil energy", "eee_srim_kev_mean": "keV electronic energy; energy-only surrogate",
                        "qf_mean": "dimensionless", "summary_lengths": "mm", "event_fields": _event_field_units(event_fields),
                        "relative_changes": "dimensionless"}}
    paths["json"].write_text(json.dumps(_json_value(export), indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    return {**{name: str(path) for name, path in paths.items()}, "status": status,
            "figures": [str(report_dir / figure["png"]) for figure in figures]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("study_dir", type=Path)
    print(json.dumps(build_report(parser.parse_args().study_dir), indent=2))
