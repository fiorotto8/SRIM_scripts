"""Update the existing publication from the bounded native low-energy extension."""
from collections import Counter
from decimal import Decimal
import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import subprocess

import numpy as np
import export_cygno_mc as exporter
import cygno_study as study
import srim_scan as core


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_new_archives(extension, publication):
    auditor = load_module(publication/'tools/independent_energy_check.py', 'extension_energy_audit')
    batches = [json.loads(p.read_text()) for p in sorted((extension/'batches').glob('*.json'))]
    plan = json.loads((extension/'extension_plan.json').read_text())
    assert {b['job']['job_id'] for b in batches} == {j['job_id'] for j in plan['jobs']}
    results = []
    for batch in batches:
        job = batch['job']
        assert job['n_ions']==100 and job['energy_kevnr'] in (.025,.04,.063)
        assert not any(batch[k] for k in ['clipped','transmitted','backscattered'])
        assert batch['effective_settings']['target_escape_count']==0
        with tarfile.open(extension/batch['raw_archive']) as archive:
            for name, digest in batch['raw_sha256'].items():
                assert hashlib.sha256(archive.extractfile(name).read()).hexdigest()==digest, (job['job_id'],name)
            native=json.loads(archive.extractfile('native_process.json').read())
            assert native['completed_dialog_ions']==100 and native['status']=='complete'
            assert batch['endpoints']['n_records']==batch['effective_settings']['n_ions_calculated']==100
            electronic,printed_e = auditor.profile(archive.extractfile('IONIZ.TXT').read(),100,True,Decimal('.05'))
            phonons,printed_p = auditor.profile(archive.extractfile('PHONON.TXT').read(),100,False,Decimal('.05'))
        budget = batch['budget']
        independent_e,independent_p = float(sum(electronic)),float(sum(phonons))
        rel_e = abs(independent_e-budget['total_electronic_energy_kev'])/max(abs(independent_e),1e-20)
        rel_p = abs(independent_p-budget['phonon_energy_kev'])/max(abs(independent_p),1e-20)
        assert max(rel_e,rel_p) <= .0003
        results.append(dict(batch_id=job['job_id'],species=job['species'],energy_keV=job['energy_kevnr'],
                            archive_member_hashes_verified=True,independent_electronic_keV=independent_e,
                            independent_phonon_keV=independent_p,relative_parser_difference=max(rel_e,rel_p),
                            closure_residual_fraction=budget['exclusive_energy_budget_with_binding_residual_fraction'],
                            printed_IONIZ_count=float(printed_e),printed_PHONON_count=float(printed_p),
                            actual_endpoints_and_verified_completion=100,
                            geometry_srmodule_energy_keV=batch['geometry_srmodule_energy_kev']))
    return dict(batches=len(batches),histories=sum(b['job']['n_ions'] for b in batches),
                archive_and_parser_checks_passed=True,rows=results,
                maximum_abs_closure_fraction=max(abs(r['closure_residual_fraction']) for r in results),
                closure_warning_batches=sum(abs(r['closure_residual_fraction'])>.01 for r in results),
                maximum_relative_parser_difference=max(r['relative_parser_difference'] for r in results),
                histogram_count_note='New histogram headers99.96–100.00; independently verified100 endpoints/effective histories/completion dialog. Header tolerance0.05 applies only to these new audited batches; no renormalization.',
                interpretation='Numerical audit, not physical validation at energies near the 21 eV displacement setting')


