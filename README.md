# SRIM Noble-Gas/CF4 Automation

This repository drives reproducible SRIM-2013 scans of Xe/CF4, Kr/CF4, and
Ar/CF4 gas mixtures. The original full-scan configuration (not the completed
staged campaign described below) covers:

- noble-gas/CF4 mole fractions from 60/40 through 95/5 in 5 percentage-point steps;
- pressures from 0.5 through 3.0 atm in 0.5-atm steps at 293.15 K;
- the constituent recoil species (noble element, C, and F);
- 100 logarithmic recoil energies from 0.1 through 500 keV;
- 1,000 full-cascade TRIM histories per gas/ion/energy point;
- three independent 10,000-ion convergence repeats at selected corner points.

The scan contains 144 gas conditions, 432 gas/ion combinations, 432 SRModule
jobs, 43,200 baseline TRIM jobs, and 324 convergence jobs.

## Current goal: timing, statistical precision and pressure consistency

### Execution roadmap: review and launch one stage per `continue`

The durable plan is [`configs/campaign_roadmap.json`](configs/campaign_roadmap.json).
It records scope, dependencies, release criteria, conditional interventions,
resource limits and the procedure to follow on every user `continue`.
This is **not an unattended queue or calendar automation**. If the current stage
is still running, report progress without starting another. After it finishes,
audit the evidence, update `review_history` and estimates, insert any necessary
bounded cross-check, then implement/test/freeze and launch only the next stage.
Later-stage launchers are planned, not pre-authorized to bypass their gates.

Working final target: **3,024 direct points** = Ar/Kr/Xe × eight noble fractions
(60–95% in 5% steps) × six pressures (0.5–3 atm in 0.5-atm steps) × seven energies
(1/3/10/30/100/300/1000 keV) × C/F/noble recoil. Begin with four independent
50-ion batches per point and add fixed independent cohorts where justified.
The existing 243 **surface-injection** points remain separate. The compatible
243-point bulk baseline is now complete and audited: a 27-point independent
pilot (C3a), then 216 points from C3b/C3bR, with four batches per point.
No surface or paired-diagnostic histories enter that baseline.
This goal is SRIM range and ionization-fraction mapping, not a
claim of detector-calibrated quenching.

| Stage | Work and review gate | Expected time | Run cap |
| --- | --- | --- | --- |
| D0 | Current 36-case pressure/depth diagnostic | 0.5–1 h compute | 2 h |
| R1 | Interpret pressure/header discrepancy and define the accepted observable/geometry | 0.5–2 h active review | No simulation |
| C2a | Original geometry attempt stopped at the mode-5 completion dialog; remaining 26 cases held | 30 min actual, preserved | 3 h |
| C1a | Observe-only reproduction and exact-completion-dialog fix canary | Both probes completed in 31 s total | 4 min each |
| C2b | Completion-aware retry: 28/28 cases completed and audited | 20.4 min actual | 3 h |
| C2c | All 19 canaries passed; high-energy phase stopped by display startup failure | 5.2 min actual | 6 h |
| C1b | Atomic display allocation: 60/60 synchronized startup/isolation/cleanup checks passed | Under 1 s actual | 5 min |
| C2d | All 37 cases and 18 depth pairs audited complete | 71 min retry; 76 min cumulative | Original 6 h cumulative, including C2c |
| C3a | Independent bulk pilot audited: 27 points, 108 batches; 22 points meet both precision targets | 89 min actual | 6 h |
| C3b | 132 cases completed, all 42 depth pairs passed; completion-window inspection race stopped remaining work | 3.65 h actual, preserved | Original 12 h cumulative |
| C1c | Completion monitor fix and one-ion integration check; raw numeric validation passed | 27 s actual | 5 min |
| C3bR | All 906 cases audited; 243 bulk points complete | 3.28 h recovery; 6.93 h C3b/recovery cumulative | Original 12 h cap |
| P2a | All eighteen 50-ion pressure canaries raw-audited; containment and input gates pass | 1 h 49 min actual | 6 h |
| P2b | All eighteen four-batch pressure points audited; all meet estimated SEM targets | 2 h 6 min actual | 8 h |
| C1d | All 60 display checks and the exact diagnostic replay passed | 32.4 s actual | 5 min; 6 min service failsafe |
| M3 | Stopped on integer-percent header validation; original attempts preserved | 41 s actual | Carried into M3R |
| M3R | 401 batches raw-audited; stopped on a pre-TRIM display connection failure; 859 batches remain | 8.41 h cumulative used | Original 24 h cumulative |
| C1e | All 120 display checks and both diagnostic replays passed; reset causality not reproduced | 6.47 min actual | 15 min |
| M3R2 | Mixture scan complete and audited: 576 combined points, 2,304 independent batches | 1.95 h final continuation; 10.47 h cumulative M3 | Original 24 h cumulative |
| P4 (current) | Add 243 pressure/energy points at 80% noble; 45 low/27 high canaries gate 900 remaining batches | 12.44 h model; 19.15 h planning | 24 h |
| G5 | Seven concentration waves, 315 points each; stop for `continue` after each | About 12–17 h per wave; 101 h total planning | 24 h per wave |
| Q6 | Fixed precision follow-ups and independently selected feature confirmations | Reserve 6–110 h if needed; skip if no follow-up remains | 24 h per wave |
| A7 | Final raw-data/model/uncertainty audit, plots, CSV/ROOT and reproducibility release | 2–4 h active work, 0.5–2 h report computation | No new transport simulations |

G5 wave order and planning hours: 60% (12.2), 65% (12.7), 70% (13.4),
75% (14.0), 85% (15.6), 90% (16.2), 95% (16.8). Recompute before each wave.
The bulk baseline requires **3,024 points / 12,096 independent batches /
604,800 histories**, including the new C3a/C3b baseline, with zero surface reuse.
P2a/P2b count the same eighteen points once, not twice. Q6 top-ups are additional.

After the October 4 M3 completion audit, all 859 outstanding mixture batches
are finished. P4 now has a **12.44-hour model / 19.15-hour planning forecast**,
including separate canary phases and conservative measured pressure costs;
the earlier rough 11-hour P4 estimate is superseded. The live schedule uses
all 504 measured one-atm mixture timings to reforecast the later G5 waves.
P4 plus G5 currently totals **about 120 compute hours (five days)** with planning
margins, excluding precision top-ups, reporting, reviews and callback delays.
Overall allow roughly **5–10 additional compute days** with
possible Q6/reporting, not a guarantee. The earlier 143-hour surface-reuse forecast
and 164-hour pre-pilot bulk forecast are historical, not the active estimate.
User callback delays, reviews, implementation and conditional interventions are
additional. Allow 15–45 minutes of active review per completed stage/wave and
0.5–2 hours to prepare each new stage type; these are work allowances, not
reserved calendar slots. No promise is made for an unsupported geometry or a
different solver. At 1 MeV, heavy-recoil batch times dominate and are measured
explicitly in P2 before extrapolating pressure costs.

Conditional steps are inserted **before dependent production**, with a new
frozen scope and estimate; no active campaign's hashed files are edited:

| Trigger | Intervention | Provisional added time |
| --- | --- | --- |
| Replay, input, density, parser or clipping defect | Preserve failing artifacts, isolate cause, justified fix + regression tests + fixed canaries | 1–4 h active work + 0.5–4 h compute |
| Surface geometry does not answer the intended recoil question | Verify supported input modes, test a separate geometry if feasible; otherwise ask for scientific direction | 2–8 h active work + 1–6 h compute for feasibility/validation only |
| Accepted physical model/geometry changes | New model ID, representative revalidation and compatible baseline rebuild; never pool old/new models | Initial 12–24 h compute allowance; reforecast required |
| New mixture bend or pressure effect | Fixed independent holdout with preregistered endpoints, sample size and margins | 1–12 h compute for a small selected study |
| RAM, timeout, stall or poor scaling | Preserve checkpoints, diagnose, reduce workers/split waves and benchmark again | 0.5–2 h active work + 0.5–3 h test compute; failed-batch reruns extra |

Precision SEM targets remain ≤5% for range and ≤2% for ionization fraction.
Independent confirmation is separate from adaptive pooled statistics; do not
sample repeatedly until significance disappears or an interval passes.
Non-significance does not certify equivalence. The final release must explicitly
report any unresolved statistical limitations and must not have unresolved
critical numerical/model defects. Extra physics, detector calibration or a
different solver require an explicit scope decision.

Regenerate the read-only schedule report and timing snapshot:

```bash
.venv/bin/python scripts/bulk_campaign_schedule.py
xdg-open runs/bulk_campaign_schedule/index.html
```

This command launches no simulations. Its forecast uses measured means from
all 1,044 independent bulk production batches across 261 complete points,
15-worker longest-first scheduling, mixture interpolation and pressure factors,
then adds 50% and 30 minutes per wave. These are
planning estimates, not statistical confidence bounds. The JSON report records
the timing-source hashes and assumptions. At 1 MeV, measured 80%-noble pressure
cost ratios replace low-energy extrapolations; other concentrations inherit
those ratios and intermediate-pressure costs interpolate the 1/3-atm anchors.
These are runtime predictions, not interpolated physics results. Recalibrate
using new mixture timings at the next review. The eighteen pressure anchors
now use four-batch P2b means rather than single P2a observations.

### Current step: direct pressure/energy grid at 80% noble (P4)

The completed mixture scan passed its cold raw-data audit on October 4:
**576 complete points, 2,304 independent batches, 115,200 histories**, including
504 points covering the full one-atm concentration grid. **477 points meet
both estimated SEM targets; 99 flags remain** (98 range, 3 ionization, two
overlapping). These are sampling-precision flags, not failed transport results;
they remain visible for fixed Q6 follow-ups. No detector-quenching claim.

Results: `runs/noble_cf4_mixtures_1atm_noreset/review/index.html`, with the
ten-page `../mixtures.pdf`, `points.csv`, `precision_flags.csv`, `timing.csv`
and descriptive `mixture_trends.csv`. All old attempts and diagnostics remain
preserved; diagnostic replays never count as independent production samples.

P4 adds **243 points / 972 independent 50-ion batches** in Ar/Kr/Xe80–CF4-20,
C/F/noble recoils, filling the missing pressures from 0.5 to 3 atm and energies
1/3/10/30/100/300/1000 keV. Four batches per point. First **45 low-energy**
production canaries must pass, then **27 one-MeV** canaries, then the remaining
900 batches. Density, composition, bindings, exact inputs/seeds, full endpoint
containment and normal completion are checked for every batch. The validated
authenticated noreset launcher is retained; any failure holds later phases.

Allow **12–19 hours**, with a **24-hour cumulative cap**, three-hour per-batch
cap and drain reserve; at most **15 cores**, **24/32 GiB soft/hard RAM**, and
**zero scan swap**. No automatic G5 or precision top-ups.

```bash
bash scripts/pressure_grid.sh status
bash scripts/pressure_grid.sh logs
bash scripts/pressure_grid.sh stop
```

Live report: `runs/noble_cf4_pressure_grid/index.html`. On completion it exports
combined CSV tables, a nine-page pressure PDF and 630 prespecified pressure
screening intervals. Existing reference data were already visible; these are
not independent confirmations or proof of global pressure equivalence.
At `REVIEW_REQUIRED`, send `continue`; report `NEEDS_ATTENTION` if seen.
P4 completion brings the total to **819 points / 3,276 batches / 163,800 histories**.

### Preserved M3R2 release and recovery design

C1e passed and was reviewed on your `continue`: **60 original-launcher and
60 noreset-launcher displays** passed all connection, authentication, allocation
and cleanup checks. Both diagnostic SRIM replays passed; the completed-reference
replay reproduced its output bytes exactly. **The suspected reset race was not
reproduced**, so this is validated launcher hardening, not proof of the original
cause. Completion-watchdog and physical checks remain unchanged and fail closed.

M3R2 retains **401 audited production batches** and runs only the **859 remaining
original inputs/seeds**. Both diagnostic replays remain excluded from production.
All frozen M3/M3R/C1e files and failed/interrupted raw attempts are preserved.
Allow **2–4 hours** at up to **15 cores**, **24/32 GiB soft/hard RAM**, zero scan
swap. The original 24-hour cumulative budget starts with **8.5204 hours already
charged**, including the 6.47-minute diagnostic. The same four batches of 50 ions
per point and all 47 currently observed complete-point precision flags remain.

```bash
bash scripts/mixtures_1atm_noreset.sh status
bash scripts/mixtures_1atm_noreset.sh logs
bash scripts/mixtures_1atm_noreset.sh stop
```

Report: `runs/noble_cf4_mixtures_1atm_noreset/index.html`. The report links the
combined `points.csv`, `batches.csv`, and, on completion, `mixtures.pdf`.
Completion gives **576 complete combined points**, including **504 at 1 atm**,
with 2,304 independent batches / 115,200 histories. At `REVIEW_REQUIRED`, send
`continue` for the precision, mixture and timing review before P4. On
`NEEDS_ATTENTION`, ask for review; do not resume a failed launcher blindly.
No automatic P4 or precision top-ups.

### Preserved C1e diagnostic scope and stop review

On October 3, M3R stopped before launching `Xe_C_e300_n90_p1_b2`: its virtual
display had reported ready, but the completion watchdog could not reconnect.
All **401 completed M3 batches passed a fresh raw-data audit**. Combined with
earlier production this gives **350 complete points, 45 partial points,
1,445 independent batches and 72,250 histories**. There are 47 complete-point
precision flags; they remain visible for later fixed follow-ups.
One failed and fourteen interrupted batches are excluded. **859 original M3
batches remain**; no seeds, physics settings or sample targets have changed.

C1e tests whether last-client Xvfb resets explain the disconnect. It compares
60 original displays with 60 `-noreset` displays, with 20 reconnects each,
authentication rejection, unique live allocation and cleanup checks. Then it
runs a byte-identical completed-reference replay and the exact failed input.
Both are diagnostic, **not new independent production samples**. The original
completion watchdog and all physical/data-integrity gates remain unchanged.
Allow **3–8 minutes**, capped at **15 minutes**, at most 15 cores, **8 GiB RAM**
and zero swap. M3's 8.4125 elapsed hours plus this diagnostic's wall time carry
forward into the original 24-hour budget. Peak M3R memory was 13.91 GiB and
host/scan swap remained zero.

