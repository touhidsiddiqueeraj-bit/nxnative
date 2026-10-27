#!/usr/bin/env python3
# Pack the game into the staged assets folder (pack_ipa.py's counterpart, for the Android build):
# the RomFS, the rewritten modules with their rewriter tables, the SPIR-V shader libraries and the
# add-on content each become one zip under <staged>/assets/dm/, written from their source folders
# without an intermediate copy (the staged side files - launcher.json, meta.json, fonts, icon, env -
# are already in place; build_apk.sh hands the whole folder to Gradle as extra assets).
#   modules.zip   the patched modules + the rewriter tables (loader.c reads them from the modules dir
#                 the app extracts) + the shader map and the PSO manifest (DM_NVN_SHMAP points there)
#   spv.zip       the chain's SPIR-V shader libraries (<key>.spv + <key>_b.json), under spv/b-<V>/
#   romfs.zip     the RomFS
#   aoc.zip       the DLC's RomFS folders by index (absent when there is none)
# deflate level 6, a file whose deflate is not smaller is stored; game data is mostly stored either
# way (pack_ipa.py's --store rule); zip64 end records wherever the count or the size needs them.
# Usage: pack_apk.py <staged dir> --romfs <dir> --modules <dir> --mods <name...> --spv <dir> [--aoc <dir>] [--tables <game profile dir>]
import argparse, collections, os, stat, struct, sys, time, zlib

LEVEL = 6
BIG = 256 << 20      # files above this are streamed instead of read whole
CHUNK = 16 << 20
L32, L16 = 0xFFFFFFFF, 0xFFFF
Entry = collections.namedtuple('Entry', 'name path st')


def walk(maps):
    """The archive's entries in order: for each mapping its parent folders, its root, then its tree
    depth first, sorted by name - pack_ipa.py's walk."""
    roots = {arc for arc, _ in maps}
    out, seen = [], set()
    def add(name, path, st):
        if name in seen: sys.exit(f'[RED] pack_apk: {name} twice in the archive')
        seen.add(name); out.append(Entry(name, path, st))
    def walkdir(d, arc):
        with os.scandir(d) as it: es = sorted(it, key=lambda e: e.name)
        for e in es:
            name = f'{arc}/{e.name}'
            if name in roots: continue   # another mapping's root: it adds its own
            st = e.stat(follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode): add(name + '/', e.path, st); walkdir(e.path, name)
            elif stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode): add(name, e.path, st)
            else: sys.exit(f'[RED] pack_apk: neither a file, a folder nor a symlink: {e.path}')
    for arc, src in maps:
        if os.path.isfile(src): add(arc, src, os.stat(src)); continue   # a single file (the modules' entries)
        add(arc + '/', src, os.stat(src)); walkdir(src, arc)
    return out


