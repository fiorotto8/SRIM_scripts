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

## Current goal: timing, statistical precision and pressure consistency

### Binding-energy input-reading canaries

All six Kr same-seed replays reproduced byte-for-byte, but all six TDATA files
report F/Kr lattice/surface energies of 2/4 eV despite zero-valued inputs.
Production remains on hold while `scripts/binding_probe.sh` tests eight isolated
50-ion cases: an archived-input baseline; zero-valued rows formatted with padded
columns, double spaces or commas; and distinct per-element diagnostic markers
with those formats plus the current single-space format. Marker energies are
diagnostic, not proposed gas physics. Seed, geometry and other inputs stay fixed.

```bash
bash scripts/binding_probe.sh start
bash scripts/binding_probe.sh status
bash scripts/binding_probe.sh logs
```

Results: `runs/binding_format_probe/index.html` and `results.json`, with raw
inputs/outputs retained in private runtime folders. Eight workers/cores, 32 GiB
hard RAM, no scan swap, 30-minute service limit and 10-minute per-case timeout.
No production files or PySRIM installation are edited by the probes, and their
outputs never enter the scan's independent statistics. `resume` reuses recorded
case results; it does not retry a recorded failure automatically.

### Current diagnostic gate: Kr same-seed reproducibility

The mixture scan completed but did not reproduce an apparent Kr/C/30 keV
concentration bend. Before more statistics or energy expansion, six isolated
same-seed replays repeat one original and one fresh-control job at each of
70/80/90% Kr, 1 atm. These runs use the original configurations and job IDs in
separate `runs/kr_repro_check/{old,new}` directories. They are **never pooled**
into independent-history counts or SEM estimates.

```bash
bash scripts/repro_check.sh start
bash scripts/repro_check.sh status
bash scripts/repro_check.sh logs
bash scripts/repro_check.sh stop
bash scripts/repro_check.sh resume
```

Open `runs/kr_repro_check/index.html`. Limits: 15 CPU cores, 32 GiB hard RAM,
zero scan swap, one cumulative hour and a one-hour service backstop. Previous
datasets stay unchanged. Input files, RANGE/IONIZ/TDATA outputs and numerical
summaries are compared explicitly. A separate check records whether TDATA
confirms the requested zero lattice/surface binding energies for all elements;
an archived sample unexpectedly reports nonzero values for F and Kr. This is
an unresolved input/output-consistency question, not yet an established cause.
The nine-point top-up and larger independent Kr sample remain on hold until
these diagnostic results have been reviewed. No fresh production is released
automatically by this diagnostic launcher.

### Current run: mixture refinement at 1 atm

The low-energy top-up finished with all 1,890 combined points meeting the
estimated SEM targets. The next fixed scan adds **65/75/85% noble gas** (CF4
remainder) for Ar/Kr/Xe, C/F/noble recoils, at **1 atm** and **1/3/10/30/100 keV**:
135 new points, eight independent 50-ion batches each (400 ions/point).

Twelve control points run first with **400 fresh ions each**, using repeat
indices 30–37, independent of all previous campaigns. The four fixed curves are:

- Kr, C recoils, 30 keV: 70/80/90% noble gas;
- Xe, Xe recoils, 30 keV: 80/90/95%;
- Kr, Kr recoils, 100 keV: 80/90/95%;
- Ar, Ar recoils, 100 keV: 60/70/80%.

In total: **1,176 new batches / 58,800 ions**, giving 2,025 combined physical
points. No 300/1,000 keV or additional-pressure simulations are automatically
released. The superset config must **not** be run directly.

```bash
bash scripts/mixture_scan.sh prepare  # freeze/check scope and forecast
bash scripts/mixture_scan.sh start
bash scripts/mixture_scan.sh status
bash scripts/mixture_scan.sh logs     # Ctrl-C exits the viewer, not the run
bash scripts/mixture_scan.sh stop
bash scripts/mixture_scan.sh resume  # also use after reboot
```

The detached service survives logout with lingering enabled. Open
`runs/noble_cf4_mixture_refinement/report/index.html`. Limits: 15 cores/workers,
24 GiB soft/32 GiB hard memory, no scan swap, **six cumulative hours across
resumes**, plus a six-hour service backstop. Other applications' swap is untouched.
Do not launch another SRIM campaign concurrently. All prior result folders stay
read-only. Control artifact checks, RAM availability, observed timing and a
remaining-budget check gate release of the new mixtures; failed precision alone
does not trigger extra histories or silently certify success.

