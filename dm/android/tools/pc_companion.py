#!/usr/bin/env python3
"""The PC companion: pick the NSPs and prod.keys on the computer, get an installed game on the phone.

One window over the whole APK pipeline (scripts/build_apk_from_nsp.py): the toolchain is checked and
guided into place, the phone shows up over USB (adb), and one button runs the build - identify the
game, extract, rewrite the modules, translate the shaders, pack the app - then aligns the APK for
16 KB-page phones, signs it with a personal key made on first use (~/.nxcompanion, so every update of
the same game installs over the last) and installs it with adb. Nothing is launched: the game's icon
lands on the home screen, the player taps it.

The phone's receiver screen rides along: plugging the phone in raises it (adb shell am start), and
the build's stages stream to it live (adb forward onto the app's localabstract:nxnative) - so the
player watches the game arrive on the phone they will play it on. It is the screen's content, never
a step the build waits on: pull the cable mid-build and everything on the computer still finishes.

A standalone build (the Releases page; dm/android/tools/freeze.py) is a single file that needs nothing
else of the project: it carries the whole build engine inside itself (payload.zip, every file git
tracks but the docs and the CI workflows), unpacks it once into ~/.nxcompanion/repo/<version> and
runs the pipeline from there. --repo still points it at a checkout instead, for development.
What every build wants from the machine itself - Java, the Android SDK, .NET, bash, make, python3,
zstd - the Toolchain page checks and guides in. Windows: the window runs by itself; the build steps
want bash, make and python3 (MSYS2 provides them, or the whole build inside WSL2), and adb sees a
USB phone once usbipd-win attaches it (the toolchain page has the commands).

Usage (Python 3.10+, no pip packages needed for the window itself):
    python3 dm/android/tools/pc_companion.py          the window
    python3 dm/android/tools/pc_companion.py --check  the same toolchain and device report on stdout
"""
import argparse
import json
import os
import queue
import re
import runpy
import secrets
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

FROZEN = hasattr(sys, 'frozen')   # a standalone build (dm/android/tools/freeze.py): this file is the app, not a checkout
REPO = Path.cwd() if FROZEN else Path(__file__).resolve().parents[3]
   # from source: the checkout this file lives in; a standalone build is one file that carries its
   # own copy of the repository instead (the payload: payload.zip inside the executable, unpacked
   # into ~/.nxcompanion/repo/<version> on first use - see ensure_payload - and found by repo_dir)
VERSION = '1.1.0'   # the standalone builds' version (freeze.py names the releases after it)
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if (sys.platform == 'win32' and FROZEN) else 0
   # the standalone Windows build is a window app: without the flag every console child it spawns
   # (adb, java, gradlew, ...) flashes its own console window; the output flows through the pipes as before
CONFIG_DIR = Path(os.environ.get('NXC_HOME') or Path.home() / '.nxcompanion')


def _trace(msg):
    """A frozen child's startup steps, on record when NXC_TRACE names a file: a windowed child prints
    nowhere, so the diagnosis of a spawn handshake that never completes runs on these lines."""
    t = os.environ.get('NXC_TRACE')
    if t:
        try:
            with open(t, 'a', encoding='utf-8') as f: f.write(f'{time.time():.1f} pid {os.getpid()}: {msg}\n')
        except OSError: pass
NDK_VERSION, CMAKE_VERSION, PLATFORM, BT_MIN = '26.3.11579264', '3.22.1', 'android-34', 35 * 10000   # what dm/android builds with (build.gradle.kts); BT_MIN: build-tools 35.0.0, the first whose zipalign knows -P 16
LANGUAGES = ['en-US', 'ja', 'fr', 'de', 'es', 'zh-Hans', 'zh-Hant', 'ko', 'it', 'pt-BR', 'ru']   # -l takes any code; these are the common ones
SDK_STEPS = ('https://developer.android.com/studio#command-line-tools-only: download the "Command line tools only" zip, '
             'unzip it to ~/android-sdk/cmdline-tools/latest (licenses are accepted on first install), then reopen this check')

# The phone's receiver screen (dm/android ReceiverActivity / ReceiverLink): the socket adb forward
# pairs with, the activity am start raises (the namespace, not the per-game applicationId), and the
# template build's own id - the shell app, the one the screen belongs to
PHONE_SOCKET = 'localabstract:nxnative'
PHONE_ACTIVITY = 'org.nxnative.android.ReceiverActivity'
SHELL_ID = 'org.nxnative.shell'
PROFILE = '010015100B514000'   # a committed profile the host compiles against for the template (the CI workflow's own GAME)
PING_SECS, LINK_TIMEOUT = 2.0, 8.0   # the link's heartbeat; either end gives up after about four missed


# ---- The companion's own settings (~/.nxcompanion/config.json: the SDK path, the signing key's passwords, the last form state) ----

def load_config(home=None):
    home = Path(home) if home else CONFIG_DIR
    try: cfg = json.loads((home / 'config.json').read_text(encoding='utf-8'))
    except (OSError, ValueError): cfg = {}
    cfg.setdefault('sdk', os.environ.get('ANDROID_HOME', ''))
    cfg.setdefault('alias', 'nxnative')
    cfg.setdefault('store_password', ''); cfg.setdefault('key_password', '')
    cfg.setdefault('language', 'en-US'); cfg.setdefault('show_fps', True); cfg.setdefault('app_name', '')
    cfg.setdefault('last_nsp', ''); cfg.setdefault('last_keys', '')
    cfg.setdefault('repo', '')   # a standalone build's repository folder (from source it is this checkout)
    return cfg


def save_config(cfg, home=None):
    home = Path(home) if home else CONFIG_DIR; home.mkdir(parents=True, exist_ok=True)
    tmp = home / 'config.json.tmp'; tmp.write_text(json.dumps(cfg, indent=1), encoding='utf-8')
    os.replace(tmp, home / 'config.json')   # the passwords are in here: never half-written
    try: os.chmod(home / 'config.json', 0o600)
    except OSError: pass


# ---- The toolchain report: every piece the build needs, with a fix for each miss ----

def _importable(name):
    try: __import__(name); return True
    except ImportError: return False


def _is_repo_dir(p):
    p = Path(p)
    return (p / 'scripts' / 'build_apk_from_nsp.py').is_file() and (p / 'dm' / 'android').is_dir()


# ---- The payload: the repository a standalone build carries inside itself ----

def payload_zip():
    """The zip a standalone build unpacked its repository from: inside the executable (a one-file
    build unpacks itself into a private temporary folder on start, and the payload rides there,
    at sys._MEIPASS) - or one dropped beside the executable by hand. None when this is no such
    build or neither is there - the toolchain report says which, and ensure_payload says it again."""
    if not FROZEN: return None
    inside = Path(getattr(sys, '_MEIPASS', '') or '') / 'payload.zip'   # where --add-data put it
    if inside.is_file(): return inside
    beside = Path(sys.executable).resolve().parent / 'payload.zip'   # dropped there by hand
    if beside.is_file(): return beside
    return None


def payload_home():
    """Where the payload unpacks: ~/.nxcompanion/repo/<version> - versioned, so a new app replaces
    its old engine (and nothing else of the user's is touched)."""
    return CONFIG_DIR / 'repo' / VERSION


def payload_ready():
    """True once the payload has unpacked (the stamp the unpacking leaves behind)."""
    return (payload_home() / '.payload').is_file()


_PAYLOAD_LOCK = threading.Lock()   # the window's unpack thread and the build worker may both unpack


def ensure_payload(progress=None):
    """Unpack the payload once, and hand back the repository it became. The modes git gave the files
    (gradlew, the translator binaries) are restored from the zip's own attributes; a stamp (.payload)
    marks a complete unpack, a leftover .tmp of a killed run is redone from scratch, and the versions
    before this one go away. progress(fraction) hears how far along it is."""
    home = payload_home()
    if payload_ready(): return home
    pz = payload_zip()
    if not pz:
        raise RuntimeError('this build carries no build engine (payload.zip is missing inside the executable):'
                           ' re-download the app')
    with _PAYLOAD_LOCK:
        if payload_ready(): return home   # a concurrent unpack finished first
        home.parent.mkdir(parents=True, exist_ok=True)
        tmp = home.parent / f'.{home.name}.tmp{os.getpid()}'
        shutil.rmtree(tmp, ignore_errors=True)
        with zipfile.ZipFile(pz) as z:
            infos = [i for i in z.infolist() if not i.is_dir()]   # the tree grows from its files
            for n, zi in enumerate(infos):
                z.extract(zi, tmp)
                mode = (zi.external_attr >> 16) & 0o7777
                if mode: os.chmod(tmp / zi.filename, mode)
                if progress and (n % 25 == 0 or n == len(infos) - 1): progress((n + 1) / len(infos))
        if home.exists(): shutil.rmtree(home, ignore_errors=True)   # a half-unpacked older attempt
        tmp.rename(home)
        (home / '.payload').write_text(VERSION, encoding='utf-8')
        for old in home.parent.iterdir():   # the engine of a version that is not this one
            if old != home and old.is_dir() and re.fullmatch(r'[\d.]+', old.name):
                shutil.rmtree(old, ignore_errors=True)
        return home


