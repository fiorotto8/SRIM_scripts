# SRIM Noble-Gas/CF4 Automation

This repository drives reproducible SRIM-2013 scans of Xe/CF4, Kr/CF4, and
Ar/CF4 gas mixtures. The canonical scan covers:

- noble-gas/CF4 mole fractions from 60/40 through 95/5 in 5 percentage-point steps;
- pressures from 0.5 through 3.0 atm in 0.5-atm steps at 293.15 K;
- the constituent recoil species (noble element, C, and F);
- 100 logarithmic recoil energies from 0.1 through 500 keV;
- 1,000 full-cascade TRIM histories per gas/ion/energy point;
- three independent 10,000-ion convergence repeats at selected corner points.

The scan contains 144 gas conditions, 432 gas/ion combinations, 432 SRModule
jobs, 43,200 baseline TRIM jobs, and 324 convergence jobs.

## Scientific scope

SRIM supplies stopping powers, ion ranges and straggling, collision transport,
and depth-dependent energy-partition profiles. This project reports two
different electronic-energy fractions:

```text
qf_trim_ionization = integral(IONIZ from primary ion + recoils) / initial energy
srmodule_electronic_fraction_proxy = integral[Se/(Se+Sn) dE] / initial energy
```

The TRIM value is the canonical SRIM ionization fraction. Neither quantity is
a detector calibration for charge, light, or scintillation.

SRIM requires density as input. Mixture densities here are calculated with the
ideal-gas law using natural atomic masses read from SRIM `ATOMDATA`. SRIM's
manual states that its gas stopping model always assumes STP. Changing pressure
therefore changes density and range scaling, but SRIM cannot predict genuine
non-STP pressure dependence of stopping or quenching.

SRIM does not calculate electron drift velocity or diffusion, attachment,
W-values, Fano factors, recombination, avalanche gain, or detector response.
Vacancy, displacement, and phonon-named outputs are retained for provenance but
are marked as SRIM internals rather than physical gas material-damage results.

