import base64
import unittest

from octoprint_rme_compatibility.protocol import (
    chunk_command,
    classify_workflow,
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
        self.assertEqual(parse_line(
            "RME_CHANGE seq=8 revision=3 domain=theme key=colors origin=local"
        ), {
            "record": "change", "seq": 8, "revision": 3,
            "domain": "theme", "key": "colors", "origin": "local",
        })
        self.assertEqual(parse_line(
            "RME_STATS distance_x_m=12.5 distance_y_m=8 distance_z_m=0.4 "
            "distance_total_m=20.9 extruded_m=456 print_time_s=900 "
            "current_print_time_s=120 jobs_started=7"
        ), {
            "record": "stats", "distance_x_m": 12.5, "distance_y_m": 8,
            "distance_z_m": 0.4, "distance_total_m": 20.9,
            "extruded_m": 456, "print_time_s": 900,
            "current_print_time_s": 120, "jobs_started": 7,
        })
        self.assertEqual(parse_line(
            "RME_STATS_OPERATIONS tool_picks=12 mmu_changes=8 "
            "filtering_time_s=300 wastebin_pellets=19"
        ), {
            "record": "stats", "tool_picks": 12, "mmu_changes": 8,
            "filtering_time_s": 300, "wastebin_pellets": 19,
        })
        self.assertEqual(parse_line(
            "RME_STATS_FAILURES crash_x=1 crash_y=2 power_panics=3 "
            "mmu_load_since_reset=4 mmu_load_total=5 "
            "mmu_general_since_reset=6 mmu_general_total=7"
        ), {
            "record": "stats", "crash_x": 1, "crash_y": 2,
            "power_panics": 3, "mmu_load_since_reset": 4,
            "mmu_load_total": 5, "mmu_general_since_reset": 6,
            "mmu_general_total": 7,
        })
        self.assertEqual(parse_line(
            "RME_STATS_MEMORY heap_free=123456 heap_total=524288"
        ), {
            "record": "stats", "heap_free": 123456, "heap_total": 524288,
        })
        self.assertEqual(parse_line(
            "RME_SESSION active=1 legacy=0 preferred_baud=1000000 "
            "fallback_baud=250000,230400,115200"
        ), {
            "record": "session", "active": 1, "legacy": 0,
            "preferred_baud": 1000000,
            "fallback_baud": "250000,230400,115200",
        })
        self.assertEqual(parse_line(
            "RME_SESSION lease=1 printer_state=idle legacy=0"
        ), {
            "record": "session", "lease": 1,
            "printer_state": "idle", "legacy": 0,
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
        self.assertEqual(parse_line(
            'loaded_filament T0 S"PLA-00D" O"Custom" H"#808080" M"Prusa / Prusament"'
        ), {
            "record": "loaded_filament", "tool": 0, "material": "PLA-00D",
            "color_name": "Custom", "color": "#808080",
            "vendor": "Prusa / Prusament",
        })
        self.assertEqual(parse_line(
            "RME_MANUFACTURER builtin=1 slot=0 name=Prusa%20%2F%20Prusament"
        ), {
            "record": "manufacturer", "builtin": 1, "slot": 0,
            "name": "Prusa / Prusament",
        })

    def test_parses_binary_safe_file_service_records_with_spaces(self):
        entry = parse_line("RME_FILE_ENTRY name=My print.bgcode type=file size=1234")
        self.assertEqual(entry, {
            "record": "file_entry", "name": "My print.bgcode",
            "type": "file", "size": 1234,
        })
        data = parse_line(
            "RME_FILE_DATA path=My print.bgcode offset=0 length=3 eof=1 data=YWJj"
        )
        self.assertEqual(data["path"], "My print.bgcode")
        self.assertTrue(data["eof"])
        self.assertEqual(data["data"], "YWJj")
        self.assertEqual(parse_line("RME_FILE_LIST_END")["record"], "file_list_end")
        self.assertEqual(
            parse_line("echo:RME_ERROR workflow=file code=invalid_path")["record"],
            "file_error",
        )
        self.assertEqual(
            parse_line("RME_FILE_BINARY_ABORTED"),
            {"record": "file_binary_aborted"},
        )
        self.assertEqual(parse_line(
            "RME_FILE_BINARY_ABORTED offset=62464 resumable=1 transport=frame"
        ), {
            "record": "file_binary_aborted", "offset": 62464,
            "resumable": 1, "transport": "frame",
        })
        self.assertEqual(parse_line(
            "echo:RME_ERROR workflow=file code=disk_write_failed "
            "offset=62464 resumable=1"
        )["offset"], 62464)
        self.assertEqual(parse_line(
            "RME_FILE_BINARY_READ_READY path=part.bgcode offset=0 length=1024"
        )["record"], "file_binary_read_ready")
        self.assertEqual(parse_line(
            "RME_FILE_BINARY_READ_COMPLETE next=1024 eof=0"
        ), {
            "record": "file_binary_read_complete", "next": 1024, "eof": 0,
        })
        self.assertEqual(parse_line(
            "RME_FIRMWARE_RESTART reconnect=1"
        ), {"record": "firmware_restart", "reconnect": 1})
        self.assertEqual(parse_line(
            "RME_FIRMWARE candidate=1 armed=0 state=ready path=FWUPD.RME "
            "size=3921020 sha256=" + "a" * 64
        ), {
            "record": "firmware_status", "candidate": 1, "armed": 0,
            "state": "ready", "path": "FWUPD.RME", "size": 3921020,
            "sha256": "a" * 64,
        })
        self.assertEqual(parse_line(
            "RME_FIRMWARE_UNSTAGED candidate=0 armed=0"
        ), {"record": "firmware_unstaged", "candidate": 0, "armed": 0})
        self.assertEqual(parse_line(
            "RME_FILE_BINARY_SUSPENDED offset=27648 resumable=1 "
            "reason=inactivity_timeout"
        ), {
            "record": "file_binary_suspended", "offset": 27648,
            "resumable": 1, "reason": "inactivity_timeout",
        })
        self.assertEqual(parse_line(
            "RME_FILE_SUSPENDED offset=3072 resumable=1 "
            "reason=inactivity_timeout"
        ), {
            "record": "file_suspended", "offset": 3072,
            "resumable": 1, "reason": "inactivity_timeout",
        })
        self.assertEqual(parse_line(
            "echo:RME_ERROR workflow=firmware code=transfer_busy"
        )["record"], "firmware_error")
        self.assertEqual(parse_line(
            "RME_LIGHT_STATE state=deep_idle screen=20 chamber=20 status=20"
        ), {
            "record": "light_state", "state": "deep_idle",
            "screen": 20, "chamber": 20, "status": 20,
        })
        self.assertEqual(parse_line(
            "RME_LIGHT_POLICY activity_timeout_s=120 event_timeout_s=300 "
            "off_timeout_s=120 door_holds_active=1 post_print_hold=1 "
            "status_finished_hold_s=300"
        )["record"], "light_policy")
        self.assertEqual(parse_line(
            "RME_LIGHT_LIVE state=idle screen=20 chamber=20 print_screen=60 "
            "print_chamber=100 print_status=100"
        )["record"], "light_live")

    def test_terminal_workflow_state_dismisses_remote_prompt(self):
        self.assertTrue(workflow_is_terminal({"state": "closed"}))
        self.assertTrue(workflow_is_terminal({"state": "completed"}))
        self.assertTrue(workflow_is_terminal({"state": "canceled"}))
        self.assertTrue(workflow_is_terminal({"state": "skipped"}))
        self.assertTrue(workflow_is_terminal({
            "workflow": "chamber_vent", "state": "open", "progress": 100,
        }))
        self.assertFalse(workflow_is_terminal({
            "workflow": "chamber_vent", "state": "open", "progress": 50,
        }))
        self.assertFalse(workflow_is_terminal({"state": "waiting"}))

    def test_refines_generic_firmware_workflows_without_overriding_named_ones(self):
        self.assertEqual("chamber_vent", classify_workflow({
            "workflow": "printer", "message": "Opening chamber vents",
        }))
        self.assertEqual("filtration", classify_workflow({
            "workflow": "printer", "message": "Post-print filtration active",
        }))
        self.assertEqual("filament_load", classify_workflow({
            "workflow": "printer", "message": "Loading filament",
        }))
        self.assertEqual("mmu", classify_workflow({
            "workflow": "mmu", "message": "MMU loading filament",
        }))


    def test_builds_binary_safe_chunk(self):
        payload = b"\x00 firmware bytes \xff"
        command = chunk_command(48, payload)
        self.assertTrue(command.startswith("M998 _ P1 O48 D"))
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