The report separates `new_mixture_points.csv`, `independent_controls.csv`, and
`control_curvature.csv` from the pooled `points.csv`. Curvature is the middle
concentration minus linear interpolation of its two neighbors, with propagated
SEM. These contrasts are descriptive, not multiplicity-corrected significance;
fresh data, not the selected old or pooled data, are the independent check.
Final PDF/PNG plots cover all five new energies and all three recoil types.
`TARGETS_MET` refers only to estimated SEM targets in the combined grid and the
independent controls; otherwise the completed run reports `REVIEW_REQUIRED`.
Neither status establishes physical model accuracy or detector quenching.

Callback: “Check the mixture scan: independent control curvature, precision,
new concentration trends, timing, and whether to extend to 300 keV/1 MeV.”

### Current follow-up: finish low-energy precision

The seven-energy campaign completed on 18 September 2026: 1,826/1,890 points
met both estimated SEM targets. The remaining 64 points (47 at 1 keV, 15 at
3 keV, two at 10 keV) receive a **fixed twelve new 50-ion batches each**,
bringing each from 400 to 1,000 ions. All previous results stay read-only.

```bash
bash scripts/low_energy_topup.sh prepare  # freeze/check selection and forecast
bash scripts/low_energy_topup.sh start
bash scripts/low_energy_topup.sh status
bash scripts/low_energy_topup.sh logs     # Ctrl-C exits the viewer only
bash scripts/low_energy_topup.sh stop
bash scripts/low_energy_topup.sh resume  # also use after a reboot
```

Open `runs/noble_cf4_low_energy_topup/report/index.html`. This detached service
survives logout; it does not automatically resume after reboot. Limits are
15 CPU cores/workers, 24 GiB soft/32 GiB hard RAM, no scan swap, and **two
cumulative supervised hours across resumes**, with a two-hour service backstop.
Other applications' swap is untouched. Start only one SRIM campaign at a time.

The frozen plan contains source hashes, exact job IDs and a measured runtime
forecast. New repeat indices 10–21 cannot overlap existing batches; seeds are
also checked for reuse at each physical point. Only selected stopping tables
and 768 TRIM batches run. **Do not run the top-up superset config directly.**
Stopping tables retain the previously successful 1–1,000 keV interval; only
the TRIM recoil grid is restricted to 1/3/10 keV. A startup attempt with a
10 keV stopping-table upper bound stalled and was stopped before TRIM began;
its logs are preserved in `runs/noble_cf4_low_energy_topup_stopping10kev_attempt`.

The report provides the combined 1,890-point table, remaining precision flags,
and a separate `independent_new_points.csv` using only the new 600 ions at each
selected point. This independent cohort has fewer ions than the pooled estimate
and can miss the SEM targets even when the 1,000-ion estimate meets them.
Pooled intervals after selection remain descriptive, not guaranteed confidence
coverage. No new pressure tests or automatic further selection are performed;
the previous fixed-cohort pressure assessment is retained. Final status is
`TARGETS_MET` only if the full combined grid meets the estimated SEM targets;
otherwise `REVIEW_REQUIRED`. Missing/invalid outputs stop with `NEEDS_ATTENTION`.
Neither status certifies detector quenching or physical model accuracy.

When calling back: “Check the low-energy top-up, independent-batch precision,
remaining flags, actual timing, and whether the mixture grid needs refinement.”

### Completed seven-energy precision campaign

The goal is a reproducible **SRIM-only** map of recoil range and ionization
fraction in Ar/Kr/Xe–CF4 over 1 keV–1 MeV, with measured compute costs and
honest statistical limitations. Detector-calibrated quenching and SRIM model
systematics are not established by adding Monte Carlo histories.

The completed first production run remains read-only at
`runs/noble_cf4_staged/production`. Its follow-up is launched/monitored with:

```bash
bash scripts/precision_scan.sh start
bash scripts/precision_scan.sh status
bash scripts/precision_scan.sh logs
bash scripts/precision_scan.sh stop
bash scripts/precision_scan.sh resume
```

Open **`runs/noble_cf4_precision/report/index.html`**. The detached user service
survives logout with lingering enabled. Resume manually after a reboot.
Limits remain 15 CPUs/workers, 24 GiB soft/32 GiB hard memory, zero scan swap.
Existing system swap from other applications is not cleared or disabled.

