#!/bin/bash
# Build one APK: the host compiled into libdmhost.so (Gradle + NDK, dm/android), the game packed as
# its assets (pack_apk.py: the RomFS, the rewritten modules and their tables, the SPIR-V shader
# libraries, the fonts, the DLC), the app's name / version / id set from the game. The counterpart of
# dm/ios/tools/build_ipa.sh, which packs the same pieces into an IPA; the extraction and the rewrite
# ran before it (scripts/build_apk_from_nsp.py, whose stages it shares with the iOS pipeline).
# Runs on Linux or macOS. Never signs with a release key: the APK is debug-signed for installing;
# the CI workflow (.github/workflows/android.yml) signs releases.
# Usage: dm/android/tools/build_apk.sh [output.apk]
#   Environment: GAME=game profile (dm/games/<base game title ID>, or one in DM_GAMES_DIR; required),
#   W=the game's work folder (required: <NSP folder>/<title ID>-work, never the home folder), PATCHED /
#   ROMFS / SPV may each be overridden (SPV: the translated SPIR-V shaders, SPVOUT of dmsh_build.py),
#   AOC=add-on content directory (default $W/data/aoc; when it exists it packs as assets/dm/aoc.zip),
#   DM_DISPLAY_NAME / DM_APP_VERSION / DM_APP_ID override the name / version / application id (the
#   defaults come from the game's control data, as build_ipa.sh takes them),
#   DM_LANG=<language code> is the game language (default en-US; the launcher replaces it on the device),
#   DM_SHOW_FPS=1 turns the frame-rate counter on, DM_ICON=another image for the app icon,
#   DM_ENV_FILE=file packs a diagnostic env file (one KEY=VALUE per line), STD_FONT=the shared-font
#   stand-in (default: the first of the usual suspects that exists).
set -u
cd "$(dirname "$0")/.." || exit 1
ANDROID=$PWD; DM=$(cd .. && pwd)
t() { echo "[$(date +%T)] $*"; }; die() { t "FAIL: $*"; exit 1; }
[ -n "${GAME:-}" ] || die "GAME is not set: GAME=<base game title ID> $0; profiles: $(python3 "$DM/games/game.py" list | awk -F'\t' '{printf "%s (%s)  ", $1, $3}')"
export GAME; GD=$(python3 "$DM/games/game.py" dir "$GAME") || die "no game profile $GAME"
gp() { python3 "$DM/games/game.py" "$GAME" "$1" || die "game profile $GD: $1"; }
[ -f "$GD/game.tsv" ] || die "no game profile $GD/game.tsv"
[ -n "${W:-}" ] || die "W is not set: W=<NSP folder>/$GAME-work (the game's work folder) $0"
PKG=$(gp package)
PATCHED=${PATCHED:-$W/build/patched_$PKG}; ROMFS=${ROMFS:-$W/data/$PKG/RomFS}; SPV=${SPV:-$W/build/android/spv}; AOC=${AOC-$W/data/aoc}
OUT_APK=${1:-$(dirname "$W")/$(gp ipa | sed 's/\.ipa$/.apk/')}
mkdir -p "$W/build/android" || die "mkdir $W/build/android"
NACP=$W/data/v0/control.nacp; [ -f "$NACP" ] || NACP=$(dirname "$ROMFS")/control.nacp
[ -n "${DM_DISPLAY_NAME:-}" ] || DM_DISPLAY_NAME=$(python3 "$DM/tools/nacp.py" "$NACP") || die "no game name in $NACP (re-extract with nx_extract, or set DM_DISPLAY_NAME)"
VNACP=$(dirname "$ROMFS")/control.nacp; [ -f "$VNACP" ] || VNACP=$W/data/v0/control.nacp
[ -n "${DM_APP_VERSION:-}" ] || DM_APP_VERSION=$(python3 "$DM/tools/nacp.py" --version "$VNACP") || { t "no display version in $VNACP: the app version is 1.0"; DM_APP_VERSION=1.0; }
export LC_ALL=C DM_APP_ID=${DM_APP_ID:-org.nxnative.$(echo "$GAME" | tr 'A-Z' 'a-z')} DM_DISPLAY_NAME DM_APP_VERSION
if [ "${DM_SHOW_FPS:-0}" = 1 ]; then export DM_SHOW_FPS=1; else export DM_SHOW_FPS=0; fi
export DM_LANG=${DM_LANG:-en-US}
MODS=$(gp modules); MARKER=$(gp romfs_marker)
for f in $MODS; do [ -f "$PATCHED/$f.patched.bin" ] && [ -f "$PATCHED/$f.fixups.tsv" ] || die "no rewritten module $PATCHED/$f (run dm/tools/patch_nso.py first)"; done
[ -e "$ROMFS/$MARKER" ] || die "no RomFS at $ROMFS (missing $MARKER)"
V=$(sed -n 's/^#define DMSH_CHAIN "\(.*\)"/\1/p' "$DM/gfx/nvn/dmsh_chain.h")   # the chain version the host is compiled with, one for every game
[ -n "$V" ] || die "no chain version in $DM/gfx/nvn/dmsh_chain.h"
[ -n "$(ls "$SPV/b-$V" 2>/dev/null)" ] || die "no SPIR-V shader libraries in $SPV/b-$V (SPVOUT=$SPV dmsh_build.py; scripts/build_apk_from_nsp.py runs it)"

