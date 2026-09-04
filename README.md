# SRIM Gas-Recoil Automation

Automation scripts for running SRIM-2013 gas-recoil studies from Windows or
WSL. The repository keeps only the project-specific Python scripts, gas table,
and helper utilities; the SRIM-2013 installation itself stays outside Git.

## Install

1. Install SRIM-2013 on Windows.

   Recommended path:

   ```text
   C:\SRIM-2013
   ```

2. Clone this repository anywhere convenient.

3. Create your local SRIM path config:

   ```bash
   cp srim-root.example.txt srim-root.txt
   ```

   Edit `srim-root.txt` so the first non-comment line points to your SRIM
   installation:

   ```text
   C:\SRIM-2013
   ```

   WSL users may also use:

   ```text
   /mnt/c/SRIM-2013
   ```

4. Install optional plotting support:

   ```bash
   python -m pip install matplotlib
   ```

5. Check that both workflows can see SRIM and the gas table:

   ```bash
   python srim_gas_sweep.py --dry-run --no-plots
   python trim_qf_sweep.py --dry-run --no-plots
   ```

If SRIM asks for administrator permission, hangs behind a UAC dialog, or cannot
write output files, run the included Windows helper:

```powershell
.\fix_srim_no_admin.ps1 -Root C:\SRIM-2013
```

## Run the Full Analysis

Run SRModule first. This creates the deterministic stopping/range table used as
a reference for the TRIM comparison plots:

```bash
python srim_gas_sweep.py \
  --out results/srmodule_all_gases_0p1_500keV_100pts.csv \
  --plots-dir plots/srmodule_all_gases_0p1_500keV_100pts \
  --workdir work/srmodule_all_gases_0p1_500keV_100pts \
  --keep-work
```

Then run the TRIM/IONIZ quenching-factor sweep:

```bash
python trim_qf_sweep.py \
  --out results/trim_qf_all_gases_0p1_500keV_100pts_100ions.csv \
  --plots-dir plots/trim_qf_all_gases_0p1_500keV_100pts_100ions \
  --workdir work/trim_qf_all_gases_0p1_500keV_100pts_100ions \
  --srmodule-csv results/srmodule_all_gases_0p1_500keV_100pts.csv \
  --keep-work
```

On systems where `python` is Python 2, use `python3`.

## Repository Layout

```text
srim_gas_sweep.py       SRModule stopping/range sweep
trim_qf_sweep.py        TRIM/IONIZ quenching-factor sweep
GasDensities.csv        gas mixtures, densities, and pressures
fix_srim_no_admin.ps1   Windows permission and compatibility repair helper
fix_srim_no_admin.bat   BAT wrapper for the PowerShell helper
srim-root.example.txt   template for local SRIM path configuration
.gitignore              keeps SRIM binaries and generated outputs out of Git
```

Local/generated files are intentionally ignored:

```text
srim-root.txt
results/
plots/
work/
logs/
SRIM executables and data folders
```

## How the Code Works

`srim_gas_sweep.py` is the fast deterministic path. For each gas row and recoil
ion, it:

1. Reads `GasDensities.csv`.
2. Parses formulas such as `CF4` and mixture labels such as
   `HeCF4(60% He 40% CF4)`.
3. Reads SRIM element and compound metadata from `Data/ATOMDATA` and
   `Data/Compound.dat`.
4. Writes `SR Module/SR.IN`.
5. Runs `SR Module/SRModule.exe`.
6. Parses the SRModule stopping/range output.
7. Interpolates the raw SRIM table onto a log-spaced energy grid.
8. Writes CSV rows and optional plots.

Its main output is a table of electronic stopping, nuclear stopping, projected
range, straggling, and an electronic-stopping quenching proxy:

```text
QF_electronic(E) = integrated electronic stopping energy / initial ion energy
```

`trim_qf_sweep.py` is the slower Monte Carlo path. For each gas, ion, and
energy, it:

1. Builds `TRIM.IN`, `TRIMAUTO`, and a per-job `manifest.json`.
2. Runs `TRIM.exe`.
3. Collects files such as `IONIZ.TXT`, `RANGE.TXT`, and `PHONON.TXT`.
4. Integrates the `IONIZ.TXT` profile.
5. Writes ionization quenching-factor rows and optional comparison plots.

The default TRIM mode is `quick-depth-and-qf`, which performs two passes:

1. A quick depth scan estimates the ionization depth for each gas/ion/energy.
2. A production pass reruns TRIM with a target width based on that depth.

The TRIM quenching factor is:

```text
QF_ioniz(E) = integrated IONIZ energy / initial ion kinetic energy
```

Both quantities are SRIM-derived energy fractions. They are not detector
calibrations for scintillation, charge yield, or light yield.

## Configuration

The scripts find SRIM in this order:

1. `--srim-root`
2. `SRIM_ROOT` environment variable
3. `srim-root.txt`
4. the directory containing the script

Relative paths for `GasDensities.csv`, `results/`, `plots/`, and `work/` are
kept relative to this repository when possible. SRIM data files and executables
are read from `--srim-root`.

Useful shared options:

```text
--gases CF4                 run only matching gas rows
--ions H,He,C,F             choose explicit recoil ions
--emin-kev 1 --emax-kev 100 --points 20
--keep-work                 preserve raw generated inputs and outputs
--no-plots                  skip Matplotlib plots
--plot-only                 regenerate plots from an existing CSV
```

Useful TRIM options:

```text
--energies-kev 1,2,5,10      use exact energies instead of the log grid
--mode quick-depth-and-qf    default two-pass workflow
--mode quick-depth-scan      run only broad depth scans
--mode qf-production         run only QF production
--parse-only                 rebuild CSV from existing work folders
--n-ions 100                 production ions per job
--quick-n-ions 100           quick-scan ions per job
```

## Platform Notes

Supported:

```text
Windows Python 3.10+ with SRIM installed at C:\SRIM-2013
WSL Python 3.10+ calling the Windows install at /mnt/c/SRIM-2013
```

Pure Linux and macOS are not supported as-is because SRIM-2013 is a Windows
application. A Wine-based workflow may be possible, but it is not implemented
here.

Do not run multiple sweeps against the same SRIM installation at the same time.
The scripts temporarily overwrite `SR.IN`, `TRIM.IN`, and `TRIMAUTO`, then
restore the previous files when the run ends.