This bounded follow-up performs:

1. Six 100-ion noble-recoil jobs (Ar/Kr/Xe, 80/20, 1 atm, 100/1000 keV)
   to compare measured batch-size timing against the completed 50-ion jobs.
   These different-sized batches are diagnostic only and are not mixed into
   equal-batch precision estimates. Data and resource checks gate further work.
2. Two additional 50-ion batches at all 1,080 existing points, bringing them
   to four independent batches (200 histories).
3. Four 50-ion batches at 810 new points at **3, 30, 300 keV**. The resulting
   seven-energy grid has 1,890 physical points, each with at least four batches.
4. Exactly one adaptive selection, frozen before collecting new data. Whole
   six-pressure groups are eligible if any point exceeds **5% range SEM or
   2% ionization-fraction SEM**, or its pressure test is flagged/indeterminate.
   Selected groups receive four more batches, up to **400 ions per point**.
   Cheapest complete groups are admitted first if the remaining budget is tight;
   deferred groups are explicitly listed, not silently marked complete.

The hard campaign budget is **72 cumulative supervised hours across resumes**,
with a six-hour reserve in phase admission estimates. A 73-hour service limit
is a backstop. Admission uses measured costs plus a 50% planning margin.
No automatic increase beyond 400 histories, seven energies, or this budget is
allowed. **Never run these superset configs with `--stage all` directly**:
the supervisor selects the intended batches and prevents duplicate baseline
histories. The original repeats 0/1 are reused read-only; new repeat indices
start at 2. All input hashes and the adaptive job list are frozen.

Statistics are reported separately from operational completion:

- Standard errors are computed from independent equal-size batch means;
  Student-t intervals are descriptive. Final adaptive intervals do not have
  guaranteed simultaneous 95% coverage. The goals are estimated SEM thresholds,
  not a guaranteed accuracy of the physics model.
- Pressure screening uses **only the fixed first four batches** at each
  pressure, Welch ANOVA for ionization fraction and pressure×projected range,
  and Benjamini–Yekutieli multiple-test adjustment. Batch normality and
  independence are assumptions; non-rejection does not prove equivalence.
- Independent confirmation uses **only the next four batches** of selected
  pressure groups. Constant/undefined variances are labelled indeterminate.
  Persistent differences warrant checking model/input/rounding assumptions,
  not automatically declaring new pressure-dependent physics.
- Final status is `TARGETS_MET` only if estimated precision targets are met,
  no independent pressure flags remain, and no groups were budget-deferred.
  Otherwise it is `REVIEW_REQUIRED`; raw successes never hide unresolved science.

Useful files: `status.json`, `budget.json`, `science_status.json`,
`timing_check.json`, `initial_plan.json`, `adaptive_plan.json`,
`completion.json`, `resource_samples.csv`, and `logs/campaign.log`.
Reports include points/batches CSV, ROOT, PDFs, and separate fixed-cohort and
independent-confirmation pressure-test CSVs. The analysis dependency is pinned
to `scipy==1.18.1` (install in `.venv` when upgrading an older checkout).

To resume this work with Codex, say: **“Check `runs/noble_cf4_precision` against
the SRIM timing/precision goal: operational status, budget, independent pressure
checks, unresolved SEM targets, timings, memory and recommended next steps.”**

## Detached staged Ar/Kr/Xe production scan on Ubuntu

```bash
bash scripts/staged_scan.sh start
bash scripts/staged_scan.sh status
bash scripts/staged_scan.sh logs        # Ctrl-C stops viewing, not the scan
bash scripts/staged_scan.sh stop        # safely stop/checkpoint
bash scripts/staged_scan.sh resume      # same frozen configs; keep completed batches
```

The detached `srim-staged-scan` user service automatically runs:

1. Krypton timing: 24 points, five ions each, at 60/95%, 1 atm and 1/10/100/1000 keV.
2. All production SRModule tables, then 12 representative 50-ion noble-recoil
   batches spanning Ar/Kr/Xe and the heavy high-energy cases.
3. A commissioning gate checks required results, numeric bounds, seeds, raw
   archive hashes, profiles, memory and measured timing. It permits production
   only if the conservative forecast is at most 60 hours and the estimated
   slowest batch is at most four hours. Failures stop the sequence for review.