```bash
bash scripts/xvfb_reset_gate.sh status
bash scripts/xvfb_reset_gate.sh logs
bash scripts/xvfb_reset_gate.sh stop
```

Report: `runs/xvfb_reset_gate/index.html`. At `REVIEW_REQUIRED`, send `continue`
to review the controls/replays and prepare a versioned M3 continuation.
Production does **not** restart automatically. Do not resume the failed M3R
launcher. P4 remains held until M3 is complete and reviewed.

### Preserved M3/M3R design and original release forecast

The original M3 stopped because its composition checker rejected SRIM's exact
`F = 50 Atomic Percent` integer formatting in two 75%-noble mixtures. Both
completed batches passed a full raw-data re-audit. The versioned M3R checker
accepts omitted decimal zeros while keeping the maximum percentage-point
error margin at 0.1 (and retaining finer printed precision where present).
All original code, plans, failed records and raw attempts remain unchanged.
M3R retains ten completed independent batches exactly once and runs only the
remaining 1,250 original cases, including seven interrupted cases. Seeds,
inputs, physical model and sample counts are unchanged. The original 41.18
seconds count against the same 24-hour budget. Thirty-five remaining low-energy
cases must pass before the other 1,215 batches are released.

M3 adds **315 direct points / 1,260 independent batches / 63,000 histories**:
Ar/Kr/Xe with C/F/noble recoils, 65/70/75/85/90% noble, at 1 atm and 293.15 K,
energies 1/3/10/30/100/300/1000 keV. Four fixed independent 50-ion batches per
point. Combining them with the audited 60/80/95% anchors gives **504 complete
1-atm points**, or **576 total points** including existing pressure slices.

The post-upgrade environment gate passed and was reviewed on your `continue`.
Density and atomic-composition calculations first reproduce all 189 old 1-atm
anchor inputs. New density follows the same ideal-gas equation and SRIM masses;
fractions are molecular percentages converted to normalized atomic fractions.
No physics result is interpolated. The accepted bulk mode 5 and binding model
are unchanged, with central starts in the larger full target width of the
adjacent audited concentration anchors. Every batch must remain contained.

First, **45 fixed 1-keV b0 production batches** check all new gas/species/
concentration combinations. All must pass executed density, atomic/mass
composition, bindings, species/energy/count, endpoint/edge, normal-completion
and private-display/helper checks before the other 1,215 batches start.
These canaries count once as production, not as independent diagnostic repeats.

Allow **roughly 12–18 hours**, with a **24-hour cumulative cap**, three-hour
per-job timeout and drain reserve, at most 15 cores, 24/32 GiB soft/hard RAM and
zero scan swap. The frozen model predicts 11.41 hours; 17.61 hours includes
50% margin plus 30 minutes. This replaces the earlier 16.2-hour estimate with
the slower measured four-batch mean of each pair of adjacent concentration
anchors. Timing interpolation is not used as a simulation result.

```bash
bash scripts/mixtures_1atm_retry.sh status
bash scripts/mixtures_1atm_retry.sh logs
bash scripts/mixtures_1atm_retry.sh stop
```

Report: `runs/noble_cf4_mixtures_1atm_retry/index.html`; combined results and batch
SEMs in `points.csv`, independent records in `batches.csv`. On completion,
`mixtures.pdf` has the noble-recoil overview and nine family/recoil mixture
plots. Partial points do not meet the fixed precision gate until four batches
are present. Preserve all 46 preceding flags and any new SEM shortfalls for
fixed Q6 follow-ups; no automatic resampling. Estimated SEM is not model accuracy.

At `REVIEW_REQUIRED`, send `continue` for the mixture/timing/precision review
before preparing P4. Report `NEEDS_ATTENTION` or `STOPPED` if seen. Do not
relaunch a failed or complete run; safe interruption recovery requires review
and preserves all prior attempts and cumulative elapsed time. No automatic P4,
selected-feature confirmation or detector-quenching claim.

Validation before recovery launch: all **259 regression tests** passed,
including 18 M3 tests and eight recovery tests for exact inputs/seeds, raw-data
revalidation, integer-format rejection boundaries, historical seed provenance,
failure preservation, frozen plans and cumulative budget inheritance.

### Completed step: post-upgrade environment check (C1d)

P2b's full raw audit passed: **18/18 pressure points meet both precision targets**,
with four independent 50-ion batches each. Worst estimated relative SEM is
**2.93% range / 0.72% ionization**. The frozen pressure screen has **17 intervals
inside working margins and 19 inconclusive**, none entirely outside. This
does not establish global pressure equivalence. P2b took 2 h 6 min, peak RAM
9.296 GiB, scan swap zero. Combined production is **261 complete points,
1,044 independent batches, 52,200 histories**; 46 earlier precision flags remain.

- [Pressure PDF with batch SEMs](runs/noble_cf4_high_energy_pressure_batches/review/pressure.pdf)
- [261-point CSV](runs/noble_cf4_high_energy_pressure_batches/review/points.csv)
- [Raw audit and 36 simultaneous pressure intervals](runs/noble_cf4_high_energy_pressure_batches/review/index.html)

The host upgraded Xvfb at 14:23 on 2 October, **after** P2b finished at 14:02,
then rebooted at 14:26. The strict historical fingerprint check correctly
held the next launch. The original binary was recovered from the cached old
package and matches its frozen SHA exactly; no OS downgrade or frozen-file
edit was made. `scripts/historical_execution.py` resolves only historical
Xvfb hash checks against these recovered bytes, verifies the upgrade chronology,
and rejects unknown current versions. New manifests always pin actual current bytes.

C1d validates the current environment with **four synchronized 15-display
waves** (allocation, authentication, isolation and cleanup), then **one exact
50-ion Kr/F1keV bulk replay**. The replay is diagnostic and never added to
independent production statistics. Allow 1–3 minutes, five-minute budget,
six-minute service failsafe, at most 15 cores, 4/8 GiB soft/hard RAM and zero swap.
M3 was held until this additional gate and its user-triggered review passed.

C1d completed at 15:00 on 2 October in **32.4 seconds**: all 60 display checks
passed, the 50-ion replay matched exactly, and source/executable hashes remained
unchanged. Peak RAM was **1.966 GiB**, host and scan swap zero. Its status is
`REVIEW_REQUIRED`; its review passed and released M3 preparation on your `continue`.

```bash
bash scripts/xvfb_upgrade_gate.sh status
bash scripts/xvfb_upgrade_gate.sh logs
bash scripts/xvfb_upgrade_gate.sh stop
```

Report: `runs/xvfb_upgrade_gate/index.html`. This completed diagnostic must not
be rerun or included in independent production statistics. M3's separate plan
and launcher are now prepared and frozen; follow its monitoring commands above.

For regression tests after this documented upgrade, use the explicit historical
audit context; live display/lifecycle tests still execute the current software:

