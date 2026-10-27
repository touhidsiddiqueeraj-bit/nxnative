// JNI bridge between the Android app (dm/android/app, Kotlin) and the host (dm/host + dm/gfx):
// the boot environment (main.m's counterpart: directories, environment variables, the log), touch
// and controller input, the window hand-off, app lifecycle, the Kotlin-side UI the front end needs
// (progress, software keyboard, notices, power state) and rumble output. The host itself runs on a
// big-stack thread started here once the game window exists (main.c is compiled in as dm_main).
#include <android/native_window.h>
#include <android/native_window_jni.h>
#include <android/log.h>
#include <jni.h>
#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "dm.h"
#include "hle.h"
#include "dm_input.h"
#include "hle_haptics.h"
#include "front.h"
#include "game_profile.h"

int dm_main(int argc, char **argv);         // dm/host/main.c (compiled with -Dmain=dm_main by CMake)
void dm_haptics_init(void (*sink)(dm_haptic_state), int enabled);   // hle_haptics.h
void dm_haptics_android_sink(dm_haptic_state st);                    // haptics_android.c
void dm_haptics_android_input(int on);                               // haptics_android.c: touch feedback

#define TAG "nxnative"
#define LOGI(...) do { fprintf(stderr, __VA_ARGS__); fputc('\n', stderr); __android_log_print(ANDROID_LOG_INFO, TAG, __VA_ARGS__); } while (0)
#define LOGE(...) do { fprintf(stderr, "[RED] " __VA_ARGS__); fputc('\n', stderr); __android_log_print(ANDROID_LOG_ERROR, TAG, __VA_ARGS__); } while (0)

static JavaVM *g_vm;
static pthread_mutex_t g_front_mu = PTHREAD_MUTEX_INITIALIZER;
static jobject g_front;                     // org.nxnative.android.DmFront: the app-side UI + power state
static struct { jmethodID progress, text_input, save_failed, thermal, low_power, brightness, screen_fps, haptic; } g_m;
static char g_files[4096];                  // the app's files root (the Documents' counterpart)
static char g_moddir[4096];                 // <filesDir>/modules: the rewritten .patched.bin files the loader reads
static int g_boot_ok, g_boot_started;

static JNIEnv *front_env(int *attached) {   // for calls made from the host's own threads
    JNIEnv *env = NULL;
    *attached = 0;
    if ((*g_vm)->GetEnv(g_vm, (void **)&env, JNI_VERSION_1_6) == JNI_OK) return env;
    if ((*g_vm)->AttachCurrentThread(g_vm, &env, NULL) == JNI_OK) { *attached = 1; return env; }
    return NULL;
}

// ---- Boot state: the game window, the module directory, the boot thread ----
static pthread_mutex_t g_win_mu = PTHREAD_MUTEX_INITIALIZER;
static ANativeWindow *g_window;
void *dm_front_window(void) {
    pthread_mutex_lock(&g_win_mu);
    void *w = g_window;
    pthread_mutex_unlock(&g_win_mu);
    return w;
}

static void *boot_thread(void *unused) {
    (void)unused;
    static char alt[65536]; stack_t ss = {.ss_sp = alt, .ss_size = sizeof alt}; sigaltstack(&ss, 0);   // crash printing must work on the host thread too
    char *argv[3] = {strdup("dmhost"), g_moddir, NULL};
    int rc = dm_main(2, argv);
    LOGI("boot: dm_main returned %d", rc);
    return NULL;
}
static void boot_start(void) {
    static int once;
    if (once++ || !g_boot_ok || !g_boot_started) return;
    pthread_attr_t a; pthread_attr_init(&a); pthread_attr_setstacksize(&a, 64u << 20);   // the Switch main thread stack is set by npdm; the host gives plenty
    pthread_t t; pthread_create(&t, &a, boot_thread, NULL); pthread_detach(t);
}

// ---- Kotlin-side UI for the front end (front_android.c calls these) ----
static int front_call_int(jmethodID *slot, int fallback) {
    if (!g_front || !*slot) return fallback;
    JNIEnv *env; int att;
    if (!(env = front_env(&att))) return fallback;
    pthread_mutex_lock(&g_front_mu);
    jint v = g_front && *slot ? (*env)->CallIntMethod(env, g_front, *slot) : fallback;
    if ((*env)->ExceptionCheck(env)) { (*env)->ExceptionClear(env); v = fallback; }   // a UI query must never take the host down
    pthread_mutex_unlock(&g_front_mu);
    if (att) (*g_vm)->DetachCurrentThread(g_vm);
    return v;
}

