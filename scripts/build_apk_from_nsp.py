#!/usr/bin/env python3
# Android APK straight from the NSPs, sharing the iOS pipeline's stages (scripts/build_ipa_from_nsp.py is
# imported as a module: identify the game → decompress NSZ → extract (nx_extract) → rewrite the code
# modules (patch_nso.py) → the shader chain → dm/android/tools/build_apk.sh, which packs the game into
# the app (pack_apk.py) and builds it with Gradle and the NDK (libdmhost.so).
#   The shaders are the chain's SPIR-V output (dmsh_build.py's SPVOUT), which the Vulkan backend loads
#   as is: the chain's Metal steps run beside them and cache, so the same work serves an iOS build too.
#   NVN games are the Android port's scope (the profiles' majority); a Vulkan or OpenGL game (Hades,
#   Digimon Story Cyber Sleuth) stops with an explanation - their host layers (dm/gfx/vk, dm/gfx/gl)
#   are the Metal backend's next porting steps.
#   A game with no profile is surveyed, then gets a provisional profile and a best-effort APK that may
#   not boot (the same path as the iOS pipeline's provisional IPAs).
# Usage: python3 scripts/build_apk_from_nsp.py <NSP folder> -k <prod.keys> [options]
#   -o <out.apk> (default: the game's name beside the NSPs), --app-name, --language / -l (as the iOS
#   script takes them), --show-fps, --survey-only, --provisional, -p <version>, -h lists everything.
# The Python running this file needs lz4, capstone and fonttools; also needed: the Android SDK / NDK
# (ANDROID_HOME, with cmake 3.22.1 and the NDK 26) and Java 17 for Gradle. The first build compiles the
# host (minutes); the game files land in the APK's assets and extract on the device at first start.
# Key values are never printed.
import argparse, os, shutil, sys
from pathlib import Path

HERE = Path(globals().get('__file__') or sys.argv[0]).resolve()
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / 'scripts'))
sys.path.insert(0, str(REPO / 'dm/games'))
sys.path.insert(0, str(REPO / 'dm/tools'))

import build_ipa_from_nsp as ios   # the shared stages: identification, extraction, the rewrite, the shader split
from build_ipa_from_nsp import (BuildError, PROG, say, run, rmtree, scan_nsps, decompress_nsz, build_nx_extract,
                                extract, extract_aoc, rewrite, split_shaders, pick_nsps, nsp_versions, vnum,
                                language_code, game_language, ask, in_ipython, find_repo, check_prereqs, chain_of,
                                same_build, survey, write_profile, derive_profile,
                                provisional_prep, refine_after_rewrite, report_profile_status, can_clone,
                                amount)


def parse_args(argv):
    ap = argparse.ArgumentParser(prog='build_apk_from_nsp.py',
                                 description='Android APK from the NSPs of one game (build_ipa_from_nsp.py\'s Android counterpart)')
    ap.add_argument('nsp_dir', nargs='?', help='folder holding one game\'s NSPs (the base game, with or without an update; DLC when present is packed too)')
    ap.add_argument('-k', '--keys', help='path of prod.keys')
    ap.add_argument('-o', '--out', help='output APK (default: the game\'s name beside the NSPs)')
    ap.add_argument('--app-name', help='the app\'s name on the home screen (default: the game name in the base NSP)')
    ap.add_argument('-l', '--language', help='game language, a code or a name: ja, fr, de, es, zh-Hans, zh-Hant, ko, Japanese, ... (default: English)')
    ap.add_argument('-p', '--package', help='version packed into the APK (default: the newest in the folder)')
    ap.add_argument('--show-fps', action='store_true', default=None, help='show the game\'s frame rate on the phone (default: on)')
    ap.add_argument('--no-show-fps', action='store_false', dest='show_fps', help='hide the frame-rate counter (default: on)')
    ap.add_argument('--survey-only', action='store_true', help='only write the survey of a game with no profile')
    ap.add_argument('--provisional', action='store_true', help='build from a provisional profile even when dm/games has one')
    ap.add_argument('--no-keep-extracted', action='store_true', help='delete the extracted game files after the APK is built')
    ap.add_argument('--env-file', help='environment switches for a diagnostic build (one KEY=VALUE per line)')
    ap.add_argument('--allow-damaged', action='store_true', help='pack a damaged NSP (files failing the NCA hash check)')
    return ap.parse_args(argv)


