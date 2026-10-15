"""Small native-SRIM adapters for the CYGNO bulk-recoil study.

The installed ``HELP - TRIM Input.pdf``, pp. 8-14--8-15, documents mode 5
as full cascades with initial positions supplied by TRIM.DAT.  Mode 7 requires
the projectile to be a constituent at its starting position and therefore
cannot represent H and O in the uncontaminated He/CF4 stopping medium.

These helpers do not launch executables or modify the installed SRIM tree.
"""

from __future__ import annotations

import math
from pathlib import Path
import re
import statistics
from typing import Any, Mapping, Sequence


NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][-+]?\d+)?")


def _number(value: str) -> float:
    return float(value.replace("D", "E").replace("d", "e"))


def _printed_numeric_quantum(value: str) -> float:
    """Value of the least significant printed decimal digit, in native units."""
    match = re.fullmatch(r"[-+]?(\d*(?:\.\d*)?)(?:[EeDd]([-+]?\d+))?", value)
    if match is None or not match.group(1) or match.group(1) == ".":
        raise ValueError(f"Invalid printed numeric value {value!r}")
    mantissa, exponent = match.groups()
    decimals = len(mantissa.split(".", 1)[1]) if "." in mantissa else 0
    return 10.0 ** (int(exponent or 0) - decimals)


def _write_crlf(path: Path, lines: Sequence[str], *, eof: bool = False) -> None:
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode("ascii") + (b"\x1a" if eof else b""))


def write_bulk_trim_dat(
    path: Path,
    ion_z: int,
    energy_kev: float,
    n_ions: int,
    start_depth_angstrom: float,
    description: str = "CYGNO bulk recoils",
) -> dict[str, Any]:
    """Write the documented ten-line header, event records, CRLF and DOS EOF.

    Use ``cascade_type=5`` in TRIM.IN. Its ion mass sets the isotope; TRIM.DAT
    supplies Z, kinetic energy in eV, initial position and direction. All ions
    point along +X in isotropic gas, so ranges are measured along their initial
    direction. Their source positions receive SRIM's atomic-scale random jitter.
    """
    if not 1 <= ion_z <= 92:
        raise ValueError("Projectile Z must be in [1, 92]")
    if not math.isfinite(energy_kev) or energy_kev <= 0:
        raise ValueError("Initial energy must be finite and positive")
    if not 1 <= n_ions <= 99_999:
        raise ValueError("TRIM.DAT supports at most 99999 events per file")
    if not math.isfinite(start_depth_angstrom) or start_depth_angstrom <= 0:
        raise ValueError("Bulk source must have finite positive depth")
    description.encode("ascii")
    if "\n" in description or "\r" in description:
        raise ValueError("Description must occupy one header line")
    lines = [
        "TRIM.DAT: full cascades for bulk CYGNO recoil projectiles",
        "Comment: First ten lines are the documented TRIM.DAT header.",
        "Comment: Line eight identifies the calculation in TRIM outputs.",
        "Comment: Mode 5; projectile isotope mass is specified in TRIM.IN.",
        "Comment: Event name has exactly five characters.",
        "Comment: Energy in eV; initial coordinates in Angstrom.",
        "Comment: Direction cosines are (1,0,0), along the initial recoil axis.",
        description[:120],
        "Event Atom Energy Depth Lateral-Y Lateral-Z Direction-X Direction-Y Direction-Z",
        "Name Z eV X-Angstrom Y-Angstrom Z-Angstrom cosX cosY cosZ",
    ]
    lines.extend(
        f"{index:05d} {ion_z:d} {energy_kev * 1000.0:.12g} "
        f"{start_depth_angstrom:.12g} 0 0 1 0 0"
        for index in range(1, n_ions + 1)
    )
    _write_crlf(path, lines, eof=True)
    return {
        "cascade_type": 5,
        "n_ions": n_ions,
        "ion_z": ion_z,
        "energy_kev": energy_kev,
        "source_depth_angstrom": start_depth_angstrom,
        "direction_cosines": [1.0, 0.0, 0.0],
        "isotope_mass_source": "TRIM.IN projectile mass",
        "source_position_jitter": "SRIM randomizes the starting position within an atomic diameter",
    }


