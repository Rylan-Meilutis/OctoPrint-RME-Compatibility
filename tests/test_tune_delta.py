import unittest
from unittest.mock import patch
import test_print_controls
from octoprint_rme_compatibility.protocol import parse_line


class TuneDelta(unittest.TestCase):
    def plugin(self):
        fixture = test_print_controls.PrintControlsTests()
        fixture.setUp()
        p = fixture.plugin()
        p._schedule_publish = lambda: None
        p._state['machine']['tune_delta'] = 1
        p._printer.get_state_id = lambda: 'PRINTING'
        return p, fixture.commands

    def test_merge_heartbeat_gap_and_full_resynchronization(self):
        p, commands = self.plugin()
        p._handle_record(parse_line('RME_TUNE delta=0 seq=1 speed=100 fan=255 buddy_mcu=50 head_mcu=60'))
        p._handle_record(parse_line('RME_TUNE delta=1 seq=2 fan=0 head_mcu=unknown'))
        self.assertEqual(100, p._state['tune']['speed'])
        self.assertEqual(0, p._state['tune']['fan'])
        self.assertEqual('unknown', p._state['tune']['head_mcu'])
        p._handle_record(parse_line('RME_TUNE delta=1 seq=3'))
        self.assertEqual(100, p._state['tune']['speed'])
        p._handle_record(parse_line('RME_TUNE delta=1 seq=5 fan=200'))
        self.assertIsNone(p._tune_sequence)
        self.assertEqual(0, p._state['tune']['fan'])
        p._query_tune(periodic=True)
        self.assertEqual('@RME TUNE QUERY', commands[-1])
        p._handle_record(parse_line('RME_TUNE delta=0 seq=6 speed=110'))
        self.assertEqual(110, p._state['tune']['speed'])
        self.assertNotIn('fan', p._state['tune'])

    def test_opt_in_periodic_full_refresh_and_legacy_fallback(self):
        p, commands = self.plugin()
        with patch('octoprint_rme_compatibility.plugin.time.monotonic', return_value=100):
            p._handle_record(parse_line('RME_TUNE delta=0 seq=7 speed=100'))
            p._query_tune(periodic=True)
        self.assertEqual('@RME TUNE QUERY since=7', commands[-1])
        p._tune_query_pending = False
        with patch('octoprint_rme_compatibility.plugin.time.monotonic', return_value=131):
            p._query_tune(periodic=True)
        self.assertEqual('@RME TUNE QUERY', commands[-1])
        p._tune_query_pending = False
        p._tune_poll_after = 0
        p._state['machine'].pop('tune_delta')
        p._query_tune(periodic=True)
        self.assertEqual('@RME TUNE QUERY', commands[-1])
        p._handle_record(parse_line('RME_TUNE speed=95'))
        self.assertIsNone(p._tune_sequence)
