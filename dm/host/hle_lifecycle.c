// Host lifecycle: graceful exit (SIGTERM; on iOS the App termination callback, on Android the
// activity's finish path, calls dm_host_shutdown).
// Pause audio, suspend graphics (wait for in-flight NVN calls to end and drain GPU work), then _exit(0); a 1.5 s fallback goes straight to _exit(0).
// The default action also exits in about 0.08 s (exit code 143); the value of this function is stopping background threads in order, exit code 0, and giving the platform lifecycle callback an entry.
#include "hle.h"
#include <stdatomic.h>
#include <signal.h>
#include <unistd.h>
#if defined(__ANDROID__)
// No GCD on Android: the 1.5 s fallback timer is a detached thread, and the SIGTERM source a plain
// sigaction handler (dm_host_shutdown logs and exits; it is async-signal-safe for that use).
#include <pthread.h>
static void *fallback_timer(void *unused) { (void)unused; usleep(1500000); LOG("[RED] graceful exit not done in 1.5 seconds, exiting at once"); _exit(0); }
static void on_term(int sig) { (void)sig; dm_host_shutdown("SIGTERM"); }
#else
#include <dispatch/dispatch.h>
#endif
void hlecount_flush(void);
void dm_host_shutdown(const char *why) {
    static _Atomic int once; if (atomic_exchange(&once, 1)) return;
    uint64_t t0 = now_ns(); LOG("received %s: graceful exit", why);
#if defined(__ANDROID__)
    pthread_t t; if (pthread_create(&t, NULL, fallback_timer, NULL)) _exit(0); pthread_detach(t);
#else
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 1500 * NSEC_PER_MSEC), dispatch_get_global_queue(QOS_CLASS_USER_INTERACTIVE, 0), ^{
        LOG("[RED] graceful exit not done in 1.5 seconds, exiting at once"); _exit(0); });
#endif
    hlecount_flush();                               // DM_HLECOUNT: _exit does not run atexit, write a copy here
    dm_audio_suspend(1);
    dm_gfx_suspend(1);
    LOG("graceful exit done (%.3f s)", (now_ns() - t0) / 1e9);
    _exit(0);
}
void dm_term_install(void) {
#if defined(__ANDROID__)
    struct sigaction sa = {0};
    sa.sa_handler = on_term;            // run the wrap-up on the signal's own thread, as the GCD source did on an ordinary thread
    sigaction(SIGTERM, &sa, NULL);
#else
    signal(SIGTERM, SIG_IGN);                       // do not take the default action: hand over to the GCD signal source to wrap up on an ordinary thread
    static dispatch_source_t src;
    src = dispatch_source_create(DISPATCH_SOURCE_TYPE_SIGNAL, SIGTERM, 0, dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0));
    dispatch_source_set_event_handler(src, ^{ dm_host_shutdown("SIGTERM"); });
    dispatch_resume(src);
#endif
}