```bash
.venv/bin/python scripts/run_historical_tests.py
```

Validation: all 233 regression tests passed, plus a fresh nine-test rerun of
the completed gate/provenance implementation before launch.

Keep `runs/xvfb_upgrade_gate/upgrade-evidence.json` and its recovered old-package
binary: they are part of the audit provenance, not disposable scratch files.

### Completed step: independent 1 MeV pressure batches (P2b)

P2a passed all 18 raw audits in **1 h 49 min**, with all 50 contained endpoints
per case, zero primary escapes/profile-edge signal, normal completion and
unchanged source. Peak RAM was 11.292 GiB; scan swap zero. The slowest batch
(Xe recoil at 0.5 atm) took 109 minutes. Across matched species, 0.5-atm batches
took 2.43–3.01 times their 3-atm counterparts.

P2a has only one batch per new point: **no independent-batch SEM or pressure
equivalence claim**. Its largest descriptive differences from the four-batch
1-atm reference means are 7.28% pressure-scaled displacement and 1.48% ionization.
See [audited pressure plots and CSV](runs/noble_cf4_high_energy_pressure/review/index.html).

P2b added exactly **54 reserved independent 50-ion batches** (repeats 1/2/3),
retaining P2a b0 by audited provenance. All eighteen Ar/Kr/Xe80/CF4-20,
C/F/noble, 1-MeV, 0.5/3-atm points now have four independent batches,
**200 ions per point**. Only seed and identifying labels change; physics,
density, geometry and event coordinates remain unchanged.

The 36 Bonferroni approximate Welch log-batch pressure intervals use the
specification frozen **before P2a observations**. All outcomes and estimated
SEM flags remain visible. No automatic top-ups or mixture expansion.

Actual elapsed time was **2 h 6 min**, versus the 1.82-hour model and
3.23-hour planning estimate. The run used an eight-hour cap, three-hour
per-job limit, 15-core maximum, 24/32 GiB RAM and zero scan swap.
All eighteen points passed the estimated precision targets; the pressure
screen retained its inconclusive outcomes. C1d is the next gate after the
post-completion OS upgrade; that review is now complete and M3 is the current stage.

```bash
bash scripts/high_energy_pressure_batches.sh status
```

Results: `runs/noble_cf4_high_energy_pressure_batches/review/index.html`.
Do not rerun completed pressure batches.

### Completed step: 1 MeV pressure timing and containment gate (P2a)

The complete bulk baseline passed raw audit: **243 points, 972 independent batches,
48,600 histories**, and all 42 paired-depth checks. C3bR took 3 h 17 min; the
C3b/recovery cumulative time was 6 h 56 min, plus the separate 89-minute pilot.
Recovery peak RAM was 12.249 GiB, scan swap zero.

**197/243 points meet both estimated SEM targets; 46 remain flagged.** There are
45 range flags and 3 ionization flags, with two overlaps. Most range flags occur at
1–10 keV. The 108 prespecified pressure intervals include 106 inconclusive and two
inside the working margins. Pressure equivalence remains unresolved.

Preliminary results:

- [Six-page PDF: mixture curves and pressure slices](runs/noble_cf4_bulk_baseline_retry/review/baseline.pdf)
- [243-point CSV with means and batch SEMs](runs/noble_cf4_bulk_baseline_retry/review/points.csv)
- [Audited report, plots, precision flags and timings](runs/noble_cf4_bulk_baseline_retry/review/index.html)

P2a runs **18 fresh 50-ion batches**: Ar/Kr/Xe80/CF4-20, C/F/noble recoils,
1 MeV, 0.5 and 3 atm. It measures heavy-recoil timing, actual parameters and containment.
The one-batch results acquire independent-batch SEMs only after P2b's three additional
batches per point. Four distinct seeds per point are reserved in the frozen P2a plan.

Pressure gas density/composition come from audited 100 keV pressure inputs;
the 1 MeV width scales inversely with density before the same central-start geometry.
Exact executed inputs, bindings, 50 endpoints, no escapes/edge signal, normal completion
and private-display evidence are required. SRIM's printed mass-density header
truncates its last decimal, so that check uses its displayed resolution rather
than an arbitrary percentage tolerance.

The later P2b screen is frozen now: 36 approximate Bonferroni Welch log-batch
pressure contrasts at 1 MeV, working ratio margins 0.95–1.05 for ionization and
0.90–1.10 for pressure-scaled displacement. Four batches per condition will be
compared with the existing 1 atm reference; inconclusive results remain visible.

The original 2.10-hour model/3.65-hour planning estimate and six-hour cap
were respected. [Audited P2a report](runs/noble_cf4_high_energy_pressure/review/index.html).

### Completed step: completion-monitor recovery (C3bR)

C3b completed **132 cases** (90 independent batches and all 42 depth diagnostics)
before stopping. Every completed case passed the raw audit and all depth pairs
passed. One Xe/F1MeV95% batch failed during final save: a redundant `xwininfo`
query hit a closing Wine window after the exact completion acknowledgement.
Its final output is incomplete. Fourteen other batches were interrupted.

The opt-in monitor now keeps the original error watchdog active while allowing
the existing 20-second exit grace without a redundant completion query. Before
acknowledgement, recognized window races get at most three inspection attempts.
Persistent/unknown errors still stop the worker; key injection is attempted once.
Eleven monitor tests passed. A one-ion simulation completed normally in 27 seconds;
its separate raw review accepts SRIM's scientific energy format (`1000000.E-03 keV`).
The initial literal-format canary result and frozen source remain preserved beside
[`review.json`](runs/completion_retry_canary/review.json).

C3bR retains all 132 audited results. It first reruns the failed **original 50-ion
input and seed**, then releases the other 773 original cases after that batch
passes every completion/parameter/endpoint/output check. The fixed baseline,
pressure-screen specification and precision flags remain as documented below.

Allow **4–6 additional hours** (3.65-hour timing model, 5.98 hours with margin).
The original **12-hour cumulative cap** includes the prior 3.65-hour run and the
software canary, leaving about **8.35 hours** maximum for this continuation.
At most 15 cores, 24/32 GiB RAM and zero scan swap. A slow run can stop safely
with checkpoints for review.

```bash
bash scripts/bulk_baseline_retry.sh status
bash scripts/bulk_baseline_retry.sh logs
bash scripts/bulk_baseline_retry.sh stop
bash scripts/bulk_baseline_retry.sh resume
```

Report: `runs/noble_cf4_bulk_baseline_retry/index.html`. Tell the assistant
`continue` at `REVIEW_REQUIRED`, or send `NEEDS_ATTENTION`/`STOPPED` for review.
The original failed run stays at `runs/noble_cf4_bulk_baseline`; use the new
launcher for recovery. The next stage requires another review and `continue`.

### Frozen scope: compatible bulk-baseline rebuild (C3b)