def configure_trim_diskfiles(
    path: Path,
    *,
    exyz_step_ev: int = 0,
    save_sputtered: bool = True,
    collision_details: int = 0,
) -> dict[str, int]:
    """Change only the six disk-file flags following their labelled header.

    EXYZ is an energy increment in eV, not a boolean. Positive increments save
    sampled primary-ion positions, which are not complete collision trajectories.
    """
    if isinstance(exyz_step_ev, bool) or int(exyz_step_ev) != exyz_step_ev or exyz_step_ev < 0:
        raise ValueError("EXYZ increment must be a non-negative integer in eV")
    if collision_details not in (0, 1, 2):
        raise ValueError("Collision detail flag must be 0, 1 or 2")
    lines = path.read_text(encoding="ascii").splitlines()
    candidates = [i for i, line in enumerate(lines) if line.lstrip().startswith("Diskfiles")]
    if len(candidates) != 1 or candidates[0] + 1 >= len(lines):
        raise ValueError("Exactly one labelled TRIM disk-file flag row is required")
    index = candidates[0] + 1
    fields = lines[index].split()
    if len(fields) != 6 or any(not re.fullmatch(r"\d+", value) for value in fields):
        raise ValueError("Unexpected TRIM disk-file row; refusing an ambiguous edit")
    fields[3:] = [str(int(save_sputtered)), str(collision_details), str(int(exyz_step_ev))]
    lines[index] = " ".join(fields)
    _write_crlf(path, lines)
    return dict(zip(("ranges", "backscatter", "transmit", "sputtered", "collisions", "exyz_step_ev"), map(int, fields)))


def configure_bulk_energy_ceiling(
    path: Path,
    requested_energy_kev: float,
    relative_margin: float = 1e-5,
) -> dict[str, float]:
    """Set a safely rounded maximum accepted energy for mode-5 TRIM.DAT.

    Mode 5 obtains each event's actual energy from TRIM.DAT. TRIM.IN's energy
    instead sets the maximum accepted input energy and stopping-table extent.
    Its legacy six-significant-digit formatting can fall below a more precise
    event energy and cause native TRIM to reject that event. The small positive
    margin and twelve-significant-digit ceiling avoid that input rejection.

    Normalize QF and energy conservation to the actual TRIM.DAT event energy,
    never to this ceiling; retain both values in result provenance. Native
    output headers may display the ceiling, so they are not authoritative for
    the initial energy of these monoenergetic event records.
    """
    if (isinstance(requested_energy_kev, bool) or not math.isfinite(requested_energy_kev)
            or requested_energy_kev <= 0):
        raise ValueError("Requested bulk event energy must be finite and positive")
    if isinstance(relative_margin, bool) or not math.isfinite(relative_margin) or relative_margin <= 0:
        raise ValueError("Bulk energy ceiling margin must be finite and positive")
    lines = path.read_text(encoding="ascii").splitlines()

    def data_row(label: str, expected_fields: int) -> tuple[int, list[str]]:
        matches = [index for index, line in enumerate(lines) if line.lstrip().startswith(label)]
        if len(matches) != 1 or matches[0] + 1 >= len(lines):
            raise ValueError(f"Exactly one labelled TRIM {label!r} data row is required")
        index = matches[0] + 1
        fields = lines[index].split()
        if len(fields) != expected_fields:
            raise ValueError(f"Unexpected TRIM {label!r} data columns")
        try:
            values = list(map(_number, fields))
        except ValueError as error:
            raise ValueError(f"Non-numeric TRIM {label!r} data row") from error
        if not all(map(math.isfinite, values)):
            raise ValueError(f"Non-finite TRIM {label!r} data row")
        return index, fields

    _, cascade = data_row("Cascade Type", 3)
    if _number(cascade[0]) != 5:
        raise ValueError("Bulk energy ceiling is supported only for full-cascade mode 5")
    index, ion_fields = data_row("Ion:", 7)
    ceiling_text = f"{requested_energy_kev * (1.0 + relative_margin):.12g}"
    ceiling = float(ceiling_text)
    if not math.isfinite(ceiling) or ceiling < requested_energy_kev:
        raise ValueError("Bulk energy ceiling margin is too small or overflows")
    ion_fields[2] = ceiling_text
    lines[index] = " ".join(ion_fields)
    _write_crlf(path, lines)
    return {"requested_energy_kev": requested_energy_kev,
            "max_energy_kev": ceiling, "relative_margin": relative_margin}