def find_repo_around(start):
    """The nxnative repository at or above a folder."""
    start = Path(start).resolve()
    for cand in [start, *start.parents]:
        if _is_repo_dir(cand): return cand
    return None


def repo_dir(cfg=None):
    """Where the repository is. From source, the checkout this file lives in; a standalone build
    carries its own (the payload, unpacked into ~/.nxcompanion/repo/<version>), and --repo or the
    config's folder still names a checkout instead - a developer pointing the app at their own tree.
    This decides, it never unpacks: ensure_payload (the build worker, --check, the window's unpack
    thread) does that, on its own time."""
    if not FROZEN: return REPO
    for cand in [cfg.get('repo') if cfg else None, os.environ.get('NXC_REPO')]:
        if not cand: continue
        if _is_repo_dir(cand): return Path(cand)
        around = find_repo_around(cand)
        if around: return around
    if payload_ready(): return payload_home()
    for cand in [Path(sys.executable).parent, os.getcwd()]:   # a build dropped into a checkout (its own engine is missing)
        if _is_repo_dir(cand): return Path(cand)
        around = find_repo_around(cand)
        if around: return around
    return payload_home()   # not unpacked yet: the report's engine row says so, the next build unpacks


def sdk_status(sdk):
    """The SDK side of the report: adb, the NDK, CMake, platform 34, build-tools with zipalign -P 16."""
    sdk = Path(sdk) if sdk else None
    if not sdk or not sdk.is_dir():
        return {'name': 'Android SDK', 'ok': False, 'detail': 'not found', 'fix': SDK_STEPS, 'fix_cmd': None}
    miss = []
    if not (sdk / 'platform-tools' / ('adb.exe' if os.name == 'nt' else 'adb')).exists(): miss.append('platform-tools (adb)')
    if not (sdk / 'ndk' / NDK_VERSION).is_dir(): miss.append(f'ndk;{NDK_VERSION}')
    if not (sdk / 'cmake' / CMAKE_VERSION).is_dir(): miss.append(f'cmake;{CMAKE_VERSION}')
    if not (sdk / 'platforms' / PLATFORM).is_dir(): miss.append(f'platforms;{PLATFORM}')
    if not bt_dir(sdk): miss.append('build-tools;35.0.0 (zipalign -P 16)')
    return {'name': 'Android SDK', 'ok': not miss, 'detail': str(sdk) + ('' if not miss else ' - missing ' + ', '.join(miss)),
            'fix': None if not miss else 'the Install button runs sdkmanager, or by hand: sdkmanager ' + ' '.join(f'"{p}"' for p in sdk_packages()),
            'fix_cmd': [str(sdkmanager_path(sdk))] + sdk_packages() if sdkmanager_path(sdk) else None}


def sdk_packages():
    return ['platform-tools', f'ndk;{NDK_VERSION}', f'cmake;{CMAKE_VERSION}', f'platforms;{PLATFORM}', 'build-tools;35.0.0']


def sdkmanager_path(sdk):
    sdk = Path(sdk)
    for p in (sdk / 'cmdline-tools' / 'latest' / 'bin' / 'sdkmanager', sdk / 'cmdline-tools' / 'latest' / 'bin' / 'sdkmanager.bat'):
        if p.exists(): return p
    return None


def bt_dir(sdk):
    """The newest build-tools whose zipalign knows -P 16 (35.0.0 or later); None when there is none."""
    best, best_v = None, -1
    d = Path(sdk) / 'build-tools'
    if not d.is_dir(): return None
    for p in d.iterdir():
        m = re.match(r'(\d+)\.(\d+)\.(\d+)$', p.name)
        if not m: continue
        v = int(m.group(1)) * 10000 + int(m.group(2)) * 100 + int(m.group(3))
        if v >= BT_MIN and v > best_v and (p / 'zipalign').exists(): best, best_v = p, v
    return best


def java_version(output):
    """'17' from openjdk version "17.0.12" ... (any line, version may sit on stderr); None when unreadable."""
    m = re.search(r'version "(\d+)', output)
    return int(m.group(1)) if m else None


def tool_report(cfg, which=shutil.which):
    """The whole guided list: what is there, what is missing, and the fix for each miss."""
    rows = []
    repo = repo_dir(cfg)
    if FROZEN:
        # the engine a standalone build carries inside itself: bundled (payload.zip, embedded in
        # the executable) and unpacked on first use - or, dropped into a checkout, that checkout instead
        if payload_zip():
            rows.append({'name': 'the build engine', 'ok': True,
                         'detail': f'unpacked at {payload_home()}' if payload_ready() else
                                   f'carried inside this app, unpacks to {payload_home()} on the first build (once)',
                         'fix': None, 'fix_cmd': None})
        else:
            rows.append({'name': 'the build engine', 'ok': _is_repo_dir(repo),
                         'detail': f'the checkout at {repo}' if _is_repo_dir(repo) else 'missing',
                         'fix': None if _is_repo_dir(repo) else
                                'this build is incomplete (no payload.zip inside the executable): re-download the app, '
                                'or run it inside a checkout',
                         'fix_cmd': None})
    else:
        rows.append({'name': 'the build engine', 'ok': _is_repo_dir(repo),
                     'detail': f'the checkout at {repo}',
                     'fix': None if _is_repo_dir(repo) else 'run from a checkout of the repository',
                     'fix_cmd': None})
    if FROZEN:
        py3 = which('python3')   # the Gradle and shell build steps run system python3; the pipeline's own modules are carried inside
        rows.append({'name': 'python3', 'ok': bool(py3),
                     'detail': f'{py3} (the build steps run it; the pipeline\'s Python is inside this app)' if py3 else 'missing - the Gradle and shell build steps run python3',
                     'fix': None if py3 else 'any Python 3 (python.org, the package manager, MSYS2 or the Store alias on Windows)',
                     'fix_cmd': None})
    else:
        py_miss = [m for m in ('lz4.block', 'capstone', 'fontTools') if not _importable(m)] + \
                  ([] if _importable('compression.zstd') or _importable('backports.zstd') else ['backports.zstd'])
        rows.append({'name': 'Python packages', 'ok': not py_miss,
                     'detail': f'in {sys.executable}' if not py_miss else 'missing ' + ', '.join(py_miss),
                     'fix': None if not py_miss else 'pip install lz4 capstone fonttools' + (' backports.zstd' if sys.version_info < (3, 14) else ''),
                     'fix_cmd': None if not py_miss else [sys.executable, '-m', 'pip', 'install', 'lz4', 'capstone', 'fonttools'] + (['backports.zstd'] if sys.version_info < (3, 14) else [])})
    for tool, how in (('bash', 'everywhere on the Mac and Linux; on Windows MSYS2 or Git Bash provide it (or the whole build in WSL2)'),
                      ('make', 'the Xcode command line tools on the Mac (xcode-select --install), build-essential on Ubuntu, MSYS2 on Windows'),
                      ('zstd', 'brew install zstd, apt install zstd, or winget install zstd'),
                      ('dotnet', 'the .NET SDK 10: brew install --cask dotnet-sdk, or see dotnet.microsoft.com/download')):
        found = which(tool)
        rows.append({'name': tool, 'ok': bool(found), 'detail': str(found or '-'), 'fix': None if found else how, 'fix_cmd': None})
    jv = which('java')
    jver = None
    if jv:
        try: jver = java_version(subprocess.run([jv, '-version'], capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW).stderr or '')
        except (OSError, subprocess.SubprocessError): pass
    rows.append({'name': 'Java 17+', 'ok': bool(jv and jver and jver >= 17),
                 'detail': f'{jv} (version {jver})' if jv else '-',
                 'fix': None if jv and jver and jver >= 17 else 'Gradle needs Java 17+: brew install --cask temurin@17, or see adoptium.net (Windows: inside WSL)', 'fix_cmd': None})
    rows.append(sdk_status(cfg.get('sdk') or os.environ.get('ANDROID_HOME')))
    xlate = repo / 'dm/gfx/tools/dmsh_xlate_bin/dmsh_xlate'
    scross = repo / 'dm/gfx/tools/spirv_cross_bin/spirv-cross'
    both = xlate.is_file() and scross.is_file()
    runnable = both and (sys.platform == 'darwin' or not run_out([str(xlate), '--source-sha'], timeout=30)[0])
    rows.append({'name': 'the build engine\'s translator and spirv-cross', 'ok': runnable,
                 'detail': ('in the engine' if both else 'missing from the engine') +
                           ('' if runnable or not both else ' - they are macOS binaries; they cannot run here'),
                 'fix': None if runnable else (('unpack it again (delete ' + str(payload_home()) + ') and let the app redo it'
                                                if FROZEN and payload_zip() else 'git checkout / clone the repository again') if not both else
                         'they are the Mac binaries the shader chain is locked to: a game\'s shader step needs a Mac today, the steps before it run here'),
                 'fix_cmd': None})
    return rows


