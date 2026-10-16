# CYGNO-MC interchange from existing full-cascade SRIM results

Run `scripts/export_cygno_mc.py` in the existing WSL analysis environment with
NumPy, SciPy and Matplotlib. This is postprocessing only. It does not launch
SRIM, touch a stop marker or lock, alter the installation, or regenerate the
running campaign's tables. Use a new export directory outside the study tree:

```bash
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 nice -n 15 python3 scripts/export_cygno_mc.py \
  --study-dir runs/cygno_hecf4_900mbar_20261015 \
  --config geant4_config.json --out exports/cygno_mc_20261016_final
```

The supplied local `geant4_config.json` is copied byte-for-byte into the export.
It describes CYGNO-MC source and macros; its own provenance explicitly says
that it is not a new detector initialization or simulation. No claim of a
validated Geant4 runtime configuration follows from this input file.

## Source gas and requested target

The existing dry-baseline simulations use He/CF4 = 60/40 molar, 900 mbar, 293 K and
density 0.0013891949590458925 g/cm3. Their elemental atomic fractions are exactly
He = 3/13, C = 2/13, F = 8/13. These are different from the molecular or mass fractions.

The requested CYGNO-MC target is 293.15 K and density 0.00138850 g/cm3. The config's
unrounded density, 0.0013884959507410994 g/cm3, is retained in the manifest;
0.00138850 is its rounded display. Its natural-element mass references also
differ slightly from the installed SRIM He = 4.003, C = 12.011, F = 18.998 u values.

**There are no direct simulations at the exact requested target conditions in
this campaign.** Source direct points keep their original conditions. Target
tables reuse QF and electronic energies unchanged under the approximation
that this dilute homogeneous gas's inclusive energy partition is invariant
under this small density/temperature change. This is a derived transfer, not
a measured or newly simulated correction. No uncertainty for that transfer
or the small natural-target-mass difference has been established. A density
ratio must not be multiplied into dimensionless QF. No range rescaling is
needed for this QF-only interface.

The contaminated stopping medium is excluded from this baseline export.
H1 and O16 remain legitimate incident projectiles in the dry baseline. Their
simulated event counts do not represent contaminant concentrations. Neither
the source counts nor `n_ions` should be replaced by the million events per
Geant4 species specified in the input config.

## Files and directness

The four QF CSVs have exactly the requested columns:
`species,Z,A,energy_keV,QF,QF_sem,n_ions,run_id,is_direct`.
`is_direct` is the integer1 for direct source simulations and0 for derived rows.

| File | Meaning |
|---|---|
| `qf_source_direct.csv` | Actual nominal full-cascade points within0.25–1000 keV per ion, at original source conditions. |
| `qf_source_with_guards.csv` | Direct source points including the broader measured energy range supporting the fit boundaries. |
| `qf_target_transferred.csv` | Same point values transferred to the requested target; not direct target simulations. Counts refer to source histories. |
| `qf_target_smooth.csv` | Smooth derived evaluation grid including exactly 0.25 and 1000 keV. `n_ions=0` means no new events at these evaluation energies. |
| `response_target_smooth.csv` | QF, cumulative electronic response R, analytic dR/dE, Monte Carlo SEMs and bootstrap percentile bands. |
| `energy_channels_source.csv` | Primary and recoil electronic contributions, their SEMs/covariance, phonon/binding bookkeeping and raw conservation diagnostics. |
| `batch_energy_channels.csv` | Independent batch values and actual seeds/counts underlying the point uncertainties. |
| `stopping_power_proxies.csv` | SRModule Se/(Se+Sn) diagnostic and stopping powers; explicitly not full-cascade QF and never fitted as QF data. |
| `response_models.json` | Knots, coefficients, covariance, bootstrap replicas, fit settings and diagnostics. |
| `cygno_srim_response.hpp` | Standalone C++ evaluator for QF,dR/dE and their Monte Carlo SEMs. |
| `point_provenance.json`, `snapshot/` | Batch IDs, original file/member hashes, effective settings and frozen reproducible analysis inputs. |

`run_id` connects each aggregate to its source batches through
`point_provenance.json`. It is not a claim that a new simulation was run.
Original raw archive paths and member hashes remain in `snapshot/batches.jsonl`.
The exporter verifies the stored IONIZ-bin integrals and archive availability;
it verifies one archived input/gas-phase setting per isotope and does not
repeat the campaign's exhaustive compressed-archive content audit.

Only completed, contained nominal full cascades enter the primary curves.
Nominal21eV,1000-sample track-sampling control runs at 1, 100, 1000 keV can contribute
independent seeds. Fine 4000-sample repeats are excluded because they share
seeds with coarse runs. 10/30 eV cutoff tests and five-history pilots remain
separate and do not inflate the nominal curve's statistics. Unknown SEMs
remain blank, not zero. The manifest lists raw conservation failures and
source versus exact-target coverage separately.

## Energy accounting and SRIM settings

For an incident ion with total kinetic energy E0 in keV,

```
Eprimary = sum(IONIZ primary-bin values) * bin_width_A /1000
Erecoil  = sum(IONIZ recoil-bin values)  * bin_width_A /1000
R(E0)   = Eprimary + Erecoil
QF(E0)  = R(E0)/E0
```

IONIZ's energy columns are already normalized per incident ion, with units
eV/(Angstrom ion). Integrate all rectangular bins, including the first bin;
do not divide by the ion count again. Secondary electronic energy is already
the recoil cascade's contribution. E2RECOIL is intermediate transfer energy
and is excluded from the terminal energy sum. The independent check uses
IONIZ+PHONON+effective vacancy/binding bookkeeping and retains its residual;
the non-electronic complement alone is not an independent closure test.