def _quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    coordinate = probability * (len(ordered) - 1)
    lower = int(math.floor(coordinate))
    upper = int(math.ceil(coordinate))
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (coordinate - lower)


def distribution_summary(values: Sequence[float]) -> dict[str, float | int | None]:
    """Sample dispersion and uncertainty of the mean are separate quantities."""
    if not values:
        return {"count": 0, "mean": None, "sample_sd": None, "sem": None,
                "min": None, "q05": None, "median": None, "q95": None, "max": None}
    sd = statistics.stdev(values) if len(values) > 1 else None
    return {
        "count": len(values), "mean": statistics.fmean(values), "sample_sd": sd,
        "sem": sd / math.sqrt(len(values)) if sd is not None else None,
        "min": min(values), "q05": _quantile(values, 0.05), "median": _quantile(values, 0.5),
        "q95": _quantile(values, 0.95), "max": max(values),
    }


def parse_range_3d(path: Path, start_depth_angstrom: float = 0.0) -> dict[str, Any]:
    """Parse final positions and measure displacement from the nominal source.

    Euclidean endpoint distance is a displacement, never a trajectory length.
    Atomic-scale source jitter is not recoverable from RANGE_3D alone.
    """
    text = path.read_text(encoding="latin-1", errors="replace")
    if "angstrom" not in text.lower():
        raise ValueError("RANGE_3D does not declare Angstrom coordinate units")
    records = []
    seen: set[int] = set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 4 or not re.fullmatch(r"\d+", fields[0]):
            continue
        try:
            ion_id = int(fields[0])
            x, y, z = map(_number, fields[1:])
        except ValueError:
            continue
        if ion_id in seen:
            raise ValueError(f"Duplicate RANGE_3D event {ion_id}")
        if not all(map(math.isfinite, (x, y, z))):
            raise ValueError("Non-finite RANGE_3D coordinate")
        seen.add(ion_id)
        dx = x - start_depth_angstrom
        records.append({
            "ion_id": ion_id,
            "absolute_x_angstrom": x,
            "projected_displacement_mm": dx * 1e-7,
            "lateral_y_mm": y * 1e-7,
            "lateral_z_mm": z * 1e-7,
            "lateral_abs_projection_mm": (abs(y) + abs(z)) * 0.5e-7,
            "radial_displacement_mm": math.hypot(y, z) * 1e-7,
            "endpoint_displacement_3d_mm": math.sqrt(dx * dx + y * y + z * z) * 1e-7,
        })
    if not records:
        raise ValueError(f"No RANGE_3D endpoint records in {path}")
    fields = ("projected_displacement_mm", "lateral_y_mm", "lateral_z_mm",
              "lateral_abs_projection_mm", "radial_displacement_mm", "endpoint_displacement_3d_mm")
    summaries = {field: distribution_summary([row[field] for row in records]) for field in fields}
    return {
        "n_records": len(records), "source_depth_angstrom": start_depth_angstrom,
        "records": records, "distributions": summaries,
        "projected_range_mm": summaries["projected_displacement_mm"]["mean"],
        "projected_range_sem_mm": summaries["projected_displacement_mm"]["sem"],
        "longitudinal_straggling_sample_sd_mm": summaries["projected_displacement_mm"]["sample_sd"],
        "lateral_y_sample_sd_mm": summaries["lateral_y_mm"]["sample_sd"],
        "lateral_z_sample_sd_mm": summaries["lateral_z_mm"]["sample_sd"],
        "endpoint_displacement_3d_mean_mm": summaries["endpoint_displacement_3d_mm"]["mean"],
        "actual_trajectory_length_mm": None,
        "interpretation": "Final primary-ion endpoints relative to nominal source, not trajectory lengths",
        "source_jitter_limit": "RANGE_3D does not record the randomized initial position",
    }


