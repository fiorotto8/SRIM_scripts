# SRIM Gas-Recoil Automation

This repository contains the files that are not part of a clean SRIM-2013
installation, but are needed to run the gas-recoil automation reproducibly.

`srim_gas_sweep.py` is the fast deterministic SRModule workflow. It writes
`SR.IN`, launches `SR Module/SRModule.exe`, parses the generated stopping/range
table, and writes range plus electronic-stopping quenching proxy values.

`trim_qf_sweep.py` is the TRIM/IONIZ Monte Carlo workflow. It writes `TRIM.IN`
and `TRIMAUTO`, launches `TRIM.exe`, parses `IONIZ.TXT`, and estimates an
ionization quenching factor from the integrated ionization profile.

The repository contains:

```text
README.md
.gitignore
srim_gas_sweep.py
trim_qf_sweep.py
GasDensities.csv
fix_srim_no_admin.ps1
fix_srim_no_admin.bat
srim-root.example.txt
```

## Supported Platforms

The scripts depend on the Windows SRIM-2013 executables `SRModule.exe` and
`TRIM.exe`.

Supported and documented:

```text
Windows Python 3.10+ with SRIM installed at C:\SRIM-2013
WSL Python 3.10+ calling the Windows SRIM install at /mnt/c/SRIM-2013
```

Pure Linux and macOS are not supported by this script as-is. The blocking
dependency is SRIM's Windows executable and its installation layout. A Wine-based
port may be possible, but it is not implemented, tested, or documented here.

## Requirements

Install SRIM-2013 separately first http://www.srim.org/. The standard path used by this project is:

```text
Windows: C:\SRIM-2013
WSL:     /mnt/c/SRIM-2013
```

The SRIM installation must contain at least:

```text
C:\SRIM-2013\Data\ATOMDATA
C:\SRIM-2013\Data\Compound.dat
C:\SRIM-2013\SR Module\SRModule.exe
C:\SRIM-2013\SR Module\SCOEF03.dat
C:\SRIM-2013\SR Module\SNUC03.dat
C:\SRIM-2013\SR Module\VERSION
C:\SRIM-2013\TRIM.exe
```

Python requirements:

```text
Python 3.10 or newer
matplotlib, optional, only needed for plots
```

Install the optional plotting dependency with:

```bash
sudo apt install python3-matplotlib
```

Without `matplotlib`, run with `--no-plots` or use only `--dry-run`.

## SRIM Path Configuration

You have three ways to tell the scripts where SRIM is installed:

1. Pass `--srim-root` on the command line.
2. Set the `SRIM_ROOT` environment variable.
3. change `srim-root.txt` and edit the path.

`srim-root.txt`may contain a Windows path such as:

```text
C:\SRIM-2013
```

or, from WSL, the mounted path:

```text
/mnt/c/SRIM-2013
```

When running under WSL, a Windows path in `srim-root.txt` is converted with
`wslpath`.

## Gas Input CSV

The scripts need a gas CSV. By default they look for `GasDensities.csv` in this
repository. If that file is not present there, they fall back to looking inside
`--srim-root`.

Expected columns:

```csv
Gas, density @ 293 K (g/cm3), Pressure (atm)
CF4, 0.00361, 1
HeCF4(60% He 40% CF4), 0.001564, 1
HeCF4CH4(58% He 37% CF4 5% CH4), 0.001484, 1
```

The `Gas` value can be a formula such as `CF4` or a mixture label using
percentages in parentheses. The script derives the recoil ions from the gas
composition when `--ions gas` is used.

## Recommended Installation

Edit `srim-root.txt` so the first non-comment line points to the SRIM-2013
installation. With that file present, the scripts can run from the repository
without copying project files into `C:\SRIM-2013`.

Run from Windows PowerShell:

```powershell
cd path\to\this\repository
python .\srim_gas_sweep.py --help
python .\trim_qf_sweep.py --help
python .\srim_gas_sweep.py --dry-run --no-plots
python .\trim_qf_sweep.py --dry-run --no-plots
```

Run from WSL:

```bash
cd /path/to/this/repository
python3 srim_gas_sweep.py --help
python3 trim_qf_sweep.py --help
python3 srim_gas_sweep.py --dry-run --no-plots
python3 trim_qf_sweep.py --dry-run --no-plots
```

You can skip `srim-root.txt` and pass the path explicitly instead:

```bash
python3 srim_gas_sweep.py \
  --srim-root /mnt/c/SRIM-2013 \
  --gas-csv GasDensities.csv \
  --out results/srim_gas_sweep_results.csv \
  --workdir work/srim_gas_sweep \
  --no-plots
```

## SRModule Sweep

Default behavior:

```text
gas rows:     every row in GasDensities.csv
ions:         gas constituents only
energy grid:  100 log-spaced points from 0.1 to 500 keV
input CSV:    GasDensities.csv in this repository
output CSV:   results/srim_gas_sweep_results.csv in this repository
work folder:  work/srim_gas_sweep in this repository
plots:        plots/ in this repository
```

