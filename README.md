# SRIM scripts for Linux and PySRIM

Reusable SRIM-2013 automation for Ubuntu and other Linux distributions using
the [fiorotto8/PySRIM fork](https://github.com/fiorotto8/pysrim). Describe your
projectile, material, density, layer thickness and energies in a TOML file.
The same runner supports solid and gas targets.

## Branches

| Branch | Purpose |
| --- | --- |
| `main` | General Linux/PySRIM tools, installation and small examples |
| `windows` | Native Windows development and CSV workflow |
| `campaign/noble-cf4-deep-scan` | Ar/Kr/Xe–CF4 campaign, frozen plans, audits, stage launchers and analysis |

The campaign scripts and their configuration history are preserved on the
campaign branch. Generated runs stay local and are excluded from Git.
Use a separate checkout or worktree when a simulation is running; its Python
files and frozen inputs must stay in place until the stage finishes.

## Quick start with Docker

Keep the two source repositories beside each other. PySRIM is pinned to the
revision used by this workflow; source edits in that checkout are visible in
the container through a bind mount.

```bash
mkdir srim-workspace
cd srim-workspace
git clone https://github.com/fiorotto8/pysrim.git
git -C pysrim checkout 9b8e1b7e91fe65bf17e9366e1fd9b9406ad125d2
git clone --branch main https://github.com/fiorotto8/SRIM_scripts.git
cd SRIM_scripts
export SRIM_DOCKER_UID=$(id -u)
export SRIM_DOCKER_GID=$(id -g)
docker compose build
docker compose run --rm srim
```

The default command prints the plan for helium ions in silicon. It does not
start a simulation. The first container invocation prepares the checked SRIM
archive and Wine components in named volumes. Docker Engine and its Compose
plugin are the host requirements; Wine runs inside the container.

Generate real PySRIM input files in a private runtime, without launching Wine:

```bash
docker compose run --rm srim python srim_project.py write-input \
  --config examples/minimal_silicon.toml --output runs/silicon-inputs
```

When ready, explicitly run the small example:

```bash
docker compose run --rm srim python srim_project.py run \
  --config examples/minimal_silicon.toml --output runs/silicon-example
docker compose run --rm srim python srim_project.py status \
  --output runs/silicon-example
```

Each invocation needs a new output directory. Input-only output and executed
output use separate directories. Jobs run sequentially, with their configured
timeout, separate SRIM copies, private Wine prefixes and authenticated Xvfb
displays. The source SRIM installation is kept intact.

## Native Ubuntu installation

Use the sibling checkout layout above and Python 3.11 or newer. The bootstrap
installs the Ubuntu Wine/Xvfb dependencies, creates a local virtual environment,
installs PySRIM in editable mode, verifies the SRIM archive and prepares Wine:

```bash
bash scripts/bootstrap_ubuntu.sh --install-system-deps
```

It prints the `SRIM_ROOT` and `WINEPREFIX` exports for the chosen installation.
The default locations are `../srim-2013` and `../.wine-srim`. To choose others:

```bash
bash scripts/bootstrap_ubuntu.sh \
  --pysrim-root /path/to/pysrim \
  --srim-root /path/to/srim-2013 \
  --wine-prefix /path/to/initialized-wine-prefix
```

If Wine and SRIM are already prepared, install only the Python environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install -e ../pysrim -r requirements.txt
.venv/bin/python pysrim_runner.py --probe
```

The editable `-e ../pysrim` installation uses your checkout directly. PySRIM's
Python import name is `srim`. Keep the pinned revision for reproducibility.

Inspect, generate inputs, and run through the same CLI:

```bash
.venv/bin/python srim_project.py plan --config examples/minimal_silicon.toml
.venv/bin/python srim_project.py write-input \
  --config examples/minimal_silicon.toml --output runs/silicon-inputs
.venv/bin/python srim_project.py run \
  --config examples/minimal_silicon.toml \
  --srim-root ../srim-2013 --wine-prefix ../.wine-srim \
  --output runs/silicon-example
.venv/bin/python srim_project.py status --output runs/silicon-example
```

Other Linux distributions can use Docker, or supply equivalent Python, 32-bit
Wine, Wine server, Xvfb, xauth, xwininfo and Visual Basic runtime components.
The system package installation in the bootstrap is Ubuntu-specific.

## Start another project

Copy `examples/minimal_silicon.toml` to your own TOML file and edit:

- `[project]`: name and `energies_kev`;
- `[ion]`: projectile symbol and mass in atomic mass units;
- `[target]`: solid/gas phase, density in g/cm³, thickness in angstroms;
- `[[target.elements]]`: each element's atomic number, mass, stoichiometry
  and displacement/lattice/surface energies in eV;
- `[trim]`: ion count, calculation mode 1 or 2, random seed and timeout.

`examples/minimal_gas.toml` demonstrates a gas target. These are small teaching
inputs. Select physical parameters and enough histories for your own study.
The runner supports one homogeneous layer and one projectile per project;
its energy list can contain several points. TRIM.DAT and campaign orchestration
are maintained on the campaign branch.

Use a distinct output directory for each run. The output contains a project
summary and per-energy job specifications, generated inputs, raw SRIM output
and logs. Inspect the raw files and validate sampling, containment and energy
accounting for your application. A successful process exit alone does not
establish scientific convergence. SRIM ionization fractions require additional
detector modeling before interpretation as a measured quenching factor.

## Development

```bash
.venv/bin/python -m unittest discover -s tests -v
docker compose config --quiet
```

The tests exercise input generation, configuration validation, isolation,
timeouts and cleanup without transport simulations. Display tests use local
Xvfb. SRIM binaries, Wine prefixes, virtual environments and generated results
are excluded from Git and the Docker build context.
