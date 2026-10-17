# CYGNO SRIM/TRIM QF study and thesis comparison

**Result:** reproducible nominal electronic-energy responses for five species.
Low-energy settings and derivative model dependence remain unresolved; these
are not calibrated free-charge QFs. The bounded extension added exactly 45 batches; no adaptive campaign was launched.

**The new 25–63 eV data fail the 1% energy-closure criterion in all 45 batches; maximum residual 14.58%. They are nominal diagnostics, not qualified low-energy detector input.**

## What was simulated

He/CF₄=60/40 **molar**, 900 mbar, existing temperature **293 K**, ideal-gas
density **0.001389194959 g/cm³**. Atomic fractions are **He=3/13, C=2/13,
F=8/13**, not mass fractions. Projectiles were H1/He4/C12/O16/F19. The
contaminant medium had final molar H₂O=O₂=0.001, He=0.5988, CF₄=0.3992;
contaminant abundances did not determine simulated counts.

Native SRIM 2013/TRIM settings: **mode 5 full cascades**, gas phase 1, compound
correction 1.0, **21 eV displacement**, maximum 2 million secondary recoils.
Requested lattice/surface binding was 10⁻⁶ eV, printed 0.00 eV; solid-default
substitution was checked. Midpoint sources used target width
`max(1 µm,8*(projected range+6*longitudinal straggling))`. Containment, history
counts and primary/secondary escape were checked. `TRIM.DAT` preserved exact
incident energies. EXYZ requested 1000 energy samples, with a 1 eV increment
floor.

The initial 28-point logarithmic 0.1–3000 keVnr grid received refinement and
adaptive 100-history batches. Completed: **1,614 production/refinement** and
**180 systematic batches**; including pilots/controls, **1,939 archives,
180,011 histories**. Shared-seed controls are not independent statistics.
All 330 production/refinement points met relative QF SEM≤2% and range SEM≤3%.
The original baseline export selected 852 nominal batches/85,200 histories, giving
**142 direct points at 0.25–1000 keV**. All species have direct 1000 keV
controls; measured guards bracket 0.25 keV.

## Exactly how repository components were used

| Source | Role in this result |
|---|---|
| [configs/cygno_hecf4_900mbar.toml](../../configs/cygno_hecf4_900mbar.toml) | Gas, grid, statistics, controls |
| [cygno_study.py](../../scripts/cygno_study.py): `gas_conditions`, `prepare`, `run_srmodule`, `run_one`, `analyse` | Density/stoichiometry, provenance, geometry, cascades, aggregation |
| [srim_scan.py](../../srim_scan.py): `prepare_runtime`, `atomic_write_json`, `archive_files_atomic`, `source_fingerprint` | Isolated installation copies, atomic records/archives, fingerprints |
| [srim_gas_sweep.py](../../srim_gas_sweep.py): `load_elements`, `write_sr_in`, `parse_stopping_output`, `interpolate_stopping` | Target masses, stopping/range geometry estimates |
| [trim_qf_sweep.py](../../trim_qf_sweep.py): `write_trim_in`, `parse_ioniz` | Existing writer; independent IONIZ cross-check |
| [cygno_study_physics.py](../../scripts/cygno_study_physics.py): `write_bulk_trim_dat`, `parse_effective_settings`, `integrate_native_profile`, `integrate_energy_budget`, `parse_range_3d`, `parse_exyz` | Source, settings, energy accounting, endpoints/sampled paths |
| [cygno_native_run.py](../../scripts/cygno_native_run.py), [start_cygno_study.ps1](../../scripts/start_cygno_study.ps1) | Native Windows execution |
| [cygno_study_adaptive.py](../../scripts/cygno_study_adaptive.py): `refinement_points`, `adaptive_repeats`, `impurity_diagnostic` | Refinement, precision, paired impurity tests |
| [cygno_study_validation.py](../../scripts/cygno_study_validation.py): `analyse_systematics`, `verify_archives` | Sensitivity controls; archive/hash checks |
| [frozen/export_cygno_mc.py](frozen/export_cygno_mc.py): `capture`, `aggregate`, `smooth`, `evaluate`, `build` | Baseline selection, snapshot, correlated SEMs, regression/evaluation |
| [frozen/validate_cygno_mc_export.py](frozen/validate_cygno_mc_export.py) | Accounting, boundaries, derivative integral, C++ comparisons |
| [reproduce.py](reproduce.py), [tools/make_recap.py](tools/make_recap.py) | Portable verification/replay and thesis-comparison figures |
| [tools/independent_energy_check.py](tools/independent_energy_check.py) | Independent Decimal audit; requires original local raw campaign |

Frozen [configuration](frozen/snapshot/geant4_config.json),
[metadata](frozen/snapshot/source_metadata.json), batches/hashes and
[validation](validation/) preserve provenance. Full archives stay in the local
campaign; SRIM binaries/databases and the thesis PDF are not redistributed.

## Energy accounting, response and uncertainty

