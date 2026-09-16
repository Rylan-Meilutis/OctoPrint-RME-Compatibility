import tempfile
import unittest

from octoprint_rme_compatibility.mapping import compatible, read_requirements, recommend, validate


class MappingTests(unittest.TestCase):
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
