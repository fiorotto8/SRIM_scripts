#!/usr/bin/env python3
"""Reproduce the thesis cross-check and short recap without running SRIM/Geant4."""
from pathlib import Path
import argparse
import csv
import hashlib
import importlib.util
import json
import re
import textwrap

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.ticker import FixedLocator, FuncFormatter
import numpy as np
from scipy.optimize import least_squares

DATASET = Path(__file__).resolve().parents[1]
OUT = DATASET / 'plots'
EXPORT = DATASET / 'frozen'
PARAMS = {'H1': [.65, 1.82, .48], 'He4': [.117, 3.9, .44],
          'C12': [.0195, 14.7, .33], 'F19': [.0083, 27.4, .303]}
NAMES = {'H1': r'$^1$H', 'He4': r'$^4$He', 'C12': r'$^{12}$C',
         'O16': r'$^{16}$O', 'F19': r'$^{19}$F'}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def analytic(p, energy):
    k, a, b = p
    f = k * (energy + a * energy**b)
    q = f / (1 + f)
    return q, q + energy * k * (1 + a*b*energy**(b-1)) / (1+f)**2


def main():
    global OUT, EXPORT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,default=OUT)
    parser.add_argument('--export-dir',type=Path,default=EXPORT)
    args=parser.parse_args()
    OUT, EXPORT=args.out.resolve(),args.export_dir.resolve()
    OUT.mkdir(parents=True,exist_ok=True)
    spec = importlib.util.spec_from_file_location('frozen_export', EXPORT / 'export_cygno_mc.py')
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)
    config = json.loads((EXPORT / 'snapshot/geant4_config.json').read_text())
    reference=json.loads((DATASET/'provenance/thesis_crosscheck.json').read_text())
    assert PARAMS==reference['thesis']['parameters_k_a_b']
    assert config['provenance']['sha256']==reference['MC_source_hashes_verified']
    for species in ['He4','C12','F19']:
        p = [config['existing_QF']['coefficients'][species][name] for name in ['k','a','b']]
        assert p == PARAMS[species], 'Thesis Table4.1 and frozen MC reference differ'
    models = {m['species']: m for m in json.loads((EXPORT / 'response_models.json').read_text())['models']}
    rows = list(csv.DictReader((EXPORT / 'qf_source_direct.csv').open()))
    review = json.loads((DATASET / 'validation/final_review.json').read_text())
    fits = {}
    for species in ['He4','C12','F19']:
        selected = [r for r in rows if r['species'] == species and 1 <= float(r['energy_keV']) <= 500]
        energy, q, sem = np.array([[float(r[k]) for k in ['energy_keV','QF','QF_sem']] for r in selected]).T
        result = least_squares(lambda p: (analytic(p,energy)[0]-q)/sem, PARAMS[species],
                               bounds=([1e-6,0,0],[10,1000,1]),
                               ftol=1e-13, xtol=1e-13, gtol=1e-13, max_nfev=10000)
        assert result.success
        fits[species] = dict(parameters_k_a_b=result.x.tolist(), n_points=len(selected),
                             energy_range_fitted_keV=[float(min(energy)),float(max(energy))],
                             requested_fit_interval_keV=[1,500],
                             diagnostic_chi2_per_dof=float(sum(result.fun**2)/(len(q)-3)),
                             maximum_abs_residual_pull=float(max(abs(result.fun))),
                             interpretation='Same thesis form refitted to new points in1–500keV; model diagnostic only; no detector/physical validation')
    e = np.geomspace(.25,1000,1201)
    plt.rcParams.update({'font.size':11,'axes.labelsize':11,'axes.titlesize':16,
                         'pdf.fonttype':42,'axes.spines.top':False,'axes.spines.right':False})
    fig, axes = plt.subplots(2,3,figsize=(14.5,9.4),sharex=True,sharey='row')
    fig.subplots_adjust(left=.07,right=.985,top=.88,bottom=.20,hspace=.15,wspace=.16)
    for col,species in enumerate(['He4','C12','F19']):
        q,qs,d,ds = exporter.evaluate(models[species],e)
        oldq,oldd = analytic(PARAMS[species],e)
        matched = (e>=fits[species]['energy_range_fitted_keV'][0]) & (e<=fits[species]['energy_range_fitted_keV'][1])
        fq,fd = analytic(fits[species]['parameters_k_a_b'],e[matched])
        # Independent checks of all analytic curves and definitions.
        probe = np.geomspace(.251,999,121);h=probe*1e-5
        for parameters in [PARAMS[species],fits[species]['parameters_k_a_b']]:
            qp,_=analytic(parameters,probe+h);qm,_=analytic(parameters,probe-h)
            np.testing.assert_allclose(((probe+h)*qp-(probe-h)*qm)/(2*h),analytic(parameters,probe)[1],rtol=1e-8)
        fits[species]['max_abs_derivative_change_vs_spline_in_fit_interval'] = float(max(abs(fd-d[matched])))
        for row,new,unc,old,refit in [(0,q,qs,oldq,fq),(1,d,ds,oldd,fd)]:
            ax=axes[row,col]
            ax.axvspan(.25,1,color='#d5d5d5',alpha=.5,zorder=0)
            ax.axvspan(500,1000,color='#d5d5d5',alpha=.5,zorder=0)
            ax.axvline(1,color='#888888',lw=.8);ax.axvline(500,color='#888888',lw=.8)
            ax.plot(e,new,color='#0072B2',lw=2.25,zorder=3)
            ax.fill_between(e,new-unc,new+unc,color='#0072B2',alpha=.18,zorder=2)
            ax.plot(e,old,color='#D55E00',ls=(0,(5,3)),lw=2.1,zorder=4)
            ax.plot(e[matched],refit,color='#009E73',ls=(0,(1,2)),lw=2.0,zorder=5)
            ax.set_xscale('log');ax.set_xlim(.25,1000);ax.set_ylim(0,1.055)
            ax.xaxis.set_major_locator(FixedLocator([.25,1,10,100,1000]))
            ax.xaxis.set_major_formatter(FuncFormatter(lambda v,pos:f'{v:g}'))
            ax.grid(alpha=.20)
        raw=[r for r in rows if r['species']==species]
        axes[0,col].errorbar([float(r['energy_keV']) for r in raw],[float(r['QF']) for r in raw],
                             yerr=[float(r['QF_sem']) for r in raw],fmt='o',ms=3,color='#0072B2',
                             elinewidth=.8,capsize=1,zorder=6)
        axes[0,col].set_title(NAMES[species])
        axes[1,col].set_xlabel('Initial energy [keV per ion]')
    axes[0,0].set_ylabel('QF (primary + recoil electronic fraction)')
    axes[1,0].set_ylabel(r'$d[E\,QF(E)]/dE$')
    fig.suptitle('CYGNO quenching recap — thesis implementation vs completed TRIM scan',fontsize=17,fontweight='bold',y=.98)
    fig.text(.5,.925,'He/CF₄ 60/40 molar · new direct simulations: 900 mbar, 293 K · electronic-energy response',ha='center',fontsize=11)
    handles=[Line2D([],[],color='#0072B2',lw=2.3,marker='o',ms=4,label='New TRIM points + smooth fit'),
             Line2D([],[],color='#D55E00',lw=2.1,ls='--',label='Thesis Table4.1 / current MC'),
             Line2D([],[],color='#009E73',lw=2,ls=':',label='Thesis form refitted to new data (diagnostic)'),
             Patch(facecolor='#d5d5d5',alpha=.5,label='Outside thesis fitted interval: 1–500 keV')]
    fig.legend(handles=handles,loc='lower center',bbox_to_anchor=(.5,.082),ncol=2,frameon=False,fontsize=10)
    fig.text(.5,.045,'Blue bands: ±1 conditional statistical SEM; systematic/model uncertainty excluded. Green refit is not a validated replacement.',ha='center',fontsize=10)
    fig.text(.5,.015,'Only He/C/F have fitted QFs implemented in current MC. H/O use unity defaults; the thesis does provide an H fit, but no O fit.',ha='center',fontsize=10)
    fig.savefig(OUT/'recap.png',dpi=190)
    # Keep the existing He/C/F page readable and add a dedicated H/O page.
    hofig, hoaxes = plt.subplots(2,2,figsize=(11.7,9.1),sharex=True,sharey='row')
    hofig.subplots_adjust(left=.09,right=.98,top=.86,bottom=.22,hspace=.17,wspace=.15)
    manifest = json.loads((EXPORT / 'manifest.json').read_text())
    flagged_h = next(c['raw_energy_budget_flags'] for c in manifest['coverage'] if c['species']=='H1')
    flagged_energies = {p['energy_keV'] for p in flagged_h}
    for col,species in enumerate(['H1','O16']):
        q,qs,d,ds = exporter.evaluate(models[species],e)
        for row,new,unc in [(0,q,qs),(1,d,ds)]:
            ax=hoaxes[row,col]
            ax.plot(e,new,color='#0072B2',lw=2.25,zorder=3)
            ax.fill_between(e,new-unc,new+unc,color='#0072B2',alpha=.18,zorder=2)
            ax.axhline(1,color='#D55E00',ls=(0,(5,3)),lw=2.1,zorder=4)
            if species=='H1':
                legacy=analytic(PARAMS['H1'],e)[row]
                ax.plot(e,legacy,color='#7B3294',ls='-.',lw=1.9,zorder=4)
                ax.axvspan(.25,1,color='#d5d5d5',alpha=.5,zorder=0)
                ax.axvspan(500,1000,color='#d5d5d5',alpha=.5,zorder=0)
            ax.set_xscale('log');ax.set_xlim(.25,1000);ax.set_ylim(0,1.055)
            ax.xaxis.set_major_locator(FixedLocator([.25,1,10,100,1000]))
            ax.xaxis.set_major_formatter(FuncFormatter(lambda v,pos:f'{v:g}'))
            ax.grid(alpha=.20)
        raw=[r for r in rows if r['species']==species]
        hoaxes[0,col].errorbar([float(r['energy_keV']) for r in raw],[float(r['QF']) for r in raw],
                              yerr=[float(r['QF_sem']) for r in raw],fmt='o',ms=3,color='#0072B2',
                              elinewidth=.8,capsize=1,zorder=5)
        if species=='H1':
            flagged=[r for r in raw if float(r['energy_keV']) in flagged_energies]
            hoaxes[0,col].plot([float(r['energy_keV']) for r in flagged],[float(r['QF']) for r in flagged],
                               marker='s',ms=7,markerfacecolor='none',markeredgecolor='#B2182B',
                               ls='',zorder=6)
        hoaxes[0,col].set_title(NAMES[species]+('\nThesis fit exists; not active in MC' if species=='H1' else '\nNo thesis fit; MC uses unity'))
        hoaxes[1,col].set_xlabel('Initial energy [keV per ion]')
    hoaxes[0,0].set_ylabel('QF (primary + recoil electronic fraction)')
    hoaxes[1,0].set_ylabel(r'$d[E\,QF(E)]/dE$')
    hofig.suptitle('CYGNO recap — new hydrogen and oxygen recoil responses',fontsize=17,fontweight='bold',y=.98)
    hofig.text(.5,.93,'He/CF₄ 60/40 molar · 900 mbar, 293 K · full-cascade TRIM',ha='center',fontsize=11)
    hohandles=[handles[0],Line2D([],[],color='#D55E00',lw=2.1,ls='--',label='Current MC: unquenched default (unity)'),
               Line2D([],[],color='#7B3294',lw=1.9,ls='-.',label='Thesis H fit (not implemented in MC)'),
               Line2D([],[],color='#B2182B',marker='s',markerfacecolor='none',ls='',ms=6,label='H points flagged by energy accounting')]
    hofig.legend(handles=hohandles,loc='lower center',bbox_to_anchor=(.5,.085),ncol=2,frameon=False,fontsize=9.5)
    hofig.text(.5,.05,'Blue bands: ±1 conditional statistical SEM; model/calibration uncertainty excluded. H red squares retain ~1.2% closure warnings.',ha='center',fontsize=9)
    hofig.text(.5,.02,'Grey applies to the thesis H fit only: extrapolation outside 1–500 keV. Unity lines are code defaults, not physical QF predictions.',ha='center',fontsize=9)
    hofig.savefig(OUT/'recap_h_o.png',dpi=190)
    probe=np.geomspace(.251,999,121);h=probe*1e-5
    qp,_=analytic(PARAMS['H1'],probe+h);qm,_=analytic(PARAMS['H1'],probe-h)
    np.testing.assert_allclose(((probe+h)*qp-(probe-h)*qm)/(2*h),analytic(PARAMS['H1'],probe)[1],rtol=1e-8)
    comparisons=[]
    for species in ['H1','He4','C12','O16','F19']:
        r=next(r for r in rows if r['species']==species and float(r['energy_keV'])==1)
        current=float(analytic(PARAMS[species],np.array([1.]))[0][0]) if species in ('He4','C12','F19') else 1.
        comparisons.append(dict(species=species,energy_keV=1.,TRIM_direct_QF=float(r['QF']),
                                TRIM_QF_sem=float(r['QF_sem']),MC_value=current,
                                MC_is_unquenched_default=species in ('H1','O16')))
    discussion='''CYGNO QF recap and thesis cross-check — 16 October 2026

Verdict
The scan reproducibly predicts SRIM electronic energy, including excitation.
It is useful as a nominal benchmark, but it is not a calibrated free-charge QF.
We cannot claim that no improvement is possible, especially below 1 keV or
for the response derivative. No further broad scan is justified by statistics alone.

What the thesis establishes
Di Giambattista's thesis [1], pp.127–130, sums both primary and secondary-recoil
IONIZ bins and divides by initial energy (Eqs.4.9–4.11): the same definition
used here. Table4.1 exactly matches the He/C/F coefficients in current MC.
The thesis simulated100 ions at23 energies and fitted1–500 keV. It explicitly
excluded lower energies. Thus the orange curve below1 and above500 keV is
EXTRAPOLATION. Eq.4.17, pp.131–132, defines the same d[E QF(E)]/dE plotted here.
Quick mode is stated for preliminary target-width optimization; the thesis does
not explicitly identify the production QF mode. It also supplies an H fit that
current MC does not use; O is absent. MC H/O unity is therefore a code default.

What was done here
Full mode5 cascades cover H1/He4/C12/O16/F19 in baseline60/40 He/CF4 and a
second medium with1000ppm each H2O/O2. Source conditions:900mbar,293K,
rho=0.001389194959g/cm3; atomic fractions He3/13,C2/13,F8/13. Requested293.15K
reuse is approximate. The campaign completed1614 production/refinement and180
systematic batches; including pilots/controls:1939 batches,180011 histories.
All1939 archives passed integrity checks; independent electronic-energy
re-integration differs by less than0.00845%. Primary/recoil energy was counted
once, with separate conservation checks. The final baseline interface has142
direct points at0.25–1000keV. He/C/F each have12 points at0.25–10keV and
statistical SEM below2%. Range/straggling were also calculated; sampled paths
are not complete3D arc lengths. Impurity QF comparisons meet the nominal2%
criterion at110 eligible points, without a simultaneous-coverage guarantee.

What the discrepancy means
At1keV, direct new He/C/F QFs are0.4011/0.2534/0.2061 versus the old formula's
0.3644/0.2344/0.1908. This8–10% difference predates smoothing; at1keV it cannot
be dismissed as extrapolation. Below1keV, however, the thesis supplied no fit data.
Both methods include secondary electronic losses: missing recoils are not an
established explanation. Exact old density/binding inputs remain unavailable.
The thesis discusses compound/CAB corrections without a numerical correction;
ours uses1.0. It says
default displacement thresholds of tens of eV were retained for topology studies
(p.135), but supplies no element-specific values. Our uniform21eV is not proven
identical. Changing21→10eV shifts1keV He/C/F QF by+3.7/+9.4/+11.5%: this shows
sensitivity, not the cause of the old/new discrepancy. Low-energy H conservation
flags remain. Published old parameter errors lack covariance, so no legacy band
has been invented.

Improvement possible without new simulations
The green curves refit the thesis form to new data inside1–500keV. Their diagnostic
chi2/dof is He2.96,C2.90,F2.58 using new statistical errors only: a useful smoother,
but not an exact description. Comparing it with the blue spline exposes derivative
model dependence. The derivative wiggles are not directly simulated observables.
Retain both as a sensitivity check; do not claim the narrow blue band is total error.
Next priorities are matching original inputs and quantifying model/fit uncertainty,
then experimental charge calibration. More simulated ions alone cannot settle these.

[1] Flaminia Di Giambattista, Background study of the LIME prototype at underground
LNGS for the CYGNO experiment, PhD thesis,1 March2024, Sec.4.1.2.1.
Printed pp.127–135 correspond to PDF pages136–144; Table4.1 is PDF page139.
Frozen inputs and hashes, matched-fit details and checks are in plot_evidence.json.
'''
    # Keep readable spacing for units and explicit sentence boundaries in the report.
    replacements={'simulated100':'simulated 100','at23':'at 23','fitted1':'fitted 1','below1':'below 1',
                  'above500':'above 500','simulations\nFull mode5':'simulations\nFull mode 5',
                  'mode5':'mode 5','baseline60':'baseline 60','with1000':'with 1000',
                  'conditions:900':'conditions: 900','293K':'293 K','900mbar':'900 mbar',
                  'Requested293.15K':'Requested 293.15 K','completed1614':'completed 1614','and180':'and 180',
                  'including pilots/controls:1939':'including pilots/controls: 1939','All1939':'All 1939',
                  'than0.00845':'than 0.00845','has142':'has 142','at0.25':'at 0.25','have12':'have 12',
                  'below2':'below 2','at110':'at 110','nominal2':'nominal 2','At1keV':'At 1 keV',
                  'This8':'This 8','at1keV':'at 1 keV','uses1.0':'uses 1.0','uniform21eV':'uniform 21 eV',
                  'Changing21':'Changing 21','shifts1keV':'shifts 1 keV','by+3.7':'by +3.7',
                  'inside1':'inside 1','He2.96,C2.90,F2.58':'He 2.96, C 2.90, F 2.58',
                  'March2024':'March 2024','Sec.4.1.2.1':'Sec. 4.1.2.1',
                  'pp.127':'pp. 127','pp.131':'pp. 131','pages136':'pages 136','page139':'page 139',
                  'Table4.1':'Table 4.1','Eq.4.17':'Eq. 4.17','Eqs.4.9':'Eqs. 4.9'}
    for old,new in replacements.items():discussion=discussion.replace(old,new)
    discussion = re.sub(r'(?<=\d)(?=(?:keV|eV|ppm|g/cm3|mbar)\b)', ' ', discussion)
    discussion = discussion.replace('Below1 ', 'Below 1 ').replace('are0.', 'are 0.')
    discussion = discussion.replace('complete3D', 'complete 3D').replace('thesis,1 March', 'thesis, 1 March')
    discussion = discussion.replace('He3/13,C2/13,F8/13', 'He=3/13, C=2/13, F=8/13')
    discussion = discussion.replace('1939 batches,180011', '1939 batches, 180011')
    discussion = discussion.replace('900 mbar,293 K', '900 mbar, 293 K')
    discussion = discussion.replace('(p.135)', '(p. 135)')
    values={r['species']:r for r in comparisons}
    h_value,o_value=values['H1'],values['O16']
    added_ho=(f"Hydrogen and oxygen in this recap\n"
              f"At 1 keV the direct new QFs are H={h_value['TRIM_direct_QF']:.4f} +/- {h_value['TRIM_QF_sem']:.4f} "
              f"and O={o_value['TRIM_direct_QF']:.4f} +/- {o_value['TRIM_QF_sem']:.4f}. "
              "Both new QFs and response derivatives are plotted on the second page. "
              "Orange unity lines show current MC defaults; purple is the separate thesis H fit, "
              "which is not implemented. No thesis O curve exists. The flagged low-energy H "
              "points are marked rather than hidden.\n\n")
    discussion=discussion.replace('What the discrepancy means\n',added_ho+'What the discrepancy means\n')
    # Two readable plot pages, followed by the compact discussion.
    with PdfPages(OUT/'recap.pdf') as pdf:
        pdf.savefig(fig);plt.close(fig)
        pdf.savefig(hofig);plt.close(hofig)
        wrapped=[]
        for paragraph in discussion.strip().split('\n\n'):
            lines=paragraph.splitlines()
            if len(lines)>1 and len(lines[0])<55 and not lines[0].startswith('[1]'):
                wrapped.append(lines[0]);lines=lines[1:]
            wrapped.extend(textwrap.wrap(' '.join(lines),width=103,break_long_words=False))
            wrapped.append('')
        for start in range(0,len(wrapped),61):
            page=plt.figure(figsize=(8.27,11.69))
            page.text(.07,.955,'CYGNO QF — thesis cross-check',fontsize=14,fontweight='bold',va='top')
            page.text(.07,.915,'\n'.join(wrapped[start:start+61]),fontsize=9.3,
                      fontfamily='DejaVu Sans',linespacing=1.3,va='top')
            pdf.savefig(page);plt.close(page)
    evidence={'thesis':{'sha256':reference['thesis']['sha256'],
                        'QF_equations':{'printed_pages':[127,128,129],'pdf_pages':[136,137,138],'equations':['4.9','4.10','4.11']},
                        'fit_table':{'printed_page':130,'pdf_page':139,'fit_domain_keV':[1,500],'ions_per_energy':100,'n_energies':23},
                        'derivative':{'printed_pages':[131,132],'pdf_pages':[140,141],'equation':'4.17'},
                        'threshold_statement':{'printed_page':135,'pdf_page':144},
                        'parameters_k_a_b':PARAMS,'legacy_parameter_covariance_available':False},
              'MC_reference_source_hashes':config['provenance']['sha256'],
              'MC_verification_scope':'Frozen source reference verified during publication preparation; no live MC checkout needed for replay',
              'input_hashes':{p.name:sha(p) for p in [EXPORT/'qf_source_direct.csv',EXPORT/'response_models.json',DATASET/'validation/systematics.json',DATASET/'validation/final_review.json']},
              'same_form_matched_domain_diagnostic':fits,'direct_1keV_comparison':comparisons,
              'plotted_species':['H1','He4','C12','O16','F19'],
              'HO_page':{'H_thesis_fit_in_current_MC':False,'O_thesis_fit_available':False,
                         'current_MC_H_O_response':'unity defaults, not fitted physical predictions',
                         'flagged_H_points':flagged_h},
              'new_simulations_run':False,'MC_files_written':False,
              'reliability':'Reproducible nominal electronic-energy prediction; not best-possible, low-energy-cutoff-converged, derivative-qualified or charge-calibrated.',
              'final_review':review,'script_sha256':sha(Path(__file__))}
    evidence['thesis']['reference']='Flaminia Di Giambattista, Background study of the LIME prototype at underground LNGS for the CYGNO experiment, PhD thesis, 1 March2024'
    (OUT/'plot_evidence.json').write_text(json.dumps(evidence,indent=2,sort_keys=True)+'\n')
    print(json.dumps({'out':str(OUT),'same_form_refit_diagnostic':fits,'discussion_words':len(discussion.split()),'MC_files_written':False},indent=2))


if __name__=='__main__':
    main()
