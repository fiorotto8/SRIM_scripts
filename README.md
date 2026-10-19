# SRIM scripts for native Windows

The `windows` branch runs SRIM-2013 directly with **64-bit Windows Python 3.15**.
Simulation, campaign control, analysis and reports use the same system Python;
WSL, Wine, Docker, PySRIM and a virtual environment are not required.
The repository can share `C:\SRIM-2013` with the installed SRIM files. Workers
use private runtime copies; keep installed executables and reference data intact.

The completed **[CYGNO QF study](studies/cygno_qf_900mbar/README.md)** contains
the five-ion numerical tables, comparison plots, technical report and frozen
replay. Existing completed runs and that publication remain reusable.

## Requirements and setup

Install Git, 64-bit Python 3.15 and [SRIM-2013](https://www.srim.org/) in a writable
directory. Verify `C:\SRIM-2013\TRIM.exe` and
`C:\SRIM-2013\SR Module\SRModule.exe`. Run these single-line commands in PowerShell:

```powershell
Set-Location C:\SRIM-2013
py -3.15 -m pip install numpy matplotlib scipy
py -3.15 -c "import numpy, matplotlib, scipy; print(numpy.__version__, matplotlib.__version__, scipy.__version__)"
```

NumPy and Matplotlib support analysis and reports. SciPy is the existing
dependency for constrained response fits and export validation; it preserves
the original fitting and integration algorithms. A native C++17 compiler
(`g++`, `clang++`, or `cl` on PATH) is needed for the maintained export's C++
interchange qualification. Run `cl` from a Visual Studio developer PowerShell.

`srim-root.txt` centralizes the installation path; its first non-comment line
should be `C:\SRIM-2013`. CSV sweeps and `srim_scan.py` also accept `--srim-root`
or `SRIM_ROOT`, with the explicit option taking priority. The CYGNO commands
default to the repository's SRIM installation and accept `--srim-root`.

SRIM needs an interactive Windows desktop for its dialogs. Use a normal
PowerShell session. If administrator compatibility flags prevent execution:

```powershell
.\fix_srim_no_admin.ps1 -Root C:\SRIM-2013 -ClearBatchCompatibility
```

## CSV stopping/range and TRIM workflows

Preview the small SRModule workload, then run it with distinct output paths:

```powershell
py -3.15 srim_gas_sweep.py --gas-csv examples\GasDensities_minimal.csv --ions C,F --points 3 --dry-run --no-plots
py -3.15 srim_gas_sweep.py --gas-csv examples\GasDensities_minimal.csv --ions C,F --points 3 --out results\minimal_srmodule.csv --workdir work\minimal_srmodule --no-plots
py -3.15 trim_qf_sweep.py --gas-csv examples\GasDensities_minimal.csv --ions C --energies-kev 10 --mode full-cascade-validation --n-ions 10 --out results\minimal_trim.csv --workdir work\minimal_trim --no-plots
```

These low-statistics examples check installation. For the supplied CSV scan:

```powershell
py -3.15 srim_gas_sweep.py --gas-csv GasDensities.csv --dry-run --no-plots
py -3.15 srim_gas_sweep.py --gas-csv GasDensities.csv --out results\srim_gas_sweep_new.csv --workdir work\srmodule_new --plots-dir plots\srmodule_new
py -3.15 trim_qf_sweep.py --gas-csv GasDensities.csv --mode quick-depth-and-qf --n-ions 100 --quick-n-ions 100 --out results\trim_qf_new.csv --workdir work\trim_qf_new --plots-dir plots\trim_qf_new --keep-work
```

The default CSV energy grid has 100 logarithmic points from 0.1 to 500 keV.
Restrict trials with, for example, `--gases CF4 --ions C,F --points 5`. Preview
TRIM with `--dry-run`; both CSV previews are read-only. Execution refuses an
existing output file or nonempty work directory. Use fresh paths for each run.

Required CSV columns are:

```csv
Gas, density @ 293 K (g/cm3), Pressure (atm)
CF4, 0.00361, 1
```

Mixture labels such as `HeCF4(60% He 40% CF4)` encode volume/mole fractions;
density is in g/cm3. Quote gas labels that contain commas.

## CYGNO campaign: plan, start, status, stop, resume, report

The [central configuration](configs/cygno_hecf4_900mbar.toml) preserves the
He/CF4 60/40 baseline at 900 mbar and 293 K, plus the medium with 1000 ppm H2O
and 1000 ppm O2. The baseline density is 0.001389194959 g/cm3; elemental atomic
fractions are He = 3/13, C = 2/13 and F = 8/13. Isotopes are H-1, He-4, C-12,
F-19 and O-16. Bulk recoils use TRIM.DAT mode 5 and full secondary cascades.

Choose a new run directory for a new campaign. Planning creates configuration
and provenance records without launching SRIM. The example campaign uses eight
independent worker runtimes and can take substantial time:

```powershell
py -3.15 scripts\cygno_study.py plan --run-dir runs\cygno_native_new
py -3.15 scripts\cygno_campaign.py start --run-dir runs\cygno_native_new --workers 8
py -3.15 scripts\cygno_campaign.py status --run-dir runs\cygno_native_new
py -3.15 scripts\cygno_campaign.py stop --run-dir runs\cygno_native_new
py -3.15 scripts\cygno_campaign.py resume --run-dir runs\cygno_native_new
py -3.15 scripts\cygno_study.py report --run-dir runs\cygno_native_new
```

Start and resume detach the coordinator with timestamped output/error logs in
the run's `logs` directory. Closing PowerShell leaves it running. Stop requests
that active batches finish and save; wait for `stopped` before shutting down.
Resume reuses saved configuration, worker count and completed batches. After an
interruption, inspect status and recover a verified stale coordinator record:

```powershell
py -3.15 scripts\cygno_campaign.py recover --run-dir runs\cygno_native_new
```

Recovery checks recorded process identity and private runtimes before releasing
stale state. The supervisor checks the requested ion count before acknowledging
the known TRIM completion dialog; unknown dialogs or mismatched counts fail.
Use campaign control for recovery; do not kill all processes named TRIM or SRModule.
The PowerShell wrappers and `scan.bat` remain convenience entry points.

The initial campaign has 28 logarithmic energies from 0.1 to 3000 keVnr, with
three independent 100-history batches per species/energy/medium. Adaptive grid
refinement and paired batches retain the configured convergence targets and
30-batch cap. Threshold and trajectory-sampling controls remain separate.
Reports render natively by default. `simulations_complete_report_pending` means
that numerical validation finished and a separately requested report remains.
Check validation files for missing points and unresolved scientific limitations.

For the general resumable `srim_scan.py` workflow:

```powershell
py -3.15 srim_scan.py plan --config configs\noble_cf4_standard.toml
py -3.15 srim_scan.py preflight --config configs\noble_cf4_standard.toml --srim-root C:\SRIM-2013 --skip-canary
py -3.15 srim_scan.py run --config configs\noble_cf4_standard.toml --srim-root C:\SRIM-2013 --run-dir runs\scan_native_new --stage all --workers 1
py -3.15 srim_scan.py status --run-dir runs\scan_native_new
py -3.15 srim_scan.py run --config configs\noble_cf4_standard.toml --srim-root C:\SRIM-2013 --run-dir runs\scan_native_new --stage all --workers 1 --resume
py -3.15 srim_scan.py validate --run-dir runs\scan_native_new
py -3.15 srim_scan.py report --run-dir runs\scan_native_new
```

Ctrl+C stops active work in the general scan and returns unfinished jobs to
pending. Resume with the same run directory to reuse completed checkpoints.
Omit `--skip-canary` to include a small native executable check during preflight.

## Interpretation, export and replay

QF is the integrated primary-plus-secondary IONIZ electronic-energy fraction.
Its electron-equivalent energy is an SRIM surrogate; physical charge/light
calibration remains unresolved. SRModule's stopping integral is a separate
proxy. SRIM does not model drift, attachment, recombination, gain or W-values.
RANGE_3D supplies endpoints; EXYZ supplies sampled primary paths, with exact
total cascade arc length unknown. See [methodology](docs/cygno_srim_methodology.md).

Create a new read-only export from completed batches, then qualify its C++ interface:

```powershell
py -3.15 scripts\export_cygno_mc.py --study-dir runs\cygno_hecf4_900mbar_20261015 --config studies\cygno_qf_900mbar\frozen\snapshot\geant4_config.json --out exports\new_export
py -3.15 scripts\validate_cygno_mc_export.py exports\new_export
py -3.15 scripts\validate_cygno_mc_export.py --publication-checksums studies\cygno_qf_900mbar
```

The export retains isotope identities, separate energy channels, constrained
smooth QF and analytic response derivative, uncertainty, provenance and replay
inputs. Source points remain at 293 K; transfer to 293.15 K is explicitly derived.
See [export documentation](docs/cygno_mc_export.md) for packaging and methods.
The publication checksum command is read-only and needs only Python's standard
library; it checks the complete stored file inventory without regenerating tables.
The frozen publication retains its historical tools and dependency versions.
Its `reproduce.py --verify-only` also regenerates tables and requires byte
identity; native numerical-library differences can fail that historical check
even when every stored SHA256 hash passes. Full byte-identical historical replay
requires the recorded environment and a C++17 `g++`. Keep the frozen data intact.

For local tests and file conventions, see [CONTRIBUTING.md](CONTRIBUTING.md).
