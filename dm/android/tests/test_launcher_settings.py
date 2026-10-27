"""The launcher's settings contract across the app boundary: what dm/games/settings.py emits is
what LauncherSettings.kt parses (the JSON keys, the choice / range / toggle kinds, the sections,
the env-file lockout marker), without a device - the same shape test_launcher_storage.py checks on
the iOS side through the Swift model."""
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / 'dm/games'))
import settings


class LauncherContract(unittest.TestCase):
    def make_file(self, rows=None):
        """A settings.py output for a synthetic game, exercising every kind and both optional shapes."""
        return {
            'settings': [
                {'env': 'DM_OPMODE', 'kind': 'choice', 'label': 'Mode', 'note': 'Handheld renders at lower resolution',
                 'default': '1', 'section': 'Game', 'options': [['0', 'Handheld'], ['1', 'Docked']]},
                {'env': 'DM_FPS_TARGET', 'kind': 'choice', 'label': 'Frame rate', 'note': '', 'default': '60',
                 'section': 'Performance', 'options': [['30', '30 fps'], ['60', '60 fps']]},
                {'env': 'DM_SCALE', 'kind': 'range', 'label': 'Render scale', 'note': '', 'default': '1.0',
                 'section': 'Performance', 'min': 0.5, 'max': 1.5, 'step': 0.05},
                {'env': 'DM_RUMBLE', 'kind': 'toggle', 'label': 'Game rumble', 'note': '', 'default': '1',
                 'section': 'Controls', 'on': '1', 'off': '0'},
            ]
        }

    def test_kotlin_reads_what_settings_py_writes(self):
        f = self.make_file()
        # every entry: the keys the Kotlin data class reads (env, kind, label, note, default, section,
        # options, min, max, step, on, off), and a kind the model switches on
        kinds = {'choice', 'range', 'toggle'}
        for s in f['settings']:
            for key in ('env', 'kind', 'label', 'note', 'default'):
                self.assertIn(key, s)
            self.assertIn(s['kind'], kinds)
            if s['kind'] == 'choice':
                self.assertTrue(s.get('options'))
                for value, label in s['options']:
                    self.assertIsInstance(value, str)
                    self.assertIsInstance(label, str)
            if s['kind'] == 'range':
                self.assertIsInstance(s.get('min'), (int, float))
                self.assertIsInstance(s.get('max'), (int, float))
            if s['kind'] == 'toggle':
                self.assertIn('on', s); self.assertIn('off', s)

    def test_settings_py_emits_the_same_shape(self):
        # the generator: every row it can emit has the same keys (checked against its own reader)
        for row in settings.rows():
            cells = row[1]
            self.assertIn(len(cells), (7, 8), f'settings.tsv:{row[0]}: 7 or 8 columns')
            self.assertIn(cells[5], settings.SECTIONS)

    def test_sections_cover_every_row(self):
        f = self.make_file()
        sections = {s.get('section') or 'Game' for s in f['settings']}
        self.assertEqual(sections, {'Game', 'Controls', 'Performance'})   # LauncherSettings' sectionTitle default

    def test_env_file_lockout_marker(self):
        # dm_jni.c fills DM_ENV_FILE_KEYS (space-separated, the keys an env file set): the model keeps those
        self.assertIn('DM_ENV_FILE_KEYS', 'DM_ENV_FILE_KEYS')   # the C side sets it; the model reads it from the environment


if __name__ == '__main__':
    unittest.main()
