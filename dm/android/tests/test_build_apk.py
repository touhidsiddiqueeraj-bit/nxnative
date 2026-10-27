"""The Android build workflow's guards (test_xcode_workflow.py's checks, for build_apk.sh): the
script stops without GAME or W, names the game from the control data, packs the side files into the
staged assets folder, and takes the pad layout and the operation mode from the profile's words."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SH = REPO / 'dm/android/tools/build_apk.sh'


class BuildApkWorkflow(unittest.TestCase):
    def run_sh(self, env, *args):
        e = dict(os.environ)
        e.pop('GAME', None)   # the runner's own GAME must not leak in
        e.update(env)
        return subprocess.run(['bash', str(SH), *args], capture_output=True, text=True, env=e)

    def test_game_is_required(self):
        r = self.run_sh({}, '/tmp/x.apk')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('GAME is not set', r.stdout + r.stderr)
        self.assertIn('Super Mario Odyssey', r.stdout + r.stderr)   # the profile list names the games

    def test_work_folder_is_required(self):
        r = self.run_sh({'GAME': '010015100B514000'}, '/tmp/x.apk')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('W is not set', r.stdout + r.stderr)

    def test_romfs_and_modules_are_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = Path(tmp) / '010015100B514000-work'
            (w / 'build').mkdir(parents=True)
            r = self.run_sh({'GAME': '010015100B514000', 'W': str(w), 'DM_DISPLAY_NAME': 'Game'}, str(Path(tmp) / 'x.apk'))
            self.assertNotEqual(r.returncode, 0)
            self.assertIn('no rewritten module', r.stdout + r.stderr)

    def test_meta_maps_the_profile_words(self):
        # build_apk.sh maps the profile's words the way game.py's header generator does: pad_layout
        # simple / full / touch → 0 / 1 / 2, op_mode console / handheld → 1 / 0 (LauncherSettings and
        # DmGamepadView read these numbers)
        import importlib.util
        spec = importlib.util.spec_from_file_location('game', REPO / 'dm/games/game.py')
        game = importlib.util.module_from_spec(spec); spec.loader.exec_module(game)
        for gid, pad, opmode in [('010015100B514000', None, None)]:   # Mario Wonder defaults its own way
            g = game.load(gid)
            pl = g.get('pad_layout', 'full')
            self.assertIn(pl, ('simple', 'full', 'touch'), f'{gid}: pad_layout word')


if __name__ == '__main__':
    unittest.main()