t "app: $DM_DISPLAY_NAME $DM_APP_VERSION ($DM_APP_ID), language $DM_LANG$([ "$DM_SHOW_FPS" = 1 ] && echo ', frame-rate counter on')"
# The packed side files, in the staged assets folder Gradle packs from (app/build.gradle.kts takes
# it as an extra assets source dir; nothing game-sized ever copies into the repository tree)
STAGE=$W/build/android/stage; STAGED=$STAGE/assets; rm -rf "$STAGE"; mkdir -p "$STAGED/dm"
L=$(GFXAPI=nvn python3 "$DM/games/settings.py" "$GAME" "$PATCHED" "$VNACP" "$STAGED/dm/launcher.json") || die "launcher settings (dm/games/settings.py)"
t "launcher: $L"
# The meta the app reads: the pad layout and the operation mode the game profile sets (the iOS build
# compiles them into the host; the app takes them from here)
# pad_layout / op_mode are the profile's words (game.py's header generator maps them the same way):
# simple = 2D, full = 3D, touch = Tap; console = docked, handheld = the touch screen in use
PL=$(python3 "$DM/games/game.py" "$GAME" pad_layout 2>/dev/null || true); PL=${PL:-full}
PLN=$( [ "$PL" = simple ] && echo 0 || { [ "$PL" = touch ] && echo 2 || echo 1; } )
OM=$(python3 "$DM/games/game.py" "$GAME" op_mode 2>/dev/null || true); OM=${OM:-console}
OMN=$( [ "$OM" = handheld ] && echo 0 || echo 1 )
printf '{"pad_layout": %s, "opmode": %s, "show_fps": %s}\n' "$PLN" "$OMN" "$DM_SHOW_FPS" > "$STAGED/dm/meta.json" || die "meta.json"
if [ -n "${DM_ENV_FILE:-}" ]; then cp "$DM_ENV_FILE" "$STAGED/dm/env.txt" || die "copy DM_ENV_FILE"; t "env: $(grep -v '^#' "$DM_ENV_FILE" | tr '\n' ' ')"; fi
# The app icon: the game's own (icon.jpg from the control data, decoded by content; Android scales it)
ICON=${DM_ICON:-$(dirname "$ROMFS")/icon.jpg}
if [ -f "$ICON" ]; then cp "$ICON" "$STAGED/dm/icon.png" && t "app icon: $ICON"
else t "no app icon ($ICON not found): the launcher shows no picture"; fi
# Shared-font stand-ins: standard = a Unicode text font (Arial Unicode on the Mac; Noto / DejaVu on
# Linux), Nintendo extension = the button icons (make_ext_font.py, needs fontTools)
STD=${STD_FONT:-$(for f in "/System/Library/Fonts/Supplemental/Arial Unicode.ttf" /usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf; do [ -f "$f" ] && echo "$f" && break; done)}
[ -n "$STD" ] || die "no shared-font stand-in (STD_FONT=a TTF / TTC)"
mkdir -p "$STAGED/dm/font"
cp "$STD" "$STAGED/dm/font/standard.ttf" || die "copy the standard font"
"${FONT_PY:-python3}" "$DM/tools/make_ext_font.py" "$STAGED/dm/font/nintendo_ext.ttf" > /dev/null || die "make_ext_font.py (pip install fonttools, or FONT_PY=python with fontTools)"

t "pack the game into the app (pack_apk.py; the already-compressed files stored, the rest deflated)"
python3 tools/pack_apk.py "$STAGED" --romfs "$ROMFS" --modules "$PATCHED" --mods $MODS --spv "$SPV/b-$V" --aoc "${AOC:-}" --tables "$GD" || die "pack_apk"
t "gradle (assembleRelease, the NDK builds the host)"
( cd "$ANDROID" && ./gradlew assembleRelease -PDM_APP_ID="$DM_APP_ID" -PDM_APP_NAME="$DM_DISPLAY_NAME" -PDM_APP_VERSION="$DM_APP_VERSION" -PDM_STAGED="$STAGED" --no-daemon ) \
  > "$W/build/android/gradle.log" 2>&1 \
  || { grep -E 'error:|error |FAILED|FAILURE' "$W/build/android/gradle.log" | head -20; die "gradle, log $W/build/android/gradle.log"; }
# a machine with dm/android/keystore.properties signs in Gradle, so the output carries no -unsigned
APK=$ANDROID/app/build/outputs/apk/release/app-release-unsigned.apk
[ -f "$APK" ] || APK=$ANDROID/app/build/outputs/apk/release/app-release.apk
[ -f "$APK" ] || die "no built APK at $ANDROID/app/build/outputs/apk/release/"
cp "$APK" "$OUT_APK" || die "copy to $OUT_APK"
t "done: $OUT_APK ($(du -h "$OUT_APK" | cut -f1)) — install with 'adb install -r', or take a signed release from the CI workflow"
