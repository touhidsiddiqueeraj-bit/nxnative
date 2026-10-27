"""The PC companion's own logic (dm/android/tools/pc_companion.py): the pipeline command it runs, the
APK it picks up afterwards, the align-sign-install commands, the config it keeps, and the toolchain
report a machine gets before its first build - the window itself is not under test (it needs a display;
the module imports with none, which this file proves)."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PC = REPO / 'dm/android/tools/pc_companion.py'


def load_pc():
    spec = importlib.util.spec_from_file_location('pc_companion', PC)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


class PipelineCommand(unittest.TestCase):
    def setUp(self): self.pc = load_pc()

    def test_the_basics(self):
        cmd = self.pc.pipeline_command('/nsps', '/keys/prod.keys')
        self.assertEqual(cmd[:1], [sys.executable])
        self.assertIn('build_apk_from_nsp.py', cmd[1])
        self.assertEqual(cmd[-2:], ['-k', '/keys/prod.keys'])
        for gone in ('--app-name', '-l', '--no-show-fps'):   # the defaults: English, frame rate on, the game's own name
            self.assertNotIn(gone, cmd)

    def test_the_options(self):
        cmd = self.pc.pipeline_command('/nsps', '/k', language='ja', show_fps=False, app_name='Wonder')
        self.assertIn('-l', cmd); self.assertEqual(cmd[cmd.index('-l') + 1], 'ja')
        self.assertIn('--no-show-fps', cmd)
        self.assertEqual(cmd[cmd.index('--app-name') + 1], 'Wonder')


class BuiltApks(unittest.TestCase):
    def setUp(self): self.pc = load_pc()

    def test_newest_first_and_old_ones_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            old, new = Path(tmp) / 'Old Game.apk', Path(tmp) / 'New Game.apk'
            old.write_bytes(b'old'); new.write_bytes(b'new')
            before = 1000000000.0
            os.utime(old, (before, before)); os.utime(new, (2000000000.0, 2000000000.0))
            self.assertEqual(self.pc.find_built_apks(tmp, 1999999999.0), [new])
            self.assertEqual(self.pc.find_built_apks(tmp, before - 1)[0], new)   # newest first even with both
            self.assertEqual(self.pc.find_built_apks(tmp, 2000000001.0), [])     # the cutoff the build started with

    def test_no_folder(self):
        self.assertEqual(self.pc.find_built_apks('/no/such/folder', 0), [])


class FinishCommands(unittest.TestCase):
    """The three commands after the pipeline: 16 KB alignment, the personal key, the install."""
    def setUp(self): self.pc = load_pc()

    def test_zipalign_and_sign(self):
        za = self.pc.zipalign_command('/sdk/build-tools/35.0.0', Path('/g/Wonder.apk'), Path('/g/Wonder.aligned.apk'))
        self.assertEqual(za[:6], ['/sdk/build-tools/35.0.0/zipalign', '-f', '-P', '16', '4', '/g/Wonder.apk'])
        ap = self.pc.apksigner_command('/sdk/build-tools/35.0.0', Path('/ks'), 'sp', 'kp', 'nxnative', Path('/a'), Path('/b'))
        self.assertEqual(ap[:2], ['/sdk/build-tools/35.0.0/apksigner', 'sign'])
        self.assertIn('--ks-pass', ap); self.assertEqual(ap[ap.index('--ks-pass') + 1], 'pass:sp')
        self.assertEqual(ap[-2:], ['/b', '/a'])   # apksigner takes --out before its input: the aligned copy signs onto the APK the build wrote

    def test_keytool(self):
        kt = self.pc.keytool_gen_command('/usr/bin/keytool', Path('/ks'), 'sp', 'kp', 'nxnative')
        self.assertEqual(kt[:4], ['/usr/bin/keytool', '-genkeypair', '-keystore', '/ks'])
        self.assertIn('-validity', kt); self.assertEqual(kt[kt.index('-validity') + 1], '10950')   # 30 years

    def test_adb_install(self):
        self.assertEqual(self.pc.adb_install_command('/adb', 'ABC123', Path('/g.apk')),
                         ['/adb', '-s', 'ABC123', 'install', '-r', '/g.apk'])
        self.assertEqual(self.pc.adb_install_command('/adb', None, Path('/g.apk')), ['/adb', 'install', '-r', '/g.apk'])


class Config(unittest.TestCase):
    def setUp(self): self.pc = load_pc()

    def test_round_trip_and_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.pc.load_config(tmp)
            self.assertEqual(cfg['language'], 'en-US'); self.assertTrue(cfg['show_fps']); self.assertEqual(cfg['alias'], 'nxnative')
            cfg.update(language='ja', last_nsp='/nsps', store_password='x')
            self.pc.save_config(cfg, tmp)
            self.assertEqual(self.pc.load_config(tmp)['last_nsp'], '/nsps')
            self.assertEqual(self.pc.load_config(tmp)['language'], 'ja')

    def test_broken_file_is_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'config.json').write_text('{ not json')
            cfg = self.pc.load_config(tmp)   # a half-written or hand-edited file falls back to the defaults
            self.assertEqual(cfg['language'], 'en-US')


class Toolchain(unittest.TestCase):
    def setUp(self): self.pc = load_pc()

    def fake_sdk(self, tmp, versions=('34.0.0', '35.0.0')):
        sdk = Path(tmp) / 'sdk'
        (sdk / 'platform-tools').mkdir(parents=True); (sdk / 'platform-tools' / 'adb').write_text('')
        (sdk / 'ndk' / self.pc.NDK_VERSION).mkdir(parents=True)
        (sdk / 'cmake' / self.pc.CMAKE_VERSION).mkdir(parents=True)
        (sdk / 'platforms' / self.pc.PLATFORM).mkdir(parents=True)
        for v in versions:
            (sdk / 'build-tools' / v).mkdir(parents=True); (sdk / 'build-tools' / v / 'zipalign').write_text('')
        return sdk

    def test_build_tools_floor(self):
        with tempfile.TemporaryDirectory() as tmp:
            sdk = self.fake_sdk(tmp, versions=('33.0.0', '34.0.0'))   # before 35: zipalign has no -P 16
            self.assertIsNone(self.pc.bt_dir(sdk))
        with tempfile.TemporaryDirectory() as tmp:
            sdk = self.fake_sdk(tmp, versions=('34.0.0', '35.0.0', '36.0.0'))
            self.assertEqual(self.pc.bt_dir(sdk).name, '36.0.0')

    def test_sdk_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            sdk = self.fake_sdk(tmp)
            r = self.pc.sdk_status(sdk)
            self.assertTrue(r['ok']); self.assertIn(str(sdk), r['detail'])
            (sdk / 'ndk' / self.pc.NDK_VERSION).rename(sdk / 'ndk' / 'gone')
            r = self.pc.sdk_status(sdk)
            self.assertFalse(r['ok']); self.assertIn('ndk', r['detail'])
        self.assertFalse(self.pc.sdk_status('/no/such/sdk')['ok'])

    def test_java_version(self):
        self.assertEqual(self.pc.java_version('openjdk version "17.0.12" 2024-07-16'), 17)
        self.assertEqual(self.pc.java_version('openjdk version "21" 2024-01-01'), 21)
        self.assertIsNone(self.pc.java_version('no version here'))

    def test_report_with_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch.dict(os.environ, {'ANDROID_HOME': str(Path(tmp) / 'sdk')}):
            with unittest.mock.patch.object(self.pc.shutil, 'which', return_value=None), \
                 unittest.mock.patch.object(self.pc, '_importable', return_value=False):
                rows = self.pc.tool_report({}, which=lambda name: None)
                by_name = {r['name']: r for r in rows}
                self.assertFalse(any(r['ok'] for r in rows if 'translator' not in r['name']))   # the checkout's own binaries stay ok
                self.assertIn('developer.android.com', by_name['Android SDK']['fix'])   # guided, not just failed
                self.assertIn('build-essential', by_name['make']['fix'])

    def test_report_with_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            sdk = self.fake_sdk(tmp)
            with unittest.mock.patch.dict(os.environ, {'ANDROID_HOME': str(sdk)}), \
                 unittest.mock.patch.object(self.pc, '_importable', return_value=True):
                rows = self.pc.tool_report({'sdk': str(sdk)}, which=lambda name: f'/bin/{name}')
                self.assertTrue(all(r['ok'] for r in rows if r['name'] != 'Java 17+'))   # java's version is run for real below

    def test_report_java_17(self):
        sdk = {'sdk': ''}
        with unittest.mock.patch.object(self.pc.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0,
                    stdout='', stderr='openjdk version "17.0.12"')), \
             unittest.mock.patch.object(self.pc, '_importable', return_value=True):
            rows = self.pc.tool_report(sdk, which=lambda name: '/usr/bin/java' if name == 'java' else f'/bin/{name}')
            row = next(r for r in rows if r['name'] == 'Java 17+')
            self.assertTrue(row['ok'])


class Devices(unittest.TestCase):
    def setUp(self): self.pc = load_pc()

    def test_parse(self):
        out = ('List of devices attached\n'
               'ABC123\tdevice product:raven model:Pixel_6_Pro device:raven\n'
               'XYZ\tunauthorized usb:1-1\n'
               '\n')
        dev = self.pc.parse_devices(out)
        self.assertEqual(dev[0], ('ABC123', 'device', 'Pixel 6 Pro'))
        self.assertEqual(dev[1][1], 'unauthorized')   # the phone has not accepted the computer yet

    def test_no_devices(self):
        self.assertEqual(self.pc.parse_devices('List of devices attached\n\n'), [])


class Keystore(unittest.TestCase):
    def setUp(self): self.pc = load_pc()

    def test_no_keytool_is_an_explanation(self):
        with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch.object(self.pc, 'CONFIG_DIR', Path(tmp)), \
             unittest.mock.patch.object(self.pc.shutil, 'which', return_value=None):
            with self.assertRaises(RuntimeError) as cm:
                self.pc.ensure_keystore({'alias': 'nxnative'})
            self.assertIn('keytool', str(cm.exception))

    def test_made_once_then_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            def fake_run_out(cmd, env=None, timeout=None):
                Path(str(cmd[cmd.index('-keystore') + 1])).write_bytes(b'ks')   # keytool's file, without keytool
                return 0, ''
            cfg = {'alias': 'nxnative'}
            with unittest.mock.patch.object(self.pc, 'CONFIG_DIR', home), \
                 unittest.mock.patch.object(self.pc.shutil, 'which', return_value='/usr/bin/keytool'), \
                 unittest.mock.patch.object(self.pc, 'run_out', side_effect=fake_run_out):
                ks, cfg = self.pc.ensure_keystore(cfg)
                self.assertTrue(ks.exists()); self.assertTrue(cfg['store_password'])   # a random password was kept
                again = self.pc.load_config(home); again.update(store_password=cfg['store_password'], key_password=cfg['key_password'])
                with unittest.mock.patch.object(self.pc, 'run_out') as no_call:   # the second call: the file and the passwords are there
                    ks2, _ = self.pc.ensure_keystore(again)
                    no_call.assert_not_called(); self.assertEqual(ks, ks2)


class CheckMode(unittest.TestCase):
    """--check prints the same report the window shows; it must run with no display and exit 1 on a bare machine."""
    def test_runs_headless(self):
        r = subprocess.run([sys.executable, str(PC), '--check'], capture_output=True, text=True, timeout=120)
        self.assertIn('toolchain report', r.stdout)
        self.assertIn('phone', r.stdout)   # the device line: either a phone, or the guidance
        self.assertIn('not ready', r.stdout)   # this machine has no SDK right now; a ready one says 'ready'
        self.assertEqual(r.returncode, 1)


if __name__ == '__main__':
    unittest.main()