The nominal calculation uses SRIM 2013, TRIM mode 5 full cascades from interior
TRIM.DAT starts, gas phase, compound correction 1, 21 eV displacement settings,
requested lattice/surface binding 1e-6 eV, and up to 2 million secondary recoils.
Effective saved binding fields print 0.00 eV; this is finite output precision,
not a proof of an exact zero cutoff. Bulk target width is 8 times the SRModule
range-plus-six-sigma estimate. The physical incident energy comes from
TRIM.DAT, not the slightly raised TRIM.IN acceptance ceiling. Natural target
masses and neutral isotopic projectile masses are retained in the snapshot.

These are SRIM electronic-energy fractions. Excitation/ionization partition,
recoil W-values, attachment, recombination and detector amplification/optical
response are not calibrated by this export. A physical free-charge QF remains
unavailable.

## Smooth response, errors and boundaries

The model is a cubic B-spline q(x),x = ln(E/keV), with a second-coefficient-
difference penalty. Coefficients are constrained to [0,1] and nondecreasing.
This explicitly assumes monotone QF as a smoothing model. It guarantees a
bounded monotone fitted QF and monotone R(E); raw statistical reversals are
retained in the direct tables. A single measured guard point below 0.25 keV and
above1000keV anchors each end. The smoothing penalty is selected by
leave-one-energy-out cross-validation. Knots, chosen penalty, residual pulls
and cross-validation scores are exported. The old three-parameter functional
form need not describe these direct data within their Monte Carlo SEMs.
Small training residuals do not establish interpolation accuracy: held-out
energy predictions can differ by more than a point's Monte Carlo SEM.
Leave-one-energy-out errors are a model diagnostic, not an assigned Gaussian
model uncertainty, and must be assessed separately when using the response.

The analytic expressions are

```
QF(E) = sum_i c_i B_i(ln E)
R(E) = E * QF(E)
dR/dE = sum_i c_i [B_i(ln E) + B'_i(ln E)]
```

Primary/recoil energies are resampled jointly within each independent energy
point. Batch-bootstrap fluctuations receive sqrt(n/(n-1)) finite-n variance
correction. 400 coefficient draws provide conditional Monte Carlo SEMs,
coefficient covariance and pointwise 2.5/97.5-percentile bands. Those bands are
not simultaneous confidence intervals or a physical-model uncertainty band;
they condition on the selected basis, penalty and shape assumption. Adaptive
stopping of the source statistics further precludes an unconditional 95%
coverage claim. Cutoff/threshold sensitivity remains a separate uncertainty,
whose active scan may not yet be complete at snapshot time.
These SEMs quantify uncertainty of an ensemble mean. IONIZ does not supply
an event-by-event ionization distribution. For correlated uncertainty studies,
use a common bootstrap coefficient replica across energies for a species;
do not treat the tabulated SEM as independent extra detector noise on each hit.

Both endpoints 0.25 and 1000 keV are supported, with analytic one-sided boundary
values/derivatives. **Outside this interval evaluation raises an error.**
There is no sub-grid clamp, continuation to zero, or extrapolation to higher
energies. Hits eventually fall below 0.25 keV: a physically validated terminal
energy policy is still required in CYGNO-MC. Missing exact-target direct
coverage is reported even when the derived smooth curve spans the interval.
The differential response can locally exceed unity although 0 <= R(E) <= E;
it is not a probability. Such values are flagged and retained without clipping.
The derivative is a model of the inclusive ensemble response, not a universally
valid multiplier for independently transported Geant4 deposits.

## CYGNO-MC integration cautions

The provided config says that no CSV loader currently exists. This export
does not change or claim to run CYGNO-MC. For standalone C++ use:

```cpp
#include "cygno_srim_response.hpp"
auto h = cygno_srim::evaluate(1,1,10.0); // Z,A,total keV per ion
// h.QF, h.QF_sem, h.dRdE, h.dRdE_sem
```

Its current NR selection `PDG>1000000000` excludes H1 protons (PDG 2212), while
O16 has no current fitted QF and receives unity. Both require explicit
integration changes before all five exported species are used. Initial ROOT
tree energy is in MeV and requires multiplication by 1000 for these tables;
no division by isotope A is appropriate.

The current hit prescription switches between QF below 1 keV and dR/dE above
1 keV. That switch does not generally integrate to the exported R(E0).
Furthermore R is inclusive of secondary cascades. Applying an inclusive
parent response and independently weighting the same descendants can double
count electronic energy. Establish track ancestry, specify which deposits
are weighted, and verify the event energy balance before using this as a
step-level detector response. R(E0) is directly suitable for an inclusive
event-level comparison. A mathematical difference R(Epre)-R(Epost) only
telescopes if its energy interval and cascade accounting are consistent.

## Frozen replay

The export contains a standalone copy of the exporter and a hashed compact
snapshot of the selected completed batch energies/settings. Reproduce the
numerical export without accessing the live scan:

```bash
OPENBLAS_NUM_THREADS=1 python3 export_cygno_mc.py \
  --snapshot snapshot --out ../cygno_mc_replay
```

The supplied release uses 400 bootstrap replicas, 401 grid points per species
and base seed 20261016. Per-model seeds/counts are in `response_models.json`;
NumPy/SciPy versions are in the manifest. Raw TRIM archives remain preserved in the original study;
the snapshot references them rather than duplicating the entire campaign.