Both differential `IONIZ.txt` columns are summed over **all bins**, multiplied
by bin width and converted eV→keV. They are already per incident ion:
**no additional division by history count**.

$$
QF(E)=\frac{E_{\rm electronic,primary}+E_{\rm electronic,recoils}}{E},
\qquad R(E)=E\,QF(E).
$$

Separate channels/covariance and remaining energy are retained. Intermediate
`E2RECOIL` transfers are **excluded** from the final-sink sum to avoid double
counting. PHONON uses SRIM's documented energy convention plus effective
vacancy-binding bookkeeping. Stopping-power ratios never supply these QFs.

A bounded, nondecreasing cubic B-spline fits QF against ln(E), with
leave-one-energy-out regularization selection. Analytic **d[E·QF]/dE** and
ordinary **dQF/dE** are distinct fitted quantities. Four hundred joint-channel
batch-bootstrap fits provide conditional statistical SEMs/pointwise
percentiles, excluding fit-choice, threshold, physics and calibration error.
d[E·QF]/dE can slightly exceed one and remains unclipped. Evaluators provide
one-sided boundary values and reject energies outside 0.025–3000 keV/ion.

For requested **293.15 K, 0.001388495951 g/cm³** (rounded 0.00138850), QF is
reused unchanged under dilute-gas invariance. Density differs by −0.0503%;
Geant4 target masses differ slightly. Transfer uncertainty is unassigned;
exact-target direct coverage is absent.

## Comparison with Flaminia's thesis and frozen CYGNO-MC reference

The comparison uses the **16 October 2026** source reference (commit `1246b920`).
The MC checkout now has local edits; those edits are untouched and unvalidated here.

Flaminia Di Giambattista's *Background study of the LIME prototype at
underground LNGS for the CYGNO experiment* (PhD, 1 March 2024), Sec. 4.1.2.1,
Eqs. 4.9–4.11/Table 4.1, printed pp. 127–130 (PDF 136–139), uses the same
IONIZ definition: 100 ions, 23 energies, fit **1–500 keV**. Eq. 4.17,
printed pp. 131–132 (PDF 140–141), defines d[E·QF]/dE.

The thesis form `QF=f/(1+f)`, `f=k*(E+a*E^b)` matches frozen CYGNO-MC He/C/F
coefficients exactly. In the frozen reference its H fit is unused: protons are excluded from NR fields,
leaving deposits unquenched. O has no thesis fit and gets unity. Orange H/O
lines are therefore defaults, not fitted predictions.

At 1 keV, direct He/C/F QFs **0.4011/0.2534/0.2061** exceed thesis
**0.3644/0.2344/0.1908** by 8–10% before smoothing; agreement improves above
10 keV. Outside 1–500 keV, thesis curves extrapolate. Original complete
cutoff/CAB/cascade inputs are unavailable; preliminary quick-mode optimization
does not establish production mode. The discrepancy's cause remains unproven.
Green same-form refits diagnose fit dependence. Missing legacy covariance
precludes a justified legacy band.

## Checks and unresolved limitations

All 1,939 original archives passed content/hash checks. Independent Decimal integration
agreed within 0.00845%: parser validation, not physics validation. Checks include
205 Python/C++ comparisons and byte-identical frozen numerical replay.
The [publication checks](validation/publication_checks.json) also record
fresh-checkout replay, 132 legacy C++ cases and the repeated raw-energy audit.

Changing displacement 21→10 eV shifts 1 keV He/C/O/F QFs by
**+3.7/+9.4/+10.8/+11.5%**: statistics cannot settle low-energy accuracy.
Two H points near 0.315 keV retain ≈1.2% closure warnings. Contaminated-H's
raw reversal is 0.25 SEM, consistent with fluctuations; its flag remains.
All 110 eligible impurity comparisons satisfy nominal ±2%, with no adaptive
simultaneous-coverage guarantee or extension to every lower-energy point.

Derivative grid/basis convergence remains unproven. EXYZ provides sampled
primary paths, not complete 3D arc lengths. Electronic energy includes
excitation; recoil-dependent W values and detector calibration remain needed.
Inclusive cascade QFs require ancestry/terminal-energy bookkeeping before
per-hit use. Status **`finished_with_unresolved_validation`** is preserved.

<!-- bounded-extension -->
## Bounded extension: 17 October 2026

The [inventory](validation/extension_inventory.json) found 180 compatible
baseline energy points in 852 batches, spanning 0.1–3000 keV. All were reused,
including 15 nominal coarse-control anchors whose `dataset_role` remains
separate in the snapshot. Non-nominal threshold and correlated fine controls,
pilots and input controls are excluded. No requested 25/40/63 eV point existed.

