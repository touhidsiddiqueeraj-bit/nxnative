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
        # both SDK variables cleared: a runner that preinstalls the SDK (ubuntu-latest does, for the
        # android workflow) must not leak it into the premise - the machine under test has nothing
        with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch.dict(os.environ, {'ANDROID_HOME': str(Path(tmp) / 'sdk'), 'ANDROID_SDK_ROOT': ''}):
            with unittest.mock.patch.object(self.pc.shutil, 'which', return_value=None), \
                 unittest.mock.patch.object(self.pc, '_importable', return_value=False), \
                 unittest.mock.patch.object(self.pc, 'REPO', Path(tmp)):   # a machine with no checkout: the engine row fails too
                rows = self.pc.tool_report({}, which=lambda name: None)
                by_name = {r['name']: r for r in rows}
                self.assertFalse(any(r['ok'] for r in rows))
                self.assertIn('run from a checkout', by_name['the build engine']['fix'])   # guided, not just failed
                self.assertIn('developer.android.com', by_name['Android SDK']['fix'])
                self.assertIn('build-essential', by_name['make']['fix'])

    def test_report_with_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            sdk = self.fake_sdk(tmp)
            with unittest.mock.patch.dict(os.environ, {'ANDROID_HOME': str(sdk)}), \
                 unittest.mock.patch.object(self.pc, '_importable', return_value=True), \
                 unittest.mock.patch.object(self.pc, 'run_out', return_value=(0, '')):   # the translator probe: on this runner it is the Mac binary
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


class SdkDiscovery(unittest.TestCase):
    """The SDK a build uses is found, not typed in: the remembered path, the environment's, then the
    usual install folders - and a distro's android-tools (adb on PATH) is not mistaken for one."""
    def setUp(self): self.pc = load_pc()

    def fake_sdk(self, tmp):
        sdk = Path(tmp) / 'found-sdk'
        (sdk / 'platform-tools').mkdir(parents=True)
        return sdk

    def test_a_remembered_sdk_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            sdk = self.fake_sdk(tmp)
            self.assertEqual(self.pc.find_sdk({'sdk': str(sdk)}), str(sdk))   # no discovery needed

    def test_the_environment_sdk_is_found_and_remembered(self):
        with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch.dict(os.environ, {'ANDROID_HOME': str(self.fake_sdk(tmp)), 'ANDROID_SDK_ROOT': ''}):
            with unittest.mock.patch.object(self.pc, 'CONFIG_DIR', Path(tmp) / 'home'), \
                 unittest.mock.patch.object(self.pc, 'save_config', lambda cfg, home=None: None):   # a test never writes the real config
                cfg = {}
                self.assertEqual(self.pc.find_sdk(cfg), os.environ['ANDROID_HOME'])
                self.assertEqual(cfg['sdk'], os.environ['ANDROID_HOME'])   # remembered for the next run

    def test_no_sdk_anywhere_is_an_empty_answer(self):
        with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch.dict(os.environ, {'ANDROID_HOME': str(Path(tmp) / 'none'), 'ANDROID_SDK_ROOT': ''}):
            with unittest.mock.patch.object(self.pc, 'sdk_candidates', lambda: [Path(tmp) / 'nothing']):
                self.assertEqual(self.pc.find_sdk({}), '')   # the report's row says what to install

    def test_a_distro_adb_folder_is_not_mistaken_for_an_sdk(self):
        with tempfile.TemporaryDirectory() as tmp:   # android-tools puts adb in /usr/bin: no sdk root anywhere
            self.assertFalse(self.pc.looks_like_sdk(Path(tmp)))
            self.assertFalse(self.pc.looks_like_sdk(Path('/usr')))


