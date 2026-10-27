// sys/sysctl.h shim (Android): the two sysctl uses of the host over /proc.
//   main.c           KERN_PROC PID p_starttime - process start wall time (the boot timeline zero point)
//   hle_nvn.c        sysctlbyname("hw.nperflevels") - performance-core count (fstep_cpu_slowed): reads
//                    the big/little split from /sys/devices/system/cpu/cpu*/cpufreq (0 big cores = 1 level)
// The struct layouts only need the fields the host reads; kinfo_proc carries p_starttime where Mach's does.
#pragma once
#include <stdint.h>
#include <sys/time.h>
#include <sys/types.h>

typedef unsigned int uint_t;

#define CTL_KERN 1
#define KERN_PROC 14
#define KERN_PROC_PID 1

struct kinfo_proc { struct { struct timeval p_starttime; } kp_proc; };   // Darwin's nested layout, the fields the host reads
#define SYSCTL_HW_NPERFLEVELS_HINT (-1)   // internal: sysctl("hw.nperflevels") is served by name below

int sysctl(const int *name, uint_t namelen, void *oldp, size_t *oldlenp, void *newp, size_t newlen);
int sysctlbyname(const char *name, void *oldp, size_t *oldlenp, void *newp, size_t newlen);
