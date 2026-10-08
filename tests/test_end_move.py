import tempfile
import unittest
import types
import logging

from test_toolmap_gate import RmeCompatibilityPlugin
from octoprint_rme_compatibility.end_move import analyze


PREFIX = "G90\nM83\nG1 Z92\nG1 X126 Y102 E0.3\nG1 E-.4\nG1 Z102 F720\nM104 T3 S0\nM104 T7 S0\nM140 S0\nM141 S0\nM870 C\nM107\nM106 P6 S0\nP0 S1\nG1 Y0 F10200\nG1 X242 Y205 F10200\nM400\n"
MOVE = "G1 Z242 F720"
TAIL = "\nM400\nM572 S0\nM221 S100\nM84 X Y E\nM151 W0 D0 T500\nM77\nM73 P100 R0\n; end\n"


class EndMoveTests(unittest.TestCase):
    def analyze(self, code, **kwargs):
        with tempfile.NamedTemporaryFile(mode="wb") as f:
            f.write(code.encode("utf-8"))
            f.flush()
            return analyze(f.name, **kwargs)

    def test_posted_end_sequence_matches_exact_byte_offset(self):
        code = PREFIX + MOVE + TAIL
        self.assertEqual(self.analyze(code), (len((PREFIX + MOVE + "\n").encode()), MOVE))

    def test_comments_unicode_and_crlf_offsets(self):
        prefix = ("; pièce\n\n" + PREFIX).replace("\n", "\r\n")
        self.assertEqual(self.analyze(prefix + MOVE + TAIL.replace("\n", "\r\n")),
                         (len((prefix + MOVE + "\r\n").encode()), MOVE))

    def test_small_relative_unparked_and_heaters_on_do_not_trigger(self):
        for prefix, move in ((PREFIX, "G1 Z103 F720"), (PREFIX + "G91\n", MOVE),
                             (PREFIX.replace("P0 S1\n", ""), MOVE),
                             (PREFIX.replace("M140 S0", "M140 S55"), MOVE),
                             (PREFIX.replace("M104 T3 S0\nM104 T7 S0", "M104 S220"), MOVE)):
            self.assertIsNone(self.analyze(prefix + move + TAIL))

    def test_later_extrusion_or_motion_invalidates_candidate(self):
        for later in ("G1 X100 E1", "G2 X100 I5 E1", "G1 X0", "G28", "M600"):
            self.assertIsNone(self.analyze(PREFIX + MOVE + "\n" + later + TAIL))

    def test_explicit_marker_unknown_geometry_and_time_limit_skip(self):
        for prefix in ("@RME SNAPSHOT\n", "G20\n", "G54\n", "G1 ZNaN\n", "{placeholder}\n"):
            self.assertIsNone(self.analyze(prefix + PREFIX + MOVE + TAIL))
        self.assertIsNone(self.analyze(PREFIX + MOVE + TAIL, timeout=-1))

    def test_queue_inserts_barrier_once_only_at_exact_file_position(self):
        plugin = RmeCompatibilityPlugin()
        plugin._state["supported"] = True
        plugin._completion_move = (456, MOVE)
        hook = plugin.gcode_queuing_hook
        self.assertIsNone(hook(None, "queuing", MOVE, None, "G1", tags={"source:file", "filepos:123"}))
        self.assertIsNone(hook(None, "queuing", MOVE, None, "G1", tags={"source:terminal", "filepos:456"}))
        self.assertIsNone(hook(None, "queuing", "G1 Z240", None, "G1", tags={"source:file", "filepos:456"}))
        self.assertEqual(hook(None, "queuing", MOVE, None, "G1", tags={"source:file", "filepos:456"}),
                         [("M400", None, {"source:file", "filepos:456"}),
                          ("@RME SNAPSHOT", None, {"source:file", "filepos:456"}),
                          (MOVE, None, {"source:file", "filepos:456"})])
        self.assertIsNone(hook(None, "queuing", MOVE, None, "G1", tags={"source:file", "filepos:456"}))

    def test_preflight_scans_local_job_and_clears_previous_target(self):
        plugin = RmeCompatibilityPlugin()
        plugin._state["supported"] = True
        plugin._logger = logging.getLogger("end-move-test")
        plugin._plugin_manager = types.SimpleNamespace(plugins={"octopod": types.SimpleNamespace(enabled=True)})
        plugin._printer = types.SimpleNamespace(get_current_data=lambda: {
            "job": {"file": {"origin": "local", "path": "part.gcode"}}})
        with tempfile.NamedTemporaryFile(mode="w") as f:
            f.write(PREFIX + MOVE + TAIL)
            f.flush()
            plugin._file_manager = types.SimpleNamespace(path_on_disk=lambda *a: f.name)
            plugin._prepare_completion_move()
        self.assertEqual(plugin._completion_move[1], MOVE)
        plugin._state["supported"] = False
        plugin._prepare_completion_move()
        self.assertIsNone(plugin._completion_move)
