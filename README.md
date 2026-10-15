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
