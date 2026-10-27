#!/usr/bin/env python3
"""The PC companion: pick the NSPs and prod.keys on the computer, get an installed game on the phone.

One window over the whole APK pipeline (scripts/build_apk_from_nsp.py): the toolchain is checked and
guided into place, the phone shows up over USB (adb), and one button runs the build - identify the
game, extract, rewrite the modules, translate the shaders, pack the app - then aligns the APK for
16 KB-page phones, signs it with a personal key made on first use (~/.nxcompanion, so every update of
the same game installs over the last) and installs it with adb. Nothing is launched: the game's icon
lands on the home screen, the player taps it.

Usage (Python 3.10+, no pip packages needed for the window itself):
    python3 dm/android/tools/pc_companion.py          the window
    python3 dm/android/tools/pc_companion.py --check  the same toolchain and device report on stdout
Windows: run it inside WSL2 (wsl --install from PowerShell); WSLg shows this window on the desktop,
and adb inside WSL sees a USB phone once usbipd-win attaches it (the toolchain page has the commands).
"""
import argparse
import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CONFIG_DIR = Path(os.environ.get('NXC_HOME') or Path.home() / '.nxcompanion')
NDK_VERSION, CMAKE_VERSION, PLATFORM, BT_MIN = '26.3.11579264', '3.22.1', 'android-34', 35 * 10000   # what dm/android builds with (build.gradle.kts); BT_MIN: build-tools 35.0.0, the first whose zipalign knows -P 16
LANGUAGES = ['en-US', 'ja', 'fr', 'de', 'es', 'zh-Hans', 'zh-Hant', 'ko', 'it', 'pt-BR', 'ru']   # -l takes any code; these are the common ones
SDK_STEPS = ('https://developer.android.com/studio#command-line-tools-only: download the "Command line tools only" zip, '
             'unzip it to ~/android-sdk/cmdline-tools/latest (licenses are accepted on first install), then reopen this check')


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
    if not bt_dir(sdk): miss.append(f'build-tools;35.0.0 (zipalign -P 16)')
    return {'name': 'Android SDK', 'ok': not miss, 'detail': str(sdk) + ('' if not miss else ' - missing ' + ', '.join(miss)),
            'fix': None if not miss else 'the Install button runs sdkmanager, or by hand: sdkmanager ' + ' '.join(f'"{p}"' for p in sdk_packages()),
            'fix_cmd': [str(sdkmanager_path(sdk))] + sdk_packages() if sdkmanager_path(sdk) else None}


def sdk_packages():
    return [f'platform-tools', f'ndk;{NDK_VERSION}', f'cmake;{CMAKE_VERSION}', f'platforms;{PLATFORM}', 'build-tools;35.0.0']


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
    py_miss = [m for m in ('lz4.block', 'capstone', 'fontTools') if not _importable(m)] + \
              ([] if _importable('compression.zstd') or _importable('backports.zstd') else ['backports.zstd'])
    rows.append({'name': 'Python packages', 'ok': not py_miss,
                 'detail': f'in {sys.executable}' if not py_miss else 'missing ' + ', '.join(py_miss),
                 'fix': None if not py_miss else 'pip install lz4 capstone fonttools' + (' backports.zstd' if sys.version_info < (3, 14) else ''),
                 'fix_cmd': None if not py_miss else [sys.executable, '-m', 'pip', 'install', 'lz4', 'capstone', 'fonttools'] + (['backports.zstd'] if sys.version_info < (3, 14) else [])})
    for tool, how in (('bash', 'everywhere on the Mac and Linux; on Windows this app runs in WSL2'),
                      ('make', 'the Xcode command line tools on the Mac (xcode-select --install), build-essential on Ubuntu'),
                      ('zstd', 'brew install zstd, or apt install zstd'),
                      ('dotnet', 'the .NET SDK 10: brew install --cask dotnet-sdk, or see dotnet.microsoft.com/download')):
        found = which(tool)
        rows.append({'name': tool, 'ok': bool(found), 'detail': str(found or '-'), 'fix': None if found else how, 'fix_cmd': None})
    jv = which('java')
    jver = None
    if jv:
        try: jver = java_version(subprocess.run([jv, '-version'], capture_output=True, text=True, timeout=30).stderr or '')
        except (OSError, subprocess.SubprocessError): pass
    rows.append({'name': 'Java 17+', 'ok': bool(jv and jver and jver >= 17),
                 'detail': f'{jv} (version {jver})' if jv else '-',
                 'fix': None if jv and jver and jver >= 17 else 'Gradle needs Java 17+: brew install --cask temurin@17, or see adoptium.net (Windows: inside WSL)', 'fix_cmd': None})
    rows.append(sdk_status(cfg.get('sdk') or os.environ.get('ANDROID_HOME')))
    xlate = REPO / 'dm/gfx/tools/dmsh_xlate_bin/dmsh_xlate'
    scross = REPO / 'dm/gfx/tools/spirv_cross_bin/spirv-cross'
    rows.append({'name': 'the repository\'s translator and spirv-cross', 'ok': xlate.exists() and scross.exists(),
                 'detail': 'in the checkout' if xlate.exists() and scross.exists() else 'missing from the checkout',
                 'fix': None if xlate.exists() and scross.exists() else 'git checkout / clone the repository again', 'fix_cmd': None})
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
    try: r = subprocess.run([adb, 'devices', '-l'], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError): return []
    return parse_devices(r.stdout)


