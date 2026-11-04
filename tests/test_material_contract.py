"""Provider identity -> explicit wire family -> authoritative firmware report."""
import logging
import unittest
from unittest.mock import Mock
from test_toolmap_gate import RmeCompatibilityPlugin, _Printer, _Settings, parse_line


class MaterialContract(unittest.TestCase):
    def plugin(self, alias, family, reported=None):
        p = RmeCompatibilityPlugin()
        p._logger = logging.getLogger('material-contract-test')
        p._settings = _Settings()
        p._printer = _Printer()
        p._schedule_publish = Mock()
        p._defer = lambda *args: None
        p._state.update(connected=True, supported=True)
        p._state['session'].update(material_repair=1, printer_state='IDLE')
        p._state['spoolmanager'].update(provider='spoolmanager', published=[{
            'alias': alias, 'database_id': 12, 'material': family,
            'vendor': 'A brand', 'color': '#123456', 'display_name': 'My spool',
        }])
        p._handle_record(parse_line('loaded_filament T7 S"%s" P"%s" O"Custom" H"#123456" M"A brand"' %
                                  (alias if reported is None else reported, alias)))
        return p

    def test_material_repairs_are_explicit_and_not_inferred_from_alias(self):
        for alias, material in [('PLA-00C', 'PLA'), ('PLA-00C', 'PETG'),
                                ('S00ABCD', 'ASA'), ('PC-ZZZ', 'PC'),
                                ('XYZ-ABC', 'TPU'), ('PA-123', 'PA')]:
            for reported in (None, ''):
                p = self.plugin(alias, material, reported)
                base = 'FLEX' if material == 'TPU' else material
                self.assertEqual(['@RME FILAMENT ASSIGN tool=7 if_profile=%s material=%s' % (alias, base)],
                                 p._loaded_material_repair_commands())
                row = p._state['loaded_filaments'][0]
                self.assertEqual(material, row['provider_material'])
                self.assertEqual(alias, row['firmware_profile'])
                self.assertEqual(alias if reported is None else reported, row['firmware_material'])
                self.assertEqual('#123456', row['color'])
                self.assertEqual('A brand', row['vendor'])
                p._handle_record(parse_line('loaded_filament T7 S"%s" P"%s" O"Custom" H"#123456" M"A brand"' % (base, alias)))
                self.assertEqual([], p._loaded_material_repair_commands())
                self.assertEqual(base, p._state['loaded_filaments'][0]['firmware_material'])

    def test_missing_identity_or_distinct_firmware_family_never_gets_relabelled(self):
        p = self.plugin('S00ABCD', 'PLA', 'PETG')
        self.assertEqual([], p._loaded_material_repair_commands())
        p = self.plugin('S00ABCD', 'PLA')
        p._state['loaded_filaments'][0].pop('provider_material')
        self.assertEqual([], p._loaded_material_repair_commands())
        p = self.plugin('S00ABCD', 'PLA')
        p._state['session'].pop('material_repair')
        self.assertEqual([], p._loaded_material_repair_commands())

    def test_repair_is_queued_before_review_and_job_gcode(self):
        p = self.plugin('S00ABCD', 'PLA')
        p._state['machine']['logical_tools'] = 1
        p._print_start_token = Mock(return_value=('new-job', 1))
        p._startup_file_info = Mock(return_value={'origin': 'local', 'path': 'real.gcode'})
        p._printer_transfer_active = Mock(return_value=False)
        order = []
        p._send_commands = lambda commands: order.extend(commands)
        p._prepare_toolmap_prompt = lambda **kw: order.append('REVIEW')
        p._prepare_mesh_area = Mock()
        p._prepare_chamber_job = Mock()
        p._prepare_completion_move = Mock()
        self.assertTrue(p._prepare_print_start(object()))
        self.assertEqual(['@RME FILAMENT ASSIGN tool=7 if_profile=S00ABCD material=PLA', 'M865 Q', 'REVIEW'], order)