def dos_time(t):
    y, mo, d, h, mi, s = time.localtime(t)[:6]
    if y < 1980: y, mo, d, h, mi, s = 1980, 1, 1, 0, 0, 0
    return (h << 11) | (mi << 5) | (s // 2), ((y - 1980) << 9) | (mo << 5) | d


class Zip:
    def __init__(self, path):
        self.f = open(path, 'wb', buffering=1 << 20)
        self.cd, self.pos = [], 0

    def w(self, b):
        self.f.write(b); self.pos += len(b)

    def local(self, name, method, crc, csize, usize, t, d):
        nb = name.encode('utf-8', 'surrogateescape')
        fl = 0 if name.isascii() else 0x800
        off = self.pos
        self.w(struct.pack('<IHHHHHIIIHH', 0x04034B50, 20 if method == 8 else 10, fl, method, t, d, crc, min(csize, L32), min(usize, L32), len(nb), 0) + nb)
        return off, nb, fl

    def put(self, e, method, crc, data, usize, t, d):
        h = self.local(e.name, method, crc, len(data), usize, t, d)
        if data: self.w(data)
        self.cd.append((e, method, crc, len(data), usize, h[0], h[1], h[2], t, d))

    def stream(self, e, t, d):   # a big file: the CRC (and sizes) patched in after its bytes
        usize = e.st.st_size
        h = self.local(e.name, 8, 0, 0, usize, t, d)
        off = h[0]
        crc, csize, c = 0, 0, zlib.compressobj(LEVEL, zlib.DEFLATED, -15)
        with open(e.path, 'rb') as f:
            while b := f.read(CHUNK):
                crc = zlib.crc32(b, crc)
                b = c.compress(b); self.w(b); csize += len(b)
            b = c.flush(); self.w(b); csize += len(b)
        method = 8
        if csize >= usize:   # not smaller: stored instead - the sizes patch the header anew
            self.f.seek(off); self.f.truncate(); self.pos = off
            method = 0; csize = usize
            self.local(e.name, 0, 0, usize, usize, t, d)
            crc = 0
            with open(e.path, 'rb') as f:
                while b := f.read(CHUNK):
                    crc = zlib.crc32(b, crc); self.w(b)
        if method == 8:   # patch the CRC and the deflated sizes into the local header
            self.f.seek(off + 14); self.f.write(struct.pack('<I', crc) + struct.pack('<II', csize, usize)); self.f.seek(0, 2)
        self.cd.append((e, method, crc, csize, usize, off, h[1], h[2], t, d))

    def close(self):
        cdoff = self.pos
        for e, method, crc, csize, usize, off, nb, fl, t, d in self.cd:
            z = [v for v in (usize, csize, off) if v >= L32]
            ext = struct.pack(f'<HH{len(z)}Q', 1, 8 * len(z), *z) if z else b''
            mode = e.st.st_mode if e.st else (stat.S_IFDIR | 0o755)
            self.w(struct.pack('<IHHHHHHIIIHHHHHII', 0x02014B50, 0x031E, 45 if z else 20 if method == 8 else 10, fl, method, t, d, crc, min(csize, L32), min(usize, L32),
                               len(nb), len(ext), 0, 0, 0, (mode << 16) | (0x10 if stat.S_ISDIR(mode) else 0), min(off, L32)) + nb + ext)
        n, blob = len(self.cd), self.pos - cdoff
        if n >= L16 or cdoff >= L32 or blob >= L32:
            zo = self.pos
            self.w(struct.pack('<IQHHIIQQQQ', 0x06064B50, 44, 0x031E, 45, 0, 0, n, n, blob, cdoff))
            self.w(struct.pack('<IIQI', 0x07064B50, 0, zo, 1))
        self.w(struct.pack('<IHHHHIIH', 0x06054B50, 0, 0, min(n, L16), min(n, L16), min(blob, L32), min(cdoff, L32), 0))
        self.f.close()


def pack(zip_path, maps, note):
    es = walk(maps)
    total = sum(e.st.st_size for e in es if e.st and stat.S_ISREG(e.st.st_mode))
    print(f'pack: {note}: {len(es)} entries, {total} bytes', flush=True)
    t0 = time.time()
    z = Zip(zip_path + '.partial')
    for e in es:
        t, d = dos_time(e.st.st_mtime if e.st else 0)
        if not e.st:
            z.put(e, 0, 0, b'', 0, t, d); continue   # a synthetic parent
        mode = e.st.st_mode
        if stat.S_ISDIR(mode):
            h = z.local(e.name, 0, 0, 0, 0, t, d)
            z.cd.append((e, 0, 0, 0, 0, h[0], h[1], h[2], t, d))
        elif stat.S_ISLNK(mode):
            target = os.readlink(e.path).encode('utf-8', 'surrogateescape')
            z.put(e, 0, zlib.crc32(target), target, len(target), t, d)
        elif e.st.st_size > BIG:
            z.stream(e, t, d)
        else:
            with open(e.path, 'rb') as f: data = f.read()
            crc, usize = zlib.crc32(data), len(data)
            method, blob = 0, data
            if data:
                c = zlib.compressobj(LEVEL, zlib.DEFLATED, -15); comp = c.compress(data) + c.flush()
                if len(comp) < len(data): method, blob = 8, comp
            z.put(e, method, crc, blob, usize, t, d)
    z.close()
    os.replace(zip_path + '.partial', zip_path)
    print(f'packed: {os.path.basename(zip_path)} ({os.path.getsize(zip_path) / 2**20:.1f} MiB, {len(es)} entries, {time.time() - t0:.0f} s)', flush=True)


def main():
    ap = argparse.ArgumentParser(description='Pack the game into the staged assets folder (zips under assets/dm/)')
    ap.add_argument('staged'); ap.add_argument('--romfs', required=True); ap.add_argument('--modules', required=True)
    ap.add_argument('--mods', nargs='+', required=True, help='the module names (main sdk ...) whose patched files and tables go in')
    ap.add_argument('--spv', required=True, help="the chain's SPIR-V folder (holds b-<V>/)")
    ap.add_argument('--aoc', default=''); ap.add_argument('--tables', required=True, help='the game profile dir (shader_map.tsv, pso_manifest.tsv)')
    a = ap.parse_args()
    dm = os.path.join(os.path.abspath(a.staged), 'assets', 'dm')
    os.makedirs(dm, exist_ok=True)
    # The modules: the patched images, the rewriter tables beside them, the shader map and PSO manifest
    mods_entries = []
    for name in a.mods:
        for suffix in ('.patched.bin', '.fixups.tsv', '.gates.tsv', '.cuts.tsv', '.obs.tsv'):
            p = os.path.join(a.modules, name + suffix)
            if os.path.exists(p): mods_entries.append((f'modules/{name}{suffix}', p))
    nro = os.path.join(a.modules, 'nro.tsv')
    if os.path.exists(nro): mods_entries.append(('modules/nro.tsv', nro))   # the NRO modules the game loads with nn::ro
    for tab in ('shader_map.tsv', 'pso_manifest.tsv', 'c1_full.tsv', 'cb_extent.tsv', 'shader_pairs.tsv', 'pso_pairs.tsv'):
        p = os.path.join(a.tables, tab)
        if os.path.exists(p): mods_entries.append((f'modules/{tab}', p))
    if not mods_entries: sys.exit('[RED] pack_apk: no modules to pack')
    spv_name = 'spv/' + os.path.basename(os.path.normpath(a.spv))
    zips = [('modules.zip', mods_entries, 'modules'),
            ('spv.zip', [(spv_name, a.spv)], 'shaders'),
            ('romfs.zip', [('romfs', a.romfs)], 'RomFS')]
    if a.aoc and os.path.isdir(a.aoc): zips.append(('aoc.zip', [('aoc', a.aoc)], 'add-on content'))
    for zname, maps, note in zips:
        pack(os.path.join(dm, zname), maps, note)
    print(f'pack_apk: staged assets at {dm}', flush=True)


if __name__ == '__main__': main()
