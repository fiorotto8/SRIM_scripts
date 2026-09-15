import io
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest

from scripts import timing_pilot


class TimingPilotTests(unittest.TestCase):
    def test_missing_checkpoint_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(timing_pilot.completed_rows(root), ([], []))
            self.assertEqual(list(root.iterdir()), [])

    def test_measured_timings_and_memory_are_exported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection = sqlite3.connect(root / "jobs.sqlite")
            connection.execute("CREATE TABLE jobs(stage TEXT,status TEXT,result_path TEXT,started_at TEXT,finished_at TEXT,attempts INTEGER)")
            connection.execute("INSERT INTO jobs VALUES('trim','complete','result.json','2026-09-15T00:00:00+00:00','2026-09-15T00:00:10+00:00',2)")
            connection.commit()
            connection.close()
            (root / "result.json").write_text(json.dumps(dict(energy_kev=10, family="Ar", noble_percent=60,
                                                             ion="Ar", n_ions=5, raw_archive="raw.tar.gz")))
            data = json.dumps(dict(cpu_seconds=7, peak_rss_bytes_observed=32*1024**2)).encode()
            with tarfile.open(root / "raw.tar.gz", "w:gz") as archive:
                info = tarfile.TarInfo(".trim-progress.json")
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            rows, counts = timing_pilot.completed_rows(root)
            self.assertEqual(rows[0]["wall_seconds_last_attempt"], 10)
            self.assertEqual(rows[0]["wall_seconds_per_ion"], 2)
            self.assertEqual(rows[0]["trim_cpu_seconds_observed"], 7)
            self.assertEqual(rows[0]["trim_peak_rss_mib_observed"], 32)
            self.assertEqual(rows[0]["attempts"], 2)