void dm_front_progress(const char *title, long done, long total) {
    if (!g_front || !g_m.progress) return;
    JNIEnv *env; int att;
    if (!(env = front_env(&att))) return;
    pthread_mutex_lock(&g_front_mu);
    if (g_front && g_m.progress) {
        jstring t = title ? (*env)->NewStringUTF(env, title) : NULL;
        (*env)->CallVoidMethod(env, g_front, g_m.progress, t, (jlong)done, (jlong)total);
        if ((*env)->ExceptionCheck(env)) (*env)->ExceptionClear(env);
        if (t) (*env)->DeleteLocalRef(env, t);
    }
    pthread_mutex_unlock(&g_front_mu);
    if (att) (*g_vm)->DetachCurrentThread(g_vm);
}

int dm_front_text_input(const char *title, const char *sub, const char *guide, const char *initial, int max_chars, char *out, int out_cap) {
    if (!g_front || !g_m.text_input) return -1;
    JNIEnv *env; int att;
    if (!(env = front_env(&att))) return -1;
    int r = 0;
    pthread_mutex_lock(&g_front_mu);
    if (g_front && g_m.text_input) {
        jstring t = (*env)->NewStringUTF(env, title && *title ? title : "");
        jstring m = (*env)->NewStringUTF(env, sub ? sub : "");
        jstring g = (*env)->NewStringUTF(env, guide ? guide : "");
        jstring i = (*env)->NewStringUTF(env, initial ? initial : "");
        jobject res = (*env)->CallObjectMethod(env, g_front, g_m.text_input, t, m, g, i, (jint)max_chars);
        if ((*env)->ExceptionCheck(env)) (*env)->ExceptionClear(env);
        else if (res) {
            const char *s = (*env)->GetStringUTFChars(env, (jstring)res, NULL);
            if (s) { snprintf(out, out_cap, "%s", s); (*env)->ReleaseStringUTFChars(env, (jstring)res, s); r = 1; }
            (*env)->DeleteLocalRef(env, res);
        }
        (*env)->DeleteLocalRef(env, t); (*env)->DeleteLocalRef(env, m); (*env)->DeleteLocalRef(env, g); (*env)->DeleteLocalRef(env, i);
    }
    pthread_mutex_unlock(&g_front_mu);
    if (att) (*g_vm)->DetachCurrentThread(g_vm);
    return r;
}

void dm_front_save_failed(int err) {
    LOGE("front end: save data write failed errno=%d, telling the player", err);
    if (!g_front || !g_m.save_failed) return;
    JNIEnv *env; int att;
    if (!(env = front_env(&att))) return;
    pthread_mutex_lock(&g_front_mu);
    if (g_front && g_m.save_failed) {
        (*env)->CallVoidMethod(env, g_front, g_m.save_failed, (jint)err);
        if ((*env)->ExceptionCheck(env)) (*env)->ExceptionClear(env);
    }
    pthread_mutex_unlock(&g_front_mu);
    if (att) (*g_vm)->DetachCurrentThread(g_vm);
}

int dm_front_thermal(void) { return front_call_int(&g_m.thermal, 0); }     // 0 nominal; the app maps BatteryManager's status
int dm_front_low_power(void) { return front_call_int(&g_m.low_power, 0); }
int dm_front_screen_max_fps(void) { return front_call_int(&g_m.screen_fps, 0); }

// Rumble: haptics_android.c's sink calls the app's Vibrator through here (DmFront.haptic(player, on, amp, sharp)).
void dm_jni_haptic(int player, int on, float amp, float sharp) {
    if (!g_front || !g_m.haptic) return;
    JNIEnv *env; int att;
    if (!(env = front_env(&att))) return;
    pthread_mutex_lock(&g_front_mu);
    if (g_front && g_m.haptic) {
        (*env)->CallVoidMethod(env, g_front, g_m.haptic, (jint)player, (jint)on, (jfloat)amp, (jfloat)sharp);
        if ((*env)->ExceptionCheck(env)) (*env)->ExceptionClear(env);
    }
    pthread_mutex_unlock(&g_front_mu);
    if (att) (*g_vm)->DetachCurrentThread(g_vm);
}