def parse_exyz(
    path: Path,
    *,
    start_depth_angstrom: float = 0.0,
    endpoints: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    initial_energy_kev: float | None = None,
) -> dict[str, Any]:
    """Measure primary-ion polylines sampled at energy intervals.

    This excludes secondary tracks and unresolved deflections between samples.
    A supplied endpoint closes the last segment; interval refinement is needed
    before assigning this lower bound a physical track-length interpretation.
    """
    text = path.read_text(encoding="latin-1", errors="replace")
    if "angstrom" not in text.lower() or "kev" not in text.lower():
        raise ValueError("EXYZ must declare Angstrom coordinates and keV energies")
    by_ion: dict[int, list[dict[str, Any]]] = {}
    first_energy_tokens: dict[int, str] = {}
    for line in text.splitlines():
        fields = line.split()
        # Native SRIM's initial position row has six fields: the final
        # last-recoil transfer is absent because no collision has occurred.
        if len(fields) < 6 or not re.fullmatch(r"\d+", fields[0]):
            continue
        try:
            values = list(map(_number, fields[1:6]))
            last_recoil_energy = _number(fields[6]) if len(fields) >= 7 else None
        except ValueError:
            continue
        if not all(map(math.isfinite, values)) or (
            last_recoil_energy is not None and not math.isfinite(last_recoil_energy)
        ):
            raise ValueError("Non-finite EXYZ entry")
        energy, x, y, z, electronic_stopping = values
        ion_id = int(fields[0])
        first_energy_tokens.setdefault(ion_id, fields[1])
        by_ion.setdefault(ion_id, []).append({
            "energy_kev": energy, "x_angstrom": x, "y_angstrom": y, "z_angstrom": z,
            "primary_electronic_stopping_ev_per_angstrom": electronic_stopping,
            "last_recoil_transfer_ev": last_recoil_energy,
        })
    if not by_ion:
        raise ValueError(f"No EXYZ primary trajectory samples in {path}")
    endpoint_records = endpoints.get("records", []) if isinstance(endpoints, Mapping) else endpoints or []
    endpoint_map = {int(row["ion_id"]): row for row in endpoint_records}
    tracks = []
    for ion_id, samples in sorted(by_ion.items()):
        points = [(row["x_angstrom"], row["y_angstrom"], row["z_angstrom"]) for row in samples]
        energy_increases = sum(b["energy_kev"] > a["energy_kev"] + 1e-8 for a, b in zip(samples, samples[1:]))
        endpoint = endpoint_map.get(ion_id)
        if endpoint is not None:
            points.append((float(endpoint["absolute_x_angstrom"]),
                           float(endpoint["lateral_y_mm"]) * 1e7,
                           float(endpoint["lateral_z_mm"]) * 1e7))
        length = sum(math.dist(a, b) for a, b in zip(points, points[1:])) * 1e-7
        first_energy_text = first_energy_tokens[ion_id]
        energy_quantum = _printed_numeric_quantum(first_energy_text)
        # SRIM prints rounded mantissas. The allowed discrepancy follows the
        # last printed digit instead of imposing a fixed relative tolerance.
        # This still distinguishes the separately padded TRIM.IN ceiling.
        energy_tolerance = 0.5 * energy_quantum + max(
            1e-12, abs(initial_energy_kev or 0.0) * 1e-12, energy_quantum * 1e-9)
        starts_at_initial_energy = (
            abs(samples[0]["energy_kev"] - initial_energy_kev) <= energy_tolerance
            if initial_energy_kev is not None else None)
        tracks.append({
            "ion_id": ion_id, "n_samples": len(samples),
            "sampled_primary_polyline_length_mm": length,
            "endpoint_added": endpoint is not None,
            "starts_at_initial_energy": starts_at_initial_energy,
            "first_sample_energy_kev": samples[0]["energy_kev"],
            "first_sample_energy_text": first_energy_text,
            "first_sample_energy_print_quantum_kev": energy_quantum,
            "initial_energy_comparison_tolerance_kev": energy_tolerance,
            "last_sample_energy_kev": samples[-1]["energy_kev"],
            "energy_increase_count": energy_increases,
            "samples": samples,
        })
    distribution = distribution_summary([row["sampled_primary_polyline_length_mm"] for row in tracks])
    return {
        "n_tracks": len(tracks), "tracks": tracks,
        "sampled_primary_polyline_length_distribution_mm": distribution,
        "sampled_primary_polyline_length_mean_mm": distribution["mean"],
        "sampled_primary_polyline_length_sem_mm": distribution["sem"],
        "actual_primary_trajectory_length_mm": None,
        "total_cascade_trajectory_length_mm": None,
        "source_depth_angstrom": start_depth_angstrom,
        "interpretation": "Sampled primary-only polyline; lower bound on actual trajectory length; excludes secondary tracks",
        "limitation": "EXYZ samples at nearest transport steps to energy increments; convergence needs smaller increments",
    }


