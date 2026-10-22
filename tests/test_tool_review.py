"""Job-scoped LCD/web mapping and failover confirmation regressions."""
import copy
import logging
import types
import unittest
from unittest.mock import Mock
from test_toolmap_gate import RmeCompatibilityPlugin, _Settings, _Printer
from octoprint_rme_compatibility.protocol import parse_line


class ToolReview(unittest.TestCase):
    def plugin(self):
        p = RmeCompatibilityPlugin()
        p._printer = _Printer()
        p._settings = _Settings()
        p._logger = logging.getLogger("review-test")
        p._plugin_manager = types.SimpleNamespace(plugins={})
        p._persist_and_publish = Mock()
        p._schedule_publish = Mock()
        p._defer = lambda callback, *args: callback(*args)
        p._state.update(supported=True, connected=True)
        p._state["machine"]["logical_tools"] = 3
        p._state["loaded_filaments"] = [
            {"tool": 0, "material": "ASA"},
            {"tool": 1, "material": "PLA"},
            {"tool": 2, "material": "ASA"},
        ]
        p._state["prompt"] = dict(kind="toolmap", review_id=123, native_state="editing",
            requirements=[{"logical": 0, "material": "ASA"}],
            count=3, mapping={0: 0, 1: 1, 2: 2}, enabled=True,
            spooljoin_review=True)
        p._toolmap_hold_active = True
        return p

    def submit(self, p, **overrides):
        record = dict(record="review", id=123, state="submitted", revision=19,
                      mapping="0,255,255", enabled=1, joins="0:2")
        record.update(overrides)
        p._handle_record(record)

    def test_lcd_confirmation_validates_fallback_and_waits_for_commit(self):
        p = self.plugin()
        self.submit(p)
        self.assertTrue(p._toolmap_hold_active)
        self.assertEqual(p._printer.command_batches[-1], ["@RME REVIEW COMMIT id=123 revision=19"])
        p._handle_record(dict(record="review", id=123, state="complete", revision=19))
        self.assertFalse(p._toolmap_hold_active)
        self.assertIsNone(p._state["prompt"])
        self.assertEqual(p._printer.holds, [False])
        self.assertEqual(p._printer.forced_commands, [])

    def test_wrong_material_shared_cycle_or_unattached_fallback_stays_held(self):
        for joins in ("0:1", "0:2,2:0", "1:2", "0:2,0:1", "8:2"):
            with self.subTest(joins=joins):
                p = self.plugin()
                self.submit(p, joins=joins)
                self.assertTrue(p._toolmap_hold_active)
                self.assertEqual(p._printer.command_batches[-1], ["@RME REVIEW REJECT id=123"])
                self.assertNotIn("commit_revision", p._state["prompt"])

    def test_stale_job_or_unvalidated_completion_cannot_release(self):
        p = self.plugin()
        self.submit(p, id=122)
        p._handle_record(dict(record="review", id=123, state="complete", revision=19))
        self.assertTrue(p._toolmap_hold_active)
        self.assertFalse(p._printer.command_batches)
        self.submit(p)
        p._handle_record(dict(record="review", id=123, state="complete", revision=18))
        self.assertTrue(p._toolmap_hold_active)

    def test_lcd_remap_normalizes_unused_rows_without_duplicate_primary(self):
        p = self.plugin()
        self.submit(p, mapping="2,255,255", joins="2:0")
        self.assertEqual(p._state["prompt"]["mapping"], {0: 2, 1: 1, 2: 0})
        self.assertEqual(p._printer.command_batches[-1], ["@RME REVIEW COMMIT id=123 revision=19"])

    def test_nested_filament_prompt_restores_original_review(self):
        p = self.plugin()
        p._toolmap_review_prompt = p._state["prompt"]
        p._state["dialog"] = dict(workflow="filament", state="waiting")
        p._handle_record(dict(record="prompt", actions=["Retry", "Abort"]))
        self.assertEqual(p._state["prompt"]["kind"], "firmware")
        p._handle_record(dict(record="prompt", actions=[]))
        self.assertIs(p._state["prompt"], p._toolmap_review_prompt)
        self.assertEqual(p._state["prompt"]["review_id"], 123)

    def test_native_review_return_dismisses_nested_dialog_without_losing_gate(self):
        p = self.plugin()
        p._toolmap_review_prompt = p._state["prompt"]
        p._state["prompt"] = dict(kind="firmware", actions=["Retry", "Abort"])
        p._state["dialog"] = dict(workflow="tool_mapping", state="waiting")
        p._handle_record(dict(record="prompt", actions=["Abort", "Print"]))
        self.assertIs(p._state["prompt"], p._toolmap_review_prompt)
        self.submit(p)
        p._handle_record(dict(record="review", id=123, state="complete", revision=19))
        self.assertIsNone(p._state["prompt"])

    def test_next_authoritative_start_resets_completion_latch(self):
        p = self.plugin()
        p._toolmap_review_completed = True
        p._printer_transfer_active = lambda: False
        for name in ("_prepare_toolmap_prompt", "_prepare_mesh_area", "_prepare_chamber_job", "_prepare_completion_move"):
            setattr(p, name, Mock())
        p.gcode_script_hook(None, "gcode", "beforePrintStarted")
        self.assertFalse(p._toolmap_review_completed)
        p._prepare_toolmap_prompt.assert_called_once_with(record_preflight=True)

    def test_web_confirmation_uses_the_same_commit_gate(self):
        p = self.plugin()
        p._apply_toolmap({0: 0, 1: 1, 2: 2}, True, release_hold=True,
                         fallbacks={"0": [2]})
        self.assertTrue(p._toolmap_hold_active)
        self.assertEqual(p._printer.command_batches[-1], ["@RME REVIEW SUBMIT id=123"])
        self.submit(p)
        p._handle_record(dict(record="review", id=123, state="complete", revision=19))
        self.assertFalse(p._toolmap_hold_active)

    def test_web_cannot_mutate_mapping_before_native_review_opens(self):
        p = self.plugin()
        p._state["prompt"]["native_state"] = "pending"
        with self.assertRaises(ValueError):
            p._apply_toolmap({0: 0, 1: 1, 2: 2}, True, release_hold=True, fallbacks={"0": []})
        self.assertFalse(p._printer.command_batches)
        self.assertTrue(p._toolmap_hold_active)

    def test_background_prompts_never_replace_the_review(self):
        p = self.plugin()
        original = copy.deepcopy(p._state["prompt"])
        for actions in (["Ok"], [], ["Abort", "Print"]):
            p._handle_record(dict(record="prompt", actions=actions))
            self.assertEqual(p._state["prompt"], original)

    def test_delayed_print_started_cannot_reopen_completed_review(self):
        p = self.plugin()
        p._release_toolmap_hold()
        p._handle_print_started({"name": "part.gcode"})
        self.assertIsNone(p._state["prompt"])
        self.assertEqual(p._printer.holds, [False])
        self.assertIn("@RME REVIEW CANCEL", str(p._printer.forced_commands))

    def test_cancel_on_lcd_cancels_job_not_resume(self):
        p = self.plugin()
        p._handle_record(dict(record="review", id=123, state="cancelled"))
        self.assertEqual(len(p._printer.cancel_calls), 1)
        self.assertTrue(p._toolmap_hold_active)

    def test_manifest_has_bounded_material_and_job_id(self):
        p = self.plugin()
        p._state["session"]["tool_review"] = 1
        p._start_native_tool_review()
        commands = p._printer.command_batches[-1]
        identity = p._state["prompt"]["review_id"]
        self.assertTrue(0 < identity < 2**31)
        self.assertEqual(commands[0], "@RME REVIEW BEGIN id=%d mapping=0,1,2 enabled=1" % identity)
        self.assertEqual(commands[-1], "@RME REVIEW SHOW id=%d" % identity)
        self.assertFalse(any("SPOOLJOIN RESET" in command for command in commands))
        self.assertTrue(p._state["prompt"]["timer_paused"])
        for invalid in ("ASA\nM112", "long_material", ""):
            p = self.plugin()
            p._state["session"]["tool_review"] = 1
            p._state["prompt"]["requirements"][0]["material"] = invalid
            p._start_native_tool_review()
            self.assertFalse(p._printer.command_batches)

    def test_blocked_begin_retry_keeps_same_id_and_is_nondestructive(self):
        p = self.plugin()
        p._state["session"]["tool_review"] = 1
        p._handle_record(dict(record="review", id=0, state="idle"))
        commands = p._printer.command_batches[-1]
        self.assertTrue(commands[0].startswith("@RME REVIEW BEGIN id=123 "))
        self.assertFalse(any(command.startswith(("@RME TOOLMAP ", "@RME SPOOLJOIN ")) for command in commands))

    def test_protocol_snapshot_parse(self):
        self.assertEqual(parse_line("RME_REVIEW id=123 state=submitted revision=4000000000 enabled=1 mapping=0,255,255 joins=0:2")["revision"], 4000000000)

    def test_stale_reply_burst_cannot_flood_begin_or_retry_after_cancel(self):
        p = self.plugin()
        p._state["session"]["tool_review"] = 1
        for _ in range(30):
            p._handle_record(dict(record="review", id=122, state="complete"))
        self.assertEqual(len(p._printer.command_batches), 1)
        p._release_toolmap_hold()
        batches_after_cancel = len(p._printer.command_batches)
        for _ in range(30):
            p._handle_record(dict(record="review", id=122, state="complete"))
        p._start_native_tool_review()
        self.assertEqual(len(p._printer.command_batches), batches_after_cancel)

    def test_old_query_cannot_retry_a_begin_still_in_fifo(self):
        p = self.plugin()
        p._state["session"]["tool_review"] = 1
        p._start_native_tool_review()
        p._toolmap_review_last_begin = (123, 0)
        p._handle_record(dict(record="review", id=122, state="complete"))
        self.assertEqual(len(p._printer.command_batches), 1)
        p._handle_record(dict(record="rme_error", message="workflow=tool_review code=printer_busy"))
        p._handle_record(dict(record="review", id=122, state="complete"))
        self.assertEqual(len(p._printer.command_batches), 2)

    def test_wire_records_have_one_ordered_worker(self):
        p = self.plugin()
        deferred = []
        p._defer = lambda callback, *args: deferred.append((callback, args))
        self.submit(p)
        p._handle_record(dict(record="review", id=123, state="complete", revision=19))
        self.assertEqual(len(deferred), 1)
        deferred[0][0](*deferred[0][1])
        self.assertFalse(p._toolmap_hold_active)
        self.assertFalse(p._toolmap_review_worker)

    def test_optional_default_save_failure_cannot_strand_committed_review(self):
        p = self.plugin()
        self.submit(p)
        p._settings.save = Mock(side_effect=OSError("disk full"))
        p._handle_record(dict(record="review", id=123, state="complete", revision=19))
        self.assertFalse(p._toolmap_hold_active)

    def test_validator_exception_rejects_review_without_releasing_hold(self):
        p = self.plugin()
        p._configure_validator_mapping = Mock(side_effect=RuntimeError("validator unavailable"))
        self.submit(p)
        self.assertTrue(p._toolmap_hold_active)
        self.assertEqual(p._printer.command_batches[-1], ["@RME REVIEW REJECT id=123"])
        self.assertEqual(p._state["prompt"]["message"], "validator unavailable")


if __name__ == "__main__":
    unittest.main()
