# Repository workflow and reproducibility

The existing native workflow is retained. `srim_gas_sweep.py` reads the gas CSV, writes SR.IN, runs SRModule, and parses stopping and range tables. `trim_qf_sweep.py` supplies TRIM input writing and the IONIZ electronic-energy integral. `srim_scan.py` supplies isolated installation copies, atomic output writing, raw archives and source fingerprints. `pysrim_runner.py` and the Ubuntu bootstrap/preflight scripts support the separate `main` branch workflow; this campaign uses native Windows executables. The active Windows `GasDensities.csv` specifies 293 K, which takes precedence over the dormant 293.15 K scan configuration.

`scripts/cygno_study.py` combines those components for the specified 60/40 He/CF4 study. The additional physics parser validates effective settings and independent energy channels. The adaptive helper proposes extra direct simulations, and the reporter consumes completed results without filling missing points. Private runtime directories, raw input/output archives, independent recorded seeds and source snapshots support replay. `summary.csv`, `comparison.csv`, compressed event endpoints and the report numerical JSON are generated data; binaries and run archives stay local under the repository ignore policy.

The campaign begins with 840 production batches (28 energies, five species, two media, three independent batches of 100 histories), plus pilots. Direct grid refinement, extra paired statistics up to 30 batches, threshold tests and EXYZ sampling tests follow. The initial nine-batch limit was increased after the completed hydrogen/helium histories showed that it could leave range precision and impurity comparisons unresolved. This changes the statistics budget, preserving the physical settings, completed histories, seeds and initial job identities; the transition is recorded under `qualification/statistics_budget_extension`. No production-completeness claim is made by this document; consult the run's validation and campaign state.

# CYGNO SRIM/TRIM methodology and interpretation

Research date: 2026-10-15. This is a methodology review, not a record of completed new simulations. All numerical campaign results must be traced to the corresponding raw files. URLs below identify primary collaboration material, original research, the SRIM authors' manuals, and NIST data.

## 1. What the existing CYGNO method actually computes

