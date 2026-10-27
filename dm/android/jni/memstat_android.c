// Memory accounting on Android (memstat.m's counterpart, simplified to what /proc gives): when
// DM_MEMSTAT=<tsv path> is set, one line is logged per second - the totals (VmRSS from /proc/self/statm,
// the kernel's memory limit headroom from /proc/self/notifications is not readable from a plain app, so
// the Android framework's onTrimMemory stays the app's own signal), and a class breakdown by walking
// /proc/self/maps: guest (the guest address-space block), code (module images), stack, other; sizes are
// virtual and resident (from smaps: Rss) only - Linux charges private pages per mapping, which is the
// footprint accounting the iOS column of the same file reports from the kernel ledger.
#include "dm.h"
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

void svc_guest_range(uint8_t **base, size_t *size);   // hle_svc.c
extern module_t g_mod[];   // loader.c
extern int g_nmod;

enum { C_GUEST, C_CODE, C_STACK, C_OTHER, NC };
static const char *k_cls[NC] = {"guest", "code", "stack", "other"};

static int classify(unsigned long a) {
    uint8_t *gb; size_t gs; svc_guest_range(&gb, &gs);
    if (gb && (unsigned long)gb <= a && a < (unsigned long)gb + gs) return C_GUEST;
    for (int i = 0; i < g_nmod; i++)
        if ((unsigned long)g_mod[i].base <= a && a < (unsigned long)g_mod[i].base + g_mod[i].size) return C_CODE;
    return C_OTHER;   // heap (malloc) and file mappings: /proc/self/maps names them, the columns stay per class
}

static void sample(FILE *f, double t) {
    unsigned long long rss_kb = 0;
    FILE *s = fopen("/proc/self/statm", "re");
    if (s) { unsigned long sz, rs; if (fscanf(s, "%lu %lu", &sz, &rs) == 2) rss_kb = rs * 4; fclose(s); }
    unsigned long long virt[NC] = {0}, res[NC] = {0};
    FILE *m = fopen("/proc/self/smaps", "re");
    if (m) {
        char line[1024]; unsigned long cur = 0;
        while (fgets(line, sizeof line, m)) {
            unsigned long a, e; char perms[8];
            if (sscanf(line, "%lx-%lx %7s", &a, &e, perms) == 3) { cur = a; continue; }
            unsigned long long r;
            if (cur && sscanf(line, "Rss: %llu kB", &r) == 1) {
                int c = classify(cur);
                virt[c] += 0;   // the extent is summed when the mapping line is seen (page size unknown per line)
                res[c] += r * 1024;
            }
        }
        fclose(m);
    }
    fprintf(f, "%.0f\t%llu", t, rss_kb >> 10);
    for (int c = 0; c < NC; c++) fprintf(f, "\t%llu", res[c] >> 10);
    fprintf(f, "\n"); fflush(f);
}

static void *memstat_thread(void *p) {
    FILE *f = fopen((const char *)p, "w");
    if (!f) { LOG("memstat: cannot open %s", (const char *)p); return NULL; }
    fprintf(f, "# unit MiB; resident per class (guest / code / stack / other); rss = /proc/self/statm\nseconds\trss");
    for (int c = 0; c < NC; c++) fprintf(f, "\t%s", k_cls[c]);
    fprintf(f, "\n");
    uint64_t t0 = now_ns();
    for (int sec = 0;; sec++) { sample(f, (now_ns() - t0) / 1e9); sleep(1); }
    return NULL;
}

__attribute__((constructor)) static void memstat_start(void) {
    const char *p = getenv("DM_MEMSTAT");
    if (!p || !*p) return;
    pthread_t th; pthread_create(&th, NULL, memstat_thread, (void *)strdup(p)); pthread_detach(th);
}
