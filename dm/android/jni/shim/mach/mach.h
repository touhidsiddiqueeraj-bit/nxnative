// mach/mach.h shim (Android): the Mach surface the host uses, over POSIX. See dm/android/jni/shim/README.
//   vm_read_overwrite        - safe read of any address (probes, backtraces, guest-stack reads): process_vm_readv
//                              on self faults the pages in and returns a short count (or EFAULT) for unmapped
//                              guest memory, never a signal - the probe/watch semantics vm_read_overwrite has.
//   vm_region_64 / mach_vm_region - protection of the mapping at an address (hle_uemalloc.c alias check,
//                              hle_fs.c and probe.c writability tests): a /proc/self/maps walk.
//   thread_info / task_info  - thread CPU time (hle_thread.c, hle_nvn.c) over CLOCK_THREAD_CPUTIME_ID;
//                              task footprint (hle_nvn.c power rows) over /proc/self/statm.
//   thread_selfcounts        - per-thread instructions / cycles: Linux has no per-thread PMU access from a
//                              plain app; both counters fall back to the CPU time in ns (the ratio the
//                              translation-detail log computes stays meaningful, the "cycles" are nominal).
// mach_port_t / mach_task_self() - self-referential handles. pthread_mach_thread_np keeps a small unique-id
// table: a bionic pthread_t is an opaque pointer, hle_thread.c stores the port value in guest objects.
#pragma once
#include <stdint.h>
#include <stddef.h>
#include <pthread.h>
#include <unistd.h>
#include <sys/types.h>
#include <time.h>

typedef int kern_return_t;
typedef int boolean_t;
typedef uint64_t vm_address_t;
typedef uint64_t vm_size_t;
typedef int vm_prot_t;
typedef void *mach_port_t;
typedef void *task_info_t;
typedef void *thread_info_t;
typedef void *thread_policy_t;
typedef int mach_msg_type_number_t;
#define VM_PROT_READ 1
#define VM_PROT_WRITE 2
#define VM_PROT_EXECUTE 4
#define KERN_SUCCESS 0
#define KERN_INVALID_ADDRESS 1
#define MACH_PORT_NULL NULL
#define FALSE 0
#define TRUE 1
enum { VM_INHERIT_NONE = 0 };

typedef struct { int64_t seconds; int64_t microseconds; } time_value_t;
typedef struct { int dummy; } *mach_task_t_;

// Unique per-thread ids for pthread_mach_thread_np (hle_thread.c stores the value in the guest thread
// object; a bionic pthread_t is an opaque pointer, so a small table maps it to a stable integer).
uint32_t dm_shim_thread_id(pthread_t t);
static inline mach_port_t mach_task_self(void) { return (mach_port_t)0; }
static inline mach_port_t mach_thread_self(void) { return (mach_port_t)(uintptr_t)dm_shim_thread_id(pthread_self()); }
static inline mach_port_t mach_port_deallocate(mach_port_t t, mach_port_t p) { (void)t; (void)p; return NULL; }
static inline mach_port_t pthread_mach_thread_np(pthread_t t) { return (mach_port_t)(uintptr_t)dm_shim_thread_id(t); }

// ---- vm_read_overwrite: (task, addr, size, data, out_size); returns KERN_SUCCESS and the bytes read ----
kern_return_t vm_read_overwrite(mach_port_t task, vm_address_t addr, vm_size_t size, vm_address_t data, vm_size_t *out_size);

enum { VM_FLAGS_FIXED = 0x1, VM_FLAGS_ANYWHERE = 0x2, VM_FLAGS_OVERWRITE = 0x8000u };
kern_return_t vm_remap(mach_port_t target, vm_address_t *to, vm_size_t size, vm_address_t mask, int flags,
                       mach_port_t src_task, vm_address_t from, int copy, vm_prot_t *cur, vm_prot_t *max, int inherit);   // sys_shim.c: mremap serves it
static inline kern_return_t vm_read(mach_port_t t, vm_address_t a, vm_size_t n, vm_address_t *d) { (void)t; (void)a; (void)n; (void)d; return KERN_INVALID_ADDRESS; }