def merge_snapshot(extension, publication, destination):
    previous = publication/'frozen/snapshot'
    exporter.load_snapshot(previous)
    shutil.copytree(previous,destination)
    with tempfile.TemporaryDirectory(prefix='cygno-extension-capture-') as temp:
        extra = Path(temp)/'snapshot'
        exporter.capture(extension, previous/'geant4_config.json',extra)
        _,_,new_records = exporter.load_snapshot(extra)
        _,_,old_records = exporter.load_snapshot(previous)
        for name in ['source_metadata.json','capture.json','source_installation.json','input_example_validation.json']:
            shutil.copy2(extra/name,destination/('extension_'+name))
        shutil.copytree(extra/'input_examples',destination/'extension_input_examples')
    parent = extension.parent.parent
    records = []
    for rows,source in [(old_records,parent),(new_records,extension)]:
        for original in rows:
            row = dict(original)
            row['source_study'] = str(source)
            row['dataset_role'] = ('compatible_nominal_coarse_control' if row['job']['stage']=='track_sampling'
                                   else 'new_nominal_extension' if row['job']['stage']=='low_energy_extension'
                                   else 'nominal_production_refinement')
            records.append(row)
    identities=[(b['job']['species'],b['job']['energy_kevnr'],b['job']['seed']) for b in records]
    assert len(identities)==len(set(identities))
    with (destination/'batches.jsonl').open('w') as handle:
        for row in records:handle.write(json.dumps(row,sort_keys=True,allow_nan=False)+'\n')
    capture=json.loads((destination/'capture.json').read_text())
    capture.update(captured_at=core.utc_now(),full_simulated_support=True,
                   parent_listed_source_batch_count=capture['listed_source_batch_count'],
                   previous_snapshot_sha256=exporter.sha(previous/'sha256.json'),
                   listed_source_batch_count=capture['listed_source_batch_count']+len(new_records),
                   selected_batch_count=len(records),source_studies=[str(parent),str(extension)],
                   dataset_role_counts=dict(Counter(b['dataset_role'] for b in records)),
                   extension_request=dict(energies_keV=[.025,.04,.063],batches_per_point=3,histories_per_batch=100,adaptive_expansion=False),
                   raw_archive_integrity_scope='Parent1939 archives previously verified; every member hash in all45 new archives reverified before publication')
    exporter.write_json(destination/'capture.json',capture)
    exporter.write_json(destination/'sha256.json',{p.relative_to(destination).as_posix():exporter.sha(p)
                        for p in sorted(destination.rglob('*')) if p.is_file() and p.name!='sha256.json'})
    return records


