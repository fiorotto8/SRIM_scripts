# Working on the Windows branch

The `windows` branch uses native Windows Python and the local SRIM-2013
executables. Run commands from the repository root in PowerShell. Ubuntu,
Docker, and PySRIM development stays on
[`main`](https://github.com/fiorotto8/SRIM_scripts/tree/main).

See the [README](README.md) for installation and the CSV scan commands.

## Local checks

With the virtual environment and SRIM installation configured as described in
the README, preview the minimal SRModule workload:

```powershell
.\.venv\Scripts\python.exe srim_gas_sweep.py `
  --gas-csv examples\GasDensities_minimal.csv `
  --ions C,F --points 3 --dry-run --no-plots
```

SRModule's `--dry-run` prints its plan without rewriting outputs. TRIM's
`--dry-run` creates job inputs and rewrites its selected output/work paths;
choose fresh paths for every trial. Do not share a SRIM installation between
simultaneous native calculations.

Run the standard-library tests and check the patch:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
git diff --check
```

The tests need `Data\ATOMDATA` from the installed SRIM tree. They look in this
repository first, then in the sibling `..\srim-2013` directory. Set
`$env:SRIM_ROOT = 'C:\SRIM-2013'` to select another installation. The optional
PySRIM adapter tests are skipped when its sibling checkout is absent; PySRIM
is not required for the native CSV workflow.

## Files and Git

The [ignore rules](.gitignore) explicitly allow maintained automation files.
SRIM binaries, local environments, logs, results, and worker files stay local.
Add an explicit allowlist entry when introducing a new maintained path, and
stage only reviewed files. Keep existing simulation data and raw outputs intact.

The [line-ending rules](.gitattributes) store normalized text in Git. Python,
shell, TOML, Markdown, and PowerShell files use LF; Windows batch and CSV files
use CRLF on checkout. Avoid unrelated changes to generated SRIM inputs and
outputs.