class TranslatorStatus(unittest.TestCase):
    """The shader chain's row: the committed translator (its launcher, or the same dll through dotnet)
    and the committed spirv-cross of the platform."""
    def setUp(self): self.pc = load_pc()

    def fake_engine(self, tmp):
        repo = Path(tmp) / 'engine'
        xdir = repo / 'dm/gfx/tools/dmsh_xlate_bin'
        xdir.mkdir(parents=True)
        for f in ('dmsh_xlate.dll', 'Ryujinx.Graphics.Shader.dll', 'Ryujinx.Common.dll'): (xdir / f).write_text('')
        sdir = repo / 'dm/gfx/tools/spirv_cross_bin'; sdir.mkdir(parents=True)
        name = 'spirv-cross' if sys.platform == 'darwin' else ('spirv-cross-linux-x86_64' if sys.platform.startswith('linux') else None)
        if name:
            sc = sdir / name; sc.write_text(''); sc.chmod(0o755)
        return repo

    def test_an_incomplete_engine_is_guided_not_runnable(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = self.pc.translator_status(Path(tmp) / 'engine', which=lambda name: '/bin/dotnet')
            self.assertFalse(row['ok'])
            self.assertTrue(row['fix'])   # every miss explains itself

    def test_a_complete_engine_that_runs_is_ok(self):
        if not (sys.platform == 'darwin' or sys.platform.startswith('linux')): self.skipTest('no committed spirv-cross for this platform')
        with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch.object(self.pc, 'run_out', return_value=(0, '')):
            row = self.pc.translator_status(self.fake_engine(tmp), which=lambda name: '/bin/dotnet')
            self.assertTrue(row['ok'])
            self.assertIn('spirv-cross', row['detail'])   # names the platform's committed build

    def test_the_launcher_that_cannot_run_falls_back_to_dotnet(self):
        if not sys.platform.startswith('linux'): self.skipTest('the fallback is the Linux shape (the Mach-O launcher)')
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.fake_engine(tmp)
            probes = []
            def run_out(cmd, **kw):   # the launcher fails, dotnet runs the dll
                probes.append(cmd[0])
                return (1, 'exec format error') if cmd[0].endswith('dmsh_xlate') else (0, 'f21af1efa737a270')
            with unittest.mock.patch.object(self.pc, 'run_out', side_effect=run_out):
                row = self.pc.translator_status(repo, which=lambda name: '/usr/bin/dotnet')
                self.assertTrue(row['ok']); self.assertIn('dotnet', row['detail'])
                self.assertEqual(len(probes), 2)   # the launcher first, the dll through dotnet second

    def test_the_dll_without_dotnet_is_a_net_fix(self):
        if not sys.platform.startswith('linux'): self.skipTest('the dotnet guidance is the Linux shape')
        with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch.object(self.pc, 'run_out', return_value=(1, 'exec format error')):
            row = self.pc.translator_status(self.fake_engine(tmp), which=lambda name: None)
            self.assertFalse(row['ok'])
            self.assertIn('.NET', row['fix'])   # guided to the runtime that runs the dll


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


class FrozenMode(unittest.TestCase):
    """What a standalone build (dm/android/tools/freeze.py) does differently: find the repository from
    where it was launched instead of around its own file, run the pipeline's scripts inside itself,
    read the build's events from a file (a windowed executable has no stdout), keep Windows from
    flashing a console per child, and answer the shader split's spawn children."""

    def setUp(self): self.pc = load_pc()

    def test_from_source_the_repo_is_this_checkout(self):
        self.assertFalse(self.pc.FROZEN)
        self.assertEqual(self.pc.repo_dir(), REPO)   # pc_companion.py sits three levels under the root

    def test_a_frozen_app_finds_the_repository_around_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp) / 'nxnative'
            (checkout / 'scripts').mkdir(parents=True); (checkout / 'scripts' / 'build_apk_from_nsp.py').write_text('')
            (checkout / 'dm' / 'android').mkdir(parents=True)
            nested = checkout / 'somewhere' / 'deep'; nested.mkdir(parents=True)
            with unittest.mock.patch.object(self.pc, 'FROZEN', True), \
                 unittest.mock.patch.object(self.pc, 'REPO', Path('/nowhere')), \
                 unittest.mock.patch.object(self.pc, 'payload_home', lambda: Path(tmp) / 'no-payload-yet'), \
                 unittest.mock.patch.object(self.pc, 'payload_ready', lambda: False):   # no unpacked engine: the walk is the fallback
                old = os.getcwd(); os.chdir(nested)
                try:
                    self.assertEqual(self.pc.repo_dir(), checkout)   # the launch folder, then its parents
                    self.assertEqual(self.pc.repo_dir({'repo': ''}), checkout)   # nothing remembered: same walk
                finally: os.chdir(old)
                self.assertEqual(self.pc.repo_dir({'repo': str(checkout)}), checkout)   # --repo's choice

    def test_a_frozen_app_prefers_its_own_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'repo' / self.pc.VERSION; home.mkdir(parents=True)
            (home / 'scripts').mkdir(); (home / 'scripts' / 'build_apk_from_nsp.py').write_text('')
            (home / 'dm' / 'android').mkdir(parents=True)
            checkout = Path(tmp) / 'a-checkout-around'
            (checkout / 'scripts').mkdir(parents=True); (checkout / 'scripts' / 'build_apk_from_nsp.py').write_text('')
            (checkout / 'dm' / 'android').mkdir(parents=True)
            with unittest.mock.patch.object(self.pc, 'FROZEN', True), \
                 unittest.mock.patch.object(self.pc, 'payload_home', lambda: home), \
                 unittest.mock.patch.object(self.pc, 'payload_ready', lambda: True), \
                 unittest.mock.patch.object(self.pc, 'REPO', Path('/nowhere')):
                old = os.getcwd(); os.chdir(checkout)
                try:
                    self.assertEqual(self.pc.repo_dir(), home)   # the bundled engine, not whatever folder it sits in
                finally: os.chdir(old)
                self.assertEqual(self.pc.repo_dir({'repo': str(checkout)}), checkout)   # --repo still overrides it

    def test_the_exe_runs_helpers_as_python3_would(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / 'helper.py'
            script.write_text('import sys\nprint("argv:", len(sys.argv), sys.argv[0].endswith("helper.py"))\nsys.exit(3)\n')
            rc = None
            try: rc = self.pc.run_as_python(script, ['-k', 'prod.keys'])
            except SystemExit as e: self.fail(f'run_as_python leaked SystemExit {e.code}')
            self.assertEqual(rc, 3)   # the child's own exit code, not a crash
            self.assertEqual(sys.path[0], str(Path(script).resolve().parent))   # the script's folder, first, as python3 does

    def test_a_crash_in_a_helper_lands_in_the_events_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / 'boom.py'
            script.write_text('raise RuntimeError("boom in the pipeline")\n')
            events = Path(tmp) / 'events.jsonl'
            env = dict(os.environ, PROGRESS_FILE=str(events))
            with unittest.mock.patch.dict(os.environ, env):
                with self.assertRaises(RuntimeError):
                    self.pc.run_as_python(script, [])
            lines = [json.loads(l) for l in events.read_text().splitlines()]
            self.assertTrue(any(e.get('ev') == 'say' and 'RuntimeError: boom in the pipeline' in e.get('msg', '') for e in lines))
            self.assertTrue(any('traceback' in e.get('msg', '').lower() for e in lines))   # the why, not just the what

    def test_the_events_file_is_followed_from_before_it_exists(self):
        class Dead:   # a process that is not yet done
            def __init__(self): self.exits_at = time.time() + 1.2
            def poll(self): return 0 if time.time() >= self.exits_at else None
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / 'events.jsonl'
            def writer():
                time.sleep(0.3); events.write_text('{"ev": "stage", "name": "one"}\n', encoding='utf-8')
                time.sleep(0.3)
                with events.open('a', encoding='utf-8') as f: f.write('{"ev": "say", "msg": "two"}\n')
            t = threading.Thread(target=writer); t.start()
            try: got = [l for l in self.pc.follow_lines(events, Dead())]
            finally: t.join()
            self.assertEqual(len(got), 2)
            self.assertIn('"one"', got[0]); self.assertIn('"two"', got[1])   # in order, none lost at the exit

    def test_windows_children_stay_hidden(self):
        self.assertEqual(self.pc.NO_WINDOW, 0)   # from source, on any platform: children share the console
        saved_platform, saved_frozen = sys.platform, getattr(sys, 'frozen', None)
        subprocess.CREATE_NO_WINDOW = 0x08000000   # the Windows-only flag, emulated for the load
        try:
            sys.platform = 'win32'; sys.frozen = True
            win = load_pc()
            self.assertEqual(win.NO_WINDOW, subprocess.CREATE_NO_WINDOW)   # every child it spawns carries this
        finally:
            sys.platform = saved_platform
            if saved_frozen is None: del sys.frozen
            else: sys.frozen = saved_frozen
            del subprocess.CREATE_NO_WINDOW
        self.assertIn('creationflags=NO_WINDOW', (REPO / 'scripts/build_ipa_from_nsp.py').read_text())   # the pipeline hides its own children the same way

    def test_spawn_children_are_answered_before_anything_else(self):
        called = []
        with unittest.mock.patch.object(self.pc, 'FROZEN', True), \
             unittest.mock.patch('multiprocessing.freeze_support', side_effect=lambda: called.append(1)):
            with self.assertRaises(SystemExit):   # '--multiprocessing-fork' is no argument of ours: argparse's exit
                self.pc.main(['--multiprocessing-fork', 'pipe_handle=5'])
        self.assertEqual(called, [1])   # the spawn child was served before the window or argparse ever ran


class PayloadMode(unittest.TestCase):
    """The engine a standalone build carries inside itself: where payload.zip is found (inside the
    executable, at sys._MEIPASS, where --add-data put it), how it unpacks (once, modes and all), and
    what the machine is told when it is not there."""

    def setUp(self): self.pc = load_pc()

    def fake_payload(self, tmp):
        """A little payload.zip the way freeze.py makes one: the tree, the modes in the attributes."""
        import zipfile
        pz = Path(tmp) / 'payload.zip'
        with zipfile.ZipFile(pz, 'w', zipfile.ZIP_DEFLATED) as z:
            for mode, path, body in ((0o755, 'dm/android/gradlew', '#!/bin/sh\n'),
                                     (0o644, 'scripts/build_apk_from_nsp.py', 'print("the pipeline")\n'),
                                     (0o644, 'dm/android/app/src/main/AndroidManifest.xml', '<manifest/>\n')):
                zi = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
                zi.external_attr = mode << 16
                zi.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(zi, body)
        return pz

    def frozen(self, **kw):
        patches = [unittest.mock.patch.object(self.pc, 'FROZEN', True)]
        for k, v in kw.items(): patches.append(unittest.mock.patch.object(self.pc, k, v))
        enter = [p.__enter__() for p in patches]
        self.addCleanup(lambda: [p.__exit__(None, None, None) for p in reversed(patches)])

    def test_payload_zip_is_found_inside_the_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            inside = Path(tmp) / 'unpacked-self'; inside.mkdir()   # what sys._MEIPASS is: the app's one unpacking
            (inside / 'payload.zip').write_bytes(b'zip-inside')
            exe = Path(tmp) / 'nxcompanion' / 'nxcompanion.exe'
            exe.parent.mkdir(parents=True); exe.write_bytes(b'')
            (exe.parent / 'payload.zip').write_bytes(b'zip-beside')   # dropped there by hand
            self.frozen()
            with unittest.mock.patch.object(sys, '_MEIPASS', str(inside), create=True), \
                 unittest.mock.patch.object(sys, 'executable', str(exe)):
                self.assertEqual(self.pc.payload_zip(), inside / 'payload.zip')   # the one-file build's own place first
            (inside / 'payload.zip').unlink()
            with unittest.mock.patch.object(sys, '_MEIPASS', str(inside), create=True), \
                 unittest.mock.patch.object(sys, 'executable', str(exe)):
                self.assertEqual(self.pc.payload_zip(), exe.parent / 'payload.zip')   # ...or one dropped beside it
                (exe.parent / 'payload.zip').unlink()
                self.assertIsNone(self.pc.payload_zip())   # a broken build carries no engine at all
            with unittest.mock.patch.object(sys, 'executable', str(exe)):   # no _MEIPASS: not a one-file build
                self.assertIsNone(self.pc.payload_zip())

    def test_the_payload_unpacks_once_with_its_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            pz = self.fake_payload(tmp)
            home = Path(tmp) / 'repo' / self.pc.VERSION
            old = home.parent / '1.0.0'; old.mkdir(parents=True); (old / 'leftover').write_text('')
            self.frozen(payload_home=lambda: home, payload_ready=lambda: (home / '.payload').is_file(),
                        payload_zip=lambda: pz)
            seen = []
            got = self.pc.ensure_payload(progress=seen.append)
            self.assertEqual(got, home)
            self.assertTrue(seen and seen[-1] == 1.0)   # the progress ran all the way
            self.assertEqual((home / 'scripts' / 'build_apk_from_nsp.py').read_text(), 'print("the pipeline")\n')
            self.assertEqual((home / 'scripts' / 'build_apk_from_nsp.py').stat().st_mode & 0o777, 0o644)
            self.assertEqual((home / 'dm' / 'android' / 'gradlew').stat().st_mode & 0o777, 0o755)   # git's +x, kept
            self.assertEqual((home / '.payload').read_text().strip(), self.pc.VERSION)   # the stamp
            self.assertFalse(old.exists())   # the engine of a version that is gone went with it
            seen2 = []
            self.assertEqual(self.pc.ensure_payload(progress=seen2.append), home)
            self.assertEqual(seen2, [])   # the second call does nothing: the stamp already says so

    def test_no_payload_is_an_explanation(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'repo' / self.pc.VERSION
            self.frozen(payload_home=lambda: home, payload_ready=lambda: False, payload_zip=lambda: None)
            with self.assertRaises(RuntimeError) as cm:
                self.pc.ensure_payload()
            self.assertIn('re-download', str(cm.exception))

    def test_the_report_names_the_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            pz = self.fake_payload(tmp)
            home = Path(tmp) / 'repo' / self.pc.VERSION
            which = lambda name: f'/bin/{name}'
            self.frozen(payload_zip=lambda: pz, payload_home=lambda: home,
                        payload_ready=lambda: (home / '.payload').is_file())
            with unittest.mock.patch.object(self.pc.shutil, 'which', which), \
                 unittest.mock.patch.object(self.pc, 'run_out', return_value=(0, '')), \
                 unittest.mock.patch.dict(os.environ, {'ANDROID_HOME': ''}):
                row = next(r for r in self.pc.tool_report({}) if r['name'] == 'the build engine')
                self.assertTrue(row['ok']); self.assertIn('unpacks to', row['detail'])   # bundled, not yet unpacked
                home.mkdir(parents=True); (home / '.payload').write_text(self.pc.VERSION)
                row = next(r for r in self.pc.tool_report({}) if r['name'] == 'the build engine')
                self.assertTrue(row['ok']); self.assertIn('unpacked at', row['detail'])
                self.frozen(payload_zip=lambda: None, payload_home=lambda: home, payload_ready=lambda: False)
                with unittest.mock.patch.object(self.pc, 'repo_dir', lambda cfg=None: Path('/nowhere')):   # no checkout around either
                    row = next(r for r in self.pc.tool_report({}) if r['name'] == 'the build engine')
                self.assertFalse(row['ok']); self.assertIn('re-download', row['fix'])   # a broken build says so


class FreezeLists(unittest.TestCase):
    """freeze.py's module lists against what the pipeline actually imports: every third-party package
    any script the companion runs (itself, in place of python3) may import must be carried inside the
    executable, or the standalone build fails only on a user's machine, at run time."""

    SCRIPTS = ['scripts/build_apk_from_nsp.py', 'scripts/build_ipa_from_nsp.py',
               'dm/tools/patch_nso.py', 'dm/tools/nso_code.py', 'dm/tools/nso_asm.py', 'dm/tools/nso_syms.py',
               'dm/tools/nro_collect.py', 'dm/tools/insn_census.py', 'dm/tools/nacp.py', 'dm/tools/make_ext_font.py',
               'dm/games/profile_guess.py', 'dm/gfx/tools/shader_split.py', 'dm/gfx/tools/cpk.py',
               'dm/gfx/tools/unityfs.py', 'dm/gfx/tools/uepak.py', 'dm/gfx/tools/dmsh_chain.py',
               'dm/gfx/tools/dmsh_build.py', 'dm/gfx/tools/dmsh_geometry.py', 'dm/gfx/tools/dmsh_facts.py',
               'dm/gfx/tools/dmsh_pull.py', 'dm/gfx/tools/dmsh_vin.py', 'dm/gfx/tools/own_compilers.py',
               'dm/gfx/tools/shader_regressions.py', 'dm/android/tools/pack_apk.py',
               'dm/android/tools/gen_build_android.py']

    def setUp(self):
        spec = importlib.util.spec_from_file_location('freeze', REPO / 'dm/android/tools/freeze.py')
        self.fz = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.fz)

    def imports_of(self, path):
        import ast
        mods = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                mods.update(a.name.split('.')[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                mods.add(node.module.split('.')[0])
        return mods

    def test_every_third_party_import_is_carried(self):
        carried = {m.split('.')[0] for m in self.fz.HIDDEN + self.fz.COLLECT}
        missing = {}
        repo_dirs = ('scripts', 'dm/tools', 'dm/games', 'dm/gfx/tools', 'dm/android/tools', 'dm/ios/tools')
        for rel in self.SCRIPTS:
            p = REPO / rel
            if not p.is_file(): continue
            for m in self.imports_of(p):
                if m in sys.stdlib_module_names or m in ('build_ipa_from_nsp', 'compression'): continue
                   # 'compression': stdlib from Python 3.14, imported under try/except before that
                if not any((REPO / d / (m + '.py')).is_file() for d in repo_dirs):   # the repository's own modules run from the checkout
                    if m not in carried: missing.setdefault(m, []).append(rel)
        self.assertFalse(missing, f'freeze.py carries none of: {missing}')

    def test_the_lazy_ones_are_listed_by_name(self):
        self.assertIn('lz4.frame', self.fz.HIDDEN)   # shader_split.py imports it inside a function
        self.assertIn('lz4.block', self.fz.HIDDEN)    # patch_nso and shader_split, the NSO's compression
        if sys.version_info >= (3, 14): self.assertIn('compression.zstd', self.fz.HIDDEN)
        else: self.assertIn('backports.zstd', self.fz.COLLECT)   # shader_split's zstd_mod() imports it lazily
        for pkg in ('capstone', 'fontTools', 'nsz'):
            self.assertIn(pkg, self.fz.COLLECT)

    def test_the_mac_build_is_universal(self):
        src = (REPO / 'dm/android/tools/freeze.py').read_text()
        self.assertIn('universal2', src)   # the repository's own translator binaries are; the build matches

    def test_the_build_is_one_file_with_the_engine_inside(self):
        src = (REPO / 'dm/android/tools/freeze.py').read_text()
        self.assertIn("'--onefile'", src)   # a single file to download, not a folder to keep together
        self.assertIn("'--add-data'", src)   # the engine rides inside the executable (no payload beside it)
        self.assertIn("PAYLOAD_NAME = 'payload.zip'", src)   # ...at sys._MEIPASS/payload.zip once it runs
        self.assertNotIn("'_internal'", src)   # nothing anywhere expects a folder build anymore

    def test_the_payload_is_the_tree_with_its_modes(self):
        files = {p: m for m, p in self.fz.payload_files()}   # the path is the key: one mode per file
        self.assertIn('scripts/build_apk_from_nsp.py', files)   # the pipeline
        self.assertIn('dm/android/gradlew', files)              # the app build
        self.assertIn('dm/games/game.py', files)                # the profiles' reader
        self.assertEqual(files['dm/android/gradlew'], 0o755)     # git's +x, whatever the runner's disk did with it
        self.assertFalse(any(p.startswith('docs/') or p.startswith('.github/') for p in files))   # no build reads those
        self.assertTrue(any(p.startswith('dm/games/') for p in files))   # the game profiles ride along

    def test_the_payload_carries_both_shader_tools_for_the_linux_build(self):
        files = {p: m for m, p in self.fz.payload_files()}
        self.assertIn('dm/gfx/tools/dmsh_xlate_bin/dmsh_xlate.dll', files)   # the translator runs through dotnet on Linux
        self.assertEqual(files.get('dm/gfx/tools/spirv_cross_bin/spirv-cross-linux-x86_64'), 0o755)   # committed, executable
        self.assertEqual(files.get('dm/gfx/tools/spirv_cross_bin/spirv-cross'), 0o755)   # the Mac's universal build


class ProgressFile(unittest.TestCase):
    """The events file a windowed build reads instead of stdout: build_ipa_from_nsp.py writes every
    JSON event also there, and only there when the driver sets PROGRESS_FILE."""

    def test_every_event_lands_in_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / 'events.jsonl'
            with unittest.mock.patch.dict(os.environ, PROGRESS_FILE=str(events)):
                spec = importlib.util.spec_from_file_location('ipa_progfile', REPO / 'scripts/build_ipa_from_nsp.py')
                ios = importlib.util.module_from_spec(spec)
                with unittest.mock.patch('sys.stdout', new=None):   # a windowed child: print can do nothing
                    spec.loader.exec_module(ios)
                    ios.PROG._json({'ev': 'say', 'msg': 'one'})
                    ios.PROG._json({'ev': 'stage', 'k': 1, 'n': 2, 'name': 'two'})
            self.assertEqual([json.loads(l)['ev'] for l in events.read_text().splitlines()], ['say', 'stage'])


if __name__ == '__main__':
    unittest.main()
