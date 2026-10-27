// pthread/qos.h shim (Android): Apple's QoS classes map onto bionic's thread priorities (nice values).
// hle_thread.c uses them for guest thread priorities (prio 44 = interactive down to background helpers)
// and hle_nvn.c's detail log prints the class. The classes keep their numeric identity for the log rows;
// pthread_set_qos_class_self_np sets the corresponding SCHED_OTHER priority through setpriority on the
// thread's own tid (the "np" variants of Apple's API are thread-self by definition).
#pragma once
#include <stdint.h>
#include <sys/resource.h>

typedef unsigned int qos_class_t;
#define QOS_CLASS_USER_INTERACTIVE 0x21
#define QOS_CLASS_USER_INITIATED 0x19
#define QOS_CLASS_DEFAULT 0x15
#define QOS_CLASS_UTILITY 0x11
#define QOS_CLASS_BACKGROUND 0x09
#define QOS_CLASS_UNSPECIFIED 0x00

static inline int pthread_set_qos_class_self_np(qos_class_t q, int rel) {
    (void)rel;
    int nice;
    switch (q) {
        case QOS_CLASS_USER_INTERACTIVE: nice = -8; break;
        case QOS_CLASS_USER_INITIATED: nice = -4; break;
        case QOS_CLASS_UTILITY: nice = 4; break;
        case QOS_CLASS_BACKGROUND: nice = 10; break;
        default: return 0;
    }
    return setpriority(PRIO_PROCESS, 0, nice);   // 0 on success: the caller treats nonzero as "could not"
}
// Darwin's (pthread_t, class out, relative priority out); Linux keeps no class, so UNSPECIFIED reports that
static inline int pthread_get_qos_class_np(pthread_t t, qos_class_t *out, int *rel) {
    (void)t; if (out) *out = QOS_CLASS_UNSPECIFIED; if (rel) *rel = 0; return 0;
}
typedef void *pthread_override_t;
static inline pthread_override_t pthread_override_qos_class_start_np(pthread_t t, qos_class_t q, int rel) {
    (void)t; (void)q; (void)rel; return NULL;   // no cross-thread boost on Android: the helper keeps its own priority
}
static inline int pthread_override_qos_class_end_np(pthread_override_t o) { (void)o; return 0; }
