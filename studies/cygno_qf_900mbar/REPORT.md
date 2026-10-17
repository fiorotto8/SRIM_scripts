# CYGNO SRIM/TRIM QF study and thesis comparison

**Result:** reproducible nominal electronic-energy responses for five species.
Low-energy settings and derivative model dependence remain unresolved; these
are not calibrated free-charge QFs. This release starts no new simulations.

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
The baseline export selects 852 nominal batches/85,200 histories, giving
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
one-sided boundary values and reject energies outside 0.25–1000 keV/ion.

For requested **293.15 K, 0.001388495951 g/cm³** (rounded 0.00138850), QF is
reused unchanged under dilute-gas invariance. Density differs by −0.0503%;
Geant4 target masses differ slightly. Transfer uncertainty is unassigned;
exact-target direct coverage is absent.

## Comparison with Flaminia's thesis and current CYGNO-MC

Flaminia Di Giambattista's *Background study of the LIME prototype at
underground LNGS for the CYGNO experiment* (PhD, 1 March 2024), Sec. 4.1.2.1,
Eqs. 4.9–4.11/Table 4.1, printed pp. 127–130 (PDF 136–139), uses the same
IONIZ definition: 100 ions, 23 energies, fit **1–500 keV**. Eq. 4.17,
printed pp. 131–132 (PDF 140–141), defines d[E·QF]/dE.

The thesis form `QF=f/(1+f)`, `f=k*(E+a*E^b)` matches frozen CYGNO-MC He/C/F
coefficients exactly. Its H fit is unused: MC excludes protons from NR fields,
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

All 1,939 archives passed content/hash checks. Independent Decimal integration
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
