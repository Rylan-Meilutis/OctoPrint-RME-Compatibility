import types
import unittest
from unittest.mock import Mock
from octoprint_rme_compatibility.octopod_notifications import HELPERS, install, is_helper


class OctopodHelpers(unittest.TestCase):
    def test_only_generated_local_automation_paths_are_helpers(self):
        for path in HELPERS:
            self.assertTrue(is_helper(dict(origin='local', path=path)))
            self.assertFalse(is_helper(dict(origin='sdcard', path=path)))
        for path in ('continuousprint_finish.gcode', 'real.gcode',
                     'ContinuousPrint/tmp/real.gcode', 'ContinuousPrint/tmp/../real.gcode'):
            self.assertFalse(is_helper(dict(origin='local', path=path)))

    def test_helper_notifications_suppressed_but_following_real_job_notifies(self):
        send = Mock(return_value=7)
        notifications = types.SimpleNamespace(send_print_job_notification=send)
        plugin = types.SimpleNamespace(_job_notifications=notifications)
        self.assertTrue(install(plugin))
        wrapped = notifications.send_print_job_notification
        self.assertTrue(install(plugin))
        self.assertIs(wrapped, notifications.send_print_job_notification)
        data = dict(job=dict(file=dict(origin='local', path=next(iter(HELPERS)))))
        printer = types.SimpleNamespace(get_current_data=lambda: data)
        for state in ('PRINTING', 'PAUSED', 'PRINTING', 'FINISHING'):
            self.assertEqual(wrapped(None, printer, dict(state_id=state, state_string=state)), 0)
        # File selection can be cleared before the operational event arrives.
        data['job']['file'] = {}
        self.assertEqual(wrapped(None, printer, dict(state_id='OPERATIONAL')), 0)
        send.assert_not_called()
        data['job']['file'] = dict(origin='local', path='actual-part.gcode')
        for state in ('PRINTING', 'FINISHING', 'OPERATIONAL'):
            self.assertEqual(wrapped(None, printer, dict(state_id=state)), 7)
        self.assertEqual(send.call_count, 3)
        data['job']['file'] = dict(origin='local', path=next(iter(HELPERS)))
        self.assertEqual(wrapped(None, printer, dict(state_id='OPERATIONAL'), test=True), 7)

    def test_absent_or_incompatible_octopod_is_untouched(self):
        self.assertFalse(install(types.SimpleNamespace()))