# ---- The phone over USB ----

def adb_path(cfg):
    p = Path(cfg.get('sdk') or '') / 'platform-tools' / ('adb.exe' if os.name == 'nt' else 'adb')
    if p.exists(): return str(p)
    return shutil.which('adb')


def parse_devices(text):
    """[(serial, state, model)] from `adb devices -l`: the state is 'device' when the phone has accepted."""
    out = []
    for ln in text.splitlines()[1:]:
        parts = ln.split()
        if len(parts) < 2 or '*' in ln: continue
        model = next((p[6:] for p in parts[2:] if p.startswith('model:')), parts[0])
        out.append((parts[0], parts[1], model.replace('_', ' ')))
    return out


def list_devices(cfg):
    adb = adb_path(cfg)
    if not adb: return None   # no adb at all: the toolchain page is where that is fixed
    try: r = subprocess.run([adb, 'devices', '-l'], capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError): return []
    return parse_devices(r.stdout)


# ---- The build: one command, the APK it leaves, and the three that finish it ----

def pipeline_command(nsp_dir, keys, language=None, show_fps=True, app_name=None, repo=None):
    """The pipeline as one command. From source it is python3 + the script; a standalone companion
    runs the script in itself (the executable takes a .py as its first argument - run_as_python)."""
    repo = repo or repo_dir()
    cmd = [sys.executable, str(repo / 'scripts' / 'build_apk_from_nsp.py'), str(nsp_dir), '-k', str(keys)]
    if language: cmd += ['-l', language]
    if app_name: cmd += ['--app-name', app_name]
    if not show_fps: cmd += ['--no-show-fps']
    return cmd


def find_built_apks(nsp_dir, since):
    """The APKs the build wrote beside the NSPs (its default output: the game's name), newest first."""
    d = Path(nsp_dir)
    if not d.is_dir(): return []
    return sorted((p for p in d.glob('*.apk') if p.stat().st_mtime >= since), key=lambda p: p.stat().st_mtime, reverse=True)


def keytool_gen_command(keytool, keystore, store_password, key_password, alias):
    return [keytool, '-genkeypair', '-keystore', str(keystore), '-alias', alias, '-keyalg', 'RSA', '-keysize', '4096',
            '-validity', '10950', '-storepass', store_password, '-keypass', key_password, '-dname', 'CN=nxnative']


def zipalign_command(bt, src, dst):
    return [str(Path(bt) / 'zipalign'), '-f', '-P', '16', '4', str(src), str(dst)]   # the phone maps the .so straight from the APK: 16 KB


def apksigner_command(bt, keystore, store_password, key_password, alias, src, dst):
    return [str(Path(bt) / 'apksigner'), 'sign', '--ks', str(keystore), '--ks-key-alias', alias,
            '--ks-pass', f'pass:{store_password}', '--key-pass', f'pass:{key_password}', '--out', str(dst), str(src)]


def adb_install_command(adb, serial, apk):
    return [adb] + (['-s', serial] if serial else []) + ['install', '-r', str(apk)]


# ---- The phone's receiver screen ----

def am_start_command(adb, serial, pkg):
    """Raise the receiver on the phone: the manifest's activity under the app's own applicationId
    (a game's id differs from the namespace, so the component is spelled out in full)."""
    return [adb, '-s', serial, 'shell', 'am', 'start', '-n', f'{pkg}/{PHONE_ACTIVITY}']


def forward_command(adb, serial):
    return [adb, '-s', serial, 'forward', 'tcp:0', PHONE_SOCKET]


def forward_remove_command(adb, serial):
    return [adb, '-s', serial, 'forward', '--remove-all']


def parse_forward_port(text):
    """The port adb picked for `forward tcp:0` (it prints just the number); None when adb said
    anything else - an error line with a digit in it must not read as a port."""
    m = re.fullmatch(r'\s*(\d+)\s*', text or '')
    return int(m.group(1)) if m else None


def pick_package(lines):
    """The app on the phone to raise the receiver on, from `pm list packages org.nxnative`: the shell
    (the template build, the receiver proper) first, any game app (its own applicationId) otherwise.
    None means the phone holds no nxnative app yet - the window offers to install the template."""
    pkgs = [ln.split(':', 1)[1].strip() for ln in lines if ln.strip().startswith('package:')]
    pkgs = [p for p in pkgs if p == 'org.nxnative' or p.startswith('org.nxnative.')]
    return SHELL_ID if SHELL_ID in pkgs else (pkgs[0] if pkgs else None)


def phone_status(ev):
    """The receiver-screen message for one PROGRESS=json event of the pipeline; None when the event
    is not for the phone (say, stage_end)."""
    kind = ev.get('ev')
    if kind in ('stage', 'step'):
        return {'event': 'status', 'state': 'building', 'stage': str(ev.get('name') or ''), 'detail': str(ev.get('step') or '')}
    if kind == 'progress':
        det = str(ev.get('step') or '')
        if ev.get('done') is not None and ev.get('total'):
            det = f"{ev.get('done'):,} / {ev.get('total'):,}" + (f" {ev.get('unit')}" if ev.get('unit') else '')
        frac = ev.get('frac')
        msg = {'event': 'status', 'state': 'building', 'stage': str(ev.get('name') or ''), 'detail': det}
        if isinstance(frac, (int, float)): msg['pct'] = round(frac * 100)   # absent: the phone's bar goes indeterminate
        return msg
    return None


def receiver_build_command(gradlew):
    """The template build - the same Gradle the pipeline runs, with no game staged - for a phone that
    holds no nxnative app at all."""
    return [str(gradlew), 'assembleRelease', f'-PDM_APP_ID={SHELL_ID}', '--no-daemon']


def find_receiver_apk(android_dir):
    """The template build's APK: app-release.apk (a machine with dm/android/keystore.properties)
    or app-release-unsigned.apk - the newer one when both are lying around."""
    d = Path(android_dir) / 'app' / 'build' / 'outputs' / 'apk' / 'release'
    cands = [p for p in (d / 'app-release.apk', d / 'app-release-unsigned.apk') if p.exists()]
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