// ---- region queries: the mapping containing (*addr), walked up as Mach does; protection on return ----
kern_return_t dm_shim_region(vm_address_t *addr, vm_size_t *size, vm_prot_t *protection);
typedef struct { vm_prot_t protection, max_protection; } vm_region_basic_info_data_64_t;
typedef vm_region_basic_info_data_64_t *vm_region_info_t;
enum { VM_REGION_BASIC_INFO_COUNT_64 = 2 };
#define VM_REGION_BASIC_INFO_64 1
static inline kern_return_t vm_region_64(mach_port_t task, vm_address_t *addr, vm_size_t *size, int flavor,
                                         vm_region_info_t info, mach_msg_type_number_t *cnt, mach_port_t *obj) {
    (void)task; (void)flavor; (void)cnt; (void)obj;
    vm_prot_t prot = 0; kern_return_t r = dm_shim_region(addr, size, &prot);
    info->protection = prot; info->max_protection = prot; return r;
}
typedef uint64_t mach_vm_address_t, mach_vm_size_t;
static inline kern_return_t mach_vm_region(mach_port_t task, mach_vm_address_t *addr, mach_vm_size_t *size, int flavor,
                                           vm_region_info_t info, mach_msg_type_number_t *cnt, mach_port_t *obj) {
    return vm_region_64(task, (vm_address_t *)addr, size, flavor, info, cnt, obj);
}

// ---- thread_info: CPU time and state (hle_thread.c thread accounting, hle_nvn.c present-thread rows) ----
enum { THREAD_BASIC_INFO = 1, THREAD_EXTENDED_INFO = 2 };
typedef struct { time_value_t user_time, system_time; int run_state; int suspend_count; } thread_basic_info_data_t;
typedef struct { int pth_curpri, pth_priority, pth_policy; } thread_extended_info_data_t;
enum { THREAD_BASIC_INFO_COUNT = 4, THREAD_EXTENDED_INFO_COUNT = 3 };
// Per-thread CPU ns so a shim's own thread_info can split it; CLOCK_THREAD_CPUTIME_ID only measures the calling thread.
uint64_t dm_shim_thread_cpu_ns(mach_port_t thread);
static inline kern_return_t thread_info(mach_port_t th, int flavor, thread_info_t out, mach_msg_type_number_t *n) {
    (void)n;
    uint64_t ns = dm_shim_thread_cpu_ns(th);
    if (flavor == THREAD_EXTENDED_INFO) {
        thread_extended_info_data_t *xi = out;
        xi->pth_curpri = 0; xi->pth_priority = 0; xi->pth_policy = 0;   // no stable POSIX priority numbers to report
        return KERN_SUCCESS;
    }
    thread_basic_info_data_t *bi = out;
    bi->user_time.seconds = 0; bi->user_time.microseconds = ns / 1000;
    bi->system_time.seconds = 0; bi->system_time.microseconds = 0;   // user+system combined, as on the Mac the sum is used
    bi->run_state = 1; bi->suspend_count = 0;                        // TH_STATE_RUNNING: the guest rows only log it
    return KERN_SUCCESS;
}
// Instructions / cycles of this thread: not readable from a plain Android app; hle_nvn.c declares the
// counter itself against the private interface, and the shim provides it (CPU ns stands in for both).
int thread_selfcounts(int kind, uint64_t *out, size_t n);

// ---- task_info: footprint for the power rows (hle_nvn.c, every 300 frames) ----
enum { TASK_VM_INFO = 1, TASK_POWER_INFO_V2 = 2 };
typedef struct { uint64_t phys_footprint, resident_size, virtual_size; } task_vm_info_data_t;
typedef struct {
    uint64_t task_interrupt_wakeups, task_platform_idle_wakeups, task_timer_wakeups;
    struct { uint64_t total_user, total_system, task_interrupt_wakeups, task_platform_idle_wakeups; } cpu_energy;
    uint64_t task_ptime;
    struct { uint64_t task_gpu_utilisation; } gpu_energy;
    uint64_t task_energy;
} task_power_info_v2_data_t;   // Apple's ledger members the power rows read; Linux fills what it can (0)
enum { TASK_VM_INFO_COUNT = 3, TASK_POWER_INFO_V2_COUNT = 3 };
uint64_t dm_shim_phys_footprint(void);
static inline kern_return_t task_info(mach_port_t t, int flavor, task_info_t out, mach_msg_type_number_t *n) {
    (void)t; (void)n;
    if (flavor == TASK_VM_INFO) {
        task_vm_info_data_t *vi = out; vi->phys_footprint = vi->resident_size = dm_shim_phys_footprint(); vi->virtual_size = 0;
    } else {
        task_power_info_v2_data_t *pw = out;   // CPU energy is not exposed on Linux: the power rows read 0
        struct timespec ts = {0, 0}; clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &ts);
        pw->cpu_energy.total_user = (uint64_t)ts.tv_sec * 1000000000ull + ts.tv_nsec; pw->cpu_energy.total_system = 0; pw->task_ptime = pw->cpu_energy.total_user;
    }
    return KERN_SUCCESS;
}
