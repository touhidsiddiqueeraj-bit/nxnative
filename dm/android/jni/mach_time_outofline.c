// The out-of-line mach_absolute_time (the seam shim/mach/mach_time.h documents): the C callers keep
// the header's static inline, but nvn_entry.S - assembly - links against the symbol itself, and an
// inline function has none. This one TU pulls in no shim headers on purpose, so the non-static
// definition and the header's inline copies never meet; the clock read is the same one.
#include <stdint.h>
#include <time.h>

uint64_t mach_absolute_time(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC_RAW, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}
