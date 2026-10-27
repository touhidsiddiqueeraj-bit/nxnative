"""The staged-asset packs pack_apk.py makes round-trip: entries, folders, symlinks, modes, the
stored-or-deflated rule, the streaming path above BIG, and zip64 counts (test_pack_ipa.py's checks,
for the Android packer)."""
import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile

REPO = Path(__file__).resolve().parents[3]
PACK = REPO / 'dm/android/tools/pack_apk.py'


class PackApk(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(); self.addCleanup(self.tmpdir.cleanup)
        self.tmp = Path(self.tmpdir.name)
        self.staged = self.tmp / 'stage'
        self.romfs = self.tmp / 'romfs'; (self.romfs / 'd\U0001F600j\340').mkdir(parents=True)
        (self.romfs / 'random.bin').write_bytes(os.urandom(30000))
        (self.romfs / 'text.txt').write_bytes(b'hello world ' * 4000)
        (self.romfs / 'd\U0001F600j\340/empty').write_bytes(b'')
        try:
            os.symlink('text.txt', self.romfs / 'link'); self.link = True
        except (OSError, NotImplementedError):
            self.link = None   # a filesystem that forbids links
        os.chmod(self.romfs / 'text.txt', 0o755)
        self.mods = self.tmp / 'patched'; self.mods.mkdir()
        (self.mods / 'main.patched.bin').write_bytes(os.urandom(8192))
        (self.mods / 'main.fixups.tsv').write_bytes(b'# KEY\tvalue\nTEXT_END\t1000\n')
        (self.mods / 'nro.tsv').write_bytes(b'sub\t0\t1\n')
        self.tables = self.tmp / 'profile'; self.tables.mkdir()
        (self.tables / 'shader_map.tsv').write_bytes(b'key\tvalue\n')
        (self.tables / 'c1_full.tsv').write_bytes(b'# chain b-1 inputs x game\n')
        self.spv = self.tmp / 'out' / 'b-1'; self.spv.mkdir(parents=True)
        (self.spv / 'a.spv').write_bytes(os.urandom(4096))
        (self.spv / 'a_b.json').write_bytes(b'{}')
        self.aoc = self.tmp / 'aoc'; (self.aoc / '0').mkdir(parents=True)
        (self.aoc / '0' / 'data.bin').write_bytes(b'A' * 100)

    def run_pack(self, *extra, big=None):
        cmd = [sys.executable, str(PACK), str(self.staged), '--romfs', str(self.romfs),
               '--modules', str(self.mods), '--mods', 'main', 'sdk', '--spv', str(self.spv),
               '--tables', str(self.tables), *extra]
        r = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, (r.stdout or '') + (r.stderr or ''))
        return self.staged / 'assets' / 'dm'

    def test_round_trip(self):
        dm = self.run_pack('--aoc', str(self.aoc))
        with zipfile.ZipFile(dm / 'romfs.zip') as z:
            names = z.namelist()
            self.assertIn('romfs/random.bin', names)
            self.assertEqual(z.read('romfs/text.txt'), b'hello world ' * 4000)
            self.assertEqual(z.read('romfs/d\U0001F600j\340/empty'), b'')
            self.assertEqual(z.getinfo('romfs/text.txt').external_attr >> 16 & 0o777, 0o755)   # Unix modes kept
            if self.link is not None:
                self.assertEqual(z.read('romfs/link'), b'text.txt')   # a link is the link text, stored
            self.assertEqual(z.getinfo('romfs/random.bin').compress_type, zipfile.ZIP_STORED)   # random data: stored, deflate would grow it
            self.assertEqual(z.getinfo('romfs/text.txt').compress_type, zipfile.ZIP_DEFLATED)   # text deflates smaller
        with zipfile.ZipFile(dm / 'modules.zip') as z:
            names = z.namelist()
            self.assertIn('modules/main.patched.bin', names)
            self.assertIn('modules/main.fixups.tsv', names)
            self.assertIn('modules/nro.tsv', names)   # the NRO table beside the modules
            self.assertIn('modules/shader_map.tsv', names)   # the shader map from the profile
            self.assertIn('modules/c1_full.tsv', names)
            self.assertNotIn('modules/sdk.patched.bin', names)   # a module with no rewritten files stays out
        with zipfile.ZipFile(dm / 'spv.zip') as z:
            self.assertEqual(z.read('spv/b-1/a.spv'), (self.spv / 'a.spv').read_bytes())
            self.assertIn('spv/b-1/a_b.json', z.namelist())
        with zipfile.ZipFile(dm / 'aoc.zip') as z:
            self.assertEqual(z.read('aoc/0/data.bin'), b'A' * 100)

    def test_no_aoc_no_zip(self):
        dm = self.run_pack()
        self.assertTrue((dm / 'modules.zip').is_file())
        self.assertFalse((dm / 'aoc.zip').exists())   # no add-on content: no aoc.zip

    def test_streamed_big_file(self):
        big = self.romfs / 'big.dat'
        big.write_bytes(b'A' * (pack_big() + 1000))   # above the streaming threshold
        dm = self.run_pack()
        with zipfile.ZipFile(dm / 'romfs.zip') as z:
            data = z.read('romfs/big.dat')
            self.assertEqual(len(data), big.stat().st_size)
            self.assertEqual(data, b'A' * big.stat().st_size)   # the CRC patched after streaming
            self.assertEqual(z.testzip(), None)

    def test_repeat_pack_replaces(self):
        self.run_pack()
        before = (self.staged / 'assets' / 'dm' / 'modules.zip').read_bytes()
        (self.mods / 'main.fixups.tsv').write_bytes(b'# KEY\tvalue\nTEXT_END\t2000\n')
        self.run_pack()
        after = (self.staged / 'assets' / 'dm' / 'modules.zip').read_bytes()
        self.assertNotEqual(before, after)   # a changed input changes the pack (no reuse index on Android: the assets pack whole)


def pack_big():
    spec = importlib.util.spec_from_file_location('pack_apk_mod', PACK)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m.BIG


if __name__ == '__main__':
    unittest.main()
