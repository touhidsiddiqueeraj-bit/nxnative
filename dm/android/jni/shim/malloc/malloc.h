// malloc/malloc.h shim (Android): Darwin's malloc zones, which hle_uemalloc.c uses to put the
// Unreal engine's own heap in a dedicated accounting zone (used / peak for the memory report).
// Linux has one allocator: the shim's zone keeps the same call shape over plain malloc, and
// malloc_usable_size stands in for malloc_size (implementation: dm/android/jni/sys_shim.c).
#pragma once
#include <stddef.h>
#include <stdint.h>
#include <sys/types.h>

typedef uint64_t vm_size_t;   // the zone calls' length type (Mach's, which this header shares on Apple)

typedef struct malloc_zone_t malloc_zone_t;

size_t malloc_size(const void *ptr);
size_t malloc_good_size(size_t n);   // Darwin rounds to the zone's quantum; Linux's allocator is asked for the real usable size
void malloc_set_zone_name(malloc_zone_t *z, const char *name);
malloc_zone_t *malloc_create_zone(vm_size_t start_size, unsigned flags);
void *malloc_zone_malloc(malloc_zone_t *z, size_t n);
void *malloc_zone_memalign(malloc_zone_t *z, size_t align, size_t n);
void *malloc_zone_realloc(malloc_zone_t *z, void *p, size_t n);
void malloc_zone_free(malloc_zone_t *z, void *p);
void malloc_destroy_zone(malloc_zone_t *z);
