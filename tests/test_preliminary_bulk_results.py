import json
from pathlib import Path
import tempfile
import unittest
from scripts import preliminary_bulk_results as g


class PreliminaryTests(unittest.TestCase):
    def setUp(self):self.points=json.loads((g.SOURCE/'points.json').read_text())

    def test_examples_convert_units_and_sem_without_relabelling_species(self):
        rows=g.examples(self.points)
        for row,family in zip(rows,('Ar','Kr','Xe')):
            point=g.select(self.points,family,family,10,80)
            self.assertEqual(row['recoil'],family)
            self.assertAlmostEqual(row['forward_displacement_um'],1000*point['projected_range_mm'])
            self.assertAlmostEqual(row['ionization_sem_percentage_points'],100*point['ionization_fraction_sem'])
        self.assertAlmostEqual(rows[0]['forward_displacement_um'],18.99415)
        self.assertAlmostEqual(rows[1]['ionization_percent'],19.638821166060364)

    def test_missing_or_duplicate_point_refused(self):
        with self.assertRaises(ValueError):g.select([], 'Ar','Ar',10,80)
        x=g.select(self.points,'Ar','Ar',10,80)
        with self.assertRaises(ValueError):g.select([x,x],'Ar','Ar',10,80)

    def test_incomplete_snapshot_not_presented_as_audited(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):g.render(self.points[:-1],{},Path(tmp))
            with self.assertRaises(ValueError):g.render(self.points,dict(complete_points=1638,precision_flags=0),Path(tmp))


if __name__=='__main__':unittest.main()
