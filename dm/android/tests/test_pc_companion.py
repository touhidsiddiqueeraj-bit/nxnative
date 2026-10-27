"""The PC companion's own logic (dm/android/tools/pc_companion.py): the pipeline command it runs, the
APK it picks up afterwards, the align-sign-install commands, the config it keeps, and the toolchain
report a machine gets before its first build - the window itself is not under test (it needs a display;
the module imports with none, which this file proves)."""
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
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


class ReceiverCommands(unittest.TestCase):
    """The commands that raise and reach the phone's receiver screen."""
    def setUp(self): self.pc = load_pc()

    def test_am_start_spells_the_whole_component(self):
        self.assertEqual(self.pc.am_start_command('/adb', 'S1', 'org.nxnative.010015100b514000'),
                         ['/adb', '-s', 'S1', 'shell', 'am', 'start', '-n',
                          'org.nxnative.010015100b514000/org.nxnative.android.ReceiverActivity'])   # the game's id is not the namespace

    def test_forward_and_port(self):
        self.assertEqual(self.pc.forward_command('/adb', 'S1'),
                         ['/adb', '-s', 'S1', 'forward', 'tcp:0', 'localabstract:nxnative'])
        self.assertEqual(self.pc.parse_forward_port('7387\n'), 7387)
        self.assertEqual(self.pc.parse_forward_port('7387'), 7387)
        self.assertIsNone(self.pc.parse_forward_port(''))
        self.assertIsNone(self.pc.parse_forward_port("error: device 'S1' not found"))   # a digit in an error is not a port
        self.assertIsNone(self.pc.parse_forward_port('error: closed (took 10s)'))

    def test_forward_remove(self):
        self.assertEqual(self.pc.forward_remove_command('/adb', 'S1'), ['/adb', '-s', 'S1', 'forward', '--remove-all'])

    def test_pick_package(self):
        lines = ['package:org.nxnative.010015100b514000', 'package:org.nxnative.0100a3500ddce000']
        self.assertEqual(self.pc.pick_package(lines), 'org.nxnative.010015100b514000')   # no shell: the first game app
        self.assertEqual(self.pc.pick_package(['package:com.android.chrome', 'package:org.nxnative.shell']),
                         self.pc.SHELL_ID)   # the template is the receiver proper
        self.assertEqual(self.pc.pick_package(['package:com.other']), None)   # nothing to raise
        self.assertEqual(self.pc.pick_package(['garbage', '', '  ']), None)

    def test_receiver_build_and_apk(self):
        cmd = self.pc.receiver_build_command(Path('/r/dm/android/gradlew'))
        self.assertEqual(cmd[:1], ['/r/dm/android/gradlew'])
        self.assertIn('-PDM_APP_ID=org.nxnative.shell', cmd)   # the template's own applicationId
        self.assertIn('assembleRelease', cmd)
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / 'app' / 'build' / 'outputs' / 'apk' / 'release'
            self.assertIsNone(self.pc.find_receiver_apk(tmp))   # no build yet
            d.mkdir(parents=True)
            (d / 'app-release-unsigned.apk').write_bytes(b'unsigned')
            self.assertEqual(self.pc.find_receiver_apk(tmp).name, 'app-release-unsigned.apk')
            import time as _t
            (d / 'app-release.apk').write_bytes(b'signed'); _t.sleep(0.01)
            self.assertEqual(self.pc.find_receiver_apk(tmp).name, 'app-release.apk')   # newer wins

    def test_phone_status(self):
        st = self.pc.phone_status({'ev': 'stage', 'k': 2, 'n': 5, 'name': 'Shaders', 'step': 'the pairs'})
        self.assertEqual(st, {'event': 'status', 'state': 'building', 'stage': 'Shaders', 'detail': 'the pairs'})
        st = self.pc.phone_status({'ev': 'progress', 'name': 'Shaders', 'step': 'translating',
                                   'done': 12345, 'total': 41516, 'unit': 'shaders', 'frac': 0.2974})
        self.assertEqual(st['detail'], '12,345 / 41,516 shaders'); self.assertEqual(st['pct'], 30)
        st = self.pc.phone_status({'ev': 'progress', 'name': 'RomFS', 'step': 'unpacking', 'done': None, 'total': None, 'frac': None})
        self.assertEqual(st['detail'], 'unpacking'); self.assertNotIn('pct', st)   # no count: the phone's bar goes indeterminate
        self.assertIsNone(self.pc.phone_status({'ev': 'say', 'msg': '...'}))   # log talk, not screen state
        self.assertIsNone(self.pc.phone_status({'ev': 'stage_end', 'ok': True}))