def response_diagnostics(frozen, previous_models, destination):
    """Basis/regularization sensitivity is not added to statistical SEM."""
    models=json.loads((frozen/'response_models.json').read_text())['models']
    _,_,records=exporter.load_snapshot(frozen/'snapshot')
    source=Path(json.loads((frozen/'snapshot/capture.json').read_text())['source_study']).name
    points=exporter.aggregate(records,source)
    previous={m['species']:m for m in previous_models['models']}
    comparison=[];stability=[];summaries=[];sensitivity_models={};no_low_models={};boundary_refinement={}
    for model in models:
        species=model['species'];lo,hi=model['valid_energy_keV']
        selected=[p for p in points if p['species']==species]
        without_low=exporter.smooth([p for p in selected if p['energy_keV']>=.1],.1,hi,0,20261017,full_support=True)
        no_low_models[species]={k:without_low[k] for k in ['species','valid_energy_keV','knots_log_energy','coefficients','coefficient_covariance']}
        variants=[]
        penalty=model['selected_penalty']
        center=len(model['coefficients'])
        for ncoef,p in [(max(8,center-2),None),(center+2,None),(center,penalty/10 if penalty else .1),(center,penalty*10 if penalty else 1.)]:
            candidate=exporter.smooth(selected,lo,hi,0,20261017,full_support=True,n_coefficients=ncoef,fixed_penalty=p)
            variants.append(candidate)
        sensitivity_models[species]=[{k:m[k] for k in ['species','valid_energy_keV','knots_log_energy','coefficients','coefficient_covariance']}
                                     for m in variants]
        if species=='H1':
            trial=next((m for m in variants if len(m['coefficients'])==14),None)
            if trial is None:trial=exporter.smooth(selected,lo,hi,0,7,full_support=True,n_coefficients=14)
            first=next(p for p in selected if p['energy_keV']==lo)
            boundary_refinement=dict(fixed14_lower_endpoint_statistical_pull=float((exporter.evaluate(trial,np.array([lo]))[0][0]-first['QF'])/first['QF_sem']),
                                     selected_lower_endpoint_statistical_pull=float(model['fit_residual_pull'][0]),
                                     action='Joint leave-one-energy-out CV selects basis and penalty; no endpoint hard constraint and no additional histories')
        energy=np.geomspace(lo,hi,801)
        q,qs,d,ds=exporter.evaluate(model,energy)
        alternative=[exporter.evaluate(m,energy) for m in variants]
        allq=np.array([q]+[v[0] for v in alternative]);alld=np.array([d]+[v[2] for v in alternative])
        for i,e in enumerate(energy):
            stability.append(dict(species=species,energy_keV=float(e),QF=float(q[i]),dR_dE=float(d[i]),
                                  QF_model_sensitivity_min=float(allq[:,i].min()),QF_model_sensitivity_max=float(allq[:,i].max()),
                                  dR_dE_model_sensitivity_min=float(alld[:,i].min()),dR_dE_model_sensitivity_max=float(alld[:,i].max())))
        overlap=np.geomspace(.25,1000,501)
        new=exporter.evaluate(model,overlap);old=exporter.evaluate(previous[species],overlap)
        no_low=exporter.evaluate(without_low,overlap)
        for i,e in enumerate(overlap):
            comparison.append(dict(species=species,energy_keV=float(e),QF_previous=float(old[0][i]),
                                   QF_updated=float(new[0][i]),QF_change=float(new[0][i]-old[0][i]),
                                   dR_dE_previous=float(old[2][i]),dR_dE_updated=float(new[2][i]),
                                   dR_dE_change=float(new[2][i]-old[2][i]),
                                   QF_without_new_low_points=float(no_low[0][i]),
                                   dR_dE_without_new_low_points=float(no_low[2][i])))
        locations=[lo,lo*1.01,.25,1.,1000.,hi/1.01,hi]
        boundaries=[]
        for e in locations:
            v=exporter.evaluate(model,np.array([e]))
            alt=[exporter.evaluate(m,np.array([e])) for m in variants]
            raw=next((p for p in selected if p['energy_keV']==e),None)
            boundaries.append(dict(energy_keV=e,QF=float(v[0][0]),dR_dE=float(v[2][0]),
                                   raw_QF=raw['QF'] if raw else None,raw_QF_sem=raw['QF_sem'] if raw else None,
                                   model_residual_statistical_pull=float((v[0][0]-raw['QF'])/raw['QF_sem']) if raw else None,
                                   derivative_statistical_sem=float(v[3][0]),
                                   derivative_model_sensitivity_span=float(max([v[2][0]]+[a[2][0] for a in alt])-min([v[2][0]]+[a[2][0] for a in alt]))))
        summaries.append(dict(species=species,valid_energy_keV=[lo,hi],raw_points=len(selected),
                              selected_penalty=penalty,selected_n_coefficients=center,weighted_residual_RMS_pull=model['weighted_residual_RMS_pull'],
                              maximum_abs_QF_change_previous_interval=float(np.max(abs(new[0]-old[0]))),
                              maximum_abs_derivative_change_previous_interval=float(np.max(abs(new[2]-old[2]))),
                              maximum_abs_QF_change_due_to_new_low_points=float(np.max(abs(new[0]-no_low[0]))),
                              maximum_abs_derivative_change_due_to_new_low_points=float(np.max(abs(new[2]-no_low[2]))),
                              maximum_QF_model_sensitivity_span=float(np.max(np.ptp(allq,axis=0))),
                              maximum_derivative_model_sensitivity_span=float(np.max(np.ptp(alld,axis=0))),
                              alternatives=[dict(n_coefficients=len(m['coefficients']),selected_penalty=m['selected_penalty']) for m in variants],
                              boundary_diagnostics=boundaries))
    exporter.write_csv(destination/'previous_response_comparison.csv',comparison)
    exporter.write_csv(destination/'derivative_stability.csv',stability)
    return dict(species=summaries,sensitivity_models=sensitivity_models,no_new_low_energy_models=no_low_models,H_boundary_refinement=boundary_refinement,
                method='Basis and penalty jointly selected by CV; sensitivity refits with plus/minus2 coefficients and weaker/stronger penalty; separate0.1–3000keV refit omits new anchors; no data held fixed to previous curve',
                previous_interval_keV=[.25,1000],previous_and_updated_correlated=True,
                change_uncertainties='Not assigned: fits reuse correlated source batches; do not add SEMs in quadrature',
                sensitivity_interpretation='Nonprobabilistic model sensitivity envelope, not a confidence interval or total systematic error',
                alternative_model_covariance_note='Zero covariance placeholders in deterministic sensitivity models are not uncertainty estimates; only the nominal400-bootstrap model supplies statistical SEM',
                derivative_stability_established=False)