double dm_front_brightness(void) {
    if (!g_front || !g_m.brightness) return -1;
    JNIEnv *env; int att;
    if (!(env = front_env(&att))) return -1;
    jdouble b = -1;
    pthread_mutex_lock(&g_front_mu);
    if (g_front && g_m.brightness) {
        b = (*env)->CallDoubleMethod(env, g_front, g_m.brightness);
        if ((*env)->ExceptionCheck(env)) { (*env)->ExceptionClear(env); b = -1; }
    }
    pthread_mutex_unlock(&g_front_mu);
    if (att) (*g_vm)->DetachCurrentThread(g_vm);
    return b;
}

// ---- The environment, as main.m sets it on iOS ----
static void env_file_read(const char *path, const char *what, char *from_file, size_t cap) {
    FILE *f = fopen(path, "re");
    if (!f) return;
    char ln[1024];
    while (fgets(ln, sizeof ln, f)) {
        char *e = strchr(ln, '=');
        if (!e || e == ln) continue;
        *e = 0;
        char *v = e + 1, *nl = strchr(v, '\n'); if (nl) *nl = 0;
        char *k = ln; while (*k == ' ' || *k == '\t') k++;
        if (!*k || *k == '#') continue;
        if (!getenv(k)) {
            setenv(k, v, 0);
            strncat(from_file, k, cap - strlen(from_file) - 2); strncat(from_file, " ", 1);
            LOGI("boot: env %s=%s (%s)", k, v, what);
        }
    }
    fclose(f);
}

static int boot_env(JNIEnv *env, jstring jromfs, jstring jaoc, jstring jpso) {
    char p[4200];
    for (const char *d = "save\0temp\0"; *d; d += strlen(d) + 1) {   // <filesDir>/save, <filesDir>/temp
        snprintf(p, sizeof p, "%s/%s", g_files, d);
        mkdir(p, 0755);
    }
    // Log: the host only writes to stderr; redirect it to <filesDir>/log.txt (the previous one is kept as log.prev.txt)
    char log[4200], prev[4200];
    snprintf(log, sizeof log, "%s/log.txt", g_files);
    snprintf(prev, sizeof prev, "%s/log.prev.txt", g_files);
    unlink(prev); rename(log, prev);
    if (freopen(log, "w", stderr)) setvbuf(stderr, NULL, _IOLBF, 0);
    LOGI("boot: log %s", log);
    // Environment switch files (on-device diagnostics): one KEY=VALUE per line, # starts a comment;
    // <filesDir>/env.txt wins over the packed one (beside the game files, put there by build_apk.sh's DM_ENV_FILE)
    char from_file[1024] = "";
    snprintf(p, sizeof p, "%s/env.txt", g_files); env_file_read(p, "files", from_file, sizeof from_file);
    snprintf(p, sizeof p, "%s/env.txt", g_files);   // the packed one lands in files root, extracted by the app at first start
    env_file_read(p, "packed", from_file, sizeof from_file);
    setenv("DM_ENV_FILE_KEYS", from_file, 1);   // the launcher keeps what an env file set (LauncherSettings.kt)
    // Game language: the one the APK was packed with (set by build_apk_from_nsp.py's --language; the launcher replaces it before the game starts)
    LOGI("boot: game language %s", getenv("DM_LANG") ?: "en-US");
    // Game files: the RomFS extracted into files by the app (romfs/), its marker proves it
    const char *romfs = jromfs ? (*env)->GetStringUTFChars(env, jromfs, NULL) : NULL;
    if (!romfs) { LOGE("boot: game files not found (pack an APK with scripts/build_apk_from_nsp.py)"); return 0; }
    snprintf(p, sizeof p, "%s/%s", romfs, DM_GAME_ROMFS_MARKER);
    if (access(p, F_OK)) { LOGE("boot: no RomFS at %s (marker %s)", romfs, DM_GAME_ROMFS_MARKER); (*env)->ReleaseStringUTFChars(env, jromfs, romfs); return 0; }
    LOGI("boot: game files %s", romfs);
    setenv("DM_ROMFS", romfs, 0);
    (*env)->ReleaseStringUTFChars(env, jromfs, romfs);
    snprintf(p, sizeof p, "%s/save", g_files); setenv("DM_SAVE", p, 0);
    snprintf(p, sizeof p, "%s/temp", g_files); setenv("DM_TEMP", p, 0);
    snprintf(g_moddir, sizeof g_moddir, "%s/modules", g_files);
    snprintf(p, sizeof p, "%s/shader_map.tsv", g_moddir); setenv("DM_NVN_SHMAP", p, 0);
    snprintf(p, sizeof p, "%s/msl", g_files); setenv("DM_MSL_DIR", p, 0);   // name kept from the Metal build: the shader libraries (SPIR-V on Android) live there
    snprintf(p, sizeof p, "%s/font", g_files); setenv("DM_FONT_DIR", p, 0);
    if (jaoc) { const char *aoc = (*env)->GetStringUTFChars(env, jaoc, NULL); if (aoc && *aoc) setenv("DM_AOC_DIR", aoc, 0); if (aoc) (*env)->ReleaseStringUTFChars(env, jaoc, aoc); }
    if (jpso) { const char *pso = (*env)->GetStringUTFChars(env, jpso, NULL); if (pso && *pso) setenv("DM_PSO_BUNDLE", pso, 0); if (pso) (*env)->ReleaseStringUTFChars(env, jpso, pso); }
    if (chdir(g_files)) LOGE("boot: chdir %s failed", g_files);
    return 1;
}