class PhoneLinkLive(unittest.TestCase):
    """The link against a fake receiver over a real socket (adb is faked: the forward names the
    fake receiver's own port): hello, the statuses, the heartbeat, and the close when the phone's
    app goes away (an install replaced it)."""

    def setUp(self): self.pc = load_pc()

    def fake_adb(self, port):
        def run_out(cmd, env=None, timeout=None):
            if len(cmd) >= 6 and cmd[3] == 'forward' and cmd[4:6] == ['tcp:0', self.pc.PHONE_SOCKET]:
                return 0, f'{port}\n'
            return 0, ''   # the remove and anything else: fine
        return run_out

    def test_hello_statuses_heartbeat_and_close(self):
        server = socket.socket(); server.bind(('127.0.0.1', 0)); server.listen(4)
        port = server.getsockname()[1]
        self.pc.run_out = self.fake_adb(port)
        seen, states, ready = [], [], []

        def receiver():
            sock, _ = server.accept(); sock.settimeout(3)
            f, out = sock.makefile('rb'), sock.makefile('wb')
            out.write(b'{"event":"ready","hasGame":false}\n'); out.flush()   # what ReceiverLink.serve sends on accept
            while True:
                line = f.readline()
                if not line: break
                seen.append(json.loads(line))
                if seen[-1].get('event') == 'ping': out.write(b'{"event":"pong"}\n'); out.flush()
                if len(seen) >= 4: break   # hello, the first ping, the status, the queued ping
            sock.close()
        threading.Thread(target=receiver, daemon=True).start()

        link = self.pc.PhoneLink('/adb', 'S1', on_state=lambda live: states.append(live), on_ready=lambda g: ready.append(g))
        link.send({'event': 'status', 'state': 'building', 'stage': 'Shaders', 'detail': '12,345 / 41,516 shaders', 'pct': 30})
        link.send({'event': 'status', 'state': 'done', 'game': 'Wonder'})   # replaces the queued building one
        link.send({'event': 'ping'})
        link.start()
        for _ in range(100):
            if len(seen) >= 4: break
            time.sleep(0.05)
        link.stop(); link.join(5)
        server.close()

        self.assertEqual(seen[0], {'event': 'hello'})                     # the greeting, no game picked yet
        self.assertEqual(seen[1].get('event'), 'ping')
        self.assertEqual(seen[2], {'event': 'status', 'state': 'done', 'game': 'Wonder'})   # only the newest status was sent
        self.assertEqual(seen[3].get('event'), 'ping')
        self.assertEqual(ready, [False])                                  # the phone told what it holds
        self.assertIn(True, states)                                       # went live when the receiver answered
        self.assertEqual(states[-1], False)                               # and off when its end closed

    def test_the_newest_status_wins(self):
        link = self.pc.PhoneLink('/adb', 'S1')
        for i in range(5): link.send({'event': 'status', 'state': 'building', 'stage': f's{i}', 'detail': ''})
        self.assertEqual([m['stage'] for m in link.pending], ['s4'])   # a screen shows one line, not a backlog
        link.send({'event': 'status', 'state': 'done', 'game': 'g'})
        self.assertEqual(link.pending[-1]['state'], 'done')


class ReceiverContract(unittest.TestCase):
    """The receiver screen's contract across the app boundary, the way test_launcher_settings checks
    the settings one: the manifest exports the activity for adb's am start, the empty shell hands the
    screen over to it, and the socket name / activity the companion hardcodes are the app's own."""

    def setUp(self): self.pc = load_pc()

    def test_the_manifest_exports_the_receiver(self):
        m = (REPO / 'dm/android/app/src/main/AndroidManifest.xml').read_text()
        self.assertIn('android:name=".ReceiverActivity"', m)
        self.assertIn('android:exported="true"', m)   # adb shell am start cannot start it otherwise
        block = m.split('android:name=".ReceiverActivity"', 1)[1].split('/>', 1)[0]   # its own element (self-closing)
        self.assertNotIn('<intent-filter', block)   # reachable by name only, not launchable from outside

    def test_the_empty_shell_hands_the_screen_over(self):
        k = (REPO / 'dm/android/app/src/main/java/org/nxnative/android/MainActivity.kt').read_text()
        hand = k.split('!= 0) {', 1)[1].split('}', 1)[0]
        self.assertIn('ReceiverActivity', hand)   # the no-game path is the receiver, not a dead end
        self.assertIn('finish()', hand)

    def test_the_companion_names_the_app_s_own_things(self):
        link = (REPO / 'dm/android/app/src/main/java/org/nxnative/android/ReceiverLink.kt').read_text()
        self.assertIn('const val NAME = "nxnative"', link)
        self.assertEqual(self.pc.PHONE_SOCKET, 'localabstract:nxnative')          # what ReceiverLink binds
        self.assertTrue(self.pc.PHONE_ACTIVITY.endswith('org.nxnative.android.ReceiverActivity'))   # namespace + activity
        self.assertTrue(link.count('"pong"') >= 1 and link.count('"ping"') >= 1)   # the heartbeat both ends speak
        kt = (REPO / 'dm/android/app/src/main/java/org/nxnative/android/ReceiverActivity.kt').read_text()
        self.assertIn('KEEP_SCREEN_ON', kt)   # the build takes minutes: the screen must not sleep mid-story

    def test_the_strings_the_screen_shows_exist(self):
        s = (REPO / 'dm/android/app/src/main/res/values/strings.xml').read_text()
        for name in ('receiver_waiting', 'receiver_connected', 'receiver_installing', 'receiver_installed', 'receiver_failed', 'receiver_usb_steps'):
            self.assertIn(name, s)

    def test_the_template_compiles_against_a_committed_profile(self):
        self.assertTrue((REPO / 'dm/games' / self.pc.PROFILE / 'game.tsv').is_file())   # the host needs one to compile
        yml = (REPO / '.github/workflows/android.yml').read_text()
        self.assertIn(f"GAME: '{self.pc.PROFILE}'", yml)   # the companion and the CI build the same template


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