# ---- The build: one command, the APK it leaves, and the three that finish it ----

def pipeline_command(nsp_dir, keys, language=None, show_fps=True, app_name=None):
    cmd = [sys.executable, str(REPO / 'scripts' / 'build_apk_from_nsp.py'), str(nsp_dir), '-k', str(keys)]
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


def run_out(cmd, env=None, timeout=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)
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

def run_check(argv):
    cfg = load_config()
    if argv.sdk:
        cfg['sdk'] = str(Path(argv.sdk).resolve()); save_config(cfg)
    rows = tool_report(cfg)
    print(f'nxnative PC companion - toolchain report ({sys.platform})')
    for r in rows:
        print(f"  {'ok  ' if r['ok'] else 'MISS'} {r['name']}: {r['detail']}")
        if not r['ok'] and r['fix']: print(f'        fix: {r["fix"]}')
    dev = list_devices(cfg)
    if dev is None: print('  phone: no adb (the SDK above)')
    elif not dev: print('  phone: none connected - plug it in over USB and enable USB debugging')
    else:
        for serial, state, model in dev: print(f'  phone: {model} ({serial}) {state}')
    ok = all(r['ok'] for r in rows)
    print('ready' if ok else 'not ready - see the fixes above')
    return 0 if ok else 1


# ---- The window ----