def refresh_documentation(publication,audit,diagnostics):
    readme=publication/'README.md'
    text=readme.read_text().replace('Final results from the completed 15–16 October 2026 SRIM/TRIM campaign,',
        'Results from the 15–16 October campaign, extended on 17 October 2026,')
    text=text.replace('401 smooth evaluation energies each, 0.25–1000 keV','401 smooth evaluation energies each, 0.025–3000 keV')
    text=text.replace('Below 0.25 and\nabove 1000 keV','Below 0.025 and\nabove 3000 keV')
    if '**Low-energy limitation:**' not in text:
        text=text.replace('Start with the [recap PDF]',
            '**Low-energy limitation:** all 45 new batches fail the retained 1% energy-closure\ncriterion (maximum 14.58%). Their response is a flagged nominal calculation.\n\nStart with the [recap PDF]')
    marker='<!-- bounded-extension -->'
    text=text.split(marker)[0].rstrip()+f'''\n\n{marker}
## 25–63 eV extension

Exactly **45 new batches / 4,500 histories** were run: three independent
100-history batches at 25, 40 and 63 eV for each ion. All 852 compatible
previous batches were reused. The resulting **195 direct points support
0.025–3000 keV per ion**. Systematic source roles remain labelled separately.

**This is numerical coverage, not validated detector or low-energy physics.**
New energy-closure residuals reach **{100*audit['maximum_abs_closure_fraction']:.2f}%**;
{audit['closure_warning_batches']} of 45 new batches exceed 1%. Only
{audit['new_points_meeting_relative_QF_SEM_2percent']} of 15 new points meet 2% relative QF SEM.
No renormalization or automatic statistical expansion was applied.

- [New direct points](tables/extension_points.csv), including channels/SEMs/closure flags.
- [Full point inventory](tables/point_inventory.csv), including the separate contaminated medium and control roles.
- [Changes from the previous response](tables/previous_response_comparison.csv).
- [Derivative sensitivity](tables/derivative_stability.csv): basis/penalty envelope, **not** a confidence band.
- [Comparison plot](plots/extension_comparison.png), including a fit excluding new low-energy anchors.

The [report](REPORT.md) explains the 21 eV setting, histogram-count anomaly,
boundary checks and MC integration. The original MC configuration is kept
as provenance; its primary-energy interval is not automatically rewritten.
'''
    readme.write_text(text)
    report=publication/'REPORT.md';text=report.read_text()
    text=text.replace('This release starts no new simulations.',
                      'The bounded extension added exactly 45 batches; no adaptive campaign was launched.')
    text=text.replace('The baseline export selects 852 nominal batches/85,200 histories, giving',
                      'The original baseline export selected 852 nominal batches/85,200 histories, giving')
    text=text.replace('outside 0.25–1000 keV/ion','outside 0.025–3000 keV/ion')
    text=text.replace('All 1,939 archives passed','All 1,939 original archives passed')
    text=text.replace("## Comparison with Flaminia's thesis and current CYGNO-MC",
                      "## Comparison with Flaminia's thesis and frozen CYGNO-MC reference")
    text=text.replace('coefficients exactly. Its H fit is unused: MC excludes protons from NR fields,',
                      'coefficients exactly. In the frozen reference its H fit is unused: protons are excluded from NR fields,')
    if 'The comparison uses the **16 October 2026**' not in text:
        text=text.replace("## Comparison with Flaminia's thesis and frozen CYGNO-MC reference",
            "## Comparison with Flaminia's thesis and frozen CYGNO-MC reference\n\nThe comparison uses the **16 October 2026** source reference (commit `1246b920`).\nThe MC checkout now has local edits; those edits are untouched and unvalidated here.")
    if '**The new 25–63 eV data fail' not in text:
        text=text.replace('## What was simulated',
            '**The new 25–63 eV data fail the 1% energy-closure criterion in all 45 batches; maximum residual 14.58%. They are nominal diagnostics, not qualified low-energy detector input.**\n\n## What was simulated')
    text=text.split(marker)[0].rstrip()
    rows='\n'.join(f"| {r['species']} | {r['selected_n_coefficients']} | {r['maximum_abs_QF_change_previous_interval']:.5f} | "
                  f"{r['maximum_abs_derivative_change_previous_interval']:.5f} | "
                  f"{r['maximum_abs_derivative_change_due_to_new_low_points']:.5f} | "
                  f"{r['weighted_residual_RMS_pull']:.2f} |" for r in diagnostics['species'])
    text+=f'''\n\n{marker}
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
However, {audit['closure_warning_batches']}/45 batches exceed the 1% energy-closure warning;
the maximum absolute residual is **{100*audit['maximum_abs_closure_fraction']:.2f}%**.
These excess-energy accounting flags remain visible, without renormalization.
Native histogram counts print 99.96–100.00 despite independently verified
100 endpoints, effective histories and completion-dialog counts. The new
auditor records that anomaly (tolerance 0.05 in these audited headers only);
it does not divide again by counts or correct the energy discrepancy.

Only {audit['new_points_meeting_relative_QF_SEM_2percent']}/15 new points meet 2% relative QF SEM.
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
{rows}

The fixed-14-coefficient trial missed the 25 eV H mean by
{abs(diagnostics['H_boundary_refinement']['fixed14_lower_endpoint_statistical_pull']):.2f} statistical SEM.
Joint cross-validation reduced that boundary residual to
{abs(diagnostics['H_boundary_refinement']['selected_lower_endpoint_statistical_pull']):.2f} SEM, without forcing the endpoint.
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
'''
    report.write_text(text.rstrip()+'\n')


