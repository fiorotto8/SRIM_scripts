#!/usr/bin/env python3
"""Make readable comparison plots from a verified, frozen CYGNO-MC handoff.

Plots source formulas and the completed full-cascade TRIM fit. This does not
run Geant4 or SRIM. The sealed handoff and its ZIP are left unchanged.
"""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.ticker import FixedLocator, FuncFormatter, MaxNLocator
import numpy as np

ORDER = ['H1', 'He4', 'C12', 'O16', 'F19']
NAMES = {'H1': r'$^{1}$H', 'He4': r'$^{4}$He', 'C12': r'$^{12}$C',
         'O16': r'$^{16}$O', 'F19': r'$^{19}$F'}
BLUE, ORANGE = '#0072B2', '#D55E00'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--export-dir', type=Path, required=True)
    parser.add_argument('--mc-root', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    export, mc, out = (p.resolve() for p in [args.export_dir, args.mc_root, args.out])
    if out.exists():
        raise ValueError('Use a new output directory to preserve earlier figures')
    if export == out or export in out.parents or out in export.parents:
        raise ValueError('Keep new plots outside the sealed handoff')
    reference = json.loads((export / 'comparison_current_mc/provenance.json').read_text())
    for name, digest in reference['verified_MC_source_sha256'].items():
        assert sha(mc / name) == digest, 'MC source changed: ' + name
    for name, digest in reference['input_sha256'].items():
        assert sha(export / name) == digest, 'Frozen input changed: ' + name
    exporter = module('frozen_exporter', export / 'export_cygno_mc.py')
    comparison = module('verified_comparison', export / 'comparison_current_mc/compare.py')
    models = {m['species']: m for m in json.loads((export / 'response_models.json').read_text())['models']}
    raw = list(csv.DictReader((export / 'qf_source_direct.csv').open()))
    parameters = reference['parameters_extracted_from_live_source']
    energy = np.geomspace(.25, 1000, 1201)
    curves, rows = {}, []
    for species in ORDER:
        model = models[species]
        q, qs, d, ds = exporter.evaluate(model, energy)
        cq, cd = comparison.old_response(parameters, species, energy)
        # Keep the coefficient covariance when deriving the ordinary QF slope.
        slope_basis = exporter.basis(model['knots_log_energy'], np.log(energy), 1) / energy[:, None]
        slope = slope_basis @ model['coefficients']
        slope_sem = np.sqrt(np.maximum(0., np.einsum('...i,ij,...j->...', slope_basis,
                                                  model['coefficient_covariance'], slope_basis)))
        current_slope = (cd - cq) / energy
        curves[species] = {'qf': (q, qs, cq), 'response': (d, ds, cd),
                           'slope': (slope, slope_sem, current_slope)}
        assert np.all(q >= 0) and np.all(q <= 1 + 1e-12) and np.all(d >= 0)
        # Verify new and existing derivatives independently before plotting.
        probe = np.geomspace(.251, 999, 121)
        h = probe * 1e-5
        for evaluator in [lambda e: exporter.evaluate(model, e)[::2],
                          lambda e: comparison.old_response(parameters, species, e)]:
            qp, _ = evaluator(probe + h)
            qm, _ = evaluator(probe - h)
            _, direct = evaluator(probe)
            np.testing.assert_allclose(((probe+h)*qp-(probe-h)*qm)/(2*h), direct,
                                       rtol=1e-7, atol=1e-9)
        for i, e in enumerate(energy):
            rows.append(dict(species=species, energy_keV=e,
                             QF_TRIM=q[i], QF_TRIM_sem=qs[i], QF_CYGNO_MC=cq[i],
                             QF_difference_percentage_points=100*(q[i]-cq[i]),
                             dE_QF_dE_TRIM=d[i], dE_QF_dE_TRIM_sem=ds[i], dE_QF_dE_CYGNO_MC=cd[i],
                             response_difference_percentage_points=100*(d[i]-cd[i]),
                             dQF_dE_TRIM_per_keV=slope[i], dQF_dE_TRIM_sem_per_keV=slope_sem[i],
                             dQF_dE_CYGNO_MC_per_keV=current_slope[i],
                             current_MC_behavior='unquenched_default' if species in ('H1','O16') else 'analytic_formula'))
    out.mkdir(parents=True)
    plt.rcParams.update({'font.size': 11, 'axes.titlesize': 15, 'axes.labelsize': 11,
                         'xtick.labelsize': 10, 'ytick.labelsize': 10,
                         'axes.spines.top': False, 'axes.spines.right': False,
                         'pdf.fonttype': 42, 'svg.fonttype': 'none'})
    handles = [Line2D([], [], color=BLUE, lw=2.4, label='This study: full-cascade TRIM fit'),
               Line2D([], [], color=ORANGE, lw=2.4, ls='--', label='Current CYGNO-MC source function'),
               Line2D([], [], color=BLUE, marker='o', ls='', ms=4, label='Direct TRIM QF points (293 K)'),
               Patch(facecolor=BLUE, alpha=.18, label='Fit uncertainty: ±1 statistical SEM')]
    labels = {'qf': 'QF', 'response': r'$d[E\,QF(E)]/dE$', 'slope': r'$dQF/dE$ [keV$^{-1}$]'}

    def draw(fig, position, species, kind):
        nested = position.subgridspec(2, 1, height_ratios=[3.2, 1.15], hspace=.06)
        top = fig.add_subplot(nested[0])
        residual = fig.add_subplot(nested[1], sharex=top)
        new, sem, old = curves[species][kind]
        top.plot(energy, new, color=BLUE, lw=2.4, zorder=3)
        top.fill_between(energy, new-sem, new+sem, color=BLUE, alpha=.18, zorder=2)
        top.plot(energy, old, color=ORANGE, lw=2.1, ls=(0,(5,3)), zorder=4)
        if kind == 'qf':
            points = [p for p in raw if p['species'] == species]
            top.errorbar([float(p['energy_keV']) for p in points], [float(p['QF']) for p in points],
                         yerr=[float(p['QF_sem']) for p in points], fmt='o', ms=3,
                         color=BLUE, elinewidth=.8, capsize=1.5, alpha=.8, zorder=5)
        factor = 1 if kind == 'slope' else 100
        difference = factor * (new-old)
        residual.plot(energy, difference, color=BLUE, lw=1.7)
        residual.fill_between(energy, difference-factor*sem, difference+factor*sem,
                              color=BLUE, alpha=.18)
        residual.axhline(0, color='#555555', lw=.9)
        limit = max(float(np.max(np.abs(difference)+factor*sem))*1.13, .05 if kind != 'slope' else .001)
        residual.set_ylim(-limit, limit)
        residual.yaxis.set_major_locator(MaxNLocator(3))
        if kind != 'slope':
            top.set_ylim(0, 1.065)  # Retains all slight d(EQ)/dE values above unity.
        else:
            top.set_yscale('symlog', linthresh=1e-4)
        top.set_ylabel(labels[kind])
        residual.set_ylabel('Δ [p.p.]' if kind != 'slope' else 'Δ [keV⁻¹]')
        residual.set_xlabel('Initial kinetic energy [keV per ion]')
        title = NAMES[species]
        if species in ('H1', 'O16'):
            title += '\nMC: unquenched default'
        top.set_title(title, pad=8)
        plt.setp(top.get_xticklabels(), visible=False)
        for ax in (top,residual):
            ax.set_xscale('log'); ax.set_xlim(.25,1000)
            ax.xaxis.set_major_locator(FixedLocator([.25,1,10,100,1000]))
            ax.xaxis.set_major_formatter(FuncFormatter(lambda x,p: f'{x:g}'))
            ax.grid(axis='both', which='major', alpha=.2, lw=.7)
        return top, residual

    titles = {'qf': 'Quenching factor: completed TRIM study vs current CYGNO-MC',
              'response': 'Response derivative: completed TRIM study vs current CYGNO-MC',
              'slope': 'Ordinary QF slope (supplement): dQF/dE, distinct from the MC response derivative'}
    filenames = {'qf':'qf_comparison_clear','response':'response_derivative_comparison_clear',
                 'slope':'qf_slope_comparison_supplement'}
    for kind in ['qf','response','slope']:
        fig = plt.figure(figsize=(15,10.5), layout='constrained')
        grid = fig.add_gridspec(2,3,hspace=.14,wspace=.10)
        for index,species in enumerate(ORDER):
            draw(fig,grid[index//3,index%3],species,kind)
        key=fig.add_subplot(grid[1,2]);key.axis('off')
        key.legend(handles=handles if kind=='qf' else [handles[0],handles[1],handles[3]],
                   loc='upper left',frameon=False,fontsize=11,bbox_to_anchor=(0,1))
        key.text(.03,.57,'Difference: this study − CYGNO-MC\n(p.p. = percentage points)\n\n'
                 'He/CF₄ = 60/40 molar, 900 mbar\n293 K TRIM → 293.15 K MC target\n\n'
                 'H/O: unquenched MC defaults.\nBands: statistical SEM only.\n'
                 'Model and calibration errors excluded.\n\n'
                 'Source functions; no new Geant4 events.',va='top',fontsize=10.5,
                 linespacing=1.3,transform=key.transAxes)
        fig.suptitle(titles[kind],fontsize=19,fontweight='bold')
        for ext in ['png','pdf','svg']:
            fig.savefig(out/(filenames[kind]+'.'+ext),dpi=190)
        plt.close(fig)
    with PdfPages(out/'comparison_by_species.pdf') as pdf:
        for species in ORDER:
            fig=plt.figure(figsize=(11.7,8.3))
            grid=fig.add_gridspec(1,2,left=.09,right=.98,top=.86,bottom=.19,wspace=.26)
            draw(fig,grid[0,0],species,'qf');draw(fig,grid[0,1],species,'response')
            fig.suptitle(NAMES[species]+' — current CYGNO-MC vs completed TRIM study',fontsize=20,fontweight='bold')
            fig.legend(handles=handles,loc='lower center',bbox_to_anchor=(.5,.015),ncols=2,frameon=False,fontsize=10)
            pdf.savefig(fig);plt.close(fig)
    with (out/'comparison_values.csv').open('w',newline='') as handle:
        writer=csv.DictWriter(handle,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    shutil.copy2(Path(__file__),out/'plot_cygno_mc_comparison.py')
    provenance={'created_UTC':datetime.now(timezone.utc).isoformat(), 'input_export':str(export),
                'MC_HEAD':reference['CYGNO_MC_HEAD'], 'MC_source_hashes_verified':reference['verified_MC_source_sha256'],
                'source_models_sha256':sha(export/'response_models.json'),
                'source_comparison_provenance_sha256':sha(export/'comparison_current_mc/provenance.json'),
                'live_formula_cpp_cases_from_verified_reference':reference['cpp_verification']['compiled_source_cases'],
                'finite_difference_checks_this_plot_run':{'energies_per_species':121,'species':5,'functions':['TRIM d(EQ)/dE','MC d(EQ)/dE']},
                'bands':'±1 conditional statistical SEM; legacy fit errors unavailable',
                'scope':'Analytic current-source functions vs derived TRIM fit; no new Geant4 events',
                'sealed_handoff_modified':False,'script_sha256':sha(Path(__file__))}
    (out/'provenance.json').write_text(json.dumps(provenance,indent=2)+'\n')
    (out/'README.md').write_text('''# Clear QF and derivative comparison

Blue: completed full-cascade TRIM fit, including primary and secondary recoil
electronic energy. Orange dashed: current CYGNO-MC source functions. Points:
direct TRIM QF data at 293 K. Blue bands: ±1 conditional statistical SEM.

- `qf_comparison_clear.pdf/png/svg`: five separate QF panels with differences.
- `response_derivative_comparison_clear.pdf/png/svg`: d[E QF(E)]/dE, the derivative used by MC.
- `comparison_by_species.pdf`: five readable pages, one isotope per page, QF and response derivative together.
- `qf_slope_comparison_supplement.pdf/png/svg`: ordinary dQF/dE, supplied separately to distinguish the two derivatives.
- `comparison_values.csv`: all plotted values and statistical uncertainties.

Difference panels are this study minus CYGNO-MC. For QF and the response
derivative, values are multiplied by100 (percentage points, not relative %).
H/O orange unity lines reflect unquenched code defaults, not fitted QFs.
MC actually uses QF at E<=1keV and the response derivative above1keV.
These figures compare the continuous source functions separately.

The TRIM response is transferred from293K to requested293.15K. Bands exclude
physical/model, threshold, smoothing, transfer and detector calibration error.
This is a source-function comparison, not a new Geant4 event simulation or
measured free-charge calibration. The existing sealed handoff/ZIP is unchanged.

Reproduce with NumPy, SciPy and Matplotlib:

```
python3 plot_cygno_mc_comparison.py --export-dir ../srim_results_20261016_final --mc-root ../../.. --out ../comparison_replay
```
''')
    (out/'SHA256SUMS').write_text(''.join(f'{sha(p)}  {p.name}\n' for p in sorted(out.iterdir()) if p.is_file() and p.name!='SHA256SUMS'))
    print(json.dumps({'output':str(out),'values':len(rows),'verified':True},indent=2))


if __name__=='__main__':
    main()
