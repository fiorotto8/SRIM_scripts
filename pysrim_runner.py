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
import sys
from pathlib import Path
from typing import Any, Dict, Mapping


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
            try:
                trim.run(str(runtime))
            except api["SRIMOutputParseError"]:
                # PySRIM's legacy parser rejects some otherwise valid SRIM-2013
                # headers (notably one-ion RANGE output). The parent pipeline
                # has stricter, tested parsers and independently requires the
                # complete standard output set before accepting the job.
                required = (
                    "TDATA.txt",
                    "RANGE.txt",
                    "LATERAL.txt",
                    "IONIZ.txt",
                    "E2RECOIL.txt",
                    "PHONON.txt",
                    "VACANCY.txt",
                    "NOVAC.txt",
                )
                if not all(
                    (runtime / name).is_file()
                    or (runtime / "SRIM Outputs" / name).is_file()
                    for name in required
                ):
                    raise
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
