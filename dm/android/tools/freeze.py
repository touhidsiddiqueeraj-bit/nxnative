#!/usr/bin/env python3
"""Freeze the PC companion into a single standalone file (dm/android/tools/pc_companion.py): the
window, the toolchain guidance, the phone link over USB, the pipeline and the build engine itself,
all inside the one executable. Every helper the pipeline spawns is [the executable, <script.py>,
...], exactly the shape it used for python3 (run_as_python), and the third-party modules those
scripts import - lz4 (block and frame), capstone, fontTools, the zstd binding, nsz - are carried
inside the file too. The shader split's ProcessPoolExecutor works as well: freeze_support() in
pc_companion.py runs the spawn children.

One file, not one folder: a onefile executable unpacks itself into a private temporary folder when
it starts, and hands that folder down to every child it spawns (PyInstaller sets _PYI_APPLICATION_HOME_DIR,
and a child of the same executable that inherits it reuses the parent's unpacked copy instead of
unpacking itself again). The old reason not to - a Defender-scanned unpack per worker, minutes
each on the shader split's dozens of children - is gone: the app pays one unpack at start, and
every child starts as fast as a folder build's did. The engine rides in the same place: payload.zip
--add-data puts it inside the executable, at sys._MEIPASS/payload.zip after the one unpack, and
the app unpacks it into ~/.nxcompanion/repo/<version> on first use (ensure_payload). What stays
outside is what the Toolchain window guides in: bash, make, python3, zstd, dotnet, Java and the
Android SDK.

Usage:  python3 dm/android/tools/freeze.py [output dir]        (dist/ under the repository by default)
Needs:  python3 -m pip install pyinstaller lz4 capstone fonttools backports.zstd nsz
       (Python 3.14+ carries zstd itself; freeze with 3.12 for the backport, as the releases do)
The Mac build is universal2 (the python.org interpreter is, and so are the repository's own
translator binaries): the wheel-makers' thin native modules - lz4, the zstd backport - get their
other arch lipo'd in first (fatten, from the PyPI wheel of the other arch). The Windows build is
a window app (--check attaches to the terminal it was run from), the Linux build a console one;
the executable is the single file to keep (nxcompanion.exe, nxcompanion - the Mac's sits inside
nxcompanion.app, the one folder a Mac app is allowed to be).
"""
import os
import platform
import subprocess
import sys
import shutil
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
ENTRY = REPO / 'dm' / 'android' / 'tools' / 'pc_companion.py'
NAME = 'nxcompanion'
# What the pipeline's scripts import at run time (they run from the unpacked payload; their modules
# from here): lz4's block and frame are single imports the freeze cannot see; capstone, fontTools and
# nsz are walked dynamically or lazily, and backports.zstd is imported from a try/except in shader_split.py.
HIDDEN = ['lz4.block', 'lz4.frame'] + (['compression.zstd'] if sys.version_info >= (3, 14) else [])
COLLECT = ['capstone', 'fontTools', 'nsz'] + ([] if sys.version_info >= (3, 14) else ['backports.zstd'])
# The payload the app unpacks: everything git tracks, minus what no build ever reads (docs are for
# people, the workflows are for GitHub) - the repository the pipeline would run in, complete.
PAYLOAD_SKIP = ('docs/', '.github/')
PAYLOAD_NAME = 'payload.zip'   # inside the executable (--add-data), at sys._MEIPASS/<name> once it runs


def pyinstaller():
    r = subprocess.run([sys.executable, '-m', 'PyInstaller', '--version'], capture_output=True, text=True)
    if r.returncode:
        sys.exit('PyInstaller is missing: python3 -m pip install pyinstaller lz4 capstone fonttools '
                 + ('backports.zstd ' if sys.version_info < (3, 14) else '') + 'nsz')
    return r.stdout.strip()


def payload_files():
    """[(mode, path)] of every file the payload carries, straight from git: the modes come from the
    index (0o755 for gradlew and the translator binaries), so the zip restores them on every
    platform - a Windows checkout loses the execute bit on disk, and the Mac build freezes on one."""
    r = subprocess.run(['git', '-C', str(REPO), 'ls-files', '-s', '-z'], capture_output=True)
    if r.returncode: sys.exit('git ls-files failed: freeze from a checkout of the repository')
    out = []
    for rec in r.stdout.split(b'\0'):
        if not rec: continue
        meta, _, path = rec.decode('utf-8').partition('\t')
        mode = int(meta.split(' ')[0], 8) & 0o777   # '100755 <sha> <stage>\t<path>'
        if any(path.startswith(s) for s in PAYLOAD_SKIP): continue
        if not (REPO / path).is_file(): continue   # a submodule or a vanished file: git knows it, the disk does not
        out.append((mode, path))
    if not out: sys.exit('git tracks no files: freeze from a checkout of the repository')
    return out


def build_payload(dest):
    """payload.zip at dest: every tracked file, deflated, the modes in each entry's attributes, the
    timestamps pinned (the same tree zips to the same bytes - two builders can cross-check)."""
    dest = Path(dest); dest.parent.mkdir(parents=True, exist_ok=True)
    files = payload_files()
    with zipfile.ZipFile(dest, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for mode, path in files:
            zi = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))   # zip's epoch: no build date inside
            zi.external_attr = mode << 16   # what Python's and unzip's extract restore permissions from
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, (REPO / path).read_bytes())
    print(f'payload: {dest} ({len(files):,} files, {dest.stat().st_size / 2**20:.1f} MB)')
    return dest