The independent pilot finished in **89.18 minutes**: all 108 batches passed raw
audit, with 108 distinct seeds, complete contained endpoints, unchanged sources,
14.684 GiB peak RAM and zero scan swap. **22/27 points meet both estimated SEM
targets.** Range flags remain for Kr/C1keV (6.29%), Xe/C1keV (5.47%), Xe/F1keV
(10.06%) and Xe/Xe100keV (5.74%); Ar/C1keV ionization SEM is 2.072%.
These statistical flags remain visible; no automatic top-ups are performed.
See the [pilot review](runs/noble_cf4_bulk_precision_pilot/review/index.html) and
[pilot plots](runs/noble_cf4_bulk_precision_pilot/pilot.pdf).

C3b adds **216 points / 864 independent 50-ion batches**, retaining the 27 audited
pilot points under the same model. Coverage: 60/80/95% noble at all seven energies
and 1 atm, plus 80% at 0.5/3 atm and 1/10/100keV. Surface results remain separate.

There are also **42 paired-depth diagnostic batches**, excluded from statistics:
36 low-energy pairs at mixture/pressure boundaries gate six heavy-recoil 1-MeV
mixture-boundary pairs, which then gate the remaining 822 independent batches.
Depth changes must be ≤0.2% ionization and ≤0.5% projected displacement, with
complete endpoints, no primary escape and no edge-bin signal throughout.

The pressure screen is frozen before new data: 108 approximate, Bonferroni-adjusted
Welch intervals on log batch observables (all family/recoil combinations,
1/10/100keV, 0.5/3 versus 1 atm). Working ratio margins are 0.95–1.05 for
ionization and 0.90–1.10 for pressure-scaled range. Intervals overlapping a margin
are **inconclusive**, not equivalence; four-batch intervals depend on distributional
assumptions. The full specification and limitations are in `plan.json`.

Timing: **7.2 hours model estimate; 11.25 hours including margin; 12-hour cap**.
At most 15 cores, 24/32 GiB soft/hard RAM, zero scan swap and three-hour job limit.
No new job starts without the full job-time/drain allowance remaining; a slow run
can stop safely for review rather than finish within the cap. Point/batch CSVs
update during execution. Review geometry, precision and pressure before P2a;
no next stage launches automatically.

```bash
bash scripts/bulk_baseline_rebuild.sh status
bash scripts/bulk_baseline_rebuild.sh logs
bash scripts/bulk_baseline_rebuild.sh stop
bash scripts/bulk_baseline_rebuild.sh resume
```

Report: `runs/noble_cf4_bulk_baseline/index.html`. Tell the assistant `continue`
at `REVIEW_REQUIRED`, or report `NEEDS_ATTENTION`/`STOPPED` for review.

### Completed step: independent-batch interior precision pilot (C3a)

All 37 C2d cases passed raw re-audit. Maximum changes when doubling interior depth
were **0.0074% ionization and 0.0112% projected displacement**. Peak RAM was
12.967 GiB, scan swap zero, source unchanged. See the
[corner review](runs/noble_cf4_bulk_corners_retry/review/index.html).
These are numerical/containment checks, not independent precision certification.

C3a covered **27 points**: Ar/Kr/Xe × C/F/noble × 1/100/1000keV, at 80% noble and
1 atm. Each gets **four fresh independent 50-ion batches**, 108 unique seeds and
5,400 histories in total. Provisional model `noble-cf4-bulk-trim5-eps1e9-v1`
uses full-cascade mode 5, central start W in target 2W, and the validated near-zero
binding parameters. Range means signed endpoint X minus initial X over all
contained primary ions—not the absolute `RANGE.TXT` mean or total track length.

The 36 low-energy batches must pass integrity before the other 72 start.
Targets remain estimated batch SEM ≤5% for projected range and ≤2% for ionization.
Precision failures are reported without automatic resampling; integrity errors
stop submission. No surface results or paired-depth histories are pooled.

Allow about **2.5 hours**, with a six-hour cumulative cap, three-hour per-job
timeout, at most 15 cores, 24/32 GiB RAM and zero scan swap. CSV points/batches
update during the run; PDF/PNG plots are generated at successful completion.
The completed raw review supports only the gated C3b expansion described above,
not a global precision certificate or a pressure-equivalence claim.

```bash
bash scripts/bulk_precision_pilot.sh status
bash scripts/bulk_precision_pilot.sh logs
bash scripts/bulk_precision_pilot.sh stop
bash scripts/bulk_precision_pilot.sh resume
```

Report: `runs/noble_cf4_bulk_precision_pilot/index.html`. Tell the assistant
`continue` on `REVIEW_REQUIRED` or `NEEDS_ATTENTION`. Full-grid production remains
held until the independent-pilot and compatible-baseline reviews.

### Completed step: display-safe bulk-corner continuation (C2d)

C2c passed all 18 low-energy cases and the exact replay, but stopped at the
high-energy phase's concurrent display startup. Ubuntu's `xvfb-run -a` exhausted
its ten display-number attempts and invoked a worker without a working display.
The watchdog stopped that case before TRIM launched, then stopped the other
in-flight workers. **No high-energy result was accepted.** The failed run remains
preserved in `runs/noble_cf4_bulk_corners` and should not be resumed directly.

The opt-in replacement uses **Xvfb `-displayfd`** for server-owned allocation,
private authentication, explicit readiness checks, disabled TCP listening and
owned-process cleanup. Four synchronized 15-display waves passed all 60 checks,
including rejection of unauthenticated clients and cleanup. C2d retains the
19 audited completed results and runs only the original 18 unfinished high-energy
cases, with unchanged inputs, seeds and scientific gates. The 313 seconds already
used count toward the original six-hour cumulative cap. No historical code or raw
results were overwritten; the adapter and new run are separate.

C2b completed and all **28 raw cases passed re-audit** in 20.4 minutes, with
12.475 GiB peak RAM and zero scan swap. All 18 interior cases contained all
50 primary ions; doubling the surrounding target depth changed ionization by
at most 0.0060831% and mean displacement by 0.0035506%. The raw `RANGE.TXT`
summary includes the starting coordinate: interior range must be calculated
from **endpoint X minus initial X**, not taken directly from that summary.
The [C2b review](runs/kr_bulk_geometry_retry/review/index.html) includes the
raw-data audit and descriptive depth/surface/pressure comparisons. Three reused
seeds at one Kr/F energy do not establish general validity or pressure equivalence.

C2c/C2d test this candidate geometry for **Ar/Kr/Xe, C/F/noble recoils, 1keV and
1MeV**, at 80% noble and 1 atm. Two interior depths at each of 18 points use
the same fresh seed, making paired numerical checks—not two independent batches.
An exact Kr/F100keV replay brings the total to 37 cases × 50 ions. The replay
and all nine low-energy pairs must pass before the eighteen high-energy cases.
All histories remain diagnostic, separate from production.

Gates require correct executed species/energy/bindings, 50 endpoints, no primary
escape, no profile-edge ionization, complete outputs and normal completion.
Paired-depth changes must be ≤0.2% for ionization and ≤0.5% for mean displacement;
these are fixed numerical screening limits, not precision/equivalence claims.
No automatic extra depth/seed trials are allowed on failure.