def integrate_native_profile(path: Path) -> dict[str, Any]:
    """Preserve arbitrary target-column count and integrate rectangular bins.

    SRIM's coordinate is the bottom of each equal-width bin (output manual
    p. 9-26); it is not an interpolation node for trapezoidal integration.
    """
    text = path.read_text(encoding="latin-1", errors="replace")
    groups: list[list[list[float]]] = []
    current: list[list[float]] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 3:
            try:
                row = list(map(_number, fields))
            except ValueError:
                row = []
            if row and row[0] > 0:
                current.append(row)
                continue
        if current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    rows = max(groups, key=len) if groups else []
    if len(rows) < 2:
        raise ValueError(f"Insufficient numeric depth bins in {path}")
    n_columns = len(rows[0])
    if any(len(row) != n_columns for row in rows):
        raise ValueError("Inconsistent native profile column count")
    if any(not all(map(math.isfinite, row)) for row in rows):
        raise ValueError("Non-finite native profile entry")
    differences = [b[0] - a[0] for a, b in zip(rows, rows[1:])]
    width = statistics.median(differences)
    if width <= 0 or any(not math.isclose(delta, width, rel_tol=1e-3, abs_tol=1e-7) for delta in differences):
        raise ValueError("Profile bins are not uniformly increasing")
    match = re.search(r"Units\s+are\s*:?\s*>+\s*(.*?)\s*<+", text, re.I)
    units = re.sub(r"\s+", " ", match.group(1)).strip() if match else "unknown"
    normalized = units.lower().replace(" ", "")
    if "ion" not in normalized:
        raise ValueError(f"Native profile lacks per-ion normalization units: {units}")
    if "angstrom" in normalized:
        multiplier = width
        integration = "sum(bin values) * bin width; already normalized per incident ion"
    elif "ev" in normalized:
        multiplier = 1.0
        integration = "sum(per-bin values); already normalized per incident ion"
    else:
        raise ValueError(f"Unrecognized native profile units: {units}")
    integrated = [math.fsum(row[index] for row in rows) * multiplier for index in range(1, n_columns)]
    count_match = re.search(r"Total\s+Ions\s+calculated\s*=\s*([\d.Ee+-]+)", text, re.I)
    return {
        "source": path.name, "units": units, "bin_width_angstrom": width,
        "bin_count": len(rows), "column_count": n_columns,
        "integrated_native_columns_per_ion": integrated,
        "values_explicitly_energy_ev": "ev" in normalized,
        "integration": integration,
        "calculated_ions": _number(count_match.group(1)) if count_match else None,
        "rows": rows,
    }