See the official [SRIM site](https://www.srim.org/) and
[TRIM input manual](https://www.srim.org/SRIM/SRIM%2008.pdf).

## Requirements

- SRIM-2013 installed on Windows/WSL, or Wine plus Xvfb on native Linux.
- Python 3.11 or newer.
- Matplotlib is optional and is used only for report plots.

The SRIM location is selected in this order: `--srim-root`, the first active
line of `srim-root.txt`, then the directory containing the scripts. A WSL path
or a Windows path may be used.

Do not run a legacy script or a manual SRIM calculation against a worker runtime
while `srim_scan.py` is using it. The source SRIM installation is never edited
by the canonical pipeline.

## Ubuntu with fiorotto8/pysrim

The native-Linux configuration uses the
[`fiorotto8/pysrim`](https://github.com/fiorotto8/pysrim) fork through Wine and
an isolated Xvfb display. It is pinned to commit
`9b8e1b7e91fe65bf17e9366e1fd9b9406ad125d2`. The adapter supplies the explicit
safe YAML loader required by current PyYAML, runs each PySRIM job in a separate
Python process, and retains the main pipeline's timeouts and private SRIM
worker directories.

Use this sibling layout under `~/Documents/SRIM`:

```text
~/Documents/SRIM/
├── pysrim/          existing fiorotto8/pysrim checkout
├── SRIM_scripts/    this repository
├── srim-2013/       created from pysrim's pinned SRIM archive
└── .wine-srim/      generated 32-bit Wine prefix
```

On the Ubuntu machine, enter this repository and run the one-time bootstrap:

```bash
cd ~/Documents/SRIM/SRIM_scripts
./scripts/bootstrap_ubuntu.sh --install-system-deps
```

The script verifies both the PySRIM commit and bundled SRIM archive hash,
installs the Ubuntu/Python dependencies, creates `.venv`, extracts SRIM beside
the two repositories, initializes the Wine prefix, and registers the Visual
Basic/OCX components. It refuses to overwrite a non-empty incomplete SRIM
directory or to alter a PySRIM checkout at a different commit.

Run the real executable canary once:

```bash
./scripts/run_ubuntu_preflight.sh
```

Then invoke the same resumable session command on at most five days. The TOML
configuration supplies four workers, an eight-hour hard budget, and a
75-minute drain margin; command-line options can still override the worker
count.

```bash
./scripts/run_ubuntu_session.sh
./.venv/bin/python srim_scan.py status --run-dir runs/noble_cf4_40h_ubuntu
```

After all stages report complete:

```bash
./.venv/bin/python srim_scan.py validate --run-dir runs/noble_cf4_40h_ubuntu
./.venv/bin/python srim_scan.py report --run-dir runs/noble_cf4_40h_ubuntu
```

The bootstrap and full SRModule/TRIM canary were exercised in a clean Ubuntu
24.04 container with Python 3.12, Wine 9, and no display. A separate four-worker
test completed eight concurrent SRModule jobs with no failures and recorded
the pinned PySRIM commit in `provenance/execution.json`.

## Strict seven-day Ubuntu campaign

`configs/noble_cf4_7day_ubuntu.toml` is the strict direct-Monte-Carlo
configuration. It retains all six pressures and all 100 energies, uses 1,000
full-cascade ions at each of the 43,200 baseline points, and does not enable
pressure scaling, interpolation, or derived rows. It also includes all 432
SRModule jobs and 324 configured 10,000-ion convergence repeats.

The detached launcher uses a PID file plus `flock`, creates timestamped logs,
and always supplies `--resume`. Campaign sessions are persisted in SQLite. An
abandoned session is conservatively charged through the next restart, so
relaunching cannot reset the cumulative 168-hour allowance. No new baseline
work starts during the final 48 hours, leaving the measured slow tail time to
drain; the hard deadline still stops and returns an unfinished job to pending.
Every SRIM worker also has a private SRIM runtime and Wine prefix, and its
wineserver is shut down after every successful job, failure, timeout, or
interrupt.

Start or resume the production directory in the background:

```bash
cd ~/Documents/SRIM/SRIM_scripts && ./scripts/start_ubuntu_7day.sh
```

Operational commands:

```bash
./scripts/status_ubuntu_7day.sh
./scripts/logs_ubuntu_7day.sh
./scripts/stop_ubuntu_7day.sh
./scripts/start_ubuntu_7day.sh
./.venv/bin/python srim_scan.py validate --run-dir runs/noble_cf4_7day_ubuntu
./.venv/bin/python srim_scan.py report --run-dir runs/noble_cf4_7day_ubuntu
```

Commissioning on `cygno03` detected an Intel Core Ultra 7 265 with 20 physical
cores/20 logical CPUs, 62 GiB RAM (about 52 GiB available), and about 1.47 TiB
free on the NVMe-backed filesystem. A repeatable eight-case full-cascade
benchmark covered C, F, Ar, Kr, and Xe recoils, pressure/composition extremes,
0.1 through 500 keV, and a slow Xe 500 keV case. Measured throughput was:

```text
workers    histories/s    bundle wall time
1          0.04075        196.30 s
2          0.08065         99.20 s
4          0.10511         76.11 s
8          0.09606         83.28 s
```

Four workers are therefore selected. The benchmark summary, host inventory,
PySRIM commit, SRIM hashes, raw-output checks, and estimate are recorded at
`runs/noble_cf4_7day_ubuntu/provenance/benchmark.json`. The estimate applies
the measured high-energy-weighted throughput to 43.2 million baseline plus
3.24 million convergence histories, then adds a 10% retry allowance, a 25%
safety factor, and 49 hours for drain/validation/reporting: approximately
168,794 hours (19.26 years). This is conservative because the benchmark is
weighted toward 500 keV, but it exceeds seven days by about 1,000 times; strict
completion inside 168 hours is not credible. In particular, one 10,000-ion
heavy 500 keV convergence repeat can itself exceed the campaign allowance.

The separately labelled feasible alternative for a short campaign remains
`configs/noble_cf4_40h_ubuntu.toml`. Its pressure-scaled and energy-interpolated
rows are explicitly derived results and must never be described as direct
full-scan Monte Carlo output.

## Five-session, 40-hour campaign

The measured full-cascade runtime makes the original 43.2-million-history grid
impractical on this machine. `configs/noble_cf4_40h.toml` is the recommended
time-boxed campaign. It retains all 144 gas conditions and all 43,200 final
gas/ion/energy rows, while distinguishing direct from derived results:

- 576 direct full-cascade TRIM anchors: eight energies, 50 ions, at 1 atm;
- pressure ranges and depth coordinates scaled with ideal-gas density;
- QF and range values interpolated onto the 100-point energy grid;
- 324 direct convergence jobs at both pressure endpoints, three repeats and
  25 ions per repeat; points above 2% RSE are explicitly warned;
- four isolated workers and a 75-minute drain margin per eight-hour session.

Run preflight once:

```bash
python3 srim_scan.py preflight --config configs/noble_cf4_40h.toml
python3 srim_scan.py plan --config configs/noble_cf4_40h.toml
```

Then run this same command on at most five separate days/sessions. Each
invocation stops launching jobs after 6 hours 45 minutes and allows active jobs
up to one hour to drain, leaving shutdown/archiving margin:

```bash
python3 srim_scan.py run \
  --config configs/noble_cf4_40h.toml \
  --run-dir runs/noble_cf4_40h \
  --stage all --workers 4 --resume --wall-hours 8 --stop-margin-minutes 75
```

After each session:

```bash
python3 srim_scan.py status --run-dir runs/noble_cf4_40h
```

After completion:

```bash
python3 srim_scan.py validate --run-dir runs/noble_cf4_40h
python3 srim_scan.py report --run-dir runs/noble_cf4_40h
```

The direct Monte Carlo values remain in `trim_anchor_results.csv`; the expanded
table is `trim_results.csv`. Every expanded row is labelled `direct`,
`pressure_scaled`, `energy_interpolated`, or both, and identifies its source job
and profile. These derived rows must not be represented as independent TRIM
Monte Carlo calculations.

Commissioning with the local environment exposing four physical cores ran four concurrent 500 keV,
50-ion jobs in 8.6, 12.3, 22.6, and 23.7 minutes, all on their first attempt.
A deliberately conservative species-scaled estimate is about 11–16 total wall
hours for the direct anchor plus convergence workload. This is an estimate from
four high-energy combinations, not a runtime guarantee; the five-session limit
provides substantial contingency. A hard deadline inside the runner returns an
unfinished job to `pending` before an eight-hour session expires.

## Strict 43.2-million-history scan

The following original configuration is retained for reproducibility, but its
measured runtime is incompatible with the five-session local-machine limit.

Run the installation and executable canaries first. Canaries use a temporary,
private copy of SRIM and remove it after a successful check.

```bash
python3 srim_scan.py preflight --config configs/noble_cf4_standard.toml
python3 srim_scan.py plan --config configs/noble_cf4_standard.toml
```

Run each resumable stage:

```bash
python3 srim_scan.py run \
  --config configs/noble_cf4_standard.toml \
  --run-dir runs/noble_cf4_standard \
  --stage srmodule --resume

python3 srim_scan.py run \
  --config configs/noble_cf4_standard.toml \
  --run-dir runs/noble_cf4_standard \
  --stage trim --workers 1 --resume

python3 srim_scan.py run \
  --config configs/noble_cf4_standard.toml \
  --run-dir runs/noble_cf4_standard \
  --stage convergence --workers 1 --resume

python3 srim_scan.py validate --run-dir runs/noble_cf4_standard
python3 srim_scan.py report --run-dir runs/noble_cf4_standard
```

Use `status` at any time:

```bash
python3 srim_scan.py status --run-dir runs/noble_cf4_standard
```

Interrupted jobs are returned to the pending queue on the next `--resume`.
Existing completed jobs and raw results are not overwritten. For a short
commissioning run, append `--limit N` to a run stage. More than one worker is
supported by isolated SRIM runtime copies; start with one worker and increase
only after the preflight and a limited run pass on the machine.

The equivalent one-command execution is available, but separate stages are
recommended because TRIM target widths depend on completed SRModule results:

```bash
python3 srim_scan.py run \
  --config configs/noble_cf4_standard.toml \
  --run-dir runs/noble_cf4_standard \
  --stage all --workers 1 --resume
```

## Output layout

Each run directory contains:

```text
gas_conditions.csv             ideal-gas inputs, SRIM masses, composition provenance
srmodule_results.csv           stopping, range, straggling, electronic proxy
trim_results.csv               direct or provenance-labelled expanded TRIM table
trim_anchor_results.csv        direct anchors for a derived/time-boxed campaign
convergence_results.csv        independent high-statistics repeats
jobs.sqlite                    resumable job state and errors
raw/{stage}/*.tar.gz           inputs, raw SRIM files, hashes, manifest
profiles/{stage}/*.json.gz     normalized depth-dependent profiles
validation.{json,csv}          completeness and consistency checks
plots/*.png                    compact family-level summaries
report.html                    validation and interpretation report
provenance/                    frozen configuration and SRIM hashes/versions
runtime/worker-*               private SRIM worker installations
```

Raw TRIM bundles retain `TDATA`, `RANGE`, `LATERAL`, `IONIZ`, `E2RECOIL`,
`PHONON`, `VACANCY`, `NOVAC`, and any generated backscatter, transmission, or
3-D endpoint files. Full collision histories and EXYZ trajectories are not
enabled for the 43,200-job baseline scan.

## Validation gates

`validate` fails if jobs or artifacts are missing, metadata or numerical bounds
are invalid, target windows remain clipped, pressure-invariant SRIM quantities
change unexpectedly, raw hashes do not match, 3-D convergence endpoints are
absent, or the selected convergence repeats exceed 2% relative standard error.
The two convergence pressures are also compared using their combined repeat
uncertainty. Differences between the deterministic SRModule range and the TRIM
Monte Carlo range are reported diagnostically rather than silently merged.

Run the unit tests with:

```bash
python3 -m unittest discover -s tests -v
```

## Legacy entry points

`srim_gas_sweep.py` and `trim_qf_sweep.py` remain available for older gas CSV
workflows. They are not the canonical interface for the noble-gas/CF4 scan.
The WSL executable launch bug has also been corrected in both legacy backends.

If an older installation asks for elevation or cannot write files, run from
Windows PowerShell:

```powershell
.\fix_srim_no_admin.ps1 -Root C:\SRIM-2013
```