Allow **about 2.4 additional hours** from measured reference costs, with a six-hour cumulative
cap and three-hour per-job timeout. At most 15 cores, 24/32 GiB RAM and zero scan
swap. After completion, review the model and new timing before any production
release. If interior geometry is adopted, preserve the surface dataset separately
and plan a compatible baseline rebuild; the earlier 143-hour forecast is still
conditional, not a forecast for a validated new geometry.

```bash
bash scripts/bulk_corner_retry.sh status
bash scripts/bulk_corner_retry.sh logs
bash scripts/bulk_corner_retry.sh stop
bash scripts/bulk_corner_retry.sh resume
```

Report: `runs/noble_cf4_bulk_corners_retry/index.html`. On `REVIEW_REQUIRED` or
`NEEDS_ATTENTION`, call back with `continue`; no next stage is launched automatically.
Reproduce the preceding review with `.venv/bin/python scripts/review_bulk_geometry.py`.

### Completed step: completion-aware interior-start geometry retry (C2b)

The original C2a attempt stopped safely after its mode-2 control passed and its
mode-5 canary timed out. The other 26 cases were never released. An observe-only
one-ion probe captured SRIM's **End of TRIM.DAT calculation** dialog, which blocks
final output saving until acknowledged. A separate one-ion software canary then
passed in 19 seconds: exact dialog acknowledgement, normal exit, complete outputs,
correct binding parameters, one-ion count and endpoint accounting.

C2b repeats the original fixed geometry design in a new directory. Its isolated
mode-5 worker acknowledges that exact visible completion title **once** and saves
the screenshot/window tree. Error dialogs still reject the job; complete final
files and all scientific checks remain mandatory. Historical runners, failed
artifacts and production data are unchanged. Diagnostic records are in
`runs/mode5_lifecycle_probe` and `runs/mode5_completion_canary`; the failed original
attempt is preserved in `runs/kr_bulk_geometry_probe` and must not be resumed.

The 36-case pressure/depth diagnostic completed in 24 minutes; all raw artifacts
and three exact replays passed. Width-only changes left range/ionization unchanged;
doubling profile-bin width changed ionization by at most 0.00494% relative. Across
2.2 → 2.3 atm the printed atom-density exponent changes incorrectly, while matched
ionization changes only 0.0293–0.1234% and pressure×range at most 0.0334%.
This supports a header-format explanation locally, not universal pressure equivalence.
Three reused seeds are diagnostic evidence, not independent confirmation statistics.