4. Full direct production: three gases, 60/70/80/90/95% noble fraction, six
   pressures (0.5–3 atm), C/F/noble recoils, and 1/10/100/1000 keV.
   **1,080 points × two independently seeded 50-ion batches = 108,000 histories.**
   The 12 commissioning batches are reused without recomputation.

The five-ion Ar/Xe timing reference is the existing
`runs/noble_cf4_timing_pilot`; it must be complete. Other previous runs are
neither modified nor mixed into production. This is an SRIM-only analysis;
no cross-code physical comparison or detector-response combination is made.

Open **`runs/noble_cf4_staged/report/index.html`**. Live products include
combined `points.csv`, individual `batches.csv`, ROOT `points`/`batches` TTrees,
an updated summary PDF, PNG/SVG plots, and a final 30-page cross-gas PDF atlas
covering every mixture/pressure combination. Original detailed profiles and
inputs remain in `production/profiles` and `production/raw`.
The ROOT writer is pinned to `uproot==5.7.6` (install in `.venv` if upgrading an
older checkout). ROOT export is read back and checked against table values.

Two batches provide only a rough between-batch SEM (one variance degree of
freedom), not a convergence guarantee or model uncertainty. A single batch
has missing SEM, not zero uncertainty; plots omit its error bar. Precision
flags identify >5% relative batch SEM. Longitudinal spread combines reported
batch variances with between-batch mean offsets. Gas pressure enters through
the SRIM density model, not a measured pressure-dependent ionization response.

Limits are 15 allowed CPUs, 15 workers, 24 GiB soft/32 GiB hard cgroup memory,
and no scan swap. A user-service lifetime limit of 96 hours prevents an
unbounded unattended run; individual production invocations have a 72-hour
budget with six hours reserved for draining. This is a one-shot staged run,
not an automatic increase to 1,000 histories or a denser energy grid.

Logout persistence must be enabled: `loginctl enable-linger "$USER"`.
The launcher checks it before starting. The service survives closing SSH or
VS Code, but is **not automatically restarted after a reboot**: use `resume`.
Completed batches survive stop/restart; interrupted batches are rerun.

Monitoring files in `runs/noble_cf4_staged`:

- `status.json`: phase, latest heartbeat and resource sample (updated about every five seconds).
- `gate.json`: measured forecast, limits, validation findings, and release decision.
- `resource_samples.csv`: whole-cgroup CPU/RAM, host available RAM and swap.
- `logs/campaign.log`: all stages, errors and checkpoint progress.
- `krypton_validation.json`, `final_validation.json`: operational/artifact checks.

An inactive service with stale `RUNNING` state is not success; the status
command explicitly flags it. Inspect `journalctl --user -u srim-staged-scan`
for launcher/resource-limit failures. To ask Codex for a follow-up, say:
“Check the SRIM staged scan in `runs/noble_cf4_staged`: status, gate, errors,
timings, memory, and result completeness. Do not change it without asking.”

## Small 1 keV–1 MeV timing pilot on Ubuntu

```bash
bash scripts/start_timing_pilot.sh
```

Open `runs/noble_cf4_timing_pilot/report.html` for live results, per-point
timings and RAM measurements. The grid deliberately has just **48 TRIM
points**: Ar and Xe, 60/95% noble gas, 1 atm, C/F/noble recoils, and
**1, 10, 100, 1,000 keV**. Five ions per point (240 total) are for timing
commissioning only, not a precision quenching study. Kr and extra pressures
are deferred until costs have been measured. SRModule also uses four energies.

The launcher uses a user systemd service, capped at **15 allowed CPUs and
15 workers**, with a 24 GiB soft RAM limit, 32 GiB hard limit, and zero swap
allocation for the scan. It requires at least 16 GiB available RAM at startup.
All descendants, including Wine and Xvfb, inherit the CPU affinity and belong
to the resource-limited cgroup. Resource samples are saved every five seconds;
the HTML refreshes every 30 seconds. The launcher checks that the previous
preliminary run has no unresolved display-watchdog rejection.

```bash
systemctl --user status srim-timing-pilot
systemctl --user stop srim-timing-pilot   # checkpoint-preserving stop
# Resume with the same start command. Diagnose launcher errors with:
journalctl --user -u srim-timing-pilot --no-pager -n 50
```