def publish(extension, publication):
    """Build and qualify before replacing the existing published numerical data."""
    sys.dont_write_bytecode=True
    state=json.loads((extension/'campaign_state.json').read_text())
    assert state['status']=='simulations_complete_report_pending' and state['completed_batches']==state['expected_batches']
    audit=check_new_archives(extension,publication)
    reference_path=publication/'provenance/previous_response_models.json'
    if reference_path.exists():
        previous=json.loads(reference_path.read_text())
    else:
        previous=json.loads((publication/'frozen/response_models.json').read_text())
        previous['models']=[{k:v for k,v in m.items() if k!='bootstrap_coefficients'} for m in previous['models']]
        previous.update(source_commit='20c35bf2b187605108f280b07194ad31e469b1fc',
                        original_models_sha256=exporter.sha(publication/'frozen/response_models.json'),
                        purpose='Compact previous250eV–1MeV response reference; bootstrap draws remain in Git history')
    stage=extension/'analysis'
    if stage.exists():
        raise RuntimeError('Analysis staging directory exists; inspect it before retrying')
    stage.mkdir()
    frozen=stage/'frozen';frozen.mkdir()
    records=merge_snapshot(extension,publication,frozen/'snapshot')
    shutil.copy2(Path(exporter.__file__),frozen/'export_cygno_mc.py')
    shutil.copy2(study.ROOT/'scripts/validate_cygno_mc_export.py',frozen/'validate_cygno_mc_export.py')
    manifest=exporter.build(frozen/'snapshot',frozen)
    assert len(json.loads((frozen/'response_models.json').read_text())['models'])==5
    import subprocess
    subprocess.run([sys.executable,str(frozen/'validate_cygno_mc_export.py'),str(frozen)],check=True)
    diagnostic_tables=stage/'diagnostic_tables';diagnostic_tables.mkdir()
    diagnostics=response_diagnostics(frozen,previous,diagnostic_tables)
    points=exporter.aggregate(records,'cygno_hecf4_900mbar_20261015')
    new_points=[{k:v for k,v in p.items() if k not in ('batch_channels','batch_ids')}
                for p in points if p['energy_keV'] in (.025,.04,.063)]
    audit.update(points=new_points,new_points=len(new_points),new_points_meeting_relative_QF_SEM_2percent=sum(p['QF_sem']/p['QF']<=.02 for p in new_points),
                 original_campaign_status='finished_with_unresolved_validation',
                 extension_status='complete_with_low_energy_model_limitations',
                 combined_compatible_batches=len(records),combined_source_histories=sum(r['job']['n_ions'] for r in records),
                 combined_direct_points=len(points),valid_energy_keV=manifest['valid_energy_keV'],
                 controls_kept_separate=True,maximum_new_batches=45,adaptive_expansion=False)
    exporter.write_json(reference_path,previous)
    # Only curated numerical inputs/outputs replace the existing frozen export.
    shutil.rmtree(publication/'frozen/snapshot')
    shutil.copytree(frozen/'snapshot',publication/'frozen/snapshot')
    for path in frozen.iterdir():
        if path.is_file() and path.suffix not in ('.png','.pdf'):
            shutil.copy2(path,publication/'frozen'/path.name)
    for path in diagnostic_tables.iterdir():shutil.copy2(path,publication/'tables'/path.name)
    exporter.write_json(publication/'validation/extension_validation.json',audit)
    exporter.write_json(publication/'validation/derivative_stability.json',diagnostics)
    exporter.write_csv(publication/'tables/extension_points.csv',new_points)
    reproducer=load_module(publication/'reproduce.py','extension_publication_reproducer')
    reproducer.make_tables(publication/'frozen',publication/'tables')
    for path in diagnostic_tables.iterdir():
        assert path.read_bytes()==(publication/'tables'/path.name).read_bytes(),path.name
    refresh_documentation(publication,audit,diagnostics)
    subprocess.run([sys.executable,str(publication/'tools/make_recap.py')],check=True)
    source_record=json.loads((publication/'provenance/publication.json').read_text())
    source_record.update(extended_at_UTC=core.utc_now(),valid_energy_keV=manifest['valid_energy_keV'],
                         source_selection=dict(batches=len(records),direct_domain_points=len(points),ions=89700,smooth_rows_per_species=401),
                         extension_scripts={p.name:exporter.sha(p) for p in [Path(__file__),study.ROOT/'scripts/cygno_qf_extension.py']})
    exporter.write_json(publication/'provenance/publication.json',source_record)
    old_comparison=publication/'frozen/comparison_current_mc/provenance.json'
    record=json.loads(old_comparison.read_text())
    record['historical_reference_note']='Legacy source/formulas verified in pre-extension20c35bf; input numerical hashes refer to that previous export, not the updated response'
    exporter.write_json(old_comparison,record)
    qualify_publication(publication,extension)
    print(json.dumps(dict(extension_simulations='complete',valid_energy_keV=manifest['valid_energy_keV'],
                          new_points=len(new_points),archive_checks_passed=True,
                          note='Existing tables, plots and report refreshed; final publication replay/checks follow'),indent=2),flush=True)


