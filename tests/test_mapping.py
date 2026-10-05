import tempfile
import unittest

from octoprint_rme_compatibility.mapping import compatible, read_requirements, recommend, validate, fallback_choices, fallback_commands, recent_mapping_valid


class MappingTests(unittest.TestCase):
    def test_fallback_ranking_chain_and_per_print_reset(self):
        req = {'logical': 0, 'material': 'PLA', 'color': '#ffffff'}
        loaded = [{'tool': i, 'material': m, 'color': c} for i, m, c in
                  [(0, 'PLA', '#ffffff'), (1, 'PLA', '#000000'),
                   (2, 'PETG', '#ffffff'), (3, 'PLA', '#ffffff')]]
        self.assertEqual(fallback_choices(req, loaded, {0}), [3, 1])
        self.assertEqual(fallback_commands([req], loaded, {0: 0}, True, {'0': [3, 1]}, 4),
                         ['@RME SPOOLJOIN RESET', '@RME SPOOLJOIN ADD from=0 to=3',
                          '@RME SPOOLJOIN ADD from=3 to=1', '@RME SPOOLJOIN QUERY'])
        self.assertEqual(fallback_commands([req], loaded, {}, False, {}, 4),
                         ['@RME SPOOLJOIN RESET', '@RME SPOOLJOIN QUERY'])
        for selections in ({'0': [0]}, {'0': [2]}, {'0': [3, 3]}, {'0': [8]}, {'0': ['1']}, {'7': [1]}):
            with self.assertRaises(ValueError):
                fallback_commands([req], loaded, {}, False, selections, 4)

    def test_fallback_cannot_steal_another_primary(self):
        reqs = [{'logical': i, 'material': 'PLA'} for i in (0, 1)]
        loaded = [{'tool': i, 'material': 'PLA'} for i in range(4)]
        for selection in ({'0': [1]}, {'0': [2], '1': [2]}):
            with self.assertRaises(ValueError):
                fallback_commands(reqs, loaded, {}, False, selection, 4)

    def test_recent_job_bypass_requires_fresh_valid_selections(self):
        reqs = [{'logical': 0, 'material': 'PLA', 'color': '#ffffff'}]
        loaded = [{'tool': 1, 'material': 'PLA', 'color': '#ffffff'}]
        file = {'origin': 'local', 'date': 100000}
        self.assertTrue(recent_mapping_valid(file, 100100, reqs, loaded, {0: 1}, True))
        for now in (99999, 186400, 200000):
            self.assertFalse(recent_mapping_valid(file, now, reqs, loaded, {0: 1}, True))
        self.assertFalse(recent_mapping_valid(file, 100100, reqs, loaded, {}, False))
        loaded[0]['color'] = '#000000'
        self.assertFalse(recent_mapping_valid(file, 100100, reqs, loaded, {0: 1}, True))
        self.assertFalse(recent_mapping_valid({}, 100100, reqs, loaded, {0: 1}, True))
    def test_material_is_hard_constraint(self):
        self.assertTrue(compatible('PETG', 'PET'))
        for a, b in [('PLA', 'PETG'), ('', ''), ('PLA', 'PLA-CF'), ('NEW', 'NEW')]:
            self.assertFalse(compatible(a, b))

    def test_color_assignment_unique_and_material_safe(self):
        required = [{'logical': 0, 'material': 'PLA', 'color': '#000000'},
                    {'logical': 1, 'material': 'PLA', 'color': '#ffffff'}]
        loaded = [{'tool': 0, 'material': 'PLA', 'color': '#ffffff'},
                  {'tool': 1, 'material': 'PLA', 'color': '#000000'},
                  {'tool': 2, 'material': 'PETG', 'color': '#000000'}]
        result = recommend(required, loaded, 3)
        self.assertEqual({0: 1, 1: 0, 2: 2}, result)
        validate(required, loaded, result, True)
        with self.assertRaises(ValueError):
            validate(required, loaded, {0: 2, 1: 0}, True)

    def test_no_fallback_to_wrong_or_unknown_material(self):
        required = [{'logical': 0, 'material': 'ASA'}]
        self.assertIsNone(recommend(required, [{'tool': 0, 'material': 'PLA'}], 1))
        self.assertIsNone(recommend([], [], 8))
        with self.assertRaises(ValueError):
            validate(required, [], {}, False)

    def test_insufficient_matching_tools(self):
        required = [{'logical': i, 'material': 'PLA'} for i in range(2)]
        self.assertIsNone(recommend(required, [{'tool': 0, 'material': 'PLA'}], 2))

    def test_orca_and_prusa_metadata_ignore_unused(self):
        with tempfile.NamedTemporaryFile() as stream:
            stream.write(b'; filament_type = PLA;PETG;PLA\n; filament_colour = #ffffff;#123456;#000000\n; filament used [mm] = 0, 12, 5\n')
            stream.flush()
            requirements = read_requirements(stream.name, 8)
        self.assertEqual([1, 2], [r['logical'] for r in requirements])
        self.assertEqual('#123456', requirements[0]['color'])

    def test_unknown_color_prefers_known_and_stable_ties(self):
        req = [{'logical': 0, 'material': 'PLA', 'color': '#000000'}]
        loaded = [{'tool': 0, 'material': 'PLA'}, {'tool': 1, 'material': 'PLA', 'color': '#111111'}]
        self.assertEqual(1, recommend(req, loaded, 2)[0])