// ---- JNI entry points (Kotlin: HostJni) ----
JNIEXPORT jint JNICALL Java_org_nxnative_android_HostJni_nativeBoot(JNIEnv *env, jclass cls, jobject front, jstring files, jstring romfs, jstring aoc, jstring pso) {
    (void)cls;
    if (!g_files[0] && files) {
        const char *f = (*env)->GetStringUTFChars(env, files, NULL);
        snprintf(g_files, sizeof g_files, "%s", f);
        (*env)->ReleaseStringUTFChars(env, files, f);
    }
    (*env)->GetJavaVM(env, &g_vm);
    pthread_mutex_lock(&g_front_mu);
    if (g_front) (*env)->DeleteGlobalRef(env, g_front);
    g_front = (*env)->NewGlobalRef(env, front);
    jclass c = (*env)->GetObjectClass(env, g_front);
    // The lookups go one by one, each checked: a miss (a rebuilt app, a renamed method) logs and
    // leaves that one slot empty - every call site falls back on a null slot - instead of a pending
    // NoSuchMethodError that would crash the app the moment nativeBoot returns. The signatures
    // must spell DmFront.kt exactly: haptic takes (player, on, amplitude, sharpness) = (IIFF)V.
    static const struct { const char *name, *sig; jmethodID *slot; } lookups[] = {
        {"progress",   "(Ljava/lang/String;JJ)V", &g_m.progress},
        {"textInput",  "(Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;I)Ljava/lang/String;", &g_m.text_input},
        {"saveFailed", "(I)V", &g_m.save_failed},
        {"thermal",    "()I", &g_m.thermal},
        {"lowPower",   "()I", &g_m.low_power},
        {"brightness", "()D", &g_m.brightness},
        {"screenFps",  "()I", &g_m.screen_fps},
        {"haptic",     "(IIFF)V", &g_m.haptic},
    };
    for (size_t i = 0; i < sizeof lookups / sizeof *lookups; i++) {
        *lookups[i].slot = (*env)->GetMethodID(env, c, lookups[i].name, lookups[i].sig);
        if ((*env)->ExceptionCheck(env)) {
            LOGE("boot: DmFront.%s%s not found, continuing without it", lookups[i].name, lookups[i].sig);
            (*env)->ExceptionClear(env);
        }
    }
    pthread_mutex_unlock(&g_front_mu);
    g_boot_ok = boot_env(env, romfs, aoc, pso);
    dm_haptics_init(dm_haptics_android_sink, 1);   // the host's rumble pipeline; the launcher's switches scale it in the sink
    return g_boot_ok ? 0 : -1;
}

JNIEXPORT void JNICALL Java_org_nxnative_android_HostJni_nativeSetSurface(JNIEnv *env, jclass cls, jobject surface) {
    (void)cls;
    pthread_mutex_lock(&g_win_mu);
    if (g_window) { ANativeWindow_release(g_window); g_window = NULL; }
    if (surface) {
        g_window = ANativeWindow_fromSurface(env, surface);
        if (g_window) ANativeWindow_acquire(g_window);
    }
    pthread_mutex_unlock(&g_win_mu);
    LOGI("front end: game window %s", g_window ? "set" : "cleared");
    g_boot_started = 1;
    boot_start();
}

