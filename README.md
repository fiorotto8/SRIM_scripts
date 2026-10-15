# SRIM scripts for native Windows

The `windows` branch is the native-Windows version of this repository. Its
primary input is `GasDensities.csv`; it launches the SRIM-2013 Windows
executables directly and does not require Docker, Wine, WSL, or PySRIM.

Ubuntu/PySRIM development remains on the
[`main` branch](https://github.com/fiorotto8/SRIM_scripts/tree/main). Keep this
checkout on `windows` for the native Windows CSV workflow.

Two entry points cover the original CSV workflow:

- `srim_gas_sweep.py` runs SRModule stopping/range calculations and writes a
  flat CSV plus optional plots.
- `trim_qf_sweep.py` runs TRIM and derives the IONIZ electronic-energy
  fraction, with quick-depth and full-cascade modes.

## Install on Windows

1. Install 64-bit Python 3.11 or newer and Git for Windows.
2. Download SRIM-2013 from the official [SRIM site](https://www.srim.org/) and
   extract it to a user-writable directory such as `C:\SRIM-2013`. Do not put
   this automation copy under `Program Files`.
3. Verify that both `C:\SRIM-2013\TRIM.exe` and
   `C:\SRIM-2013\SR Module\SRModule.exe` exist.
4. Clone this branch and create its virtual environment from PowerShell:

```powershell
git clone --branch windows https://github.com/fiorotto8/SRIM_scripts.git
Set-Location SRIM_scripts
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install matplotlib
```

Edit `srim-root.txt` so its first non-comment line is your installation path:

```text
C:\SRIM-2013
```

You can instead pass `--srim-root C:\SRIM-2013` on every command or set the
`SRIM_ROOT` environment variable. The explicit command-line option has the
highest priority.

If Windows asks for administrator privileges or denies writes, open a normal
(non-administrator) PowerShell and run:

```powershell
.\fix_srim_no_admin.ps1 -Root C:\SRIM-2013 -ClearBatchCompatibility
```

The helper grants the current user access inside the selected SRIM directory,
removes `RUNASADMIN` compatibility flags from the batch executables, and
refuses protected Windows installation folders.

## Minimal working example

`examples\GasDensities_minimal.csv` contains one CF4 condition. First inspect
the three-energy SRModule plan without launching SRIM:

```powershell
.\.venv\Scripts\python.exe srim_gas_sweep.py `
  --gas-csv examples\GasDensities_minimal.csv `
  --ions C,F --points 3 --dry-run --no-plots
```

Then run the same small calculation:

```powershell
.\.venv\Scripts\python.exe srim_gas_sweep.py `
  --gas-csv examples\GasDensities_minimal.csv `
  --ions C,F --points 3 --no-plots `
  --out results\minimal_srmodule.csv
```

A minimal 10-ion full-cascade TRIM check at 10 keV is:

```powershell
.\.venv\Scripts\python.exe trim_qf_sweep.py `
  --gas-csv examples\GasDensities_minimal.csv `
  --ions C --energies-kev 10 `
  --mode full-cascade-validation --n-ions 10 `
  --out results\minimal_trim.csv --no-plots
```

Use these low-statistics commands only to verify installation and understand
the files; they are not production-quality physics runs.

## Run the supplied GasDensities.csv scan

Preview the complete SRModule workload, then run it:

```powershell
.\.venv\Scripts\python.exe srim_gas_sweep.py `
  --gas-csv GasDensities.csv --dry-run --no-plots

.\.venv\Scripts\python.exe srim_gas_sweep.py `
  --gas-csv GasDensities.csv `
  --out results\srim_gas_sweep_results.csv `
  --plots-dir plots\srmodule
```

The default energy grid has 100 logarithmic points from 0.1 to 500 keV and
uses every constituent element in each selected gas. Restrict a trial with,
for example, `--gases CF4 --ions C,F --points 5`.

For the original quick-depth plus production-QF TRIM workflow:

```powershell
.\.venv\Scripts\python.exe trim_qf_sweep.py `
  --gas-csv GasDensities.csv `
  --mode quick-depth-and-qf `
  --n-ions 100 --quick-n-ions 100 `
  --out results\trim_qf_results.csv `
  --plots-dir plots\trim_qf --keep-work
```

Always run `--dry-run` with the same filters first. The legacy scripts rewrite
their selected output/work paths when starting; use distinct paths for trials
and production, and do not point two simultaneous runs at the same SRIM tree.

## CSV format

The required columns are:

```csv
Gas, density @ 293 K (g/cm3), Pressure (atm)
CF4, 0.00361, 1
```

Mixture labels encode volume/mole percentages, for example
`HeCF4(60% He 40% CF4)`. Percentages must resolve to elements known by SRIM,
and density is supplied in g/cm3. Keep commas out of the gas label unless the
field is CSV-quoted.

## Outputs and interpretation

Generated files are ignored by Git. By default they are placed under
`results\`, `plots\`, and `work\`. Use `--keep-work` when raw SRModule/TRIM
inputs are needed for auditing.

The TRIM quantity reported as `qf_ioniz` is the integrated electronic energy
in the IONIZ outputs divided by initial recoil energy. The SRModule value is a
stopping-power integral proxy. Neither is a detector charge/light calibration.
SRIM also does not model drift, diffusion, attachment, recombination, gain,
W-values, or Fano factors.

## Troubleshooting

- Run commands from the repository root so relative CSV/output paths are
  unambiguous.
- Close interactive SRIM windows before automation starts.
- Keep SRIM in a writable directory and run Python from a normal shell.
- If a previous run was interrupted, end stray `TRIM.exe` or `SRModule.exe`
  processes in Task Manager before retrying with fresh work/output paths.
- Add `--no-plots` if Matplotlib is not installed.

See [CONTRIBUTING.md](CONTRIBUTING.md) for local checks and Git file conventions.

## CYGNO He/CF4 study at 900 mbar

The dedicated [study configuration](configs/cygno_hecf4_900mbar.toml) uses the
active CSV temperature of 293 K. The ideal-gas baseline density is
0.001389194959 g/cm3; the elemental atomic fractions are He = 3/13,
C = 2/13, and F = 8/13. These differ from the 60/40 molecular fractions.
The second medium contains 1000 ppm H2O and 1000 ppm O2 by final molar fraction,
with the remaining gas retaining the 60/40 He/CF4 ratio.
The [methodology notes](docs/cygno_srim_methodology.md) document the repository
workflow, primary CYGNO/SRIM sources, energy accounting, and charge calibration.

The [study driver](scripts/cygno_study.py) reuses the existing installation,
input writers, parsers, and raw-archive helpers. It runs all five explicit
isotopic projectiles (H-1, He-4, C-12, F-19, O-16) in both media. Bulk starting
positions use TRIM.DAT mode 5 with full secondary cascades. Private runtime
copies protect the installed SRIM tree and previous results. The native
[supervisor](scripts/cygno_native_run.py) checks the completed history count
before acknowledging the mode-5 completion dialog automatically.

In this checkout, WSL Python supplies orchestration and the available analysis
packages; the actual calculations use native Windows SRIM executables, without
Wine or PySRIM. Inspect the plan, then launch the detached campaign:

```bash
python3 scripts/cygno_study.py plan
./scripts/start_cygno_study.sh
python3 scripts/cygno_study.py status
```

The launcher prints the PID and timestamped log. To request a stop after active
jobs finish, create the marker below. After the campaign has stopped and its
lock has been released, remove that marker and rerun the same launcher to
resume completed jobs without overwriting them.

```bash
touch runs/cygno_hecf4_900mbar_20261015/.stop-requested
```

Results, raw archives, frozen source/configuration snapshots, and the report
are written under `runs/cygno_hecf4_900mbar_20261015/`. Regenerate the numerical
tables and report from completed batches with:

```bash
python3 scripts/cygno_study.py report
```

The campaign starts with 28 logarithmic energies from 0.1 to 3000 keVnr and
three independent 100-history batches per species, energy, and medium. It
then refines the grid and adds paired batches where the QF/range precision
targets are unmet, up to nine batches. Separate runs test cascade thresholds
and primary-path sampling. Missing points, low-energy conservation failures,
and unresolved convergence remain explicit in the output.

The QF is the integrated IONIZ primary-plus-secondary electronic-energy
fraction. Its electron-equivalent energy is an SRIM surrogate; physical charge
quenching requires recoil-specific calibration. RANGE_3D gives endpoints;
EXYZ gives sampled primary paths, with exact total cascade length left unknown.