def main(args):
    PROG.mode = ios.progress_mode(ios.PROGRESS)
    repo = find_repo(HERE)
    nsp_dir = Path(os.path.expanduser(args.nsp_dir or ios.NSP_DIR or ask('NSP folder: ', 'the NSP folder'))).resolve()
    keys = Path(os.path.expanduser(args.keys or ios.PROD_KEYS or ask('path of prod.keys: ', 'prod.keys (-k)'))).resolve()
    if not nsp_dir.is_dir(): raise BuildError(f'not a folder: {nsp_dir}')
    if not keys.is_file(): raise BuildError(f'no such file: {keys}')
    import game as gamemod, profile_guess as pg, nacp
    lang_in = args.language or ios.LANGUAGE
    want_lang = language_code(lang_in) if lang_in else 'en-US'
    env = dict(os.environ, PATH=os.pathsep.join(ios.TOOL_PATHS + [os.environ.get('PATH', '/usr/bin:/bin')]))
    env.setdefault('J', str(os.cpu_count() or 8))
    py = sys.executable
    if not os.environ.get('ANDROID_HOME'): raise BuildError('ANDROID_HOME is not set: point it at the Android SDK (with the NDK 26 and cmake 3.22.1 installed)')
    if shutil.which('java', path=env['PATH']) is None: raise BuildError('java not found (Java 17: Gradle needs it)')

    # ---- identify the game, as the iOS pipeline does ----
    app, fam = scan_nsps(nsp_dir)
    fam = decompress_nsz(nsp_dir, app, fam, keys, env)
    force = args.provisional
    g, provisional, shadowed = None if force else gamemod.by_title(app), False, False
    alias = None if force or g else same_build(nsp_dir, app, fam, keys, repo, env)
    if alias:
        g = alias[0]; say(f'no game profile for {app:016X}, but its main build {alias[2]} is game profile {g.id}\'s {alias[1]} ({g.get("name", "")}) under another title ID')
    if g is None:
        pdir = nsp_dir / f'{app:016X}_build' / 'profile'
        if not args.survey_only:
            rmtree(ios.derived_key(pdir / f'{app:016X}'))
        if (pdir / f'{app:016X}' / 'game.tsv').exists() and not args.survey_only:
            say(f'no game profile for {app:016X}: reusing the provisional profile from the last run (delete it to survey again)')
        else:
            facts = survey(nsp_dir, app, fam, keys, repo, py, env)
            if args.survey_only: return
            facts['survey'] = str(nsp_dir / f'{app:016X}_survey' / 'survey.md')
            write_profile(pdir / f'{app:016X}', app, facts)
        os.environ['DM_GAMES_DIR'] = env['DM_GAMES_DIR'] = os.pathsep.join([str(pdir)] + [d for d in os.environ.get('DM_GAMES_DIR', '').split(os.pathsep) if d and d != str(pdir)])
        if force and gamemod.by_title(app):
            os.environ['DM_GAMES_FIRST'] = env['DM_GAMES_FIRST'] = '1'; shadowed = True
        g, provisional = gamemod.load(f'{app:016X}'), True
        say('[RED] provisional game profile: values guessed from the survey, the game may not boot')
    say(f'game profile: {g.id}{" (provisional: " + str(g.dir) + ")" if provisional else ""}')
    builds, need = g.builds(), g.words('shader_versions')
    have, derived = nsp_versions(fam, builds), False
    pkg_ver = args.package or max(have, key=vnum)
    if pkg_ver not in have: raise BuildError(f'the version to pack (-p) is not in the folder, which holds {", ".join(sorted(have, key=vnum))}')
    if provisional and pkg_ver not in builds: raise BuildError(f'the version to pack must be one the game profile registers: {", ".join(builds)}')
    if not provisional and pkg_ver not in builds or alias and not derived:
        derived = f'{pkg_ver} is not a version game profile {g.id} registers' if pkg_ver not in builds else (f'title {app:016X} is not game profile {g.id}\'s title {g.title_id:016X}' if alias else derived)
        if derived and not provisional: say(f'{derived}: packing {pkg_ver} from a profile derived from it')
    versions = [pkg_ver] if derived else (need + ([pkg_ver] if pkg_ver not in need else []))
    work = nsp_dir / f'{g.id}_build'
    out_apk = Path(os.path.expanduser(args.out or f'{ios.mac_app_name(nacp.name(Path(work, "extract") / pkg_ver / "control.nacp") or "Game")}.apk')) if args.out else None
    env.update(DM_GAME=g.id, DM_ROOT=str(work), WORK=str(work / 'tmp'))
    logs = work / 'logs'
    xlate = repo / 'dm/gfx/tools/dmsh_xlate_bin/dmsh_xlate'
    check_prereqs(env, xlate, android=True)
    picks = pick_nsps(fam, have, versions)

    # ---- extract, rewrite ----
    ex = work / 'extract'; ex.mkdir(parents=True, exist_ok=True); (work / 'tmp').mkdir(exist_ok=True)
    PROG.plan(1 + len(versions) + bool(fam['dlc']) + 3)
    PROG.stage('build nx_extract')
    nx = build_nx_extract(repo, work, logs, env)
    PROG.end()
    dirs, clone = {}, None
    for ver in versions:
        b, u = picks[ver]
        dirs[ver] = extract(nx, keys, ex, ver, b, u, clone, logs, g)
        if u is None and clone is None and can_clone(ex): clone = dirs[ver] / 'RomFS'
    if derived:
        pdir = work / 'profile'
        disp = derive_profile(g, pdir / g.id, pkg_ver, dirs[pkg_ver], g.words('modules'), app)
        os.environ['DM_GAMES_DIR'] = env['DM_GAMES_DIR'] = os.pathsep.join([str(pdir)] + [d for d in os.environ.get('DM_GAMES_DIR', '').split(os.pathsep) if d and d != str(pdir)])
        os.environ['DM_GAMES_FIRST'] = env['DM_GAMES_FIRST'] = '1'
        g = gamemod.load(g.id)
        say(f'derived game profile for {pkg_ver} ({disp}): {g.dir}')
    game = dirs[pkg_ver]
    src = pg.build_sources(game / 'ExeFS', game / 'RomFS', g.words('modules'))
    gfx = src['graphics'][0]
    if gfx != 'nvn': raise BuildError(f'this game runs on {"Vulkan" if gfx == "vulkan" else "OpenGL"} ({src["graphics"][1]}), whose host layer the Android port has not carried over yet (dm/gfx/{"vk" if gfx == "vulkan" else "gl"}: the Metal backend\'s next porting step); NVN games are the port\'s scope today')
    if src['glslc_capture'][0]: say(src['glslc_capture'][1]); say('the run-time shader capture boots the game in the Mac host: run this script on a Mac for this game')
    aoc = extract_aoc(nx, keys, repo, work, app, fam, logs)
    base = next((dirs[v] for v in versions if picks[v][1] is None), dirs[versions[0]])
    app_name = args.app_name or nacp.name(base / 'control.nacp')
    if not app_name: raise BuildError(f'no game name in the base extraction ({base / "control.nacp"}; delete extract/ and run again, or give --app-name)')
    app_version = nacp.version(game / 'control.nacp') or nacp.version(base / 'control.nacp')
    have_lang = nacp.languages(game / 'control.nacp') or nacp.languages(base / 'control.nacp')
    lang = game_language(want_lang, bool(lang_in), have_lang)
    say(f'app name: {app_name}; version: {app_version or "1.0"}; game language: {lang}')

    patched = work / f'patched_{pkg_ver}'
    g = rewrite(py, repo, game / 'ExeFS', patched, env, logs, g, provisional)
    ios.chain_header(py, repo, env, logs)
    if provisional or derived:
        provisional_prep(py, repo, env, logs, g)
        g = refine_after_rewrite(g, game, patched, None if not derived else g)

    # ---- the shaders: the chain's SPIR-V ----
    sh = work / 'shader'
    sv = versions if derived else need
    split_shaders(py, repo, [dirs[v] / 'RomFS' for v in sv], [dirs[v] / 'ExeFS/main' for v in sv], sh / 'pkg', env, logs, g, bool(provisional or derived), None, sorted(p for p in aoc.iterdir() if p.is_dir()) if aoc else [], None)
    chain = chain_of(repo)
    spv = work / 'build' / 'android' / 'spv'
    PROG.stage('SPIR-V shaders')
    # own_compilers.py drives dmsh_build.py (the compiler pinning is the Mac's; on Linux the
    # translated SPIR-V needs no Metal compiler, the Mac outputs it caches come along when present)
    run([py, repo / 'dm/gfx/tools/own_compilers.py', sh / 'pkg', sh / 'cache'], logs / 'dmsh_build_spv.log',
        env=dict(env, XLATE=str(xlate), SPVOUT=str(spv)))
    PROG.end()
    libs = spv / f'b-{chain}'
    if not any(libs.glob('*.spv')): raise BuildError(f'no SPIR-V shaders at {libs} (dmsh_build.log)')
    say(f'SPIR-V shaders: {sum(1 for _ in libs.glob("*.spv")):,} libraries (chain b-{chain})')

    # ---- the APK ----
    out_apk = Path(os.path.expanduser(args.out)).resolve() if args.out else nsp_dir / f'{ios.mac_app_name(app_name)}{"-provisional" if shadowed else ""}.apk'
    say(f'building the APK → {out_apk}')
    apk_env = dict(env, GAME=g.id, W=str(work), PATCHED=str(patched), ROMFS=str(game / 'RomFS'), SPV=str(spv), FONT_PY=py,
                   DM_DISPLAY_NAME=app_name, DM_LANG=lang, AOC=str(aoc or ''), DM_SHOW_FPS='1' if (args.show_fps is not False) else '0')
    if app_version: apk_env['DM_APP_VERSION'] = app_version
    if args.env_file: apk_env['DM_ENV_FILE'] = str(Path(os.path.expanduser(args.env_file)).resolve())
    PROG.stage('app build', step='packing the game into the app')
    run(['bash', repo / 'dm/android/tools/build_apk.sh', str(out_apk)], logs / 'build_apk.log', env=apk_env)
    if not out_apk.is_file(): raise BuildError(f'no APK at {out_apk}')
    PROG.end(f'{out_apk.name}, {amount(out_apk.stat().st_size, "B")}')
    rmtree(work / 'tmp')
    if args.no_keep_extracted:
        say(f'deleting the extracted game files: {ex}'); rmtree(ex)
    say(f'done: {out_apk} ({out_apk.stat().st_size / 2**30:.2f} GB); install with adb install -r {out_apk}')
    say("the app's files land in its private storage on first start; its log is in Android/data (log.txt), its saves in files/save")
    report_profile_status(g, g, pkg_ver, derived, provisional)


def cli(argv):
    environ = dict(os.environ)
    try:
        main(parse_args(argv))
    except BuildError as e:
        PROG.close(ok=False); PROG.say(f'[RED] {e}'); return 1
    except KeyboardInterrupt:
        PROG.close(ok=False); PROG.say('[RED] interrupted'); return 130
    except Exception as e:
        PROG.close(ok=False)
        if PROG.mode == 'json': PROG.say(f'[RED] {type(e).__name__}: {e}')
        raise
    finally:
        PROG.close(ok=False)
        for k in os.environ.keys() - environ.keys(): del os.environ[k]
        os.environ.update(environ)
    return 0


if __name__ == '__main__':
    code = cli(sys.argv[1:])
    if not in_ipython(): sys.exit(code)