def run_gui(argv):
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from tkinter.scrolledtext import ScrolledText

    cfg = load_config()

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title('nxnative - put a game on the phone')
            self.geometry('780x600'); self.minsize(640, 480)
            self.worker = None; self.proc = None; self.devices = []
            self.queue = None
            f = ttk.Frame(self, padding=10); f.pack(fill='both', expand=True)

            ttk.Label(f, text='One game\'s NSPs (base, update, DLC) and the console\'s prod.keys:', ).grid(row=0, column=0, columnspan=3, sticky='w', pady=(0, 4))
            self.v_nsp = tk.StringVar(value=cfg['last_nsp'])
            ttk.Entry(f, textvariable=self.v_nsp).grid(row=1, column=0, sticky='ew', padx=(0, 4))
            ttk.Button(f, text='NSP folder\u2026', command=self.pick_nsp).grid(row=1, column=1)
            self.v_keys = tk.StringVar(value=cfg['last_keys'])
            ttk.Entry(f, textvariable=self.v_keys).grid(row=2, column=0, sticky='ew', pady=(6, 0), padx=(0, 4))
            ttk.Button(f, text='prod.keys\u2026', command=self.pick_keys).grid(row=2, column=1)

            opt = ttk.Frame(f); opt.grid(row=3, column=0, columnspan=3, sticky='ew', pady=(10, 0))
            ttk.Label(opt, text='Language').pack(side='left')
            self.v_lang = tk.StringVar(value=cfg['language'])
            self.lang = ttk.Combobox(opt, textvariable=self.v_lang, values=LANGUAGES, width=8); self.lang.pack(side='left', padx=(4, 12))
            self.v_fps = tk.BooleanVar(value=cfg['show_fps'])
            ttk.Checkbutton(opt, text='Show frame rate', variable=self.v_fps).pack(side='left', padx=(0, 12))
            ttk.Label(opt, text='App name').pack(side='left')
            self.v_name = tk.StringVar(value=cfg['app_name'])
            ttk.Entry(opt, textvariable=self.v_name, width=18).pack(side='left', padx=4)   # empty: the game's own name
            f.columnconfigure(0, weight=1)

            state = ttk.Frame(f); state.grid(row=4, column=0, columnspan=3, sticky='ew', pady=(12, 0))
            self.b_phone = tk.Label(state, text='\u25cf ?', fg='#888'); self.b_phone.pack(side='left')
            self.b_tools = tk.Label(state, text='\u25cf ?', fg='#888'); self.b_tools.pack(side='left')
            self.b_setup = ttk.Button(state, text='Toolchain\u2026', command=self.open_setup); self.b_setup.pack(side='left', padx=(8, 0))
            self.b_sdk = ttk.Button(state, text='SDK path\u2026', command=self.pick_sdk); self.b_sdk.pack(side='left', padx=(4, 0))

            self.b_build = ttk.Button(f, text='Build & install', command=self.start)
            self.b_build.grid(row=5, column=0, columnspan=3, sticky='ew', pady=(14, 6))
            self.v_stage = tk.StringVar(value='idle')
            ttk.Label(f, textvariable=self.v_stage, anchor='w').grid(row=6, column=0, columnspan=3, sticky='ew')
            self.log = ScrolledText(f, height=14, state='disabled', wrap='none', bg='#111', fg='#ddd', font=('Menlo' if sys.platform == 'darwin' else 'Consolas' if os.name == 'nt' else 'monospace', 10))
            self.log.grid(row=7, column=0, columnspan=3, sticky='nsew')
            for tag, color in (('ok', '#6f6'), ('err', '#f66'), ('dim', '#999')): self.log.tag_configure(tag, foreground=color)
            f.rowconfigure(7, weight=1); f.columnconfigure(0, weight=1)

            self.protocol('WM_DELETE_WINDOW', self.close)
            self.after(50, self.refresh_tools); self.after(2000, self.poll_phone)

        # -- helpers --
        def say(self, msg, tag=None):
            self.log['state'] = 'normal'; self.log.insert('end', msg + '\n', tag or ()); self.log.see('end'); self.log['state'] = 'disabled'

        def pick_nsp(self):
            d = filedialog.askdirectory(title='The folder holding one game\'s NSPs', parent=self)
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
            self.b_tools['fg'] = '#2a2' if not miss else '#d33'
            self.b_build['text'] = 'Build & install' if not miss else 'Build & install (toolchain incomplete)'
            self.setup_rows = rows

        def poll_phone(self):
            dev = list_devices(cfg)
            if dev is None:
                self.b_phone['text'] = '\u25cf no adb'; self.b_phone['fg'] = '#d33'; self.devices = []
            else:
                self.devices = [d for d in dev if d[1] == 'device']
                if self.devices: self.b_phone['text'] = f"\u25cf {self.devices[0][2]} ({self.devices[0][0]})"; self.b_phone['fg'] = '#2a2'
                else: self.b_phone['text'] = '\u25cf no phone - plug in over USB, enable USB debugging'; self.b_phone['fg'] = '#d33'
            self.after(2000, self.poll_phone)

        # -- the setup window: the guided list, each miss with its fix and an Install button where one click can do it --
        def open_setup(self):
            win = tk.Toplevel(self); win.title('Toolchain'); win.transient(self)
            win.geometry('720x420')
            body = ttk.Frame(win, padding=10); body.pack(fill='both', expand=True)
            text = ScrolledText(body, height=16, wrap='word'); text.pack(fill='both', expand=True)
            text.tag_configure('ok', foreground='#282'); text.tag_configure('miss', foreground='#c22')
            def render(rows):
                text['state'] = 'normal'; text.delete('1.0', 'end')
                for r in rows:
                    text.insert('end', f"{'[ok] ' if r['ok'] else '[MISS] '} {r['name']} - {r['detail']}\n", 'ok' if r['ok'] else 'miss')
                    if not r['ok'] and r['fix']: text.insert('end', f'       fix: {r["fix"]}\n', 'miss')
                text['state'] = 'disabled'
            rows = tool_report(cfg); render(rows)
            bar = ttk.Frame(body); bar.pack(fill='x', pady=(6, 0))
            def run_fix():
                nonlocal rows
                sel = [r for r in rows if not r['ok'] and r['fix_cmd']]
                if not sel: return
                rc, out = run_out(sel[0]['fix_cmd'])
                text['state'] = 'normal'; text.insert('end', f'\n$ {" ".join(sel[0]["fix_cmd"])}\n{out.strip()[-1500:]}\n', 'dim'); text['state'] = 'disabled'
                rows = tool_report(cfg); render(rows); self.refresh_tools()
            ttk.Button(bar, text='Install the first missing piece', command=run_fix).pack(side='left')
            ttk.Button(bar, text='Check again', command=lambda: (setattr(self, 'setup_rows', tool_report(cfg)), render(self.setup_rows), self.refresh_tools())).pack(side='left', padx=6)
            ttk.Label(bar, text='Windows: the whole app runs inside WSL2 (wsl --install); WSLg shows this window,\nand adb needs the phone attached with usbipd-win (see the README)', justify='left').pack(side='right')

        # -- the build --
        def start(self):
            if self.worker and self.worker.is_alive(): return
            nsp, keys = self.v_nsp.get().strip(), self.v_keys.get().strip()
            if not nsp or not Path(nsp).is_dir():
                messagebox.showerror('NSP folder', 'Pick the folder that holds one game\'s NSPs (the base game; update and DLC if you have them)', parent=self); return
            if not keys or not Path(keys).is_file():
                messagebox.showerror('prod.keys', 'Pick the console\'s prod.keys file', parent=self); return
            rows = tool_report(cfg); miss = [r['name'] for r in rows if not r['ok']]
            if miss and not messagebox.askyesno('Toolchain', 'These are missing: ' + ', '.join(miss) + '\n\nStart anyway (it will stop at the first one)?', parent=self): return
            cfg.update(last_nsp=nsp, last_keys=keys, language=self.v_lang.get().strip() or 'en-US',
                       show_fps=bool(self.v_fps.get()), app_name=self.v_name.get().strip()); save_config(cfg)
            self.log['state'] = 'normal'; self.log.delete('1.0', 'end'); self.log['state'] = 'disabled'
            self.b_build['state'] = 'disabled'
            self.queue = queue.Queue()
            self.worker = threading.Thread(target=self.build, args=(nsp, keys, cfg['language'], cfg['show_fps'], cfg['app_name']), daemon=True)
            self.worker.start(); self.after(100, self.drain)

        def build(self, nsp, keys, lang, fps, app_name):
            """The worker thread: the pipeline, then align-sign-install. Everything reaches the window through the queue."""
            q = self.queue; started = time.time() - 5
            env = dict(os.environ, PROGRESS='json')   # build_apk_from_nsp.py's own events, one JSON object a line
            if cfg.get('sdk'): env['ANDROID_HOME'] = cfg['sdk']
            cmd = pipeline_command(nsp, keys, lang, fps, app_name)
            q.put(('stage', f'building: {" ".join(cmd[1:])}'))
            try:
                self.proc = subprocess.Popen(cmd, cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
                for line in self.proc.stdout or []:
                    ev = None
                    try: ev = json.loads(line)
                    except ValueError: pass
                    if ev and ev.get('ev') in ('stage', 'step'):
                        q.put(('stage', f"[{ev.get('k', '?')}/{ev.get('n', '?')}] {ev.get('name', '')} \u00b7 {ev.get('step', '')}"))
                        if ev.get('step'): q.put(('log', ev['step'], 'dim'))
                    elif ev and ev.get('ev') == 'say': q.put(('log', str(ev.get('msg', ''))))
                    else: q.put(('log', line.rstrip()))
                rc = self.proc.wait(); self.proc = None
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
                    q.put(('stage', f'installing on {self.devices[0][2]}\u2026'))
                    rc, out = run_out(adb_install_command(adb, serial, apk), timeout=600)
                    if rc: raise RuntimeError('adb install failed: ' + out.strip()[-300:] + ' (USB debugging: the phone must show the trust prompt)')
                    q.put(('done', f'installed on {self.devices[0][2]} - the game\'s icon is on the home screen'))
            except Exception as e:   # the worker never takes the window down: the reason goes to the log
                q.put(('fail', str(e)))
        def drain(self):
            try:
                while True:
                    kind, *rest = self.queue.get_nowait()
                    if kind == 'stage': self.v_stage.set(rest[0])
                    elif kind == 'log': self.say(rest[0], *rest[1:])
                    elif kind == 'done':
                        self.v_stage.set('done'); self.say(rest[0], 'ok'); self.b_build['state'] = 'normal'
                        self.b_build['text'] = 'Build & install'
                    elif kind == 'fail':
                        self.v_stage.set('failed'); self.say(rest[0], 'err'); self.b_build['state'] = 'normal'
            except queue.Empty: pass
            if self.worker and self.worker.is_alive(): self.after(100, self.drain)
            else: self.drain_final()

        def drain_final(self):
            try:
                while True:
                    kind, *rest = self.queue.get_nowait()
                    if kind == 'stage': self.v_stage.set(rest[0])
                    elif kind == 'log': self.say(rest[0], *rest[1:])
                    elif kind == 'done': self.v_stage.set('done'); self.say(rest[0], 'ok'); self.b_build['state'] = 'normal'; self.b_build['text'] = 'Build & install'
                    elif kind == 'fail': self.v_stage.set('failed'); self.say(rest[0], 'err'); self.b_build['state'] = 'normal'
            except queue.Empty: pass

        def close(self):
            if self.worker and self.worker.is_alive() and not messagebox.askyesno('A build is running', 'Stop it and quit?', parent=self): return
            if self.proc:
                try: self.proc.terminate()
                except OSError: pass
            self.destroy()

    App().mainloop()


def main(argv):
    ap = argparse.ArgumentParser(prog='pc_companion.py', description='pick the NSPs and prod.keys, get an installed game on the phone (dm/android/tools/pc_companion.py)')
    ap.add_argument('--check', action='store_true', help='print the toolchain and device report and exit (no window)')
    ap.add_argument('--sdk', help='remember the Android SDK path (the Toolchain button in the window does the same)')
    args = ap.parse_args(argv)
    if args.check: return run_check(args)
    run_gui(args)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