The installed SRIM input manual (pages 8-14/8-15), its example TRIM1.DAT and
PySRIM's calculation-mode documentation support mode 5 with specified starting
positions. The [official input manual](https://www.srim.org/SRIM/SRIM%2008.pdf)
describes this interface. C2b tests it before any 1-MeV expansion:

- One mode-2 surface control with primary endpoint output, and one mode-5
  near-surface canary; both must pass before further variants.
- Three archived seeds × 0.5/1/3 atm × near-surface, central and deeper-central
  starts: 27 mode-5 cases, plus the mode-2 control = 28 fixed 50-ion cases.
- Keep all 1,400 histories separate from production. Preserve raw RANGE.TXT
  summaries and endpoint-minus-start ranges separately; do not assume the
  summary already subtracts the starting coordinate. Interior cases require
  50 endpoints, no primary escape, and no ionization at either profile edge.

Allow 30–90 minutes, with a three-hour cumulative cap, at most 15 cores,
24/32 GiB soft/hard RAM and zero scan swap. No new production model is adopted
automatically; changing geometry requires a compatibility decision and new timing
forecast. The previous six-day baseline estimate remains conditional.

```bash
bash scripts/bulk_geometry_retry.sh status
bash scripts/bulk_geometry_retry.sh logs
bash scripts/bulk_geometry_retry.sh stop
bash scripts/bulk_geometry_retry.sh resume
```

Report: `runs/kr_bulk_geometry_retry/index.html`. Reproduce the read-only D0 audit
with `bash scripts/bulk_geometry_probe.sh review`; it writes
`runs/kr_pressure_diagnostic/review/summary.json`. On the next `continue`, review
C2b before deciding whether to extend validation, adopt a compatible model, or
release the high-energy canaries. Existing production data remain unchanged.

### Completed step: bounded Kr pressure/geometry diagnostic

`scripts/pressure_diagnostic.sh` runs 36 fixed 50-ion cases at Kr80/CF4-20,
F recoil, 100 keV. First, three archived 0.5/1/3-atm cases must reproduce
RANGE/IONIZ/TDATA byte-for-byte. Only then are variants released:

- Three archived 1-atm seeds at 0.5/1/2.2/2.3/3 atm, with exactly scaled
  mass density and inverse-pressure target/view depth (15 cases).
- At 0.5/1/3 atm, double target depth while keeping the viewing depth fixed,
  then double both depths, for the same three seeds (18 cases).

The 2.2/2.3-atm pair brackets the printed atomic-density exponent transition.
Depth variants separate finite-target and binning sensitivity. All cases still
start at the surface: an internal bulk-gas recoil is **not** implemented by
this diagnostic. Reused seeds are deliberate; none of the 1,800 diagnostic
histories enter production counts or independent precision estimates.

Planning allowance: about one hour; hard/cumulative cap: two hours. Limits:
15 cores/workers, 24/32 GiB soft/hard RAM, zero scan swap, isolated Wine prefixes.
Each case retains raw inputs/outputs; completed cases are hash-checked on resume.
Failures stop further releases. No automatic production expansion follows.

```bash
bash scripts/pressure_diagnostic.sh start
bash scripts/pressure_diagnostic.sh status
bash scripts/pressure_diagnostic.sh logs
bash scripts/pressure_diagnostic.sh stop
bash scripts/pressure_diagnostic.sh resume
```

Report: `runs/kr_pressure_diagnostic/index.html`; descriptive within-seed
contrasts: `runs/kr_pressure_diagnostic/comparisons.csv`. Results require review,
not a rule that keeps sampling until a p-value passes.

### Reviewed result: precision complete; pressure diagnostic required

The fixed closeout finished on 30 September in **1 h 3 min**: all 564 new
batches completed, peak RAM 11.62 GiB, zero scan swap. The combined corrected
dataset contains **243 points / 92,050 histories**. All points meet estimated
range SEM ≤5% and ionization-fraction SEM ≤2%; all 47 independent new 600-ion
cohorts also pass. Maximum pooled relative SEMs are 4.990% and 1.981%.

Coverage is 60/80/95% noble at 1 atm and 1/3/10/30/100/300/1000 keV for all
Ar/Kr/Xe families and C/F/noble recoils (189 points), plus 0.5/3 atm at 80%
noble and 1/10/100 keV (54 points). This is not the full original scan above.

One of twelve independent pressure tests is flagged: F recoils at 100 keV
in 80% Kr / 20% CF4, ionization fraction, BY-adjusted p=0.03355. Independent
means at 0.5/1/3 atm are 0.54220/0.52510/0.53119. This is not yet an established
physical effect. A raw-output audit also finds that TDATA prints an atomic
density ten times too small at 3 atm, despite correctly scaled mass-density
inputs. A formatting defect is possible; calculation impact is unresolved.
Surface backscattering is present and must not be silently discarded or
interpreted as fully contained bulk-gas recoil energy loss.

Offline review (does not launch simulations or change frozen results):

```bash
.venv/bin/python scripts/review_corrected_closeout.py
xdg-open runs/noble_cf4_corrected_closeout/review/index.html
```

The review links the existing PDF, plots, point/batch data and ROOT file, and
adds the raw-input/IONIZ audit and 63 descriptive 95%/60% mixture comparisons.
The next gate is a bounded fixed-input replay and matched-seed pressure/geometry
diagnostic, not broad production or repeated sampling until a p-value passes.
No further run is launched by the review. SRIM ionization fraction remains a
model observable, **not detector-calibrated quenching**; statistical precision
does not certify model accuracy or pressure equivalence.

### Completed step: fixed precision closeout and independent pressure confirmation

The corrected refinement finished on 29 September in 2 h 48 min, with all 840
new batches validated, no retries/clipping/parameter mismatches, peak RAM
22.16 GiB and zero scan swap. All 25 independent 600-ion top-up cohorts met
the estimated 5% range / 2% ionization-fraction SEM targets. In the expanded
243-point grid, 208 points met both targets and 35 new 200-ion points remained
flagged. Those are 6/15/6/6/1/1 points at 1/3/10/30/100/300 keV respectively.

All 54 multiplicity-adjusted pressure screening tests were unflagged. This is
not equivalence: four-batch estimates showed up to 20.2% mean spread in
pressure×range and 7.2% in ionization fraction. Six groups crossed descriptive
selection thresholds of 10% range or 5% ionization spread: Ar/Ar/1 keV,
Kr/F/1 keV, Kr/F/100 keV, Kr/Kr/1 keV, Xe/F/10 keV and Xe/Xe/1 keV.
These thresholds select independent checks; they are not significance tests.

`scripts/corrected_closeout.sh` adds exactly 600 fresh ions at 47 existing
points: all 35 precision flags plus 12 additional controls to cover all three
pressures (0.5/1/3 atm, 80% noble) in those six groups. There is no grid expansion.
The 564 new 50-ion batches use repeats 100–111 with distinct seeds. Only these
new cohorts enter the independent pressure confirmation (Welch ANOVA and BY
adjustment across twelve fixed group/observable tests). Precision and pressure
results are reported regardless of whether they pass; no additional batches
are released automatically and non-rejection never certifies equivalence.

```bash
bash scripts/corrected_closeout.sh start
bash scripts/corrected_closeout.sh status
bash scripts/corrected_closeout.sh logs
bash scripts/corrected_closeout.sh stop
bash scripts/corrected_closeout.sh resume
```

Report: `runs/noble_cf4_corrected_closeout/report/index.html`, with combined
point results, independent new-cohort estimates, pressure confirmation,
precision flags, batch provenance and final PDF/PNG/ROOT exports. Previous
results remain read-only. Final planned history total: 92,050 over the same
243 points. The original 35 precision flags each go from 200 to 800 ions;
some controls already had more histories, retained explicitly in the plan.

Planning estimate: 1 h 45 min including margin; cap: three cumulative hours.
Limits remain 15 cores/workers, 24/32 GiB soft/hard RAM and zero scan swap.
The next step is review and an analysis summary of this bounded dataset,
not an automatic expansion to untested mixtures or 1 MeV pressures.

### Current follow-up: corrected precision, intermediate energies and pressure

The corrected 108-point map completed on 28 September in 3 h 44 min. Its 412
new batches had no retries, clipping or target-energy mismatches; peak RAM was
13.47 GiB, scan swap zero. All artifacts and reused points were rechecked.
83/108 points met the estimated 5% range / 2% ionization-fraction SEM targets.
The 25 flagged points comprise 18 at 1 keV, six at 10 keV and one at 100 keV.

The fixed next run is `scripts/corrected_refinement.sh`, using the same
`noble-cf4-nearzero-1e9-v1` model and keeping previous data read-only:

- 25 flagged points: twelve new 50-ion batches each, taking 200 → 800 ions;
- 81 new points at 3/30/300 keV, 60/80/95% noble and 1 atm, all three gas
  families and C/F/noble recoils, four 50-ion batches per point;
- 54 new pressure points at 0.5/3 atm, 80% noble, 1/10/100 keV, all three
  families and recoil species, four 50-ion batches per point.

This adds 840 batches / 42,000 histories and produces a combined 243-point,
63,850-history corrected-model dataset. Repeats 80–91 for the top-up and
80–83 for the new grids have checked distinct seeds. The six-hour cumulative
cap covers all three stages; the conservative planning estimate is 5.1 hours.
Limits remain 15 cores/workers, 24/32 GiB soft/hard RAM and zero scan swap.
Each stage is audited before the next is released. No adaptive extra batches
are launched, even if statistical targets are missed.

```bash
bash scripts/corrected_refinement.sh start
bash scripts/corrected_refinement.sh status
bash scripts/corrected_refinement.sh logs
bash scripts/corrected_refinement.sh stop
bash scripts/corrected_refinement.sh resume
```

Report: `runs/noble_cf4_corrected_refinement/report/index.html`. Live CSVs
include the combined grid, provenance/timing, precision flags and the independent
new 600-ion cohorts. Final PDF/PNG/ROOT exports cover the extended energy map
and available mixture/pressure points. Raw outputs and checkpoints are under
`{topup,energy,pressure}` within that run directory.

Pressure diagnostics use only the fixed first four batches at each of 0.5/1/3
atm, excluding precision top-ups. They compare pressure×range and ionization
fraction with Welch ANOVA and BY adjustment over a fixed family of 54 tests.
Unestimable tests remain indeterminate; no significance flags are issued before
the full family is available. Small batch counts limit sensitivity, and
non-rejection is not proof of pressure equivalence. Pooled post-selection SEMs
are descriptive; detector calibration and model uncertainty are not included.

No 1 MeV pressure extension, new mixture concentrations, legacy binding model
or interpolated result rows are included. Those remain later decisions.

### Current scan: corrected-model 108-point mixture map

The binding confirmation passed all ten fixed-setting replays and all ten
predeclared statistical comparisons. Its 50 fresh batches finished with no
retries/clipping in a total study time of 2 h 45 min; peak RAM was 24 GiB and
scan swap zero. This supports numerical use of the explicit 1e-9 eV model at
the tested points, not detector calibration or universal physical-model accuracy.

The new model identity is `noble-cf4-nearzero-1e9-v1`: 21 eV displacement and
1e-9 eV lattice/surface energies. `scripts/corrected_scan.sh` supervises:

- Ar/Kr/Xe, 60/80/95% noble, 1 atm and 293.15 K;
- C/F/noble recoils at 1/10/100/1000 keV: 108 direct points;
- 103 new points with four independent 50-ion batches each;
- read-only reuse of five accepted `binding_confirmation/fresh/epsilon_1e9`
  points with five independent 50-ion batches each.

There are 412 new batches (20,600 histories), 25 reused batches (1,250 histories),
and 21,850 total histories. New repeats 70–73 have checked distinct seeds.
No literal-zero, epsilon_1e6, replay or five-ion pilot results enter this map.
Per-batch source directories/raw archives and model identity are retained.

First, 72 batches cover the 60/95% and 1/1000 keV endpoints (36 points × 100
ions). Only after target-energy, numeric, archive, clipping, source-integrity
and resource checks pass are the remaining 340 batches released. This endpoint
gate is not a precision gate. The final report flags estimated range SEM >5%
or ionization-fraction SEM >2%; there are no automatic precision top-ups.

```bash
bash scripts/corrected_scan.sh start
bash scripts/corrected_scan.sh status
bash scripts/corrected_scan.sh logs
bash scripts/corrected_scan.sh stop
bash scripts/corrected_scan.sh resume
```

Report: `runs/noble_cf4_corrected_coarse/report/index.html`, with point/batch CSVs
and precision flags updated during the scan. PDF/PNG/ROOT snapshots are generated
after the endpoint phase and at completion. New checkpoints/raw archives are
under `runs/noble_cf4_corrected_coarse/scan`; accepted source results are not moved
or modified. Frozen hashes protect configurations, code and reused artifacts.

Limits: 15 cores/workers, RAM soft/hard 24/32 GiB, no scan swap, eight cumulative
hours including restarts. The timing model predicts about 6.1 hours including
50% scheduling margin and 30 minutes overhead, using historical timings only
(not historical physics results) with measured corrected-model adjustments.
This is not a completion guarantee. The run ends for review without releasing
additional energies, mixtures, pressures or top-ups automatically.

### Current follow-up: replay-gated binding confirmation

The 72-job binding pilot finished in 15.4 minutes, with all target energies
confirmed, no retries/clipping, 19.0 GiB peak RAM and no scan swap. Its strict
byte-equality gate passed 31/36 pairs. Projected-range summaries matched for all
pairs, but five had secondary-ionization differences (largest integrated change:
0.0556% relative, Xe recoil at 1 MeV); three also had secondary-recoil distribution
differences. The five-ion tests are diagnostics, not precision measurements.
The previous gate/result files remain unchanged.

`binding_confirmation.sh` first replays those five points at each epsilon with
unchanged seeds/settings. Only if all ten replay input/output sets are identical
does it release five independent 50-ion batches per point per epsilon (50 new
jobs, 2500 histories). The five points are Ar/C/1000 keV, Kr/F/1 keV,
Kr/Kr/1000 keV, Xe/F/1000 keV and Xe/Xe/1000 keV, all 80% noble and 1 atm.

```bash
bash scripts/binding_confirmation.sh start
bash scripts/binding_confirmation.sh status
bash scripts/binding_confirmation.sh logs
bash scripts/binding_confirmation.sh stop
bash scripts/binding_confirmation.sh resume
```

Separate report: `runs/binding_confirmation/index.html`; raw data/checkpoints
are under `{replay,fresh}/{epsilon_1e6,epsilon_1e9}`. Replays and previous pilot
histories never enter the independent-batch statistics. Fresh repeats 60–64
use distinct seeds, matched across epsilon models. No models are pooled.

The predeclared statistical gate uses mean within-pair relative differences:
range margin ±0.5%, integrated ionization-fraction margin ±0.2%. Two-sided
Student-t intervals use Bonferroni adjustment over all ten point/observable
comparisons, for nominal simultaneous 95% coverage under the independent,
approximately normal batch-difference assumption. Every interval must fit
strictly inside its margin. Five batch pairs are a small sample; an inconclusive
gate triggers review, not automatic extra sampling. This does not certify
depth profiles, untested mixtures, gas-model accuracy or detector quenching.

Resource caps: 15 cores/workers, RAM soft/hard 24/32 GiB, zero scan swap,
four cumulative hours including restarts; the small-pilot-based planning
estimate is about 3.8 hours, not a completion guarantee. Checkpoints survive
interruption. No full production is launched even if both gates pass.

### Current gate: cross-gas near-zero binding validation

The candidate workaround is being tested separately from production:
Ar/Kr/Xe at 80% noble / 20% CF4 and 1 atm; C/F/noble recoils at
1, 10, 100 and 1000 keV. Each of 36 points compares 1e-6 with 1e-9 eV
lattice/surface bindings using the same seed and five ions per model.
These 72 jobs are **paired numerical diagnostics, not independent precision batches**.
They do not modify or enter the existing 2025-point dataset.

```bash
bash scripts/binding_validation.sh start   # detached; do not repeat while active
bash scripts/binding_validation.sh status
bash scripts/binding_validation.sh logs
bash scripts/binding_validation.sh stop
bash scripts/binding_validation.sh resume
```

Report: `runs/binding_validation/index.html`; paired CSV: `comparisons.csv` in
that directory. Each candidate has its own `epsilon_1e6` or `epsilon_1e9`
subdirectory with raw archives, checkpoint database and provenance. The job
IDs/seeds intentionally match across these two isolated directories; always
retain the model-directory identity when analyzing these data.

Limits: 15 cores/workers, 24/32 GiB soft/hard RAM, zero scan swap and two
cumulative hours. The gate requires reported target energies to match requests,
valid unclipped artifacts, matching paired seeds/geometry, and byte-identical
RANGE/IONIZ outputs for every pair. Differences require investigation. Even a
passing gate does **not** automatically release production or certify gas-model
accuracy, statistical precision, or detector quenching.

New TRIM/convergence jobs now fail closed on a requested-versus-TDATA
displacement/lattice/surface-energy mismatch. The raw archive retains the failed
check and evidence; no successful result is published, and new submissions stop.
The check uses relative tolerance 1e-5 with no absolute floor, so tiny positive
bindings cannot pass as zero. Revalidation also reads TDATA in legacy archives:
old zero-binding configs/results may now fail this check. They are preserved,
not retroactively corrected; do not resume their production or pool models.
The historical exact-zero diagnostic suites bypass the production job runner
intentionally and remain separate diagnostics.

### Binding-energy input-reading canaries

**Result:** the probes confirm element-dependent exact-zero fallback for
F/Kr in the tested SRIM input. Whitespace changes do not fix it. Explicit
1e-6/1e-9 eV bindings are read correctly and give identical RANGE/IONIZ outputs
in the matched-seed canary, with an ionization fraction about 0.574% above the
literal-zero baseline. This is one diagnostic case, not a scan-wide correction.
See [the evidence and next validation gate](docs/binding-energy-diagnostics.md).
Production settings remain unchanged and further production is on hold.

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

The completed format probe found identical outputs for all zero-input spacings;
all positive per-element markers were read correctly, even with single spaces.
The second suite checks exact-zero handling: integer/scientific zero, tiny
positive bindings of 1e-6/1e-9 eV, and one zero-valued element among positive
markers. These remain diagnostic values, not an approved production workaround.

```bash
bash scripts/binding_probe.sh start zero-handling
bash scripts/binding_probe.sh status zero-handling
bash scripts/binding_probe.sh logs zero-handling
```

Its separate report is `runs/binding_zero_probe/index.html`. Neither suite
edits production settings or the completed scans. TDATA's printed precision
may limit exact equality checks for very small requested values; retain the
raw inputs and compare the calculated outputs as well.

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