def fatten():
    """Make every installed native module universal2, in place: PyInstaller's universal2 build
    refuses a thin .so/.dylib, and on the Mac most of the pipeline's wheels ship one arch only
    (lz4, capstone, the zstd backport, zstandard - an nsz dependency; a hand list lost track of
    the last one, so this walks site-packages itself). Each thin module's other arch is fetched
    from its PyPI wheel and lipo'd into the installed file. Returns False (and says why) when a
    thin one has no other-arch wheel: the build then cannot be universal2."""
    import importlib.metadata as md, json, sysconfig, tempfile, urllib.request
    other = 'x86_64' if platform.machine() == 'arm64' else 'arm64'
    py = f'cp{sys.version_info.major}{sys.version_info.minor}'
    site = Path(sysconfig.get_paths()['purelib'])
    dists = md.packages_distributions()
    lipo = lambda f: subprocess.run(['lipo', '-info', str(f)], capture_output=True, text=True).stdout
    thin_left = []
    for top in sorted(site.iterdir()):
        if not top.is_dir(): continue
        thin = [so for pat in ('*.so', '*.dylib') for so in top.rglob(pat)   # capstone ships a .dylib
                if other not in lipo(so)]
        if not thin: continue
        names = [d for d in dists.get(top.name, []) if d]
        merged = False
        for name in dict.fromkeys(names):   # two dists can own the same import (a venv's real one beside a fake): merge once
            try:
                ver = md.version(name)   # the installed one: its other-arch wheel has the same layout and version
                with urllib.request.urlopen(f'https://pypi.org/pypi/{name}/{ver}/json', timeout=30) as f:
                    files = json.load(f)['urls']
                cand = [f for f in files if f['filename'].endswith(f'_{other}.whl') and 'macosx' in f['filename']
                        and (py in f['filename'] or 'py3-none' in f['filename'] or 'abi3' in f['filename'])]
                if not cand: continue
                with tempfile.TemporaryDirectory() as tmp:
                    wheel = Path(tmp) / cand[0]['filename']
                    with urllib.request.urlopen(cand[0]['url'], timeout=300) as r: wheel.write_bytes(r.read())
                    with zipfile.ZipFile(wheel) as z: z.extractall(tmp)
                    tree = Path(tmp) / top.name   # a wheel unpacks under the import's own top-level folder
                    for so in thin:
                        piece = tree / so.relative_to(top)
                        if not piece.is_file(): continue
                        fat = Path(str(so) + '.fat')
                        if subprocess.run(['lipo', '-create', str(so), str(piece), '-o', str(fat)]).returncode: continue
                        shutil.move(fat, so); merged = True
            except (OSError, ValueError, zipfile.BadZipFile): continue   # no such project or no wheel: try the next name
        if merged:
            still = [so for so in thin if other not in lipo(so)]
            print(f'fatten: {top.name} is universal2 now ({len(thin) - len(still)} native module(s) merged)')
            thin = still
        if thin: thin_left.append(top.name)
    if thin_left:
        print(f'fatten: thin native modules remain in {thin_left}: no {other} wheel on PyPI for them', file=sys.stderr)
        return False
    return True


def main(argv):
    console = '--console' in argv   # a debugging variant: no --windowed, so a console sits behind the window
    argv = [a for a in argv if a != '--console']
    out = Path(argv[0]).resolve() if argv else REPO / 'dist'
    version = pyinstaller()
    # A window app on Windows and the Mac (no console behind the window; --check attaches to the
    # terminal it was run from, see _attach_console); on Linux a console app, so --check prints
    # from a terminal and the window opens from the file manager just the same.
    windowed = sys.platform in ('win32', 'darwin') and not console
    # The payload first: it is an input to the freeze, not something to place beside the result
    # (--add-data embeds it in the executable; a onefile child inherits the one unpacking anyway).
    work = REPO / 'build' / 'nxcompanion'
    payload = build_payload(work / PAYLOAD_NAME)
    universal = False
    if sys.platform == 'darwin':
        universal = fatten()   # the wheel-makers' thin native modules get their other arch
        if not universal: sys.exit('the universal2 Mac build needs every native module fat (see fatten above)')
    cmd = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
           '--name', NAME, '--distpath', str(out), '--workpath', str(work), '--specpath', str(work),
           '--onefile',   # the single file: the app unpacks itself once and its children reuse it
           '--add-data', f'{payload}{os.pathsep}.',   # the engine, inside the executable: sys._MEIPASS/payload.zip
           *( ['--windowed'] if windowed else [] ),
           *( ['--target-architecture', 'universal2'] if universal else [] ),
           *(a for h in HIDDEN for a in ('--hidden-import', h)),
           *(a for c in COLLECT for a in ('--collect-all', c)),
           str(ENTRY)]
    print(' '.join(cmd))
    r = subprocess.run(cmd, cwd=str(REPO))
    if r.returncode: sys.exit(r.returncode)
    app = out / f'{NAME}.app'
    exe = app / 'Contents' / 'MacOS' / NAME if sys.platform == 'darwin' else \
        out / (NAME + ('.exe' if os.name == 'nt' else ''))
    if not exe.exists(): sys.exit(f'no output at {exe}')
    inside = exe.stat().st_size / 2**20
    print(f'{exe} ({inside:.1f} MB, one file, engine inside) - PyInstaller {version}, Python {sys.version.split()[0]}')
    if sys.platform != 'darwin': print('smoke test: run it with --check')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