[cygno_qf_extension.py](../../scripts/cygno_qf_extension.py): `inventory`,
`extension_jobs`, `run_extension` used the existing native `prepare`, `execute`
and `run_one`; [cygno_qf_extension_analysis.py](../../scripts/cygno_qf_extension_analysis.py)
audited new archives, merged frozen inputs and reused the original exporter.
The plan is fixed at 3 independent batches of 100 histories per new point,
45 batches/4,500 histories total, with no adaptive expansion. Original raw
files and configuration were preserved; new raw data are under
`runs/cygno_hecf4_900mbar_20261015/extensions/low_energy_20261017/`.
No new contaminated-medium or threshold scan was requested or run.

Combined: 897 compatible batches/89,700 histories, 195 direct points,
**0.025–3000 keV per ion**. Direct conditions remain **293 K**; the existing
**293.15 K transfer** remains derived. At 25/40 eV, the existing 50 eV SRModule
endpoint was used conservatively **only for target geometry**, with containment
checked. It supplies no low-energy QF or stopping-table replacement.

### Validation and interpretation

All 45 new archives passed member hashes and independent profile integration.
However, 45/45 batches exceed the 1% energy-closure warning;
the maximum absolute residual is **14.58%**.
These excess-energy accounting flags remain visible, without renormalization.
Native histogram counts print 99.96–100.00 despite independently verified
100 endpoints, effective histories and completion-dialog counts. The new
auditor records that anomaly (tolerance 0.05 in these audited headers only);
it does not divide again by counts or correct the energy discrepancy.

Only 6/15 new points meet 2% relative QF SEM.
Three batch means give imprecise SEMs; nominal bootstrap bands are conditional,
not guaranteed 95% coverage. Additional histories were not automatically run.
The 21 eV **target-displacement setting is not an ionization threshold**.
At 25 eV it is comparable to incident energy. No cutoff-convergence evidence
exists at these new energies; existing 1 keV controls do not establish it.
The closure anomaly's cause is unresolved. The new low-energy response is a
flagged nominal SRIM output, **not energy-conservation-qualified detector input**.

### Smooth model, derivatives and previous-response changes

The original bounded monotone cubic B-spline in ln(E), now jointly selecting
12/14/16/18/20 coefficients and penalty by leave-one-energy-out CV, and
400 paired-channel batch bootstraps
now use **all measured support**, with directly simulated endpoints. Neither
QF nor d[E·QF]/dE was fixed to the previous response. The analytic derivative
and its covariance come from the same fitted coefficients; dQF/dE is separate.
Python/C++ endpoint agreement, derivative integration and out-of-range rejection
are checked. Endpoint derivatives are one-sided model values, with no measured
support beyond those endpoints.

Maximum absolute differences over the old 0.25–1000 keV interval:

| Ion | Coefficients | ΔQF vs previous | ΔdR/dE vs previous | ΔdR/dE when omitting new low points | Fit RMS pull |
|---|---:|---:|---:|---:|---:|
| H1 | 12 | 0.00285 | 0.02083 | 0.00664 | 1.26 |
| He4 | 20 | 0.00518 | 0.01972 | 0.00281 | 1.04 |
| C12 | 18 | 0.00205 | 0.01054 | 0.00163 | 0.90 |
| O16 | 20 | 0.00058 | 0.00274 | 0.00138 | 0.73 |
| F19 | 12 | 0.00266 | 0.00807 | 0.00173 | 1.45 |

The fixed-14-coefficient trial missed the 25 eV H mean by
8.25 statistical SEM.
Joint cross-validation reduced that boundary residual to
0.62 SEM, without forcing the endpoint.
This improves the fit, not the underlying energy-closure failure.

Alternative fits with two fewer/more coefficients and weaker/stronger penalties quantify
shape sensitivity in [derivative_stability.json](validation/derivative_stability.json).
A separate refit of 0.1–3000 keV **without** the new low-energy anchors measures
their inclusion's effect together with the changed basis domain/penalty; it
does not isolate a physical cause. None is a validated replacement or a total uncertainty band.
Derivative stability is **not established**, especially at boundaries. Old/new
fits share source data, so uncertainties on changes are unassigned; their SEMs
must not be added as independent errors. The compact prior coefficients and
source hash are retained; the full previous release is in Git commit `20c35bf`.

### MC integration

Use `frozen/cygno_srim_response.hpp`, `response_models.json` and the per-ion CSVs.
The numerical bounds are **[0.025, 3000] keV per ion**, inclusive; evaluators
throw outside, with no extrapolation or clamping. `evaluate(Z,A,E)` returns
`QF`, `QF_sem`, `dRdE`, `dRdE_sem`; input E is total keV, so 25 eV is 0.025.
The CSVs retain the nine-column contract, direct source points, transferred
target values, separate electronic channels and raw flags. `is_direct=0`
and `n_ions=0` label smooth target-grid rows. The original `geant4_config.json`
is an archived reference with its old primary interval; consuming MC jobs must
choose their energy interval explicitly. This work did not write to CYGNO-MC.
Inclusive cascade response still requires ancestry/terminal-energy accounting
and detector calibration. Numerical bounds do not certify low-energy physics.
