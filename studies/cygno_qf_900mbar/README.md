# CYGNO recoil electronic response: He/CF₄ at 900 mbar

Results from the 15–16 October campaign, extended on 17 October 2026,
covering **H1, He4, C12, O16 and F19**. Energy is total kinetic energy in
**keV per ion**, not keV/u. QF means primary-plus-secondary electronic energy
divided by incident energy; it is not a measured free-charge quenching factor.

**Low-energy limitation:** all 45 new batches fail the retained 1% energy-closure
criterion (maximum 14.58%). Their response is a flagged nominal calculation.

Start with the [recap PDF](plots/recap.pdf) and [short technical report](REPORT.md).
The plots compare the new direct points and smooth responses with Flaminia
Di Giambattista's thesis fits and the formulas/defaults present in the frozen
CYGNO-MC source configuration. These are function comparisons, not new Geant4
event simulations.

## Tables

| Files | Contents |
|---|---|
| [H1](tables/H1.csv), [He4](tables/He4.csv), [C12](tables/C12.csv), [O16](tables/O16.csv), [F19](tables/F19.csv) | 401 smooth evaluation energies each, 0.025–3000 keV; QF, response and derivative with statistical uncertainties |
| [raw_points.csv](tables/raw_points.csv) | All eligible direct baseline points, including energies outside the exported interval used to bracket the fit |
| [energy_channels_source.csv](tables/energy_channels_source.csv) | Separate primary/recoil electronic energies, SEMs, covariance and energy-accounting flags |
| [threshold_sensitivity.csv](tables/threshold_sensitivity.csv) | Displacement-setting controls; sensitivity estimates, not a total uncertainty band |
| [impurity_comparison.csv](tables/impurity_comparison.csv) | Baseline versus explicit H₂O/O₂ stopping-medium comparisons |
| [thesis_mc_comparison.csv](tables/thesis_mc_comparison.csv) | New response versus thesis fits and frozen MC formulas/defaults; unavailable O thesis values are blank |

Common fields are `species,Z,A,energy_keV,QF,QF_sem,n_ions,run_id,is_direct`.
Smooth tables additionally contain `R_keV=E*QF`, `R_keV_sem`, `dR_dE`,
`dR_dE_sem`, ordinary `dQF_dE_per_keV`/`dQF_dE_sem_per_keV` and pointwise
2.5/97.5% bootstrap quantiles. **dR/dE=d[E·QF]/dE differs from dQF/dE.**
`is_direct=1` identifies
original-condition simulations; smooth/target-transferred rows have
`is_direct=0`. Smooth rows have `n_ions=0`: no additional histories were run at
those evaluation energies. SEM describes uncertainty in the estimated mean,
not an eventwise fluctuation. Details and limits are in the report.

## Reproduce

From the repository root, use Python 3.11 for a fresh installation of the
pinned [requirements.txt](requirements.txt) (Matplotlib 3.6.3 has no Python
3.12 wheel), then run:

```sh
python -m pip install -r studies/cygno_qf_900mbar/requirements.txt
python studies/cygno_qf_900mbar/reproduce.py --verify-only
python studies/cygno_qf_900mbar/reproduce.py --out /tmp/cygno_qf_replay
```

Use an unused output directory. A C++17 compiler (`g++`) is required for the
Python/C++ agreement check; the evaluator is
[frozen/cygno_srim_response.hpp](frozen/cygno_srim_response.hpp).
The verified replay used the existing Python 3.12.3 environment, NumPy 1.26.4,
SciPy 1.11.4 and Matplotlib 3.6.3; a fresh dependency installation was not
tested. Replay uses the bundled [frozen inputs](frozen/)
and does not require SRIM, the thesis PDF, a CYGNO-MC checkout, or a new scan.
The original campaign and full raw archives remain in the local ignored
`runs/cygno_hecf4_900mbar_20261015/` directory; they are not bundled here.
The optional [independent raw-energy audit](tools/independent_energy_check.py)
requires that original campaign: pass `--study-dir PATH` and optionally
`--out NEW_FILE.json`. It only reads the campaign.
Superseded exports and draft recaps were removed after preservation checks;
[reorganization.json](provenance/reorganization.json) lists the cleanup.

**Conditions:** direct simulations used 293 K and
0.001389194959 g/cm³. Tables for the requested 293.15 K,
0.001388495951 g/cm³ medium reuse QF under the documented gas-transfer
approximation. Exact-target direct simulations are absent. Below 0.025 and
above 3000 keV the supplied evaluator rejects inputs. The bands exclude
model, threshold, fit-choice, gas-transfer and detector-calibration errors.

<!-- bounded-extension -->
## 25–63 eV extension

Exactly **45 new batches / 4,500 histories** were run: three independent
100-history batches at 25, 40 and 63 eV for each ion. All 852 compatible
previous batches were reused. The resulting **195 direct points support
0.025–3000 keV per ion**. Systematic source roles remain labelled separately.

**This is numerical coverage, not validated detector or low-energy physics.**
New energy-closure residuals reach **14.58%**;
45 of 45 new batches exceed 1%. Only
6 of 15 new points meet 2% relative QF SEM.
No renormalization or automatic statistical expansion was applied.

- [New direct points](tables/extension_points.csv), including channels/SEMs/closure flags.
- [Full point inventory](tables/point_inventory.csv), including the separate contaminated medium and control roles.
- [Changes from the previous response](tables/previous_response_comparison.csv).
- [Derivative sensitivity](tables/derivative_stability.csv): basis/penalty envelope, **not** a confidence band.
- [Comparison plot](plots/extension_comparison.png), including a fit excluding new low-energy anchors.

The [report](REPORT.md) explains the 21 eV setting, histogram-count anomaly,
boundary checks and MC integration. The original MC configuration is kept
as provenance; its primary-energy interval is not automatically rewritten.
