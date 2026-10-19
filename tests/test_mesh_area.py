import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("mesh_area", Path(__file__).resolve().parents[1] / "octoprint_rme_compatibility/mesh_area.py")
mesh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mesh)


class MeshAreaTests(unittest.TestCase):
    def analyze(self, code, **kwargs):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".gcode") as f:
            f.write(code)
            f.flush()
            return mesh.analyze(f.name, 250, 205.5, **kwargs)

    def test_absolute_and_relative_extrusion(self):
        for mode, extrusion in (("M83", "E1"), ("M82\nG92 E10", "E11")):
            self.assertEqual(self.analyze("G90\n%s\nG0 X10 Y20\nG1 X30 Y40 %s\nG0 X240 Y200\n" % (mode, extrusion)),
                             "@RME MESH SET x=9.00 y=19.00 width=22.00 height=22.00")

    def test_relative_xyz_and_extrusion_override(self):
        self.assertEqual(self.analyze("M83\nG90\nG0 X10 Y20\nG91\nG1 X20 Y20 E1\n"),
                         "@RME MESH SET x=9.00 y=19.00 width=22.00 height=22.00")

    def test_stationary_purge_does_not_define_bounds(self):
        self.assertEqual(self.analyze("M83\nG0 X-5 Y-5\nG1 E20\nG0 X10 Y20\nG1 X30 Y40 E1\n"),
                         "@RME MESH SET x=9.00 y=19.00 width=22.00 height=22.00")

    def test_all_tools_and_wipe_tower_included(self):
        result = self.analyze("M83\nG0 X10 Y20\nG1 X30 Y40 E1\nT4\nG0 X100 Y100\nG1 X120 Y120 E1\n")
        self.assertEqual(result, "@RME MESH SET x=9.00 y=19.00 width=112.00 height=102.00")

    def test_arc_envelope(self):
        self.assertEqual(self.analyze("M83\nG0 X50 Y50\nG2 X70 Y50 I10 J0 E1\n"),
                         "@RME MESH SET x=49.00 y=39.00 width=22.00 height=22.00")

    def test_unsafe_or_unsupported_falls_back(self):
        for code in ("G20", "G92 X3", "G54", "G18", "G90.1", "M218 T1 X2", "@unknown",
                     "G92 XNaN", "G92", "G92 E1 E2", "G92 EInf",
                     "G1 XNaN Y2 E1", "G1 X260 Y20 E1", "G2 X30 Y20 R10 E1"):
            self.assertIsNone(self.analyze("M83\nG0 X10 Y20\n" + code + "\nG1 X30 Y40 E1\n"), code)

    def test_inert_unknown_position_and_timeout(self):
        self.assertIsNone(self.analyze("; comments\nM77\n"))
        self.assertIsNone(self.analyze("M83\nG1 X10 Y20 E1\n"))
        self.assertIsNone(self.analyze("M83\nG0 X10 Y20\nG1 X30 Y40 E1\n", timeout=-1))

    def test_probe_recognition(self):
        self.assertTrue(mesh.is_adaptive_probe("G29 P1 ; probe"))
        self.assertFalse(mesh.is_adaptive_probe("G29 P10"))
        self.assertFalse(mesh.is_adaptive_probe("G29 P1 X10 Y10 W20 H20"))
        self.assertFalse(mesh.is_adaptive_probe("G29 P1 C"))


if __name__ == "__main__":
    unittest.main()