Typical full run from the repository:

```bash
python3 srim_gas_sweep.py \
  --out results/srmodule_all_gases_0p1_500keV_100pts.csv \
  --plots-dir plots/srmodule_all_gases_0p1_500keV_100pts \
  --workdir work/srmodule_all_gases_0p1_500keV_100pts \
  --keep-work
```

Useful options:

```text
--gases CF4                 run only matching gas rows
--ions H,He,C,F             choose explicit recoil ions
--emin-kev 1 --emax-kev 100 --points 20
--compound-mode none        default; use no SRIM compound dictionary comparison
--compound-mode dictionary  record dictionary metadata when available
--compound-mode compare     write rows for both none and dictionary decisions
--keep-work                 preserve generated SR.IN and raw SRModule output
--no-plots                  skip Matplotlib plot generation
--plot-only                 regenerate plots from an existing CSV
```

## TRIM QF Sweep

`trim_qf_sweep.py` imports shared parsing helpers from `srim_gas_sweep.py`, so
keep both scripts together. It uses the same `GasDensities.csv` and SRIM-root
configuration.

Typical full TRIM run:

```bash
python3 trim_qf_sweep.py \
  --out results/trim_qf_all_gases_0p1_500keV_100pts_100ions.csv \
  --plots-dir plots/trim_qf_all_gases_0p1_500keV_100pts_100ions \
  --workdir work/trim_qf_all_gases_0p1_500keV_100pts_100ions \
  --srmodule-csv results/srmodule_all_gases_0p1_500keV_100pts.csv \
  --keep-work
```

Useful TRIM-only options:

```text
--energies-kev 1,2,5,10      use exact energies instead of the log grid
--mode quick-depth-and-qf    default two-pass depth scan plus QF production
--mode quick-depth-scan      run only broad depth scans
--mode qf-production         run QF production with fallback or forced width
--mode full-cascade-validation
--n-ions 100                 production ions per job
--quick-n-ions 100           quick depth scan ions per job
--parse-only                 rebuild CSV from existing work folders
--density-mode csv           use density from GasDensities.csv
--density-mode ideal-gas     recompute gas density from pressure and mixture
```

TRIM modifies top-level SRIM files while each job runs. The script restores the
previous `TRIM.IN` and `TRIMAUTO` afterwards, but do not run multiple TRIM
sweeps against the same SRIM installation at the same time.

If Python-driven TRIM launch is unreliable, the script still writes
`work/.../run_trim_jobs.bat` and `trim_run_list.txt`. You can run that batch
launcher from Windows and then rebuild the CSV with:

```bash
python3 trim_qf_sweep.py \
  --parse-only \
  --workdir work/trim_qf_all_gases_0p1_500keV_100pts_100ions \
  --out results/trim_qf_all_gases_0p1_500keV_100pts_100ions.csv
```

## Output Meaning

The CSV includes gas metadata, parsed stoichiometry, ion metadata, stopping
power, projected range, straggling, and an electronic-stopping quenching proxy.

The quenching proxy is calculated from SRModule stopping powers:

```text
QF_electronic(E) = integrated electronic stopping energy / initial ion energy
Eee              = QF_electronic(E) * Eion
```

This is a stopping-power-derived electronic energy fraction. It is not a
detector-calibrated scintillation, ionization, light-yield, or charge-yield
model.

For TRIM rows:

```text
QF_ioniz(E) = integrated IONIZ energy / initial ion kinetic energy
Eee         = QF_ioniz(E) * Eion
```

That is an IONIZ-profile-derived ionization energy fraction, not a detector
calibration.

## No-Admin SRIM Fix

`fix_srim_no_admin.ps1` is included because SRIM installations can inherit file
permissions or Windows compatibility flags that make batch execution fail from
Python or WSL.

Run it from Windows PowerShell if SRModule/TRIM asks for elevation, times out
behind a UAC prompt, or reports locked output files:

```powershell
cd path\to\this\repository
.\fix_srim_no_admin.ps1 -Root C:\SRIM-2013
```

The BAT wrapper does the same thing with PowerShell execution policy bypassed:

```cmd
fix_srim_no_admin.bat -Root C:\SRIM-2013
```

Useful check-only mode:

```powershell
.\fix_srim_no_admin.ps1 -Root C:\SRIM-2013 -CheckOnly -SkipExecutionTest
```

The script refuses protected folders such as `Program Files`, grants the current
user write access under the SRIM root, inspects compatibility flags, and removes
`RUNASADMIN` where possible.

## Notes

The script temporarily overwrites `SR Module/SR.IN` while running and restores
the previous file at the end. Avoid running multiple copies against the same
SRIM installation at the same time.

If SRModule output files are locked, close stale `SRModule.exe` processes from
Task Manager and retry. On WSL, the script calls Windows `cmd.exe` and
PowerShell through WSL interop, so the SRIM path should live under `/mnt/c` or
another Windows-mounted drive.