def seal(publication):
    files=[p for p in sorted(publication.rglob('*')) if p.is_file() and p.name!='SHA256SUMS'
           and '__pycache__' not in p.parts and p.suffix!='.pyc']
    (publication/'SHA256SUMS').write_text(''.join(exporter.sha(p)+'  '+p.relative_to(publication).as_posix()+'\n' for p in files))


def qualify_publication(publication,extension):
    """Full replay from an independent copy; no SRIM or MC reads are needed."""
    source_path=publication/'provenance/publication.json'
    source=json.loads(source_path.read_text())
    source.update(energy_accounting_qualified=False,physical_charge_calibrated=False,
                  extension_execution_runner_sha256=json.loads((extension/'extension_plan.json').read_text())['runner_sha256'],
                  extension_source_sha256={p.name:exporter.sha(p) for p in [Path(__file__),study.ROOT/'scripts/cygno_qf_extension.py',
                      study.ROOT/'scripts/cygno_study.py',study.ROOT/'scripts/export_cygno_mc.py',study.ROOT/'scripts/validate_cygno_mc_export.py']})
    exporter.write_json(source_path,source)
    seal(publication)
    env=dict(study.os.environ,OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1',PYTHONDONTWRITEBYTECODE='1')
    log=extension/'logs/publication_replay.log'
    with tempfile.TemporaryDirectory(prefix='cygno-extended-publication-') as temp:
        checkout=Path(temp)/'dataset'
        shutil.copytree(publication,checkout,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        with log.open('w') as handle:
            subprocess.run([sys.executable,str(checkout/'reproduce.py'),'--out',str(Path(temp)/'replayed')],
                           check=True,env=env,stdout=handle,stderr=subprocess.STDOUT)
        result=json.loads((Path(temp)/'replayed/step_2.log').read_text())
        for name in ['recap.png','recap_h_o.png','extension_comparison.png']:
            assert exporter.sha(checkout/'plots'/name)==exporter.sha(Path(temp)/'replayed/plots'/name),name
    audit=json.loads((publication/'validation/extension_validation.json').read_text())
    checks=dict(checked_at_UTC=core.utc_now(),scope='Numerical/software reproduction only; all45 new batches fail the retained1percent energy-closure criterion',
                standalone_copy_replay_passed=True,numerical_replay=result,all_published_tables_byte_identical=True,
                all_recap_PNGs_byte_identical=True,full_numeric_bounds_keV=[.025,3000],
                source_batches=897,source_histories=89700,direct_points=195,new_batches=45,new_histories=4500,
                new_archive_member_hashes_and_independent_parser_checks_passed=True,
                energy_accounting_qualified=False,physical_charge_calibrated=False,
                closure_warning_new_batches=audit['closure_warning_batches'],maximum_abs_new_closure_fraction=audit['maximum_abs_closure_fraction'],
                derivative_stability_established=False,MC_files_written=False,new_simulations_launched_by_postprocessor=0,
                fresh_dependency_installation_tested=False,previous_publication_commit='20c35bf')
    exporter.write_json(publication/'validation/publication_checks.json',checks)
    seal(publication)
    subprocess.run([sys.executable,str(publication/'reproduce.py'),'--verify-only'],check=True,env=env)
    state=json.loads((extension/'campaign_state.json').read_text())
    state.update(status='complete_with_low_energy_model_limitations',postprocessed_at=core.utc_now(),
                 publication_directory=str(publication),new_batches=45,energy_accounting_qualified=False)
    core.atomic_write_json(extension/'campaign_state.json',state)
    core.atomic_write_json(extension/'campaign_completion.json',state)