def parse_effective_settings(tdata_sav_path: Path) -> dict[str, Any]:
    """Read SRIM's effective settings, which can differ from requested zeros.

    TDATA.sav reports target energies to 0.01 eV and projectile mass to four
    decimal places. Reported zero bindings therefore do not establish an exact
    mathematical zero. Unlike some TDATA.txt headers, its density is scientific
    notation without the observed low-density formatting defect. Gas/solid
    phase is not recorded in this file and remains unknown.
    """
    lines = tdata_sav_path.read_text(encoding="latin-1", errors="replace").splitlines()

    def section_index(marker: str) -> int:
        found = [index for index, line in enumerate(lines) if line.lstrip().startswith(marker)]
        if len(found) != 1:
            raise ValueError(f"Missing or ambiguous TDATA.sav section {marker!r}")
        return found[0]

    def numeric_next(marker: str, minimum: int) -> list[float]:
        index = section_index(marker) + 1
        if index >= len(lines):
            raise ValueError(f"Truncated TDATA.sav section {marker!r}")
        try:
            values = list(map(_number, lines[index].split()))
        except ValueError as error:
            raise ValueError(f"Non-numeric TDATA.sav section {marker!r}") from error
        if len(values) < minimum or not all(map(math.isfinite, values)):
            raise ValueError(f"Invalid TDATA.sav section {marker!r}")
        return values

    ion = numeric_next("ION DATA:", 4)
    counts = numeric_next("ION NUMB:", 5)
    cascade = numeric_next("Cascade Type,", 1)
    dimensions = numeric_next("TGT DATA: -------", 2)
    n_elements, n_layers = map(int, dimensions[:2])
    if n_elements <= 0 or n_layers <= 0:
        raise ValueError("Invalid effective target dimensions")
    target_index = section_index("TGT DATA: Atomic Numb") + 1
    settings: dict[str, dict[str, Any]] = {}
    order = []
    symbols = ("H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn "
               "Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd "
               "Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U").split()
    for line in lines[target_index:target_index + n_elements]:
        try:
            values = list(map(_number, line.split()))
        except ValueError as error:
            raise ValueError("Invalid effective target-energy row") from error
        if len(values) != 5 or not all(map(math.isfinite, values)):
            raise ValueError("Invalid effective target-energy column count")
        z = int(values[0])
        key = str(z)
        if key in settings or not 1 <= z <= len(symbols):
            raise ValueError("Bulk-study effective target requires unique valid atomic numbers")
        order.append(z)
        settings[key] = {
            "z": z, "symbol": symbols[z - 1], "mass_u": values[1],
            "displacement_energy_ev": values[2], "lattice_binding_energy_ev": values[3],
            "surface_binding_energy_ev": values[4],
        }
    layer_index = section_index("TGT Layers:") + 1
    layers = []
    for line in lines[layer_index:layer_index + n_layers]:
        match = re.match(r'^\s*"([^"]*)"\s+(.+)$', line)
        if not match:
            raise ValueError("Invalid effective target-layer row")
        values = list(map(_number, match.group(2).split()))
        if len(values) != n_elements + 3 or not all(map(math.isfinite, values)):
            raise ValueError("Invalid effective target-layer columns")
        layers.append({
            "name": match.group(1).strip(), "width_angstrom": values[0],
            "bottom_depth_angstrom": values[1], "density_g_cm3": values[2],
            "atomic_fractions_by_z": {str(z): fraction for z, fraction in zip(order, values[3:])},
        })
    escape_values = numeric_next("Total Sputtered Atoms and Energy (eV)", 2 * n_elements)
    if len(escape_values) != 2 * n_elements or any(value < 0 for value in escape_values):
        raise ValueError("Effective target escape requires one non-negative count/energy pair per element")
    target_escape = {
        str(z): {"z": z, "symbol": settings[str(z)]["symbol"],
                 "count": escape_values[2 * index], "total_energy_ev": escape_values[2 * index + 1]}
        for index, z in enumerate(order)
    }
    return {
        "ion_z": int(ion[0]), "ion_mass_u": ion[1], "energy_kev": ion[2],
        "angle_deg": ion[3], "n_ions_calculated": int(counts[0]),
        "n_ions_requested": int(counts[1]), "autosave_ions": int(counts[2]),
        "transmitted_ions": int(counts[3]), "backscattered_ions": int(counts[4]),
        "cascade_type": int(cascade[0]), "target_energy_settings": settings,
        "target_element_order_z": order, "target_layers": layers,
        "density_g_cm3": layers[0]["density_g_cm3"] if len(layers) == 1 else None,
        "target_escape_by_element": target_escape,
        "target_escape_count": math.fsum(row["count"] for row in target_escape.values()),
        "target_escape_total_energy_ev": math.fsum(row["total_energy_ev"] for row in target_escape.values()),
        "target_escape_interpretation": "Total escaping target atoms and kinetic energy over all incident histories; not normalized per ion",
        "gas_phase": None, "gas_phase_interpretation": "Not recorded in TDATA.sav; verify requested TRIM.IN gas phase separately",
        "target_energy_report_resolution_ev": 0.01,
        "target_energy_precision_limit": "Reported zero means rounded to 0.01 eV; use requested inputs and default-substitution checks together",
        "source": str(tdata_sav_path),
    }


