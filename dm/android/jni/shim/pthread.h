// pthread.h shim (Android): Darwin's pthread extensions the host uses, over POSIX.
//   pthread_setname_np(name)          - Darwin names the calling thread with one argument; POSIX
//                                       takes (pthread_t, name). The macro keeps the call sites.
//   pthread_cond_timedwait_relative_np(cond, mutex, ts) - Darwin's relative timeout; POSIX takes an
//                                       absolute one, so the shim converts (ts.tv_sec += now).
#pragma once
#include_next <pthread.h>
#include <time.h>
#include <pthread/qos.h>

// Darwin spells pthread_setname_np with one argument (the calling thread); POSIX and bionic take
// (pthread_t, name). The host's sources use the one-argument form, libvpx the two-argument one, so
// the macro dispatches on the argument count (parenthesised calls reach the real function).
#define DM_CONCAT_(a, b) a##b
#define DM_CONCAT(a, b) DM_CONCAT_(a, b)
#define DM_NARG(...) DM_NARG_(__VA_ARGS__, 2, 1)
#define DM_NARG_(_1, _2, N, ...) N
#define pthread_setname_np(...) DM_CONCAT(dm_setname_, DM_NARG(__VA_ARGS__))(__VA_ARGS__)
static inline int dm_setname_1(const char *name) { return (pthread_setname_np)(pthread_self(), name); }
static inline int dm_setname_2(pthread_t t, const char *name) { return (pthread_setname_np)(t, name); }
static inline int pthread_cond_timedwait_relative_np(pthread_cond_t *cond, pthread_mutex_t *mu, const struct timespec *rel) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    ts.tv_sec += rel->tv_sec; ts.tv_nsec += rel->tv_nsec;
    if (ts.tv_nsec >= 1000000000l) { ts.tv_sec++; ts.tv_nsec -= 1000000000l; }
    return pthread_cond_timedwait(cond, mu, &ts);
}

// pthread_get_stacksize_np / pthread_get_stackaddr_np (hle_svc.c sizes a guest thread's host
// fallback): Android exposes the stack via pthread_attr_getstack
static inline size_t pthread_get_stacksize_np(pthread_t t) {
    pthread_attr_t a;
    if (pthread_getattr_np(t, &a)) return 0;
    size_t n = 0;
    pthread_attr_getstacksize(&a, &n);
    pthread_attr_destroy(&a);
    return n;
}
static inline void *pthread_get_stackaddr_np(pthread_t t) {
    pthread_attr_t a;
    if (pthread_getattr_np(t, &a)) return NULL;
    void *p = NULL; size_t n = 0;
    pthread_attr_getstack(&a, &p, &n);
    pthread_attr_destroy(&a);
    return p;
}
