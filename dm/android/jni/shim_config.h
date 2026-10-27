// Common includes for the Android shim implementations (dm/android/jni/*_shim.c). Pulls the shim
// headers in first, so the Mach / GCD / CF declarations there win over anything else, then the
// Android NDK media headers the audio and video shims build on.
#pragma once
#include <TargetConditionals.h>
#include <mach/mach.h>
#include <mach/mach_time.h>
#include <sys/sysctl.h>
#include <dispatch/dispatch.h>
#include <CommonCrypto/CommonDigest.h>
#include <CoreFoundation/CoreFoundation.h>

int dm_shim_nperflevels(void);