The CYGNO simulation meeting of 7 February 2022, slide 2, specifies an ensemble energy fraction obtained from `IONIZ.TXT`, including both incident-ion and secondary-target-recoil electronic losses. It then differentiates a fitted cumulative response, `F(E) = d[E QF(E)]/dE`, for a spatial ionization profile. Slides 3–4 compare assigning the incident-species or recoiling-species response to secondary losses. Neither treatment agrees universally with external measurements; the slides explicitly question excitation contributions and low-energy SRIM accuracy. These are simulation prescriptions and diagnostics, not a measured He/CF4 charge-yield calibration. [CYGNO QF comparison, 7 February 2022](https://agenda.infn.it/event/30182/contributions/162908/attachments/88121/117891/QF_comparison_7-02-2022.pdf).

For this study, use the unambiguous full-cascade ensemble definition

```text
Ee_primary(E0)   = sum_j IONIZ_IONS[j]    * bin_width_A[j]   [eV / incident ion]
Ee_secondary(E0) = sum_j IONIZ_RECOILS[j] * bin_width_A[j]   [eV / incident ion]
f_e_SRIM(E0)     = (Ee_primary + Ee_secondary) / (1000 * E0_keVnr)
Eee_SRIM_keV    = E0_keVnr * f_e_SRIM(E0)
```

`Eee_SRIM` is an electronic-energy proxy, not a measured charge-equivalent calibration. Keep primary and secondary columns separately as well as their sum. Do not multiply the secondary electronic term by a further QF: it already describes energy transferred to electrons. Do not sum an inclusive parent response and separately add the same cascade response. The derivative construction is unnecessary for the integrated QF requested here; any later spatial digitization must specify which losses it weights and conserve its inclusive energy once.

The published CYGNO overview reports SRIM-based H, He, C and F QFs for 60/40 He/CF4 at 1 atm for sensitivity projections; its Figure 3 calls the production-to-stopping-point distance a 3D range. That distance is a displacement, not arc length. This publication is a useful methodological comparison, not experimental validation of the present 900 mbar calculation or of oxygen recoils. [Amaro et al., *The CYGNO Experiment*, Instruments 6 (2022) 6](https://doi.org/10.3390/instruments6010006).

## 2. Histogram integration and energy bookkeeping

The installed `HELP - TRIM Output.pdf` (Chapter 9, pp. 9-25–32) describes `IONIZ` in eV/(Angstrom ion), with depth at each bin's bottom edge; `E2RECOIL` is energy transfer to recoils. [SRIM Chapter 9](https://www.srim.org/SRIM/SRIM%2009.pdf).

Integrate bins by `sum(y * width)`, including the first width from the viewing minimum. Trapezoidal integration can lose half of first-bin deposition. Use complete integer ion counts and a viewing window covering deposition. Do not add intermediate `E2RECOIL` transfer to the terminal secondary electronic contribution. Independently check

```text
E0 = Ee_primary + Ee_secondary + E_non_electronic + E_escaped + E_unresolved.
```

A remainder obtained by subtraction is not an independent conservation check. Parse actual `PHONON` units; do not silently reinterpret counts as energies. Preserve cutoff/binding ambiguity.

## 3. Why this is not yet a physical ionization QF

The original W-value analysis distinguishes energy given to electrons (which includes excitation) from electron-ion pair production. W includes the competition among ionization, excitation, molecular degrees of freedom and sub-excitation energy; it depends on particle species and energy. For complete containment,

```text
W_i(E) = E / mean(N_pairs_i)
Q_charge(E; W_e_reference) = W_e_reference / W_i(E).
```

With a nonlinear electron calibration, determine physical `Eee` by matching signals, `N_electron(Eee) = N_ion(E_nr)`, rather than assuming constant electron W at sub-keV energies. The paper finds gas/species-dependent discrepancies with SRIM and does not provide a measured He/CF4 60/40 correction. [Katsioulas, Knights and Nikolopoulos, *Ionisation quenching factors from W-values in pure gases for rare event searches*, arXiv:2105.01414v2, Sections II and IV](https://arxiv.org/pdf/2105.01414).

An explicit phenomenological calibration could define `alpha_i(E) = N_pairs_i(E) / Ee_SRIM_eV(E)` and use `N_pairs_i = alpha_i * Ee_SRIM_eV`. Equivalently define `C_i(E) = Q_charge(E)/f_e_SRIM(E)`. Neither `alpha_i` nor `C_i` is supplied by TRIM. A constant `C_i = 1` is an assumption requiring validation, and dividing electronic energy by a first ionization potential does not supply it. Do not apply an ion W-value to the electronic fraction a second time: that W is defined relative to the ion's full initial energy and already includes its energy partition.

CYGNO's February 2021 discussion explicitly recognizes this ambiguity and proposes considering the NR/ER response ratio `Qnr/Qer` before using the electron W-value. It presents this as a possible approach, not a determined numerical correction. [Cortez, Di Giambattista and Baracchini, *Ionization Yield and W-value*, especially slides 10–11](https://agenda.infn.it/event/25766/contributions/129858/attachments/78771/101966/Ionization_Yield.pdf).

The 2026 CYGNO readout-simulation preprint assumes an ER W-value of 35 eV and models electron attachment, GEM gain/saturation and optical collection separately. Its calibration data are X-rays; it supplies no NR-specific SRIM correction. Small oxygen contamination can alter electron survival substantially even if stopping changes little. Do not infer a free-charge, collected-charge or camera-light QF from a stopping-medium impurity test. [Amaro et al., *Simulation of the CYGNO Gaseous TPC Optical Readout*, arXiv:2601.01435v1, Sections 3.1–3.5](https://arxiv.org/html/2601.01435v1).

Needed for a physical charge calibration: species- and energy-dependent recoil measurements (or validated ion W-values) in the same gas mixture and operating conditions, plus electron reference calibration and control of recombination/attachment. Optical NR response additionally requires the calibrated gain, saturation and light-response chain. No numerical physical-charge correction has been established in this review.

## 4. Gas phase, cascade thresholds and geometry

The installed `HELP - TRIM Input.pdf` (Chapter 8, pp. 8-3, 8-6–7, 8-11–15) requires gas phase selection and follows full-cascade recoils down to the smallest displacement threshold. It distinguishes compound bonding corrections from composition, notes sparse heavy-ion gas data and an STP gas-stopping assumption. `TRIM.DAT` supports interior starts, with energies in eV, positions in Angstrom, direction cosines and CRLF records. Large cascades can exceed memory limits. [SRIM Chapter 8](https://www.srim.org/SRIM/SRIM%2008.pdf).

Consequently, record phase, density, stopping correction and thresholds explicitly. Entering the correct density sets a length scale, without validating pressure-dependent charge-state effects. State whether Bragg additivity is assumed; do not call gas “vacancies” physical lattice damage. Test low-energy threshold sensitivity. For intrinsic bulk response, place the start safely inside a thick homogeneous target and subtract its coordinates from endpoints. Increasing rear thickness cannot fix entrance backscatter loss. Preserve escape records and warning diagnostics; none of these checks passes automatically.

This campaign uses a common 21 eV displacement parameter and requested lattice/surface binding energies of 1e-6 eV. These are SRIM model settings, with no measured He/CF4 gas threshold established here. Sub-displacement recoil energy enters non-electronic/phonon bookkeeping rather than further explicit secondary electronic transport. Energy closure therefore does not validate the low-energy charge partition. The 10/21/30 eV comparisons at 1, 100 and 1000 keVnr quantify sensitivity to selected settings; they do not establish zero-cutoff convergence throughout the grid. The reported electronic-energy fraction includes the secondary recoils tracked under these conditions.

## 5. What a 3D track output can and cannot establish

The manual identifies `RANGE_3D` as endpoints and `EXYZ` as primary positions at energy intervals, excluding secondary electronic deposition (Chapter 9, pp. 9-13–17). Thus distinguish projected displacement, 3D displacement, and sampled primary polyline length `sum(||r[k+1]-r[k]||)`. Endpoints cannot determine arc length. Verify endpoint identity and sampling convergence. A primary trajectory excludes other cascade branches. [SRIM Chapter 9](https://www.srim.org/SRIM/SRIM%2009.pdf).

Detailed `COLLISON.txt` output offers additional 3D collision information: primary collision coordinates and, with recoil details enabled, cascade recoil records. The output manual states that only primary collisions producing displacements are tabulated. Recoil numbers are local to a cascade; the documented columns do not provide an explicit parent-track identifier. Consecutive recoil rows therefore cannot simply be joined into one trajectory. Complete primary and cascade arc lengths were not recorded or established by the production outputs used here. Qualification of collision filtering, recoil histories and transport-step sampling is needed before assigning such lengths; their absence here does not establish that SRIM cannot supply useful collision trajectories. [SRIM Chapter 8, p. 8-23](https://www.srim.org/SRIM/SRIM%2008.pdf), [Chapter 9, pp. 9-34–9-39](https://www.srim.org/SRIM/SRIM%2009.pdf).

## 6. Projectiles and isotope mass provenance

The NIST isotopic-composition table gives the following relative **neutral atomic masses**, in u, appropriate to documenting the SRIM projectile mass convention. Do not substitute the isotope mass number or a bare-nucleus mass. Natural target-element masses remain those installed in SRIM and should be recorded separately.

| Projectile | Z | Atomic mass / u |
|---|---:|---:|
| 1H | 1 | 1.00782503223 |
| 4He | 2 | 4.00260325413 |
| 12C | 6 | 12.0000000 |
| 16O | 8 | 15.99491461957 |
| 19F | 9 | 18.99840316273 |

[NIST Atomic Weights and Isotopic Compositions, complete table](https://pml.nist.gov/cgi-bin/Compositions/stand_alone.pl?ascii=ascii); [NIST fluorine](https://pml.nist.gov/cgi-bin/Compositions/stand_alone.pl?ele=F&ascii=ascii). These values identify the chosen source table; no claim of a newer mass evaluation is made.

## 7. Reporting rules for this campaign

- Label `f_e_SRIM` and `Eee_SRIM` explicitly; physical charge QF and charge-calibrated keVee remain uncalibrated.
- Compare baseline and impurity-medium runs with the same event allocation, independent recorded seeds and statistical uncertainties. Specify whether ppm denotes mole fractions of the final gas or additives relative to the original mixture. Intrinsic stopping and charge transport require separate impurity conclusions.
- Additional matched batches are requested when either medium misses the QF/range precision targets or an in-domain impurity QF comparison is unresolved. Impurity classification uses paired contrasts `wet - (1 - delta) * dry` and `wet - (1 + delta) * dry`, with `delta = 0.02`; these retain the uncertainty in the baseline. Nominal Student-t intervals distinguish equivalence within the selected band, a material increase/decrease, and an inconclusive result. Adaptive repeated checks do not guarantee 95% sequential or simultaneous coverage. Three-batch variance projections are planning estimates; the classification must use actual completed histories, and a comparison still unresolved at the budget limit remains inconclusive.
- Keep stochastic uncertainty, finite-bin/trajectory sampling effects, energy-grid interpolation, gas-threshold sensitivity, compound/phase-model limitations and unknown charge calibration as separate uncertainty sources.
- The separate `srmodule_medium_comparison` tables use exact matching native energies for each species and report contaminated/baseline minus one. This is a deterministic stopping/range screen with unassigned model uncertainty and finite printed precision. It supplies no full-cascade QF or physical charge equivalence. Energy-channel SEMs come from independent batch means; combined lateral-straggling uncertainty deletes paired y/z endpoints jointly. Numerical convergence flags apply to the configured QF and projected-range SEM targets.
- `IONIZ` is an ensemble histogram and supplies no per-event energy-variance estimate. Independent batches/seeds can estimate Monte Carlo uncertainty of its integral. Endpoint files permit event-level range statistics; small pilot batches cannot establish distribution-tail convergence.
- Test monotonicity on `Eee_SRIM(E_nr)` before inversion. Retain statistically significant failures and restrict interpolation to the simulated domain; no unsupported extrapolation to the requested electron-equivalent interval.
- No measured He/CF4 60/40, 900 mbar physical-ionization correction was established by these sources. Simulated electronic-energy curves must not be presented as detector-qualified charge response.

## 8. Configuration qualification actually executed in this checkout

These are qualification checks, not completed production curves. The corresponding inputs, effective settings and outputs are preserved under `raw/` and `provenance/`.

- A native full-cascade, interior-source F recoil canary at 100 keVnr completed 20 histories. Despite requested zero lattice/surface binding, the effective saved settings substituted 2/4 eV for C and F. The missing terminal energy was consistent with the vacancy-times-binding bookkeeping. That canary is excluded from production results.
- A second native F canary at 10 keVnr completed five histories using positive 1e-6 eV lattice/surface inputs. Effective binding fields reported 0.00 eV (their precision is 0.01 eV). IONIZ plus PHONON closed to about 5e-5 relative residual. This is a numerical configuration check, not a gas-model or detector calibration.
- PHONON's energy interpretation is supported by the SRIM authors' [Target Damage tutorial, pages 5–6](https://www.srim.org/SRIM/Tutorials/SRIM%20Tutorial%204%20-%20Target%20Damage.pdf), which calculates its energy in eV/keV per incident ion. Preserve the native heading and test the independent channel sum rather than defining the remaining energy by subtraction alone.
- The text `TDATA.txt` density display can omit the scientific exponent for this low-density gas: it displayed 1.389 g/cm3 while `TDATA.sav` retained 1.3892e-3 g/cm3 and the atomic number density agreed with the latter. Production validates the saved effective density against the gas calculation and preserves both raw files.
- Mode 5 displays an end-of-TRIM.DAT completion dialog even with TRIMAUTO enabled. Its displayed OK button has control ID 2 here. The supervisor acknowledges only that exact process-owned completion dialog after checking its completed history count. Six consecutive 100-history qualification batches completed automatically; errors and unrelated windows are not acknowledged.
- Very-low-energy control bins show SRIM numerical limitations: the 0.1 keVnr H pilot electronic-plus-PHONON sum exceeded the initial energy by about 4%. Such points lie below the requested 0.25 keV SRIM-electronic surrogate interval; they remain preserved and flagged. Production requires the configured 1% energy closure in the requested surrogate domain and retains low-energy failures explicitly.
- Low-energy IONIZ/PHONON headers can print fractional counts such as 99.99 or 4.99 even when saved integer settings and final endpoints establish 100 or five completed histories. The output manual (p. 9-26) documents fractional profile normalization for an ion still in flight; the exact completed-run bookkeeping behind this discrepancy is unverified. Preserve those native header values and integrate the already-normalized profiles without an additional division or unsupported correction. Use integer saved settings and endpoint counts for completed event statistics. An independent Decimal integration of archived raw bins is retained under `qualification/independent_energy_check.json`, with its replay script and explicit snapshot scope.
- Matched concurrency qualification compared four and eight isolated native workers on eight F recoil batches at 10 keVnr, both media, 20 histories per batch. Identical inputs and seeds produced identical energy-channel integrals, final endpoints and sampled-path summaries. Eight workers completed the 160-history workload in 49.79 s versus 74.03 s for four (1.49 times the throughput), excluding runtime-copy setup. This benchmark concerns the stated cases on this eight-core machine. Production resumed with the existing `--workers 8` execution override; the physics configuration and cached completed batches were preserved. Replay inputs, raw outputs, script and timing results are retained under `qualification/worker_scaling*`.

- Input precision qualification accepted all 28 logarithmic grid energies in native TRIM. Mode 5 uses TRIM.IN as an upper energy ceiling; its generated value is raised by 1e-5 relative to prevent rounded-down rejection of the exact TRIM.DAT energies. An additional three-history canary with a 0.1% higher ceiling verified that actual injected energy remained the TRIM.DAT value. EXYZ energies are checked using the precision of their printed digits. Raw checks are retained in `qualification/energy_input_validation.json`.

The detached campaign continues beyond these canaries. Its current report must be read with the recorded incomplete/validation status until all required direct, convergence and systematic runs have finished.
