import os
import tempfile
import unittest

from octoprint_rme_compatibility.preview import read_preview


class PreviewTests(unittest.TestCase):
    def test_first_layer_and_metadata(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".gcode", delete=False) as f:
            f.write("; max_z_height: 48\nG90\nM83\n;LAYER_CHANGE\n@Object boat id:33 copy 0\n"
                    "G1 X10 Y20\nG1 X11 Y21 E.1\n@Objectstop boat\n"
                    "G1 X30 Y30 E1\n;LAYER_CHANGE\n@Object boat\nG1 X50 E1\n"
                    "; estimated printing time (normal mode) = 1h 41m 9s\n"
                    "; printable_area = 0x0,248x0,248x205,0x205\n")
        try:
            result = read_preview(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual(6069, result["estimated_seconds"])
        self.assertEqual(48, result["height"])
        self.assertEqual(1, len(result["objects"]))
        self.assertEqual([[10, 20, 11, 21]], result["objects"][0]["segments"])
        self.assertEqual([248, 205], result["bed"][2])
