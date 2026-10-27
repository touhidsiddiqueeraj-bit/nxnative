// time.h shim (Android): Darwin's clock_gettime_nsec_np (a nanosecond clock read in one call) over
// POSIX clock_gettime. The host reads it in its timing rows (hle_fs.c commit timings, hle_nvn.c's
// frame counters). include_next reaches the real <time.h> first declared by the system.
#pragma once
#include_next <time.h>
#include <stdint.h>

static inline uint64_t clock_gettime_nsec_np(clockid_t clk) {
    struct timespec ts;
    clock_gettime(clk, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}
