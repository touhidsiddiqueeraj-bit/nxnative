// mach/mach_time.h shim (Android): the Mach clock over CLOCK_MONOTONIC_RAW.
// mach_absolute_time() is a free-running tick source; the host divides by the timebase on Apple.
// On Linux the same clock already reports nanoseconds, so the timebase is 1/1 and every existing
// conversion in the host (now_ns(), dm_thermal... profilers, nvn_dirty.c pool timing) is unchanged.
#pragma once
#include <stdint.h>
#include <time.h>

typedef struct { uint32_t numer, denom; } mach_timebase_info_data_t;
typedef int kern_return_t;
#define KERN_SUCCESS 0
#define KERN_INVALID_ADDRESS 1

static inline uint64_t mach_absolute_time(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC_RAW, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}
static inline kern_return_t mach_timebase_info(mach_timebase_info_data_t *tb) { tb->numer = 1; tb->denom = 1; return 0; }

// mach_wait_until(deadline in ticks): with the 1/1 timebase this is an absolute CLOCK_MONOTONIC_RAW
// deadline (sleep_until_ns, present pacing)
#include <errno.h>
static inline kern_return_t mach_wait_until(uint64_t deadline) {
    struct timespec ts = { .tv_sec = (time_t)(deadline / 1000000000ull), .tv_nsec = (long)(deadline % 1000000000ull) };
    int r;
    while ((r = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, NULL)) == EINTR) ;
    return r ? KERN_INVALID_ADDRESS : 0;
}
