import base64
import unittest

from octoprint_rme_compatibility.protocol import (
    chunk_command,
    dialog_response_command,
    parse_line,
    toolmap_commands,
    workflow_is_terminal,
)


class ProtocolTests(unittest.TestCase):
    def test_parses_machine_and_event_records(self):
        self.assertEqual(parse_line("RME_MACHINE hotends=2 logical_tools=5 single_nozzle=0"), {
        "record": "machine",
        "hotends": 2,
        "logical_tools": 5,
        "single_nozzle": 0,
        })
        self.assertEqual(parse_line(
        'RME_EVENT seq=7 type=progress workflow=probing state=active progress=50 message="Probing point 4"'
        ), {
        "record": "event",
        "seq": 7,
        "type": "progress",
        "workflow": "probing",
        "state": "active",
        "progress": 50,
        "message": "Probing point 4",
        })


    def test_parses_prompt_toolmap_and_upload_records(self):
        self.assertEqual(parse_line("RME_PROMPT Retry,Unload,Abort")["actions"], [
        "Retry",
        "Unload",
        "Abort",
        ])
        self.assertEqual(parse_line("RME_TOOLMAP 1 L0=2 L1=-1"), {
        "record": "toolmap",
        "enabled": True,
        "mapping": {0: 2, 1: -1},
        })
        self.assertEqual(parse_line("FW_UPLOAD OFFSET 96"), {"record": "upload_offset", "offset": 96})
        self.assertEqual(parse_line("Error:FW_UPLOAD HASH"), {"record": "upload_error", "message": "HASH"})
        self.assertEqual(parse_line("Error: FW_UPLOAD WRITE"), {"record": "upload_error", "message": "WRITE"})
        self.assertEqual(parse_line(
            'loaded_filament T2 S"PLA-00A" O"Orange" H"#ff8000"'
        ), {
            "record": "loaded_filament", "tool": 2, "material": "PLA-00A",
            "color_name": "Orange", "color": "#ff8000",
        })

    def test_terminal_workflow_state_dismisses_remote_prompt(self):
        self.assertTrue(workflow_is_terminal({"state": "closed"}))
        self.assertTrue(workflow_is_terminal({"state": "completed"}))
        self.assertFalse(workflow_is_terminal({"state": "waiting"}))


    def test_builds_binary_safe_chunk(self):
        payload = b"\x00 firmware bytes \xff"
        command = chunk_command(48, payload)
        self.assertTrue(command.startswith("M998 P1 O48 D"))
        self.assertEqual(base64.b64decode(command.split("D", 1)[1]), payload)


    def test_validates_named_dialog_and_bijective_toolmap(self):
        self.assertEqual(dialog_response_command("MMU_disable"), '@RME DIALOG RESPOND A"MMU_disable"')
        with self.assertRaises(ValueError):
            dialog_response_command('bad"action')
        self.assertEqual(toolmap_commands({0: 1, 1: 0}), [
        "@RME TOOLMAP SET logical=0 physical=1",
        "@RME TOOLMAP SET logical=1 physical=0",
        "@RME TOOLMAP ENABLE value=1",
        "@RME TOOLMAP QUERY",
        ])
        with self.assertRaises(ValueError):
            toolmap_commands({0: 1, 1: 1})
