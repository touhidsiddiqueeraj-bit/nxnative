// dispatch/dispatch.h shim (Android): GCD semaphores over POSIX sem_t - the host's only dispatch use
// outside hle_lifecycle.c (which takes its own Android branch). hle_audio.c's buffer-release
// notification (dispatch_semaphore_signal from the AAudio callback, dispatch_semaphore_wait with a
// millisecond timeout in dm_pcmout_wait) maps 1:1; sem_timedwait needs an absolute time, so the shim
// computes it from the dispatch_time_t delta on each call.
//   dispatch_semaphore_create(value) - the value is the initial count (hle_audio.c passes 0)
//   dispatch_semaphore_signal        - long: > 0 when a waiter was woken (sem_post's old value)
//   dispatch_semaphore_wait(timeout) - 0 on acquire, non-zero (KERN_OPERATION_TIMED_OUT) on timeout
#pragma once
#include <errno.h>
#include <semaphore.h>
#include <stdlib.h>
#include <stdint.h>
#include <time.h>

#define NSEC_PER_SEC 1000000000ll
#define NSEC_PER_MSEC 1000000ll
#define NSEC_PER_USEC 1000ll
typedef int64_t dispatch_time_t;
#define DISPATCH_TIME_NOW 0
#define DISPATCH_TIME_FOREVER INT64_MAX
typedef struct { sem_t sem; } *dispatch_semaphore_t;

static inline dispatch_time_t dispatch_time(dispatch_time_t from, int64_t delta) { return from == DISPATCH_TIME_NOW ? delta : from + delta; }

static inline dispatch_semaphore_t dispatch_semaphore_create(long value) {
    dispatch_semaphore_t s = (dispatch_semaphore_t)malloc(sizeof *s);
    if (s && sem_init(&s->sem, 0, (unsigned)value)) { free(s); return NULL; }
    return s;
}
static inline long dispatch_semaphore_signal(dispatch_semaphore_t s) { return sem_post(&s->sem) ? -1 : 0; }
static inline long dispatch_semaphore_wait(dispatch_semaphore_t s, dispatch_time_t timeout) {
    if (timeout == DISPATCH_TIME_FOREVER) return sem_wait(&s->sem) ? -1 : 0;
    struct timespec ts; clock_gettime(CLOCK_REALTIME, &ts);
    ts.tv_sec += timeout / NSEC_PER_SEC; ts.tv_nsec += timeout % NSEC_PER_SEC;
    if (ts.tv_nsec >= NSEC_PER_SEC) { ts.tv_sec++; ts.tv_nsec -= NSEC_PER_SEC; }
    return sem_timedwait(&s->sem, &ts) ? ETIMEDOUT : 0;   // the callers test nonzero, as they do for KERN_OPERATION_TIMED_OUT
}
static inline void dispatch_release(dispatch_semaphore_t s) { sem_destroy(&s->sem); free(s); }
static inline void dispatch_retain(dispatch_semaphore_t s) { (void)s; }