JNIEXPORT void JNICALL Java_org_nxnative_android_HostJni_nativeTouch(JNIEnv *env, jclass cls, jfloatArray jt) {
    (void)cls;
    jfloat t[32 * 3];
    jsize n = (*env)->GetArrayLength(env, jt) / 3;
    if (n > 32) n = 32;
    (*env)->GetFloatArrayRegion(env, jt, 0, n * 3, t);
    dm_touch pts[32];
    for (int i = 0; i < n; i++) { pts[i].id = (uint32_t)t[3 * i]; pts[i].x = t[3 * i + 1]; pts[i].y = t[3 * i + 2]; }
    dm_touch_set(pts, (int)n);   // hle_touch.c: the whole set is pushed once on every change
}

// The virtual gamepad's state: dm_input_set (the touch / keyboard path); physical controllers go
// through nativePad below, and the host merges both when the game reads player 1 (dm_input.h)
JNIEXPORT void JNICALL Java_org_nxnative_android_HostJni_nativeInput(JNIEnv *env, jclass cls, jlong buttons, jint lx, jint ly, jint rx, jint ry) {
    (void)env; (void)cls;
    dm_input_set((uint64_t)buttons, (int32_t)lx, (int32_t)ly, (int32_t)rx, (int32_t)ry);
}
// Player 1's physical controller (and the DM_BTN_SHAKE bit, which counts there only)
JNIEXPORT void JNICALL Java_org_nxnative_android_HostJni_nativePad(JNIEnv *env, jclass cls, jlong buttons, jint lx, jint ly, jint rx, jint ry) {
    (void)env; (void)cls;
    dm_pad_set((uint64_t)buttons, (int32_t)lx, (int32_t)ly, (int32_t)rx, (int32_t)ry);
}
JNIEXPORT void JNICALL Java_org_nxnative_android_HostJni_nativePadPlayers(JNIEnv *env, jclass cls, jint mask) {
    (void)env; (void)cls;
    dm_pad_players((uint32_t)mask);
}

// App lifecycle (front_ios.m's focus events): 0 resign active, 1 enter background, 2 return to foreground, 3 become active.
// On entering background, freeze the guest clock and suspend after in-flight GPU work completes (submitting GPU work in background gets the process killed).
JNIEXPORT void JNICALL Java_org_nxnative_android_HostJni_nativeFocus(JNIEnv *env, jclass cls, jint ev) {
    (void)env; (void)cls;
    switch (ev) {
        case 0: dm_focus_set(2); break;
        case 1: dm_focus_set(3); dm_audio_suspend(1); guest_clock_freeze_for(1, 1); dm_gfx_suspend(1); break;
        case 2: dm_gfx_suspend(0); guest_clock_freeze_for(1, 0); dm_audio_suspend(0); dm_focus_set(2); break;
        case 3: dm_focus_set(1); break;
    }
}

JNIEXPORT void JNICALL Java_org_nxnative_android_HostJni_nativeShutdown(JNIEnv *env, jclass cls, jstring why) {
    (void)cls;
    const char *w = why ? (*env)->GetStringUTFChars(env, why, NULL) : "app";
    dm_host_shutdown(w);   // logs, flushes its counters, _exit(0)
    if (why) (*env)->ReleaseStringUTFChars(env, why, w);
}

// The frame-rate counter (front_ios.m): the frames put on screen in the last second
uint64_t mtl_present_count(void);            // nvn_vk.c
JNIEXPORT jlong JNICALL Java_org_nxnative_android_HostJni_nativePresentCount(JNIEnv *env, jclass cls) {
    (void)env; (void)cls;
    return (jlong)mtl_present_count();
}

JNIEXPORT void JNICALL Java_org_nxnative_android_HostJni_nativeHapticInput(JNIEnv *env, jclass cls, jint on) {
    (void)env; (void)cls;
    dm_haptics_android_input(on ? 1 : 0);   // the launcher's "Touch feedback" switch
}

// The launcher applies its choices as environment variables before the game starts (LauncherSettings.apply)
JNIEXPORT void JNICALL Java_org_nxnative_android_HostJni_nativeSetenv(JNIEnv *env, jclass cls, jstring key, jstring value) {
    (void)cls;
    if (!key || !value) return;
    const char *k = (*env)->GetStringUTFChars(env, key, NULL);
    const char *v = (*env)->GetStringUTFChars(env, value, NULL);
    if (k && v) {
        setenv(k, v, 1);
        LOGI("launcher: %s=%s", k, v);
    }
    if (k) (*env)->ReleaseStringUTFChars(env, key, k);
    if (v) (*env)->ReleaseStringUTFChars(env, value, v);
}