class PhoneLink(threading.Thread):
    """The phone's receiver screen over adb: forward a TCP port onto the app's abstract socket, then
    JSON lines - hello, the build's stages, the install - with a ping every PING_SECS so both ends
    know the moment the cable, the app or the computer goes away. Either end dropping is not an
    error: the link waits and tries again (the auto redetect), and the newest status replaces any
    queued one, so the screen always shows the last word. It never gates the build or the install."""

    def __init__(self, adb, serial, on_state=lambda live: None, on_ready=None, game=''):
        super().__init__(daemon=True, name='phone-link')
        self.adb, self.serial, self.on_state, self.on_ready, self.game = adb, serial, on_state, on_ready, game
        self.lock = threading.Lock(); self.pending = []
        self.stop_flag = threading.Event()

    def send(self, obj):
        with self.lock:
            if obj.get('event') == 'status':   # a newer status is the only one worth showing
                self.pending = [m for m in self.pending if m.get('event') != 'status']
            self.pending.append(obj)

    def stop(self):
        self.stop_flag.set()

    def run(self):
        try:
            while not self.stop_flag.is_set():
                self.pump()
                if not self.stop_flag.is_set(): time.sleep(1.5)
        finally:
            try: run_out(forward_remove_command(self.adb, self.serial), timeout=10)
            except Exception: pass
            self.on_state(False)

    def connect(self):
        rc, out = run_out(forward_command(self.adb, self.serial), timeout=10)
        port = parse_forward_port(out) if not rc else None
        if not port: return None
        try:
            sock = socket.create_connection(('127.0.0.1', port), timeout=3)
            sock.setblocking(False)
            return sock
        except OSError:
            run_out(forward_remove_command(self.adb, self.serial), timeout=10)
            return None

    def pump(self):
        sock = self.connect()
        if not sock: return
        live = False
        try:
            hello = {'event': 'hello'}
            if self.game: hello['game'] = self.game
            self._write(sock, hello)
            self._write(sock, {'event': 'ping'})
            last_ping = last_pong = time.time()
            buf = b''
            while not self.stop_flag.is_set():
                now = time.time()
                if now - last_ping >= PING_SECS:
                    if not self._write(sock, {'event': 'ping'}): break
                    last_ping = now
                if now - last_pong > LINK_TIMEOUT: break   # the receiver stopped answering
                with self.lock:
                    out, self.pending = self.pending, []
                if not all(self._write(sock, m) for m in out): break
                try: r, _, _ = select.select([sock], [], [], 0.2)
                except (OSError, ValueError): break
                if sock not in r: continue
                try: chunk = sock.recv(4096)
                except (BlockingIOError, InterruptedError): continue
                except OSError: break
                if not chunk: break   # the app closed its end (or was replaced by an install)
                buf += chunk
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    try: msg = json.loads(line.decode('utf-8', 'replace'))
                    except ValueError: continue
                    ev = msg.get('event')
                    if ev == 'pong': last_pong = time.time()
                    elif ev == 'ready' and self.on_ready: self.on_ready(bool(msg.get('hasGame')))
                    if not live: live = True; self.on_state(True)   # the receiver answered: it is really there
        finally:
            try: sock.close()
            except OSError: pass
            run_out(forward_remove_command(self.adb, self.serial), timeout=10)
            if live: self.on_state(False)

    def _write(self, sock, obj):
        data = (json.dumps(obj, ensure_ascii=False) + '\n').encode('utf-8')
        for _ in range(50):   # a full send buffer drains in a moment; never spin for long
            try:
                sock.sendall(data); return True
            except BlockingIOError:
                time.sleep(0.02)
            except (OSError, ValueError):
                return False
        return False


def run_out(cmd, env=None, timeout=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout, creationflags=NO_WINDOW)
        return r.returncode, (r.stdout or '') + (r.stderr or '')
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


def ensure_keystore(cfg, log=lambda m: None):
    """The personal signing key, made once with keytool and kept in ~/.nxcompanion; the passwords are
    random on first use and saved to the config (the same key signs every later update of every game)."""
    home = CONFIG_DIR; home.mkdir(parents=True, exist_ok=True)
    ks = home / 'release.keystore'
    if ks.exists() and cfg.get('store_password') and cfg.get('key_password'): return ks, cfg
    keytool = shutil.which('keytool')
    if not keytool: raise RuntimeError('keytool not found: Java is needed to make the signing key')
    if not cfg.get('store_password'): cfg['store_password'] = secrets.token_urlsafe(18)
    if not cfg.get('key_password'): cfg['key_password'] = secrets.token_urlsafe(18)
    log('making the personal signing key (once): ' + str(ks))
    rc, out = run_out(keytool_gen_command(keytool, ks, cfg['store_password'], cfg['key_password'], cfg['alias']))
    if rc or not ks.exists(): raise RuntimeError('keytool failed: ' + out.strip()[-400:])
    save_config(cfg)
    return ks, cfg


# ---- --check: the same report on stdout, for the terminal and for the tests ----

def _attach_console():
    """A standalone Windows build is a window app: launched from a terminal it attaches to that
    terminal so --check prints there (an Explorer launch has no console; a pipe keeps its own
    handle, so a parent reading the output is never cut off either)."""
    if sys.platform != 'win32' or not FROZEN or sys.stdout is None: return
    try:
        import ctypes
        if ctypes.windll.kernel32.AttachConsole(-1):   # ATTACH_PARENT_PROCESS
            sys.stdout = open('CONOUT$', 'w', encoding='utf-8', errors='replace')
            sys.stderr = sys.stdout
    except (OSError, AttributeError, ValueError): pass


def run_as_python(script, args):
    """Run a repository script exactly as python3 would, in this process. A standalone companion
    is the pipeline's interpreter: every helper the pipeline spawns is [this executable, <script.py>,
    ...] - the same shape it used for python itself - and this runs it with the script's folder
    first on sys.path, its own sys.argv, and __name__ == '__main__'."""
    _trace(f'run_as_python {script}')
    if sys.stdout is None:   # a windowed build carries no stdout of its own; when the parent gave the
        # process one (a pipe), fd 1 is still there to take over, and the pipeline's events flow through it
        try: sys.stdout = open(1, 'w', encoding='utf-8', errors='replace', closefd=False)
        except (OSError, ValueError): sys.stdout = open(os.devnull, 'w', encoding='utf-8', errors='replace')
    if sys.stderr is None: sys.stderr = sys.stdout
    script = str(Path(script).resolve())
    if FROZEN:
        # CPython never tells a frozen Windows app's spawn children where the running script lives
        # (spawn.py: WINEXE sends no init_main_from_path), so the functions those scripts define in
        # __main__ - the shader split's workers - cannot be unpickled in the child: it dies on
        # "Can't get attribute 'scan_file' on __main__'. Send the script's path the way an unfrozen
        # run does; everywhere else the real one already does, and setdefault leaves it be.
        import multiprocessing.spawn
        real_get_preparation_data = multiprocessing.spawn.get_preparation_data

        def get_preparation_data_with_path(name):
            d = real_get_preparation_data(name)
            d.setdefault('init_main_from_path', script)
            return d

        multiprocessing.spawn.get_preparation_data = get_preparation_data_with_path
    sys.path.insert(0, str(Path(script).parent))
    sys.argv = [script, *args]
    try:
        runpy.run_path(script, run_name='__main__')
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except BaseException:   # a windowed child has no stderr to print a crash on: the traceback goes to the
        # event file the driver reads (when this is the pipeline), then on to whoever else can hear it
        import traceback
        tb = traceback.format_exc()
        pf = os.environ.get('PROGRESS_FILE')
        if pf:
            try:
                with open(pf, 'a', encoding='utf-8') as f: f.write(json.dumps({'ev': 'say', 'msg': tb.rstrip()[-4000:]}) + '\n')
            except OSError: pass
        try: sys.stderr.write(tb)
        except (OSError, ValueError, AttributeError): pass
        raise
    return 0


def follow_lines(path, proc):
    """The pipeline's event lines from its PROGRESS_FILE, one by one and in order: the file may not
    exist yet (it is created on the first event), and the reader stops only once the pipeline has
    exited and the file has nothing more. A standalone build reads this instead of the child's
    stdout: a windowed executable has none even through a pipe (the bootloader wires none)."""
    f = None
    try:
        while True:
            if f is None:
                if path.is_file(): f = open(path, encoding='utf-8', errors='replace')
                elif proc.poll() is not None: return   # no events at all
                else: time.sleep(0.25); continue
            line = f.readline()
            if line: yield line; continue
            if proc.poll() is not None:
                time.sleep(0.1); line = f.readline()   # the last write can land just before the exit
                if line: yield line
                return
            time.sleep(0.25)
    finally:
        if f is not None: f.close()


def run_check(argv):
    _attach_console()
    cfg = load_config()
    if argv.sdk:
        cfg['sdk'] = str(Path(argv.sdk).resolve()); save_config(cfg)
    if argv.repo:
        cfg['repo'] = str(Path(argv.repo).resolve()); save_config(cfg)
    if FROZEN and payload_zip() and not payload_ready():
        print(f'unpacking the build engine (once, into {payload_home()}) ...')
        ensure_payload(progress=lambda f: print(f'\r  {f:4.0%}', end='', flush=True))
        print()
    rows = tool_report(cfg)
    print(f'nxnative PC companion {VERSION} - toolchain report ({sys.platform})')
    for r in rows:
        print(f"  {'ok  ' if r['ok'] else 'MISS'} {r['name']}: {r['detail']}")
        if not r['ok'] and r['fix']: print(f'        fix: {r["fix"]}')
    dev = list_devices(cfg)
    adb = adb_path(cfg)
    if dev is None: print('  phone: no adb (the SDK above)')
    elif not dev: print('  phone: none connected - plug it in over USB and enable USB debugging')
    else:
        for serial, state, model in dev: print(f'  phone: {model} ({serial}) {state}')
        for serial, state, model in [d for d in dev if d[1] == 'device' and adb]:
            rc, out = run_out([adb, '-s', serial, 'shell', 'pm', 'list', 'packages', 'org.nxnative'], timeout=15)
            pkg = pick_package(out.splitlines()) if not rc else None
            print(f'  receiver app on it: {pkg}' if pkg
                  else '  receiver app: none on this phone - the companion window offers to install the template')
    ok = all(r['ok'] for r in rows)
    print('ready' if ok else 'not ready - see the fixes above')
    return 0 if ok else 1


