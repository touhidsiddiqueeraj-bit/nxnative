// libkern/OSCacheControl.h shim (Android): cache synchronization after run-time code writes.
// __clear_cache is the compiler's portable ARM routine (it emits the right icache maintenance for
// the architecture); it never fails, so kCacheFunctionPrepareForExecution returns 0 as on Apple.
#pragma once
#include <stdint.h>

enum { kCacheFunctionPrepareForExecution = 1 };
static inline int sys_cache_control(int fn, void *addr, size_t len) {
    if (fn != kCacheFunctionPrepareForExecution) return -1;
    __builtin___clear_cache((char *)addr, (char *)addr + len);
    return 0;
}
// Apple's other entry point name (the i-cache half; the d-cache one is a no-op for anonymous memory)
static inline void sys_icache_invalidate(void *addr, size_t len) { __builtin___clear_cache((char *)addr, (char *)addr + len); }
