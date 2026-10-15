"""Portable project checks: fixtures and plain Python children, never SRIM/Wine."""

import contextlib
import io
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import srim_project as project


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.config = project.ROOT / "examples" / "minimal_silicon.toml"
        self.value = project.load_project(self.config)

    def test_plan_is_read_only_and_arbitrary_solid_projectile_supported(self):
        with patch.object(project.Path, "mkdir", side_effect=AssertionError("plan wrote a directory")), \
             patch.object(project.subprocess, "Popen", side_effect=AssertionError("plan launched a process")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(project.main(["--config", str(self.config)]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "PLANNED")
        self.assertEqual(result["target"]["phase"], "solid")
        self.assertEqual(self.value["base_spec"]["ion_symbol"], "He")
        self.assertEqual(self.value["base_spec"]["elements"][0]["symbol"], "Si")

    def test_rejects_unsupported_modes_nan_duplicate_energies_and_boolean_ions(self):
        original = self.config.read_text()
        for change in (
            original.replace("calculation = 2", "calculation = 5"),
            original.replace("calculation = 2", "calculation = 3"),
            original.replace("[100.0]", "[100.0, 100.0]"),
            original.replace("[100.0]", "[nan]"),
            original.replace("ions = 10", "ions = true"),
            original.replace('phase = "solid"', 'phase = "liquid"'),
        ):
            with self.subTest(change=change[-100:]), tempfile.TemporaryDirectory() as temporary:
                config = Path(temporary) / "bad.toml"
                config.write_text(change)
                with self.assertRaises(ValueError):
                    project.load_project(config)

    def test_gas_composition_and_per_energy_seeds(self):
        value = project.load_project(project.ROOT / "examples" / "minimal_gas.toml")
        self.assertEqual(value["base_spec"]["phase"], 1)
        self.assertEqual({element["symbol"]: element["stoich"] for element in value["base_spec"]["elements"]}, {"C": 1, "H": 4})
        self.assertEqual([job["random_seed"] for job in project.planned_jobs(value)], [23456, 23457])

    @unittest.skipUnless(importlib.util.find_spec("srim"), "Install the sibling PySRIM checkout for input generation")
    def test_examples_generate_real_inputs_without_executing_srim(self):
        for name, phase in (("minimal_silicon", 0), ("minimal_gas", 1)):
            with self.subTest(example=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "source"
                source.mkdir()
                # Input generation needs an installed executable marker but never runs it.
                (source / "TRIM.exe").write_bytes(b"input-only fixture; not executable")
                config = project.ROOT / "examples" / (name + ".toml")
                value = project.load_project(config)
                output = root / "output"
                with patch.object(project, "private_display", side_effect=AssertionError("input generation launched Xvfb")), \
                     patch.object(project, "stop_private_wine", side_effect=AssertionError("input generation launched Wine")):
                    result = project.execute(value, config, source, output, write_only=True)
                self.assertEqual(result["status"], "INPUTS_WRITTEN")
                for job in result["jobs"]:
                    content = (output / job["job_id"] / "runtime" / "TRIM.IN").read_bytes()
                    marker = b"Target layer phases (0=Solid, 1=Gas)\r\n" + str(phase).encode() + b"\r\n"
                    self.assertIn(marker, content)
                self.assertFalse((source / "TRIM.IN").exists())

    def fixture(self, root):
        source = root / "source"
        source.mkdir()
        (source / "TRIM.exe").write_bytes(b"fixture executable, never launched")
        (source / "RANGE.txt").write_text("stale source output")
        (source / "SRIM Outputs").mkdir()
        (source / "SRIM Outputs" / "RANGE.TXT").write_text("second stale source output")
        prefix = root / "wine-original"
        (prefix / "drive_c").mkdir(parents=True)
        (prefix / "system.reg").write_text("fixture registry")
        return source, prefix

    def test_output_overlap_existing_and_symlink_refused_before_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, prefix = self.fixture(root)
            alias = root / "source-alias"
            alias.symlink_to(source, target_is_directory=True)
            for output in (source, source / "nested", alias / "nested", prefix / "nested", root):
                with self.subTest(output=output), patch.object(project.shutil, "copytree") as copytree:
                    with self.assertRaises((ValueError, FileExistsError)):
                        project.execute(self.value, self.config, source, output, write_only=False, wine_prefix=prefix)
                    copytree.assert_not_called()
            self.assertFalse((source / "nested").exists())
            self.assertFalse((prefix / "nested").exists())

    def test_write_input_clones_source_uses_adapter_and_retains_input_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _ = self.fixture(root)
            output = root / "new-output"

            def fake_adapter(command, runtime, environment, timeout, server):
                self.assertIn("--write-only", command)
                self.assertNotEqual(runtime, source)
                self.assertFalse((runtime / "RANGE.txt").exists())
                self.assertFalse((runtime / "SRIM Outputs" / "RANGE.TXT").exists())
                self.assertEqual(json.loads(Path(command[3]).read_text())["phase"], 0)
                (runtime / "TRIM.IN").write_text("fixture input")

            with patch.object(project, "run_child", side_effect=fake_adapter), \
                 patch.object(project, "private_display", side_effect=AssertionError("write-input opened display")), \
                 patch.object(project, "stop_private_wine", side_effect=AssertionError("write-input launched Wine")):
                result = project.execute(self.value, self.config, source, output, write_only=True)
            self.assertEqual(result["status"], "INPUTS_WRITTEN")
            self.assertEqual((source / "RANGE.txt").read_text(), "stale source output")
            self.assertFalse((source / "TRIM.IN").exists())
            self.assertEqual(json.loads((output / "summary.json").read_text())["status"], "INPUTS_WRITTEN")
            with self.assertRaises(FileExistsError):
                project.execute(self.value, self.config, source, output, write_only=True)

    def test_run_copies_own_prefix_and_cleans_it_on_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, prefix = self.fixture(root)
            output = root / "new-output"

            @contextlib.contextmanager
            def display(_runtime):
                yield {"DISPLAY": ":fixture", "XAUTHORITY": "fixture-auth"}, None, {}

            def fake_adapter(command, runtime, environment, timeout, server):
                copied = Path(environment["WINEPREFIX"])
                self.assertNotEqual(copied, prefix)
                self.assertTrue((copied / "system.reg").is_file())
                self.assertTrue(output in copied.parents)
                self.assertEqual(environment["DISPLAY"], ":fixture")
                (copied / "system.reg").write_text("job registry")
                raise TimeoutError("fixture timed out")

            with patch.object(project.shutil, "which", return_value="/fixture/tool"), \
                 patch.object(project, "private_display", side_effect=display), \
                 patch.object(project, "run_child", side_effect=fake_adapter), \
                 patch.object(project, "stop_private_wine") as cleanup:
                with self.assertRaisesRegex(TimeoutError, "fixture timed out"):
                    project.execute(self.value, self.config, source, output, write_only=False, wine_prefix=prefix)
            self.assertEqual((prefix / "system.reg").read_text(), "fixture registry")
            self.assertEqual(cleanup.call_count, 1)
            owned, environment = cleanup.call_args.args
            self.assertEqual(str(owned), environment["WINEPREFIX"])
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["status"], "FAILED")
            self.assertEqual(summary["jobs"][0]["status"], "FAILED")

    def test_missing_fresh_outputs_fail_even_when_adapter_returns_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, prefix = self.fixture(root)
            output = root / "output"

            def adapter(command, runtime, *_args):
                (runtime / "TRIM.IN").write_text("fixture input")

            with patch.object(project.shutil, "which", return_value="/fixture/tool"), \
                 patch.object(project, "private_display", return_value=contextlib.nullcontext(({}, None, {}))), \
                 patch.object(project, "run_child", side_effect=adapter), \
                 patch.object(project, "stop_private_wine"):
                with self.assertRaisesRegex(RuntimeError, "required outputs"):
                    project.execute(self.value, self.config, source, output, write_only=False, wine_prefix=prefix)
            self.assertEqual(json.loads((output / "summary.json").read_text())["status"], "FAILED")

    def test_private_wine_cleanup_rejects_source_prefix_mismatch(self):
        with patch.object(project.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "owned private prefix"):
                project.stop_private_wine(Path("/owned/prefix"), {"WINEPREFIX": "/source/prefix"})
            run.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "Linux process groups")
    def test_timeout_kills_child_process_group_without_running_srim(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pid_file = root / "pid"
            code = "import os,pathlib,time;pathlib.Path('pid').write_text(str(os.getpid()));time.sleep(30)"
            with self.assertRaisesRegex(TimeoutError, "exceeded"):
                project.run_child([sys.executable, "-c", code], root, os.environ, .3)
            self.assertTrue(pid_file.exists())
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 0)

    @unittest.skipUnless(os.name == "posix", "Linux process groups")
    def test_interrupt_kills_owned_child_process_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            captured = []
            actual_popen = project.subprocess.Popen

            def start_child(*args, **kwargs):
                child = actual_popen(*args, **kwargs)
                captured.append(child)
                return child

            with patch.object(project.subprocess, "Popen", side_effect=start_child), \
                 patch.object(project.time, "monotonic", side_effect=[0, InterruptedError("fixture signal")]):
                with self.assertRaisesRegex(InterruptedError, "fixture signal"):
                    project.run_child([sys.executable, "-c", "import time; time.sleep(30)"], root, os.environ, 10)
            self.assertIsNotNone(captured[0].poll())
            with self.assertRaises(ProcessLookupError):
                os.kill(captured[0].pid, 0)


if __name__ == "__main__":
    unittest.main()
