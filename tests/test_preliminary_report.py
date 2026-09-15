import csv
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts import preliminary_report as report


class PreliminaryReportTests(unittest.TestCase):
    def test_missing_database_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            self.assertEqual(report.checkpoint(path), ([], []))
            self.assertEqual(list(path.iterdir()), [])

    def test_only_committed_complete_trim_is_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            connection = sqlite3.connect(path / "jobs.sqlite")
            connection.execute("CREATE TABLE jobs(job_id TEXT,stage TEXT,status TEXT,result_path TEXT)")
            connection.executemany("INSERT INTO jobs VALUES(?,?,?,?)", [
                ("one", "trim", "complete", "one.json"),
                ("two", "trim", "running", "two.json"),
                ("three", "srmodule", "complete", "three.json"),
            ])
            connection.commit()
            connection.close()
            row = dict(job_id="one", family="Ar", noble_percent=80, pressure_atm=1., ion="Ar", energy_kev=7.)
            (path / "one.json").write_text(json.dumps(row))
            rows, counts = report.checkpoint(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["source_run"], path.name)
            self.assertIn(("trim", "running", 1), counts)

    def test_csv_preserves_provenance_and_warning_commas(self):
        row = dict(source_run="old", job_id="test", warnings="one, two")
        parsed = list(csv.DictReader(io.StringIO(report.csv_text([row], report.FIELDS))))
        self.assertEqual(parsed[0]["source_run"], "old")
        self.assertEqual(parsed[0]["warnings"], "one, two")

    def test_table_escapes_output(self):
        self.assertNotIn("<script>", report.table([{"warnings": "<script>"}], ["warnings"]))


if __name__ == "__main__":
    unittest.main()