def integrate_energy_budget(
    ioniz_path: Path,
    phonon_path: Path | None,
    e2recoil_path: Path | None,
    initial_energy_kev: float,
    *,
    phonon_values_are_ev: bool = False,
    phonon_energy_convention: str = "unknown",
    vacancy_path: Path | None = None,
    effective_settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Separate final sinks from intermediate recoil-transfer diagnostics.

    PHONON's usual heading says ``Phonons/(Angstrom-Ion)``. The installed
    manual calls this energy loss but does not state an eV conversion in that
    heading. Consequently energy closure is null by default. An explicitly
    enabled legacy boolean is marked conditional. The explicit convention
    ``srim_documented_energy`` uses the energy interpretation supported by
    SRIM Tutorial 4 pp. 5--6 and the output manual pp. 9-26--9-27. Its native
    heading survives, and energy closure remains a numerical test. Vacancy
    counts times effective lattice binding provide SRIM's binding bookkeeping,
    not an additional gas-material damage observable.
    """
    if not math.isfinite(initial_energy_kev) or initial_energy_kev <= 0:
        raise ValueError("Initial energy must be finite and positive")
    if phonon_energy_convention not in ("unknown", "srim_documented_energy"):
        raise ValueError("Unknown PHONON energy convention")
    ioniz = integrate_native_profile(ioniz_path)
    if not ioniz["values_explicitly_energy_ev"] or len(ioniz["integrated_native_columns_per_ion"]) != 2:
        raise ValueError("IONIZ must contain exactly two explicitly eV-normalized energy channels")
    primary, secondary = [value / 1000.0 for value in ioniz["integrated_native_columns_per_ion"]]
    phonon = integrate_native_profile(phonon_path) if phonon_path is not None else None
    transfer = integrate_native_profile(e2recoil_path) if e2recoil_path is not None else None
    phonon_native = phonon["integrated_native_columns_per_ion"] if phonon else None
    explicit_phonon_energy = bool(phonon and phonon["values_explicitly_energy_ev"])
    documented_phonon_energy = phonon_energy_convention == "srim_documented_energy"
    phonon_energy = (
        math.fsum(phonon_native) / 1000.0
        if phonon_native is not None and (explicit_phonon_energy or phonon_values_are_ev or documented_phonon_energy) else None
    )
    vacancy = integrate_native_profile(vacancy_path) if vacancy_path is not None else None
    binding_by_element = []
    binding_sink = None
    if vacancy is not None and effective_settings is not None:
        if "vacanc" not in vacancy["units"].lower():
            raise ValueError("VACANCY must declare vacancy-count units")
        order = list(effective_settings["target_element_order_z"])
        counts = vacancy["integrated_native_columns_per_ion"]
        if len(counts) != len(order) + 1:
            raise ValueError("VACANCY columns must contain primary knock-ons and each target element")
        for z, count in zip(order, counts[1:]):
            setting = effective_settings["target_energy_settings"][str(z)]
            energy = float(setting["lattice_binding_energy_ev"]) * count / 1000.0
            binding_by_element.append({"z": z, "symbol": setting["symbol"],
                "vacancies_per_ion": count, "lattice_binding_energy_ev": setting["lattice_binding_energy_ev"],
                "vacancy_binding_energy_kev": energy})
        binding_sink = math.fsum(row["vacancy_binding_energy_kev"] for row in binding_by_element)
    electronic = primary + secondary
    remainder = initial_energy_kev - electronic
    closure = initial_energy_kev - electronic - phonon_energy if phonon_energy is not None else None
    binding_closure = closure - binding_sink if closure is not None and binding_sink is not None else None
    transfer_kev = (
        [value / 1000.0 for value in transfer["integrated_native_columns_per_ion"]]
        if transfer and transfer["values_explicitly_energy_ev"] else None
    )
    return {
        "initial_energy_kev": initial_energy_kev,
        "primary_electronic_energy_kev": primary,
        "secondary_electronic_energy_kev": secondary,
        "total_electronic_energy_kev": electronic,
        "qf_electronic": electronic / initial_energy_kev,
        "eee_electronic_proxy_kev": electronic,
        "remaining_energy_kev": remainder,
        "remaining_energy_fraction": remainder / initial_energy_kev,
        "remaining_energy_interpretation": "Complement of electronic deposition; can include non-electronic energy, escape and untracked residual",
        "phonon_integrated_native_columns_per_ion": phonon_native,
        "phonon_energy_kev": phonon_energy,
        "phonon_energy_convention": (
            "explicit eV units" if explicit_phonon_energy else
            "SRIM documented energy convention; native Phonons heading retained" if documented_phonon_energy and phonon else
            "conditional eV interpretation of Phonons heading" if phonon_values_are_ev and phonon else
            "unknown; native Phonons heading does not explicitly specify energy units"
        ),
        "phonon_energy_convention_supported": explicit_phonon_energy or documented_phonon_energy,
        "phonon_energy_convention_source": "SRIM Tutorial 4 pp.5-6; HELP - TRIM Output pp.9-26--9-27" if documented_phonon_energy else None,
        "binding_energy_sink_kev": binding_sink,
        "binding_energy_sink_by_element": binding_by_element,
        "binding_energy_interpretation": "SRIM vacancy-count times effective lattice-binding bookkeeping; not a gas defect-energy measurement",
        "exclusive_energy_budget_residual_kev": closure,
        "exclusive_energy_budget_residual_fraction": closure / initial_energy_kev if closure is not None else None,
        "exclusive_energy_budget_with_binding_residual_kev": binding_closure,
        "exclusive_energy_budget_with_binding_residual_fraction": binding_closure / initial_energy_kev if binding_closure is not None else None,
        "energy_closure_verified": False,
        "unresolved_energy_channels": ["primary escape", "secondary escape", "transport cutoff residual", "binding channels if nonzero"],
        "recoil_transfer_diagnostic_columns_kev": transfer_kev,
        "recoil_transfer_interpretation": (
            "E2RECOIL columns retain native order: first Energy from IONS; remaining Energy Absorbed by each target element "
            "(one aggregate recoil column in some outputs). These transfer/absorption diagnostics are excluded from the "
            "final-sink sum to avoid double counting. The headers do not identify a separate recoil-to-recoil channel."
        ),
        "ioniz_profile": ioniz, "phonon_profile": phonon, "recoil_transfer_profile": transfer,
        "vacancy_profile": vacancy,
        "qf_interpretation": "SRIM electronic-energy fraction including primary and secondary recoils; physical charge QF requires calibration",
    }