# ---- The window ----
def run_gui(argv):
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from tkinter.scrolledtext import ScrolledText
    import tkinter.font as tkfont

    cfg = load_config()

    # The house style, sized for a window someone uses twice: a quiet surface, white cards with a
    # hairline, one accent color, and nothing that is not a step - the pipeline's detail stays one
    # click down, in the log; the options behind their own small disclosure.
    SURFACE, CARD, LINE = '#f4f5f7', '#ffffff', '#e3e6ea'
    INK, DIM, ACCENT, ACCENT_DARK = '#1c2430', '#68717d', '#2456c8', '#1c439b'
    OK, WARN, ERR = '#1d7a3d', '#b06a00', '#b3261e'
    MONO = 'Menlo' if sys.platform == 'darwin' else 'Consolas' if os.name == 'nt' else 'monospace'

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title('nxnative companion')
            self.geometry('760x680'); self.minsize(620, 540)
            self.configure(bg=SURFACE)
            self.worker = None; self.proc = None; self.devices = []
            self.queue = queue.Queue()   # everything the workers and threads want to say, one place
            self.link = None; self.link_serial = None; self.events = queue.Queue()   # the phone's receiver screen
            self.unpacking = False
            fam = tkfont.nametofont('TkDefaultFont').actual('family')
            self.fam = fam   # the setup dialog's rows want the same family
            F_TITLE, F_STEP, F_BIG = (fam, 15, 'bold'), (fam, 10, 'bold'), (fam, 11, 'bold')

            body = tk.Frame(self, bg=SURFACE); body.pack(fill='both', expand=True)
            body.grid_columnconfigure(0, weight=1)

            def card(row, title):
                """A white panel with its heading; the content lands in the rows after it."""
                c = tk.Frame(body, bg=CARD, highlightthickness=1, highlightbackground=LINE, bd=0)
                c.grid(row=row, column=0, sticky='ew', padx=18, pady=(0, 10))
                tk.Label(c, text=title, bg=CARD, fg=INK, anchor='w', font=F_STEP)\
                    .grid(row=0, column=0, columnspan=3, sticky='w', padx=14, pady=(10, 2))
                c.grid_columnconfigure(1, weight=1)
                return c

            def field(c, row, label, value, browse):
                tk.Label(c, text=label, bg=CARD, fg=DIM, anchor='w', width=9)\
                    .grid(row=row, column=0, sticky='w', padx=(14, 6), pady=3)
                e = ttk.Entry(c, textvariable=value); e.grid(row=row, column=1, sticky='ew', pady=3)
                b = ttk.Button(c, text=browse); b.grid(row=row, column=2, sticky='e', padx=(6, 12), pady=3)
                return e, b

            # -- the header: the name, and the one status that is not a step --
            head = tk.Frame(body, bg=SURFACE); head.grid(row=0, column=0, sticky='ew', padx=18, pady=(16, 8))
            tk.Label(head, text='nxnative', bg=SURFACE, fg=INK, font=F_TITLE).pack(side='left')
            tk.Label(head, text=f'companion {VERSION}', bg=SURFACE, fg=DIM).pack(side='left', padx=(8, 0), pady=(7, 0))
            self.b_tools = tk.Label(head, text='\u25cf tools: \u2026', bg=SURFACE, fg=DIM, cursor='hand2')
            self.b_tools.pack(side='right', pady=(8, 0))
            self.b_tools.bind('<Button-1>', lambda e: self.open_setup())

            # -- 1: the game's files --
            c1 = card(1, '1 \u00b7 The game\u2019s files')
            self.v_nsp = tk.StringVar(value=cfg['last_nsp'])
            _, b1 = field(c1, 1, 'NSP folder', self.v_nsp, 'Browse\u2026'); b1.configure(command=self.pick_nsp)
            self.v_keys = tk.StringVar(value=cfg['last_keys'])
            _, b2 = field(c1, 2, 'prod.keys', self.v_keys, 'Browse\u2026'); b2.configure(command=self.pick_keys)
            tk.Label(c1, text='One game\u2019s NSPs (the base game; update and DLC when you have them) and the console\u2019s prod.keys.',
                     bg=CARD, fg=DIM, anchor='w', justify='left')\
                .grid(row=3, column=0, columnspan=3, sticky='w', padx=14, pady=(2, 10))

            # -- 2: the phone --
            c2 = card(2, '2 \u00b7 The phone')
            self.l_phone = tk.Label(c2, text='\u25cf looking for a phone\u2026', bg=CARD, fg=DIM, anchor='w')
            self.l_phone.grid(row=1, column=0, sticky='w', padx=14, pady=(6, 0))
            self.b_recv = ttk.Button(c2, text='Install the receiver app\u2026', command=self.install_receiver)
            self.b_recv.grid(row=1, column=1, sticky='e', padx=(6, 0))   # only while a phone is there with no nxnative app on it
            self.b_recv.grid_remove()
            self.l_link = tk.Label(c2, text='receiver: -', bg=CARD, fg=DIM, anchor='e')
            self.l_link.grid(row=1, column=2, sticky='e', padx=12)
            tk.Label(c2, text='Plug it in over USB, with USB debugging on - its screen follows the build, and the game lands on it.',
                     bg=CARD, fg=DIM, anchor='w', justify='left')\
                .grid(row=2, column=0, columnspan=3, sticky='w', padx=14, pady=(2, 10))

            # -- the action: one button, one bar, one line of where it is --
            act = tk.Frame(body, bg=SURFACE); act.grid(row=3, column=0, sticky='ew', padx=18, pady=(4, 10))
            act.grid_columnconfigure(0, weight=1)
            self.b_build = tk.Button(act, text='Build & install', command=self.start, bg=ACCENT, fg='#ffffff',
                                    activebackground=ACCENT_DARK, activeforeground='#ffffff', disabledforeground='#a8bce8',
                                    relief='flat', bd=0, padx=24, pady=10, font=F_BIG, cursor='hand2')
            self.b_build.grid(row=0, column=0, sticky='ew')
            st = ttk.Style(self)
            st.configure('nx.Horizontal.TProgressbar', troughcolor=LINE, background=ACCENT,
                         lightcolor=ACCENT, darkcolor=ACCENT, bordercolor=SURFACE, thickness=8)
            self.bar = ttk.Progressbar(act, style='nx.Horizontal.TProgressbar', maximum=1.0, value=0.0)
            self.bar.grid(row=1, column=0, sticky='ew', pady=(10, 2))
            self.v_stage = tk.StringVar(value='idle')
            tk.Label(act, textvariable=self.v_stage, bg=SURFACE, fg=DIM, anchor='w')\
                .grid(row=2, column=0, sticky='ew')

            # -- the options, away until asked for --
            self.v_more = tk.BooleanVar(value=False)
            self.b_more = tk.Label(act, text='\u25b8 Options', bg=SURFACE, fg=ACCENT, cursor='hand2')
            self.b_more.grid(row=3, column=0, sticky='w', pady=(6, 0))
            self.b_more.bind('<Button-1>', lambda e: self.toggle_options())
            self.opt = tk.Frame(act, bg=SURFACE)
            self.v_lang = tk.StringVar(value=cfg['language'])
            tk.Label(self.opt, text='Language', bg=SURFACE, fg=DIM).grid(row=0, column=0, sticky='w')
            ttk.Combobox(self.opt, textvariable=self.v_lang, values=LANGUAGES, width=9).grid(row=0, column=1, sticky='w', padx=(4, 16))
            self.v_fps = tk.BooleanVar(value=cfg['show_fps'])
            ttk.Checkbutton(self.opt, text='Show frame rate', variable=self.v_fps).grid(row=0, column=2, sticky='w', padx=(0, 16))
            tk.Label(self.opt, text='App name', bg=SURFACE, fg=DIM).grid(row=0, column=3, sticky='w')
            self.v_name = tk.StringVar(value=cfg['app_name'])
            ttk.Entry(self.opt, textvariable=self.v_name, width=18).grid(row=0, column=4, sticky='w', padx=4)   # empty: the game's own name

            # -- the log: what the build actually said, one click down --
            logc = tk.Frame(body, bg=CARD, highlightthickness=1, highlightbackground=LINE, bd=0)
            logc.grid(row=4, column=0, sticky='nsew', padx=18, pady=(0, 16))
            body.rowconfigure(4, weight=1)
            tk.Label(logc, text='Details', bg=CARD, fg=DIM, anchor='w', font=F_STEP).grid(row=0, column=0, sticky='w', padx=14, pady=(10, 2))
            self.log = ScrolledText(logc, height=8, state='disabled', wrap='none', bg='#171a20', fg='#d6dae2',
                                    font=(MONO, 10), bd=0, highlightthickness=0, insertbackground='#d6dae2')
            self.log.grid(row=1, column=0, sticky='nsew', padx=10, pady=(0, 10))
            logc.grid_columnconfigure(0, weight=1); logc.rowconfigure(1, weight=1)
            for tag, color in (('ok', '#6f6'), ('err', '#f66'), ('dim', '#8a919c')): self.log.tag_configure(tag, foreground=color)

            self.protocol('WM_DELETE_WINDOW', self.close)
            self.after(50, self.refresh_tools); self.after(100, self.drain); self.after(2000, self.poll_phone)
            if FROZEN and payload_zip() and not payload_ready(): self.begin_unpack()

        def begin_unpack(self):
            """The engine's one-time unpack, out of the window's way: the bar runs, the button waits."""
            self.unpacking = True
            self.b_build['state'] = 'disabled'
            self.v_stage.set('unpacking the build engine (once, about a minute) \u2026')
            self.bar.config(mode='indeterminate'); self.bar.start(14)

            def unpack():
                try:
                    ensure_payload(progress=lambda f: self.queue.put(('bar', f)))
                    self.queue.put(('payload', 'the build engine is ready'))
                except Exception as e:   # the app cannot build: the reason goes where the user reads
                    self.queue.put(('fail', str(e)))
            threading.Thread(target=unpack, daemon=True, name='unpack').start()

        def toggle_options(self):
            self.v_more.set(not self.v_more.get())
            if self.v_more.get():
                self.opt.grid(row=4, column=0, sticky='w', pady=(4, 0)); self.b_more['text'] = '\u25be Options'
            else:
                self.opt.grid_remove(); self.b_more['text'] = '\u25b8 Options'

        # -- helpers --
        def say(self, msg, tag=None):
            self.log['state'] = 'normal'; self.log.insert('end', msg + '\n', tag or ()); self.log.see('end'); self.log['state'] = 'disabled'

        def bar_set(self, frac):
            """The pipeline's own word on where it is: a fraction drives the bar, None pulses it."""
            if frac is None:
                if str(self.bar.cget('mode')) != 'indeterminate':
                    self.bar.config(mode='indeterminate'); self.bar.start(14)
            else:
                if str(self.bar.cget('mode')) != 'determinate':
                    self.bar.stop(); self.bar.config(mode='determinate')
                self.bar.config(value=max(0.0, min(1.0, float(frac))))

        def pick_nsp(self):
            d = filedialog.askdirectory(title='The folder holding one game\u2019s NSPs', parent=self)
            if d: self.v_nsp.set(d)
        def pick_keys(self):
            p = filedialog.askopenfilename(title='prod.keys', parent=self)
            if p: self.v_keys.set(p)
        def pick_sdk(self):
            d = filedialog.askdirectory(title='The Android SDK folder (adb, ndk, build-tools inside)', parent=self)
            if d:
                cfg.update(sdk=d); save_config(cfg); self.refresh_tools()

        def refresh_tools(self):
            rows = tool_report(cfg)
            miss = [r for r in rows if not r['ok']]
            self.b_tools['text'] = f"\u25cf tools: {'ready' if not miss else str(len(miss)) + ' missing'}"
            self.b_tools['fg'] = OK if not miss else WARN
            if not self.unpacking:
                self.b_build['text'] = 'Build & install' if not miss else 'Build & install (toolchain incomplete)'
            self.setup_rows = rows

        def poll_phone(self):
            dev = list_devices(cfg)
            if dev is None:
                self.l_phone['text'] = '\u25cf no adb - the Toolchain page fixes that'; self.l_phone['fg'] = ERR; self.devices = []
                self.stop_link()
            else:
                self.devices = [d for d in dev if d[1] == 'device']
                if self.devices:
                    serial, model = self.devices[0][0], self.devices[0][2]
                    self.l_phone['text'] = f'\u25cf {model} \u00b7 connected'; self.l_phone['fg'] = OK
                    if serial != self.link_serial:   # plugged in, or a different phone: raise the receiver on it
                        self.stop_link(); self.link_serial = serial
                        threading.Thread(target=self.raise_receiver, args=(serial,), daemon=True).start()
                else:
                    plugged = [d for d in dev if d[1] != 'device']
                    self.l_phone['text'] = ('\u25cf plugged in - accept the USB debugging prompt on the phone' if plugged
                                            else '\u25cf no phone yet - plug one in over USB')
                    self.l_phone['fg'] = WARN if plugged else DIM
                    self.stop_link()   # ...and back to "waiting" goes the receiver screen (unplug = replug = raised again)
            self.after(2000, self.poll_phone)

        def drain(self):
            """One place, every tick: what the workers and the phone threads want the window to show."""
            try:
                while True:
                    kind, *rest = self.queue.get_nowait()
                    if kind == 'stage':
                        self.v_stage.set(rest[0]); self.bar_set(None)
                    elif kind == 'log': self.say(rest[0], *rest[1:])
                    elif kind == 'bar': self.bar_set(rest[0])
                    elif kind == 'payload':
                        self.unpacking = False; self.bar_set(1.0)
                        self.v_stage.set(rest[0]); self.say(rest[0], 'dim'); self.refresh_tools()
                        self.b_build['state'] = 'normal'
                    elif kind == 'done':
                        self.v_stage.set('done'); self.bar_set(1.0); self.say(rest[0], 'ok')
                        self.b_build['state'] = 'normal'; self.b_build['text'] = 'Build & install'
                    elif kind == 'fail':
                        self.v_stage.set('failed'); self.bar_set(0.0); self.say(rest[0], 'err')
                        self.b_build['state'] = 'normal'
                        self.unpacking = False   # a failed unpack keeps the engine missing: the next build says so again
                    elif kind == 'link':
                        if self.link: self.link.stop()   # one link at a time
                        self.link = rest[0]; self.link.start()
                    elif kind == 'pkg':
                        if rest[0]: self.say(f'receiver app on the phone: {rest[0]}', 'dim')
                        else: self.b_recv.grid()   # nothing to raise: offer the template
                    elif kind == 'live':
                        self.l_link['text'] = 'receiver: live' if rest[0] else 'receiver: off'
                        self.l_link['fg'] = OK if rest[0] else DIM
            except queue.Empty: pass
            self.after(100, self.drain)

        def raise_receiver(self, serial):
            """A worker (adb takes a moment): find the app to raise, am start it, hand the link back
            through the event queue."""
            adb = adb_path(cfg)
            if not adb: return
            rc, out = run_out([adb, '-s', serial, 'shell', 'pm', 'list', 'packages', 'org.nxnative'], timeout=15)
            pkg = pick_package(out.splitlines()) if not rc else None
            self.events.put(('pkg', pkg))
            if not pkg: return   # the phone holds no nxnative app: the window offers to install the template
            rc, out = run_out(am_start_command(adb, serial, pkg), timeout=15)
            if rc: self.events.put(('log', out.strip()[-200:]))
            self.events.put(('link', PhoneLink(adb, serial, on_state=lambda live: self.events.put(('live', live)))))

        def stop_link(self):
            if self.link: self.link.stop(); self.link = None
            self.link_serial = None
            self.l_link['text'] = 'receiver: -'; self.l_link['fg'] = DIM
            self.b_recv.grid_remove()

        def phone(self, obj):
            """The build's word to the phone's screen; queued, never blocking, dropped when nobody
            is listening (the link is content, not a step)."""
            link = self.link
            if link and obj: link.send(obj)

        def install_receiver(self):
            if self.worker and self.worker.is_alive(): return
            if self.unpacking:
                messagebox.showinfo('The build engine', 'It is still unpacking - a moment.', parent=self); return
            rows = tool_report(cfg); miss = [r['name'] for r in rows if not r['ok']]
            if miss and not messagebox.askyesno('Toolchain', 'These are missing: ' + ', '.join(miss) + '\n\nStart anyway?', parent=self): return
            self.b_recv.grid_remove(); self.b_build['state'] = 'disabled'
            self.worker = threading.Thread(target=self.receiver_install, daemon=True)
            self.worker.start()

        def receiver_install(self):
            """The template build for a phone with no nxnative app on it: the same Gradle the games
            go through (no game staged, the shell's applicationId), then the same align-sign-install."""
            q = self.queue
            try:
                if FROZEN: ensure_payload(progress=lambda f: q.put(('bar', f)))   # the engine, unpacked before it is needed
                android = repo_dir(cfg) / 'dm' / 'android'
                gradlew = android / ('gradlew.bat' if os.name == 'nt' else 'gradlew')
                if not gradlew.exists(): raise RuntimeError('no gradlew in the engine: ' + str(android))
                env = dict(os.environ)
                if cfg.get('sdk'): env['ANDROID_HOME'] = cfg['sdk']
                env['GAME'] = PROFILE   # the host compiles against a committed profile (the template carries no game files; the same GAME the CI workflow sets)
                cmd = receiver_build_command(gradlew)
                q.put(('stage', 'building the receiver app (the empty template, no game in it)'))
                self.proc = subprocess.Popen(cmd, cwd=str(android), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env, creationflags=NO_WINDOW)
                for line in self.proc.stdout or []: q.put(('log', line.rstrip()))
                rc = self.proc.wait(); self.proc = None
                if rc: raise RuntimeError(f'the template build stopped with exit code {rc} (the lines above say why)')
                apk = find_receiver_apk(android)
                if not apk: raise RuntimeError('the build left no APK in app/build/outputs/apk/release')
                q.put(('log', f'the build wrote {apk.name}', 'dim'))
                bt = bt_dir(cfg.get('sdk') or os.environ.get('ANDROID_HOME', ''))
                if not bt: raise RuntimeError('no build-tools 35 in the SDK: open Toolchain and install it')
                ks, cfg2 = ensure_keystore(cfg, log=lambda m: q.put(('log', m, 'dim')))
                aligned = apk.with_suffix('.aligned.apk')
                rc, out = run_out(zipalign_command(bt, apk, aligned))
                if rc: raise RuntimeError('zipalign failed: ' + out.strip()[-300:])
                rc, out = run_out(apksigner_command(bt, ks, cfg2['store_password'], cfg2['key_password'], cfg2['alias'], aligned, apk))
                aligned.unlink(missing_ok=True)
                if rc: raise RuntimeError('signing failed: ' + out.strip()[-300:])
                adb = adb_path(cfg)
                serial = self.devices[0][0] if self.devices else None
                if not adb or not serial:
                    q.put(('done', f'built and signed the receiver app: {apk}\nno phone right now - when one is plugged in: adb install -r "{apk}"'))
                    return
                q.put(('stage', 'installing the receiver app\u2026'))
                rc, out = run_out(adb_install_command(adb, serial, apk), timeout=600)
                if rc: raise RuntimeError('adb install failed: ' + out.strip()[-300:])
                q.put(('done', 'the receiver app is installed - its icon is on the home screen. Plug the phone in again (or wait a moment) and the companion takes over'))
            except Exception as e:
                q.put(('fail', str(e)))

        # -- the setup window: the guided list, each miss with its fix and an Install button where one click can do it --
        def open_setup(self):
            win = tk.Toplevel(self); win.title('Toolchain'); win.transient(self)
            win.geometry('720x480'); win.configure(bg=SURFACE)
            body = tk.Frame(win, bg=SURFACE); body.pack(fill='both', expand=True, padx=14, pady=14)
            # the report as a card, the same surface the window's own cards sit on: one row a piece,
            # the fix under its miss (dim), so the page reads as a list, not a terminal
            text = ScrolledText(body, wrap='word', bg=CARD, fg=INK, relief='flat', bd=0, padx=14, pady=10,
                                highlightthickness=1, highlightbackground=LINE, font=(self.fam, 10),
                                insertbackground=INK, selectbackground='#cdd8f3')
            text.pack(fill='both', expand=True)
            text.tag_configure('ok', foreground=OK)
            text.tag_configure('miss', foreground=ERR)
            text.tag_configure('fix', foreground=DIM)
            text.tag_configure('dim', foreground=DIM)
            def render(rows):
                text['state'] = 'normal'; text.delete('1.0', 'end')
                for r in rows:
                    text.insert('end', '\u2713 ' if r['ok'] else '\u2717 ', 'ok' if r['ok'] else 'miss')
                    text.insert('end', f"{r['name']}  ", ('ok' if r['ok'] else 'miss'))
                    text.insert('end', f"{r['detail']}\n", 'dim')
                    if not r['ok'] and r['fix']: text.insert('end', f'       fix: {r["fix"]}\n', 'fix')
                text['state'] = 'disabled'
            rows = tool_report(cfg); render(rows)
            bar = tk.Frame(body, bg=SURFACE); bar.pack(fill='x', pady=(10, 0))
            def run_fix():
                nonlocal rows
                sel = [r for r in rows if not r['ok'] and r['fix_cmd']]
                if not sel: return
                rc, out = run_out(sel[0]['fix_cmd'])
                text['state'] = 'normal'; text.insert('end', f'\n$ {" ".join(sel[0]["fix_cmd"])}\n{out.strip()[-1500:]}\n', 'miss'); text['state'] = 'disabled'
                rows = tool_report(cfg); render(rows); self.refresh_tools()
            ttk.Button(bar, text='Install the first missing piece', command=run_fix).pack(side='left')
            ttk.Button(bar, text='Check again', command=lambda: (setattr(self, 'setup_rows', tool_report(cfg)), render(self.setup_rows), self.refresh_tools())).pack(side='left', padx=6)
            ttk.Button(bar, text='SDK path\u2026', command=self.pick_sdk).pack(side='left')
            tk.Label(bar, text='Windows: the window runs by itself; the build steps want bash, make and python3\n(MSYS2 provides them, or the whole build inside WSL2), and adb needs the phone attached with usbipd-win',
                     bg=SURFACE, fg=DIM, justify='right').pack(side='right', padx=(10, 0))

        # -- the build --
        def start(self):
            if self.worker and self.worker.is_alive(): return
            if self.unpacking:
                messagebox.showinfo('The build engine', 'It is still unpacking - a moment.', parent=self); return
            nsp, keys = self.v_nsp.get().strip(), self.v_keys.get().strip()
            if not nsp or not Path(nsp).is_dir():
                messagebox.showerror('NSP folder', 'Pick the folder that holds one game\u2019s NSPs (the base game; update and DLC if you have them)', parent=self); return
            if not keys or not Path(keys).is_file():
                messagebox.showerror('prod.keys', 'Pick the console\u2019s prod.keys file', parent=self); return
            rows = tool_report(cfg); miss = [r['name'] for r in rows if not r['ok']]
            if miss and not messagebox.askyesno('Toolchain', 'These are missing: ' + ', '.join(miss) + '\n\nStart anyway (it will stop at the first one)?', parent=self): return
            cfg.update(last_nsp=nsp, last_keys=keys, language=self.v_lang.get().strip() or 'en-US',
                       show_fps=bool(self.v_fps.get()), app_name=self.v_name.get().strip()); save_config(cfg)
            self.log['state'] = 'normal'; self.log.delete('1.0', 'end'); self.log['state'] = 'disabled'
            self.bar_set(0.0)
            self.b_build['state'] = 'disabled'
            self.worker = threading.Thread(target=self.build, args=(nsp, keys, cfg['language'], cfg['show_fps'], cfg['app_name']), daemon=True)
            self.worker.start()

        def build(self, nsp, keys, lang, fps, app_name):
            """The worker thread: the pipeline, then align-sign-install. Everything reaches the window through the queue."""
            q = self.queue; started = time.time() - 5
            env = dict(os.environ, PROGRESS='json')   # build_apk_from_nsp.py's own events, one JSON object a line
            prog_file = None
            if FROZEN:   # a windowed build reads its events from a file: its stdout is not wired even through a pipe
                prog_file = Path(tempfile.gettempdir()) / f'nxcompanion-{os.getpid()}-{int(time.time())}.jsonl'
                env['PROGRESS_FILE'] = str(prog_file)
            if cfg.get('sdk'): env['ANDROID_HOME'] = cfg['sdk']
            if FROZEN and not payload_ready():
                q.put(('stage', 'unpacking the build engine (once) \u2026'))
                ensure_payload(progress=lambda f: q.put(('bar', f)))
                q.put(('log', 'the build engine is ready', 'dim'))
            repo = repo_dir(cfg)
            cmd = pipeline_command(nsp, keys, lang, fps, app_name, repo=repo)
            q.put(('stage', f'building: {" ".join(cmd[1:])}'))
            self.phone({'event': 'status', 'state': 'building', 'stage': 'starting', 'detail': os.path.basename(nsp)})
            try:
                self.proc = subprocess.Popen(cmd, cwd=str(repo),
                                             stdout=subprocess.DEVNULL if prog_file else subprocess.PIPE,
                                             stderr=subprocess.STDOUT if not prog_file else subprocess.DEVNULL,
                                             text=True, env=env, creationflags=NO_WINDOW)
                lines = follow_lines(prog_file, self.proc) if prog_file else (self.proc.stdout or [])
                last_stage = ''
                for line in lines:
                    ev = None
                    try: ev = json.loads(line)
                    except ValueError: pass
                    if ev and ev.get('ev') in ('stage', 'step'):
                        if ev.get('name'): last_stage = str(ev['name'])
                        q.put(('stage', f"[{ev.get('k', '?')}/{ev.get('n', '?')}] {ev.get('name', '')} \u00b7 {ev.get('step', '')}"))
                        if ev.get('step'): q.put(('log', ev['step'], 'dim'))
                        self.phone(phone_status(ev))
                    elif ev and ev.get('ev') == 'progress':
                        self.phone(phone_status(ev))   # the shader counts: the phone's bar lives on these
                        q.put(('bar', ev.get('frac')))   # ...and the window's bar does too
                        q.put(('log', line.rstrip()))
                    elif ev and ev.get('ev') == 'say':
                        q.put(('log', str(ev.get('msg', ''))))
                        self.phone({'event': 'status', 'state': 'building', 'stage': last_stage, 'detail': str(ev.get('msg', ''))})
                    else: q.put(('log', line.rstrip()))
                rc = self.proc.wait(); self.proc = None
                if prog_file:
                    try: prog_file.unlink()
                    except OSError: pass
                if rc: raise RuntimeError(f'the build stopped with exit code {rc} (the lines above say why)')
                apks = find_built_apks(nsp, started)
                if not apks: raise RuntimeError('the build reported success but no APK appeared beside the NSPs')
                apk = apks[0]
                q.put(('log', f'the build wrote {apk.name}', 'ok'))
                # 16 KB zip alignment (zipalign -P 16), then the personal key: the phone loads the
                # library straight from the APK, and updates install over the same signature
                bt = bt_dir(cfg.get('sdk') or os.environ.get('ANDROID_HOME', ''))
                if not bt: raise RuntimeError('no build-tools 35 in the SDK (zipalign -P 16): open Toolchain and install it')
                ks, cfg2 = ensure_keystore(cfg, log=lambda m: q.put(('log', m, 'dim')))
                aligned = apk.with_suffix('.aligned.apk')
                rc, out = run_out(zipalign_command(bt, apk, aligned))
                if rc: raise RuntimeError('zipalign failed: ' + out.strip()[-300:])
                rc, out = run_out(apksigner_command(bt, ks, cfg2['store_password'], cfg2['key_password'], cfg2['alias'], aligned, apk))
                aligned.unlink(missing_ok=True)
                if rc: raise RuntimeError('signing failed: ' + out.strip()[-300:])
                q.put(('log', f'signed and 16 KB-aligned: {apk}', 'ok'))
                adb = adb_path(cfg)
                serial = self.devices[0][0] if self.devices else None
                if not adb: q.put(('done', f'built and signed {apk.name} - no adb to install it: connect the phone and run\n  adb install -r "{apk}"'))
                elif not serial:
                    q.put(('done', f'built and signed {apk.name} - no phone connected right now: connect it and run\n  adb install -r "{apk}"'))
                else:
                    game = apk.stem
                    self.phone({'event': 'status', 'state': 'installing', 'game': game})
                    q.put(('stage', f'installing on {self.devices[0][2]}\u2026'))
                    rc, out = run_out(adb_install_command(adb, serial, apk), timeout=600)
                    if rc: raise RuntimeError('adb install failed: ' + out.strip()[-300:] + ' (USB debugging: the phone must show the trust prompt)')
                    self.phone({'event': 'status', 'state': 'done', 'game': game})   # the shell app survives a game's install; a same-app update closes its own link
                    q.put(('done', f'installed on {self.devices[0][2]} - the game\u2019s icon is on the home screen'))
            except Exception as e:   # the worker never takes the window down: the reason goes to the log
                q.put(('fail', str(e)))
                self.phone({'event': 'status', 'state': 'error', 'message': str(e).strip()[-300:]})

        def close(self):
            if self.worker and self.worker.is_alive() and not messagebox.askyesno('A build is running', 'Stop it and quit?', parent=self): return
            if self.proc:
                try: self.proc.terminate()
                except OSError: pass
            self.destroy()

    App().mainloop()