Use `timing_results.csv` and `resource_samples.csv` to size the next scan.
Per-point time includes Wine startup, target enlargement when needed, parsing
and archiving; multiplying five-ion times is only a rough estimate for a
larger run. Independent repeats and multiple batch sizes are needed before
claiming stable throughput or uncertainty. The dedicated report is specific
to this 48-point configuration. Never edit a run's frozen TOML to scale it;
create a new configuration and run directory.

Display monitoring retries transient inspection failures three times,
preserves stderr, and still stops on persistent display loss or blocking
dialogs. Xvfb diagnostics are saved as `.xvfb.log` in each private runtime
and successful TRIM archives. Recover an exact rejected point with
`srim_scan.py run --config ... --run-dir ... --stage trim --resume --job-id ID`.
Do not run two schedulers against the same checkpoint.

Clearing existing system swap needs administrator access and sufficient
available RAM: `sudo swapoff /swap.img && sudo swapon /swap.img` on cygno03.
The scan's no-swap limit does not disable swap for unrelated applications.

## Fast preliminary results on the Ubuntu host

With the existing native Ubuntu setup (`.venv`, sibling `pysrim`, `srim-2013`
and `.wine-srim`), run:

```bash
bash scripts/start_preliminary.sh
```

Open `runs/noble_cf4_preliminary/report.html` in a browser. It updates once a
minute and links downloadable CSVs and plots. It also shows existing results
from `runs/noble_cf4_7day_ubuntu`, when present, without modifying that run.

The coarse scan has 270 **direct** full-cascade TRIM points: Ar/Kr/Xe,
60/80/95% noble gas, 1/3 atm, all three constituent recoil species, and five
log-spaced energies (0.1, 0.841, 7.071, 59.460, 500 keV). It uses 20 isolated
workers and only **20 ions per point**: low-statistics exploratory results,
not converged precision estimates. Deterministic SRModule curves retain 100
energies. Low-energy points are scheduled first and gas families interleaved
to deliver broad coverage early. No pressure interpolation or convergence
campaign is performed. There is a 24-hour session limit with a two-hour drain
margin; rerun the same command to resume if necessary.

```bash
# Use fewer workers on smaller hosts:
bash scripts/start_preliminary.sh --workers 8
# Status, or an independent one-time report refresh:
.venv/bin/python srim_scan.py status --run-dir runs/noble_cf4_preliminary
.venv/bin/python scripts/preliminary_report.py
# Stop safely (keep all completed results):
bash scripts/stop_ubuntu_7day.sh runs/noble_cf4_preliminary
```

Do not change the frozen configuration of an existing run. For different
history counts or grids, copy the preliminary TOML and use a new run directory
with `srim_scan.py run --config ... --run-dir ... --stage all --resume`.
The report distinguishes SRModule's electronic-stopping proxy from full TRIM
primary-plus-secondary ionization fraction; neither is a calibrated detector
response. Its dedicated quick-look layout targets the supplied 270-point,
20-ion configuration, not arbitrary custom grids.

If VS Code indexes this workspace, exclude Wine `dosdevices` links and
generated `runs` from searches and file watching; `dosdevices/z:` points to
the whole filesystem. Disable symlink-following and npm autodetection when
unneeded. File indexing should not consume the scan's CPU allocation.

## Docker quick start (recommended)

The `main` branch runs the Xe/CF4, Kr/CF4, and Ar/CF4 scan in Docker and uses
the adjacent `fiorotto8/pysrim` source tree directly. Both repositories are
bind-mounted, so a source edit is visible to the next container invocation;
the SRIM installation and 32-bit Wine prefix live in separate named volumes.

Clone the repositories as siblings and select the pinned PySRIM revision:

```bash
mkdir SRIM-workspace
cd SRIM-workspace
git clone https://github.com/fiorotto8/pysrim.git
git -C pysrim checkout 9b8e1b7e91fe65bf17e9366e1fd9b9406ad125d2
git clone --branch main https://github.com/fiorotto8/SRIM_scripts.git
cd SRIM_scripts
```

Docker Engine with the Compose plugin is the only host runtime dependency.
Build the image and preserve host ownership for generated results:

```bash
export SRIM_DOCKER_UID=$(id -u)
export SRIM_DOCKER_GID=$(id -g)
docker compose build
docker compose run --rm srim
```

The last command prints the exact standard workload. On the first container
run, the entrypoint verifies the PySRIM commit and SRIM archive checksum,
extracts SRIM into its named volume, and initializes the Wine components. It
does not modify the PySRIM checkout.

