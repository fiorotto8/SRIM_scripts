"""Portable references must not retroactively identify historical batch code."""

import json
from pathlib import Path
import tempfile
import unittest

from scripts.cygno_study_report import _provenance_references


class ReportProvenanceTests(unittest.TestCase):
    def test_current_snapshot_does_not_replace_unknown_historical_batch_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            revision = root / "provenance" / "source_revisions" / "revision-a"
            revision.mkdir(parents=True)
            manifest_contents = {"cygno_study.py": "private-manifest-content-not-exported"}
            (revision / "manifest.json").write_text(json.dumps(manifest_contents), encoding="utf-8")
            (root / "provenance" / "current_source_revision.json").write_text(
                json.dumps({"revision": "revision-a", "manifest": manifest_contents}), encoding="utf-8")
            (root / "batches").mkdir()
            (root / "batches" / "legacy.json").write_text(json.dumps({
                "job": {"job_id": "old-job"}, "raw_archive": "raw/pilot/old-job.tar.gz"}), encoding="utf-8")
            (root / "batches" / "versioned.json").write_text(json.dumps({
                "job": {"job_id": "new-job"}, "source_revision": "revision-a",
                "raw_archive": "raw/production/new-job.tar.gz"}), encoding="utf-8")
            stopping = root / "raw" / "srmodule" / "baseline_H"
            stopping.mkdir(parents=True)
            (stopping / "manifest.json").write_text("{}", encoding="utf-8")
            problems = []
            result = _provenance_references(root, problems)
            self.assertEqual(problems, [])
            self.assertEqual(result["current_source_revision"], "../provenance/current_source_revision.json")
            self.assertEqual(result["source_revision_manifests"], [{
                "revision": "revision-a", "path": "../provenance/source_revisions/revision-a/manifest.json"}])
            self.assertEqual(result["srmodule_manifests"], ["../raw/srmodule/baseline_H/manifest.json"])
            batches = {row["job_id"]: row for row in result["raw_batch_manifests"]}
            self.assertIsNone(batches["old-job"]["source_revision"])
            self.assertIsNone(batches["old-job"]["source_revision_manifest"])
            self.assertEqual(batches["old-job"]["source_revision_status"], "unknown")
            self.assertEqual(batches["old-job"]["raw_manifest"], {
                "archive": "../raw/pilot/old-job.tar.gz", "member": "manifest.json"})
            self.assertEqual(batches["new-job"]["source_revision"], "revision-a")
            self.assertEqual(batches["new-job"]["source_revision_manifest"],
                             "../provenance/source_revisions/revision-a/manifest.json")
            self.assertEqual(result["unknown_source_revision_batches"], 1)
            self.assertNotIn("private-manifest-content-not-exported", json.dumps(result))

    def test_absent_provenance_stays_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            result = _provenance_references(Path(directory), [])
        self.assertIsNone(result["current_source_revision"])
        self.assertEqual(result["source_revision_manifests"], [])
        self.assertEqual(result["raw_batch_manifests"], [])
        self.assertEqual(result["srmodule_manifests"], [])


if __name__ == "__main__":
    unittest.main()