def main(argv):
    _trace('main entered')
    if FROZEN:
        import multiprocessing
        from multiprocessing import spawn as mp_spawn
        # A shader-split worker (ProcessPoolExecutor's spawn start method) re-runs this executable on
        # Windows; the handshake it then performs has hung there, so it runs here step by step, each
        # written to the trace file a windowed child can otherwise not talk through (the same calls
        # multiprocessing's own freeze_support makes, and any failure's traceback ends up in the file).
        # Everything else - the tracker's '-c' helpers, and every other platform - goes to the stock one.
        if sys.platform == 'win32' and mp_spawn.is_forking(sys.argv):
            kwds = {}
            for arg in sys.argv[2:]:
                name, value = arg.split('=', 1)
                kwds[name] = None if value == 'None' else int(value)
            _trace(f'the spawn worker, kwds {kwds}')
            try:
                import msvcrt
                import _winapi
                from multiprocessing import context as mp_context
                source_process = None
                if 'parent_pid' in kwds:
                    _trace('opening the parent process')
                    source_process = _winapi.OpenProcess(_winapi.SYNCHRONIZE | _winapi.PROCESS_DUP_HANDLE,
                                                         False, kwds.pop('parent_pid'))
                _trace('duplicating the pipe handle')
                new_handle = mp_context.reduction.duplicate(kwds['pipe_handle'], source_process=source_process)
                _trace('wrapping the handle as a file descriptor')
                fd = msvcrt.open_osfhandle(new_handle, os.O_RDONLY)
                # the two loads _main does, one trace line each: the preparation data says how the
                # child is meant to rebuild __main__ (the parent's path, or a module name), and the
                # process object is what unpickles the target function out of that __main__
                from multiprocessing import process as mp_process
                with os.fdopen(fd, 'rb', closefd=True) as from_parent:
                    mp_process.current_process()._inheriting = True
                    try:
                        preparation_data = mp_context.reduction.pickle.load(from_parent)
                        _trace('prep: ' + repr({k: v for k, v in preparation_data.items()
                                                 if k in ('init_main_from_name', 'init_main_from_path', 'sys_argv', 'dir', 'name')}))
                        mp_spawn.prepare(preparation_data)
                        m = sys.modules['__main__']
                        _trace(f'after prepare: __main__ file={getattr(m, "__file__", None)} '
                               f'spec={getattr(m, "__spec__", None)} has_work={hasattr(m, "work")} has_twice={hasattr(m, "twice")}')
                        self = mp_context.reduction.pickle.load(from_parent)
                        _trace('the process object unpickled')
                    finally:
                        del mp_process.current_process()._inheriting
                exitcode = self._bootstrap(source_process)
                _trace(f'the worker ran, exitcode {exitcode}')
                sys.exit(exitcode or 0)
            except SystemExit:
                raise
            except BaseException:
                import traceback
                _trace('the spawn handshake failed:\n' + traceback.format_exc())
                sys.exit(1)   # not re-raised: a windowed build's uncaught error is a modal dialog, which on a machine with nobody at it is a hang
        _trace('argv: ' + repr(sys.argv))
        _trace('calling freeze_support')
        multiprocessing.freeze_support()   # the tracker's '-c' helpers here, and the worker children on the Mac
        _trace('freeze_support returned (not a spawn child)')
    if argv and argv[0].endswith('.py') and Path(argv[0]).is_file():
        return run_as_python(argv[0], argv[1:])   # the pipeline's helper scripts: run them as python3 would
    ap = argparse.ArgumentParser(prog='pc_companion', description='pick the NSPs and prod.keys, get an installed game on the phone (dm/android/tools/pc_companion.py)')
    ap.add_argument('--check', action='store_true', help='print the toolchain and device report and exit (no window)')
    ap.add_argument('--sdk', help='remember the Android SDK path (the Toolchain button in the window does the same)')
    ap.add_argument('--repo', help='use this checkout instead of the build engine the app carries (development)')
    args = ap.parse_args(argv)
    if args.check: return run_check(args)
    run_gui(args)
    return 0


if __name__ == '__main__':
    _trace('the module is __main__')
    sys.exit(main(sys.argv[1:]))
