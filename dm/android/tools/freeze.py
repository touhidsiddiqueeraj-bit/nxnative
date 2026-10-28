#!/usr/bin/env python3
"""Freeze the PC companion into one standalone executable (dm/android/tools/pc_companion.py):
the window, the toolchain guidance, the phone link over USB - and the build pipeline itself,
which a standalone companion runs inside its own Python: every helper the pipeline spawns is
[the executable, <script.py>, ...], exactly the shape it used for python3 (run_as_python), and the
third-party modules those scripts import - lz4 (block and frame), capstone, fontTools, the zstd
binding, nsz - are carried inside the file. The shader split's ProcessPoolExecutor works too:
freeze_support() in pc_companion.py runs the spawn children. What stays outside is what the
Toolchain window guides in: bash, make, python3, zstd, dotnet, Java and the Android SDK; the
repository itself too, whose folder the companion asks for (it is dropped into a checkout, or
pointed at one with --repo).

Usage:  python3 dm/android/tools/freeze.py [output dir]        (dist/ under the repository by default)
Needs:  python3 -m pip install pyinstaller lz4 capstone fonttools backports.zstd nsz
       (Python 3.14+ carries zstd itself; freeze with 3.12 for the backport, as the releases do)
The Mac build is universal2 (the python.org interpreter is, and so are the repository's own
translator binaries): the wheel-makers' thin native modules - lz4, the zstd backport - get their
other arch lipo'd in first (fatten, from the PyPI wheel of the other arch); the Windows build is
a window app (--check attaches to the terminal it was run from), the Linux build a console one.
"""
import os
import platform
import subprocess
import sys
import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
ENTRY = REPO / 'dm' / 'android' / 'tools' / 'pc_companion.py'
NAME = 'nxcompanion'
# What the pipeline's scripts import at run time (they run from the repository; their modules from
# here): lz4's block and frame are single imports the freeze cannot see; capstone, fontTools and nsz
# are walked dynamically or lazily, and backports.zstd is imported from a try/except in shader_split.py.
HIDDEN = ['lz4.block', 'lz4.frame'] + (['compression.zstd'] if sys.version_info >= (3, 14) else [])
COLLECT = ['capstone', 'fontTools', 'nsz'] + ([] if sys.version_info >= (3, 14) else ['backports.zstd'])
FATTEN = ['lz4', 'capstone', *COLLECT, 'pycryptodome']   # the ones that may carry a native module (nsz pulls pycryptodome)


def pyinstaller():
    r = subprocess.run([sys.executable, '-m', 'PyInstaller', '--version'], capture_output=True, text=True)
    if r.returncode:
        sys.exit('PyInstaller is missing: python3 -m pip install pyinstaller lz4 capstone fonttools '
                 + ('backports.zstd ' if sys.version_info < (3, 14) else '') + 'nsz')
    return r.stdout.strip()


def fatten(packages):
    """Make the installed native modules universal2, in place: PyInstaller's universal2 build refuses
    a thin .so, and on the Mac some of the pipeline's wheels ship one arch only (lz4 and the zstd
    backport; capstone and pycryptodome are fat already). The other arch's wheel is fetched from
    PyPI and lipo'd into the installed file. Returns False (and says why) when a thin one has no
    other-arch wheel: the build then cannot be universal2."""
    import importlib.util, json, tempfile, urllib.request, zipfile
    other = 'x86_64' if platform.machine() == 'arm64' else 'arm64'
    py = f'cp{sys.version_info.major}{sys.version_info.minor}'
    for pkg in packages:
        spec = importlib.util.find_spec(pkg)
        if spec is None or not spec.submodule_search_locations: continue
        root = Path(spec.submodule_search_locations[0])
        thin = [so for pat in ('*.so', '*.dylib') for so in root.rglob(pat)   # capstone ships a .dylib
                if other not in subprocess.run(['lipo', '-info', str(so)], capture_output=True, text=True).stdout]
        if not thin: continue
        with urllib.request.urlopen(f'https://pypi.org/pypi/{pkg}/json', timeout=30) as f:
            files = json.load(f)['urls']
        cand = [f for f in files if f['filename'].endswith(f'_{other}.whl') and 'macosx' in f['filename']
                and (py in f['filename'] or 'py3-none' in f['filename'] or 'abi3' in f['filename'])]
        if not cand:
            print(f'fatten: {pkg} has thin native modules but no {other} wheel on PyPI', file=sys.stderr)
            return False
        with tempfile.TemporaryDirectory() as tmp:
            wheel = Path(tmp) / cand[0]['filename']
            with urllib.request.urlopen(cand[0]['url'], timeout=300) as r: wheel.write_bytes(r.read())
            with zipfile.ZipFile(wheel) as z: z.extractall(tmp)
            tree = Path(tmp).joinpath(*pkg.split('.'))
            for so in thin:
                merge = Path(str(so) + '.fat')
                r = subprocess.run(['lipo', '-create', str(so), str(tree / so.relative_to(root)), '-o', str(merge)])
                if r.returncode or not merge.is_file():
                    print(f'fatten: lipo refused {so}', file=sys.stderr); return False
                shutil.move(merge, so)
        print(f'fatten: {pkg} is universal2 now ({len(thin)} native module(s) merged)')
    return True


def main(argv):
    out = Path(argv[0]).resolve() if argv else REPO / 'dist'
    version = pyinstaller()
    # A window app on Windows and the Mac (no console behind the window; --check attaches to the
    # terminal it was run from, see _attach_console); on Linux a console app, so --check prints
    # from a terminal and the window opens from the file manager just the same.
    windowed = sys.platform in ('win32', 'darwin')
    universal = False
    if sys.platform == 'darwin':
        universal = fatten(FATTEN)   # the wheel-makers' thin native modules get their other arch
        if not universal: sys.exit('the universal2 Mac build needs every native module fat (see fatten above)')
    cmd = [sys.executable, '-m', 'PyInstaller', '--onefile', '--noconfirm', '--clean',
           '--name', NAME, '--distpath', str(out), '--workpath', str(REPO / 'build' / 'nxcompanion'),
           '--specpath', str(REPO / 'build' / 'nxcompanion'),
           *( ['--windowed'] if windowed else [] ),
           *( ['--target-architecture', 'universal2'] if universal else [] ),
           *(a for h in HIDDEN for a in ('--hidden-import', h)),
           *(a for c in COLLECT for a in ('--collect-all', c)),
           str(ENTRY)]
    print(' '.join(cmd))
    r = subprocess.run(cmd, cwd=str(REPO))
    if r.returncode: sys.exit(r.returncode)
    exe = out / (NAME + ('.exe' if os.name == 'nt' else ''))
    app = out / f'{NAME}.app' if sys.platform == 'darwin' else None
    got = app if (app and app.exists()) else exe
    if not got.exists(): sys.exit(f'no output at {got}')
    print(f'{got} ({got.stat().st_size / 2**20:.1f} MB) - PyInstaller {version}, Python {sys.version.split()[0]}')
    if sys.platform != 'darwin': print('smoke test: run it with --check')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
