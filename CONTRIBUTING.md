# Working on the Windows branch

Use system **64-bit Python 3.15**, Git and the native SRIM installation. Run
single-line commands from the repository root in PowerShell. Requirements and
normal workflows are in the [README](README.md). NumPy, Matplotlib and the
existing SciPy export dependency are installed directly; no virtual environment
or WSL bridge is needed.

## Local checks

```powershell
py -3.15 srim_gas_sweep.py --gas-csv examples\GasDensities_minimal.csv --ions C,F --points 3 --dry-run --no-plots
py -3.15 -m unittest discover -s tests -v
git diff --check
```

Unit tests must not launch SRIM. They use temporary directories and the
installed `Data\ATOMDATA` (repository, sibling `..\srim-2013`, or `SRIM_ROOT`).
The two historical PySRIM adapter tests may skip when its optional sibling
checkout is absent. They do not gate native operation.

Both CSV sweeps' `--dry-run` previews are read-only. Real runs require a fresh
output file and empty work directory. Integration checks use a small ion count and private runtime
copies, verify native completion and output parsing, and compare installed
executable/reference-data hashes before and after. Keep a logged-in interactive
desktop for TRIM dialogs. Never terminate a process based on its name alone.

Preserve compositions, isotopes, seeds, grids, energy accounting, uncertainty
estimators and validation thresholds. Native input files retain SRIM's expected
CRLF format. Persist relative references with `/` separators. Explicitly close
SQLite connections before temporary directory cleanup on Windows. Generated
interchange JSON, JSONL, C++ and checksum text use UTF-8/LF for portable hashes;
CSV writers use explicit newline handling.

## Files and Git

This checkout shares a directory with SRIM. Never replace or remove installed
executables/reference files, existing run results or frozen publication data.
Use a new run/export destination; retain checkpoints and raw archives.

The [ignore rules](.gitignore) allowlist maintained automation files while
excluding installed binaries and generated outputs. Add an explicit allowlist
entry for a new maintained path and stage only reviewed files. Do not change
branches, commit or push as part of local verification without authorization.

The [line-ending rules](.gitattributes) normalize maintained source text;
Windows batch and CSV files use CRLF on checkout. Published data and checksum
manifests retain their exact bytes. Keep historical frozen replay tools intact
when improving the maintained scripts.
