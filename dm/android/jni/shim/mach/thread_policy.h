// mach/thread_policy.h shim (Android): the real-time thread policy query of hle_nvn.c's DM_NVN_PROF
// detail log (thread_policy_get THREAD_TIME_CONSTRAINT_POLICY). Linux/Android has no RT audio-style
// policy an app may set this way; the query reports "not in effect" (def = 1), exactly the state
// the host logs when the Mac never activated it.
#pragma once
#include <stdint.h>
#include "mach.h"

enum { THREAD_TIME_CONSTRAINT_POLICY = 1 };
typedef struct { uint32_t period, computation, constraint; uint8_t preemptible; } thread_time_constraint_policy_data_t;
enum { THREAD_TIME_CONSTRAINT_POLICY_COUNT = 4 };
static inline int thread_policy_get(mach_port_t th, int policy, thread_policy_t out, mach_msg_type_number_t *cnt, boolean_t *def) {
    (void)th; (void)policy; (void)out; (void)cnt; *def = 1;   // the default: no real-time policy in effect
    return 0;
}
static inline int thread_policy_set(mach_port_t th, int policy, thread_policy_t in, mach_msg_type_number_t cnt) {
    (void)th; (void)policy; (void)in; (void)cnt; return 0;   // accepted, no effect (SCHED_OTHER stays)
}
