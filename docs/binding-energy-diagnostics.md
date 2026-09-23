# Exact-zero binding handling: isolated SRIM-2013 diagnostics

Checked on cygno03, 23 September 2026. Production remains on hold. This note
records observations, not a claim that the gas model or detector quenching is
validated.

## Reproducibility

Six Kr/C/30 keV jobs at 70/80/90% Kr and 1 atm were replayed with the same
seeds in private runtimes. All six reproduced TRIM.IN, TRIMAUTO, TRIM.cfg,
RANGE.TXT, IONIZ.TXT and TDATA.TXT byte-for-byte. Diagnostics were excluded
from independent history counts. See `runs/kr_repro_check/comparison.json`.

## Format tests

Eight 50-ion canaries used the archived Kr80/CF4-20 input for carbon recoils
at 30 keV, 1 atm, seed 268673998. The geometry and source installation were
unchanged. Positive per-element displacement/lattice/surface markers were
read correctly with single spaces, double spaces, padded columns and commas.
Literal zero bindings retained the same mismatch with all tested spacings.
This disproves the proposed spacing explanation for this test case.

Artifacts: `runs/binding_format_probe/{plan,results,completion}.json` and
private `runtime/worker-*` inputs/outputs. The archived baseline reproduced.

## Zero-handling tests

A second, separate diagnostic directory used the same physical point,
seed, 50-ion count and unchanged geometry. Only the three target-energy rows
were edited. All eight canaries completed; the baseline again reproduced.

| Requested binding values | Reported C lattice/surface | Reported F lattice/surface | Reported Kr lattice/surface |
| --- | --- | --- | --- |
| Zero, original/integer/scientific notation | 0 / 0 eV | 2 / 4 eV | 2 / 4 eV |
| 1e-6 eV for each element | 1e-6 / 1e-6 | 1e-6 / 1e-6 | 1e-6 / 1e-6 |
| 1e-9 eV for each element | 1e-9 / 1e-9 | 1e-9 / 1e-9 | 1e-9 / 1e-9 |

Setting only F or only Kr to zero among otherwise positive markers reproduced
the 2/4 eV fallback for that element, while the other positive markers were
retained. Zero for C among positive markers was accepted. Thus this is
element-dependent exact-zero handling in the tested executable/input, not a
general inability to read multi-element values. The tests do not locate the
internal SRIM code responsible or prove the behaviour for every gas/element.

Artifacts: `runs/binding_zero_probe/{plan,results,completion}.json` and its
private runtime folders. Installed inputs remained byte-for-byte unchanged;
PySRIM and production configuration files were not edited.

## Measured effect in this canary

| Input | Projected range | Integrated ionization fraction |
| --- | --- | --- |
| Exact zero (F/Kr report 2/4 eV) | 0.1047 mm | 0.5096917352205 |
| 1e-6 eV bindings | 0.1047 mm | 0.5126183817441666 |
| 1e-9 eV bindings | 0.1047 mm | 0.5126183817441666 |

The ionization fraction changes by about **0.574% relative**, or **0.293
percentage points**. RANGE.TXT and IONIZ.TXT are byte-identical between the
two near-zero tests; TDATA differs because it reports the different input
values. This establishes a calculated-output effect, not merely a metadata
display difference. It is one fixed-seed, 50-ion case, **not** an estimate of
the correction over the scan or a new ensemble uncertainty measurement.

The binding mismatch does not, by itself, explain the original Kr range
curvature discrepancy: the compared old/new cohorts shared the same requested
physics, and this matched-seed canary's projected range did not change.

## Consequences and next gate

- Existing ionization results must not be labelled results of the intended
  all-zero-binding model. Preserve them with their provenance; do not apply
  a universal rescaling or merge them into a corrected-model dataset.
- A **candidate**, not yet production-approved, workaround is explicit
  1e-9 eV lattice/surface energies as a numerical approximation to zero.
  Validate it across Ar/Kr/Xe, multiple recoil species and low/high energies,
  including an epsilon-sensitivity comparison, before adopting it.
- Add input-versus-TDATA checks to the production validator so artifact
  completion alone cannot hide parameter mismatches. Such checks must
  account for TDATA's numeric precision without accepting 2/4 eV as zero.
- Keep a new model/configuration identity if parameters change; existing
  frozen campaigns and their results should remain immutable.
- Only after this gate, resume the nine-point precision cleanup and the
  larger independent Kr control study under the chosen consistent model.

The completed format/zero-handling probes used at most eight cores, 32 GiB RAM
and no scan swap. No additional production simulations were released.

## Cross-gas validation pilot

`scripts/binding_validation.sh` prepares/runs a separate fixed 36-point grid:
Ar/Kr/Xe, 80% noble, 1 atm, C/F/noble recoils, 1/10/100/1000 keV. Each point
has two five-ion simulations with identical job IDs/seeds and requested geometry,
one with 1e-6 eV bindings and one with 1e-9 eV bindings. The two model directories
under `runs/binding_validation` are separate identities; never concatenate them
as independent batches. Configurations are `configs/binding_validation_*.toml`.

Predeclared numerical gate: every target-energy report must agree with its
request, all artifacts/numeric checks must pass without clipping, paired
geometry/seeds must agree, and RANGE/IONIZ must be byte-identical for all 36
pairs. This intentionally conservative gate sends any difference to review;
it does not estimate ensemble model shifts or validate physical gas assumptions.
Full production remains gated even on success. After review, a corrected-model
precision campaign needs a distinct identity and cannot top up old-model data.

The pilot uses at most 15 cores/workers, 24/32 GiB soft/hard RAM, no scan swap,
two cumulative hours and a two-hour service backstop. Frozen plan/code/config
hashes, source installation fingerprints, raw files, timings and resource samples
are retained. Monitor with `bash scripts/binding_validation.sh status` or `logs`;
use `stop` and `resume` for checkpointed interruption.

The shared scan runner now checks displacement/lattice/surface energies against
TDATA before accepting a job, retaining its raw archive on failure and halting
new submissions. Relative tolerance 1e-5 permits small print rounding without an
absolute floor that could hide tiny values rounded to zero. Missing, duplicate,
malformed or nonfinite reports fail. Offline validation checks actual archived
TDATA too, including legacy archives; old literal-zero jobs are not grandfathered
into a false model validation. Previous saved reports/data remain unchanged.