Run the real, isolated 10-ion canary before starting a campaign:

```bash
docker compose run --rm srim python srim_scan.py preflight \
  --config configs/noble_cf4_standard_docker.toml --canary-ions 10
```

The complete direct-Monte-Carlo scan is resumable by stage:

```bash
docker compose run --rm srim python srim_scan.py run \
  --config configs/noble_cf4_standard_docker.toml \
  --run-dir runs/noble_cf4_standard_docker --stage srmodule --workers 4 --resume
docker compose run --rm srim python srim_scan.py run \
  --config configs/noble_cf4_standard_docker.toml \
  --run-dir runs/noble_cf4_standard_docker --stage trim --workers 4 --resume
docker compose run --rm srim python srim_scan.py run \
  --config configs/noble_cf4_standard_docker.toml \
  --run-dir runs/noble_cf4_standard_docker --stage convergence --workers 4 --resume
docker compose run --rm srim python srim_scan.py validate \
  --run-dir runs/noble_cf4_standard_docker
docker compose run --rm srim python srim_scan.py report \
  --run-dir runs/noble_cf4_standard_docker
```

The standard scan is scientifically strict but extremely expensive. Read the
runtime estimate below before starting all 43.2 million baseline histories.
Use `--limit 1` while commissioning; it limits newly selected jobs without
changing the persisted grid.

### Minimal examples

The preflight command above is the smallest real SRIM example: one SRModule
case and one 10-ion TRIM case, executed against a disposable runtime. To learn
the legacy CSV input format without launching SRIM, run:

```bash
docker compose run --rm srim python srim_gas_sweep.py \
  --srim-root /data/srim-volume/srim-2013 \
  --gas-csv examples/GasDensities_minimal.csv \
  --points 3 --dry-run --no-plots
```

`examples/GasDensities_minimal.csv` contains one CF4 condition. Copy it and
add rows using the same three columns for a small custom teaching example.

### Installing PySRIM without Docker

Keep PySRIM beside this repository and install the pinned checkout in editable
mode. Editable installation is what keeps Python synchronized with local
PySRIM changes:

```bash
git clone https://github.com/fiorotto8/pysrim.git ../pysrim
git -C ../pysrim checkout 9b8e1b7e91fe65bf17e9366e1fd9b9406ad125d2
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip setuptools wheel
./.venv/bin/python -m pip install -e ../pysrim matplotlib
```

On Ubuntu, `scripts/bootstrap_ubuntu.sh --install-system-deps` performs this
installation and also prepares SRIM, Wine, Xvfb, and the required Visual Basic
components. In Docker, `PYTHONPATH=/workspace/pysrim` points at the read-only
bind mount instead of copying or repackaging PySRIM.

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

## Native-host requirements

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

The PySRIM adapter sets the private worker's `Data/TRIM.cfg` secondary-recoil
allocation to 2,000,000. The bundled 200,000 limit caused heavy 500-keV Xe
cascades to open a blocking warning on this host. This allocation change does
not change the input gas composition, energies, binding energies, cascade mode,
or requested histories. SRIM's [input manual, section 8-31](https://www.srim.org/SRIM/SRIM%2008.pdf)
documents this setting; the dialog's reference to `SRIM.cfg` is misleading.

TRIM runs under a display watchdog requiring `xwininfo` (`x11-utils` on
Ubuntu, included in the Docker image). It checks every two seconds for the
visible TRIM/error dialogs, saves a screenshot in the worker runtime, and
rejects the job without dismissing the warning. The stage stops launching new
work on a dialog/watchdog failure and leaves the affected job pending. Other
active workers may finish normally. The monitor does not infer completion from
a live process or a fresh heartbeat; `scripts/status_ubuntu_7day.sh` shows the
heartbeat and cumulative TRIM CPU seconds so activity can be checked across
successive samples.

Each new raw archive includes `Data/TRIM.cfg`, its hash in the manifest, and
the monitor/process logs. The September 9 recovery retained the three completed
TRIM jobs, backed up the checkpoint and four interrupted inputs/outputs under
`runs/noble_cf4_7day_ubuntu/provenance/recoil-limit-incident-20260909/`, and
restarted the interrupted jobs from their original seeds. Campaign runtime
already spent waiting at the dialogs remains charged against the 168-hour cap.

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
