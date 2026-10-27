// Android shim implementations: the Mach / GCD / CommonCrypto / CoreFoundation surfaces the host's C
// files use (the headers they come from are dm/android/jni/shim/*). One .c on purpose: everything the
// Android build has to stand in for, in one place. The audio and video decoders have their own files
// (audio_shim.c, video_shim.c).
#define _GNU_SOURCE   // process_vm_readv / renameat2 hide behind __USE_GNU in bionic's headers
#include "shim_config.h"
#include <stdatomic.h>
#include <malloc/malloc.h>   // the zone calls this file implements (size_t typedef from the header)

#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <semaphore.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>   // mremap / MREMAP_* (vm_remap serves the virtual pool)
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/uio.h>   // process_vm_readv (vm_read_overwrite's safe reads)
#include <sys/types.h>
#include <sys/uio.h>
#include <time.h>
#include <malloc.h>   // malloc_usable_size (the zone shim's sizes)
#include <unistd.h>

// ---- thread ids and per-thread CPU time (pthread_mach_thread_np / thread_info) ----
#define TID_SLOTS 4096
static struct { pthread_t t; uint32_t id; _Atomic long long cpu_ns; } g_tids[TID_SLOTS];
static pthread_mutex_t g_tid_mu = PTHREAD_MUTEX_INITIALIZER;
static _Atomic uint32_t g_tid_next;

uint32_t dm_shim_thread_id(pthread_t t) {
    pthread_mutex_lock(&g_tid_mu);
    for (int i = 0; i < TID_SLOTS; i++) if (g_tids[i].id && pthread_equal(g_tids[i].t, t)) { pthread_mutex_unlock(&g_tid_mu); return g_tids[i].id; }
    // a new thread: park it in a slot (round-robin past the watermark; guest threads number in the hundreds at most)
    static int park = 0;
    for (int i = 0; i < TID_SLOTS; i++) {
        int k = (park + i) % TID_SLOTS;
        if (!g_tids[k].id) { g_tids[k].t = t; g_tids[k].id = ++g_tid_next; g_tids[k].cpu_ns = 0; park = k + 1; pthread_mutex_unlock(&g_tid_mu); return g_tids[k].id; }
    }
    pthread_mutex_unlock(&g_tid_mu);
    return 0;   // table full: the id degenerates to 0 (only the logs' thread identities are affected)
}

uint64_t dm_shim_thread_cpu_ns(mach_port_t thread) {
    uint32_t id = (uint32_t)(uintptr_t)thread;
    if (!id) return 0;
    // the calling thread's CPU time is exact; other threads get their tracked snapshot (the host's
    // rows that read other threads are diagnostic counters)
    for (int i = 0; i < TID_SLOTS; i++)
        if (g_tids[i].id == id) {
            if (pthread_equal(g_tids[i].t, pthread_self())) {
                struct timespec ts; clock_gettime(CLOCK_THREAD_CPUTIME_ID, &ts);
                uint64_t ns = (uint64_t)ts.tv_sec * 1000000000ull + ts.tv_nsec;
                atomic_store(&g_tids[i].cpu_ns, ns);
                return ns;
            }
            return (uint64_t)atomic_load(&g_tids[i].cpu_ns);
        }
    return 0;
}

// ---- malloc zones (hle_uemalloc.c's Unreal heap): one allocator on Linux, the shape kept ----
struct malloc_zone_t { int unused; };
static struct malloc_zone_t g_dm_zone;
size_t malloc_size(const void *ptr) { return ptr ? malloc_usable_size((void *)(uintptr_t)ptr) : 0; }
size_t malloc_good_size(size_t n) { return n; }   // no zone quantum on Linux: the requested size is the usable size
void malloc_set_zone_name(malloc_zone_t *z, const char *name) { (void)z; (void)name; }
malloc_zone_t *malloc_create_zone(vm_size_t start_size, unsigned flags) { (void)start_size; (void)flags; return &g_dm_zone; }
void *malloc_zone_malloc(malloc_zone_t *z, size_t n) { (void)z; return malloc(n); }
void *malloc_zone_memalign(malloc_zone_t *z, size_t align, size_t n) { (void)z; void *p = NULL; return posix_memalign(&p, align ? align : 16, n ? n : 1) ? NULL : p; }
void *malloc_zone_realloc(malloc_zone_t *z, void *p, size_t n) { (void)z; return realloc(p, n); }
void malloc_zone_free(malloc_zone_t *z, void *p) { (void)z; free(p); }
void malloc_destroy_zone(malloc_zone_t *z) { (void)z; }

// ---- vm_remap (the virtual pool's alias mapping): Linux's mremap, the same alias step ----
kern_return_t vm_remap(mach_port_t target, vm_address_t *to, vm_size_t size, vm_address_t mask, int flags,
                       mach_port_t src_task, vm_address_t from, int copy, vm_prot_t *cur, vm_prot_t *max, int inherit) {
    (void)target; (void)mask; (void)copy; (void)inherit;   // one address space, no mask, and Linux remaps are moves
    if (!(flags & VM_FLAGS_FIXED)) { *cur = *max = 0; return KERN_INVALID_ADDRESS; }
    void *r = mremap((void *)(uintptr_t)from, size, size, MREMAP_FIXED | MREMAP_MAYMOVE, (void *)(uintptr_t)*to);
    if (r == MAP_FAILED || (uintptr_t)r != *to) return KERN_INVALID_ADDRESS;
    *cur = *max = VM_PROT_READ | VM_PROT_WRITE;
    return KERN_SUCCESS;
}

// ---- vm_read_overwrite: safe read of any address (probes, backtraces, guest stacks) ----
kern_return_t vm_read_overwrite(mach_port_t task, vm_address_t addr, vm_size_t size, vm_address_t data, vm_size_t *out_size) {
    (void)task;
    struct iovec local = { (void *)data, size }, remote = { (void *)addr, size };
    ssize_t n = process_vm_readv(getpid(), &local, 1, &remote, 1, 0);
    if (n < 0) { if (out_size) *out_size = 0; return KERN_INVALID_ADDRESS; }
    if (out_size) *out_size = (vm_size_t)n;
    return KERN_SUCCESS;
}

// ---- vm_region / mach_vm_region: the mapping at or after an address, with its protection ----
static unsigned prot_of(const char *perms) {   // "rwxp" from /proc/self/maps
    unsigned p = 0;
    if (perms[0] == 'r') p |= VM_PROT_READ;
    if (perms[1] == 'w') p |= VM_PROT_WRITE;
    if (perms[2] == 'x') p |= VM_PROT_EXECUTE;
    return p;
}
kern_return_t dm_shim_region(vm_address_t *addr, vm_size_t *size, vm_prot_t *protection) {
    FILE *f = fopen("/proc/self/maps", "re");
    if (!f) return KERN_INVALID_ADDRESS;
    char line[1024];
    kern_return_t r = KERN_INVALID_ADDRESS;
    while (fgets(line, sizeof line, f)) {
        unsigned long s, e; char perms[8];
        if (sscanf(line, "%lx-%lx %7s", &s, &e, perms) != 3) continue;
        if (s > *addr) break;   // walked past the address: not mapped, exactly what vm_region_64 reports
        if (e <= *addr) continue;
        *addr = s; *size = e - s; *protection = prot_of(perms); r = KERN_SUCCESS; break;
    }
    fclose(f);
    return r;
}

// Instructions / cycles of this thread (hle_nvn.c thr_instr): a private libSystem interface on
// Apple; a plain Android app cannot read per-thread PMU counts, so both stand in as the CPU time
// in ns - the ratios the detail log computes stay meaningful (nominal "cycles").
int thread_selfcounts(int kind, uint64_t *out, size_t n) {
    (void)kind;
    uint64_t ns = dm_shim_thread_cpu_ns(mach_thread_self());
    if (n >= 8) out[0] = ns;
    if (n >= 16) out[1] = ns;
    return n >= 16 ? 0 : KERN_INVALID_ADDRESS;
}

// ---- task footprint (hle_nvn.c power rows, every 300 frames) ----
uint64_t dm_shim_phys_footprint(void) {
    FILE *f = fopen("/proc/self/statm", "re");
    if (!f) return 0;
    unsigned long rss_pages = 0;
    if (fscanf(f, "%*lu %lu", &rss_pages) != 1) rss_pages = 0;
    fclose(f);
    return (uint64_t)rss_pages * 4096;
}

// ---- sysctl (main.c KERN_PROC start time; hle_nvn.c hw.nperflevels) ----
int sysctl(const int *name, uint_t namelen, void *oldp, size_t *oldlenp, void *newp, size_t newlen) {
    (void)newp; (void)newlen;
    if (namelen == 4 && name[0] == CTL_KERN && name[1] == KERN_PROC && name[2] == KERN_PROC_PID) {
        char path[64]; snprintf(path, sizeof path, "/proc/%d/stat", name[3] > 0 ? name[3] : (int)getpid());
        FILE *f = fopen(path, "re");
        if (!f) { errno = ENOENT; return -1; }
        char state = 0; unsigned long starttime = 0;
        // fields: pid (comm) state ppid pgrp session tty tpgid flags minflt ... starttime (field 22)
        int n = 0;
        while (n < 22) {
            int c = getc(f);
            if (c == EOF) break;
            if (c == '(') { while ((c = getc(f)) != ')' && c != EOF) {} continue; }   // the comm field
            ungetc(c, f);
            if (n == 21) { if (fscanf(f, "%lu", &starttime) != 1) starttime = 0; break; }
            unsigned long skip; if (fscanf(f, "%lu %c", &skip, &state) != 2) break; n++;   // consumes this field's tail and the next word
            n++;
        }
        fclose(f);
        if (!starttime) { errno = EINVAL; return -1; }
        if (!oldp || !oldlenp || *oldlenp < sizeof(struct kinfo_proc)) { if (oldlenp) *oldlenp = sizeof(struct kinfo_proc); errno = ENOMEM; return -1; }
        struct kinfo_proc *kp = oldp;
        kp->kp_proc.p_starttime.tv_sec = starttime / 100;
        kp->kp_proc.p_starttime.tv_usec = (starttime % 100) * 10000;
        return 0;
    }
    errno = ENOENT;
    return -1;
}

int sysctlbyname(const char *name, void *oldp, size_t *oldlenp, void *newp, size_t newlen) {
    (void)newp; (void)newlen;
    if (!strcmp(name, "hw.nperflevels")) {   // fstep_cpu_slowed: performance cores present?
        if (oldp && oldlenp && *oldlenp >= sizeof(int)) {
            if (*oldlenp != sizeof(int)) { *oldlenp = sizeof(int); errno = ENOMEM; return -1; }
            *(int *)oldp = dm_shim_nperflevels();
            return 0;
        }
        if (oldlenp) *oldlenp = sizeof(int);
        return 0;
    }
    errno = ENOENT;
    return -1;
}

int dm_shim_nperflevels(void) {
    // count cores in a cluster with the highest max frequency; 0 found (an emulator) = 1 level
    int best = 0, nperf = 0;
    for (int cpu = 0; cpu < 32; cpu++) {
        char p[96]; snprintf(p, sizeof p, "/sys/devices/system/cpu/cpu%d/cpufreq/cpuinfo_max_freq", cpu);
        FILE *f = fopen(p, "re");
        if (!f) break;
        long khz = 0; if (fscanf(f, "%ld", &khz) != 1) khz = 0;
        fclose(f);
        if (khz > best) { best = khz; nperf = 1; }
        else if (khz == best && khz) nperf++;
    }
    return nperf ? 2 : 1;   // big cores and little cores = 2 levels; one kind = 1
}

// ---- ELF section tables (mach-o/getsect.h): the host's swhle / swforce / swnvn tables ----
extern const char __start_swhle[], __stop_swhle[];
extern const char __start_swforce[], __stop_swforce[];
extern const char __start_swnvn[], __stop_swnvn[];

const void *dm_getsectiondata_elf(const char *section, unsigned long *size) {
    struct { const char *name; const char *start, *stop; } tabs[] = {
        {"swhle", __start_swhle, __stop_swhle},
        {"swforce", __start_swforce, __stop_swforce},
        {"swnvn", __start_swnvn, __stop_swnvn},
    };
    for (unsigned i = 0; i < sizeof tabs / sizeof *tabs; i++)
        if (!strcmp(section, tabs[i].name)) {
            if (size) *size = (unsigned long)(tabs[i].stop - tabs[i].start);
            return tabs[i].start;
        }
    if (size) *size = 0;
    return NULL;
}

// ---- CommonCrypto SHA-256 (loader.c DM_LOADHASH, hle_ro.c, hle_nvn.c) ----
static const uint32_t K256[64] = {
    0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
    0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
    0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
    0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
    0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
    0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2,
};
static uint32_t ror(uint32_t v, int n) { return (v >> n) | (v << (32 - n)); }
static void sha256_block(CC_SHA256_CTX *c, const uint8_t *p) {
    uint32_t w[64];
    for (int i = 0; i < 16; i++) w[i] = (uint32_t)p[4 * i] << 24 | (uint32_t)p[4 * i + 1] << 16 | (uint32_t)p[4 * i + 2] << 8 | p[4 * i + 3];
    for (int i = 16; i < 64; i++) {
        uint32_t s0 = ror(w[i - 15], 7) ^ ror(w[i - 15], 18) ^ (w[i - 15] >> 3);
        uint32_t s1 = ror(w[i - 2], 17) ^ ror(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    uint32_t a = c->h[0], b = c->h[1], cc = c->h[2], d = c->h[3], e = c->h[4], f = c->h[5], g = c->h[6], h = c->h[7];
    for (int i = 0; i < 64; i++) {
        uint32_t S1 = ror(e, 6) ^ ror(e, 11) ^ ror(e, 25), ch = (e & f) ^ (~e & g);
        uint32_t t1 = h + S1 + ch + K256[i] + w[i];
        uint32_t S0 = ror(a, 2) ^ ror(a, 13) ^ ror(a, 22), mj = (a & b) ^ (a & cc) ^ (b & cc);
        uint32_t t2 = S0 + mj;
        h = g; g = f; f = e; e = d + t1; d = cc; cc = b; b = a; a = t1 + t2;
    }
    c->h[0] += a; c->h[1] += b; c->h[2] += cc; c->h[3] += d; c->h[4] += e; c->h[5] += f; c->h[6] += g; c->h[7] += h;
}
int CC_SHA256_Init(CC_SHA256_CTX *c) {
    static const uint32_t iv[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    memcpy(c->h, iv, sizeof iv); c->bytes = 0; c->used = 0;
    return 1;
}
int CC_SHA256_Update(CC_SHA256_CTX *c, const void *data, CC_LONG len) {
    const uint8_t *p = data;
    c->bytes += len;
    if (c->used) {
        unsigned take = 64 - c->used; if (take > len) take = len;
        memcpy(c->buf + c->used, p, take); c->used += take; p += take; len -= take;
        if (c->used == 64) { sha256_block(c, c->buf); c->used = 0; }
    }
    while (len >= 64) { sha256_block(c, p); p += 64; len -= 64; }
    if (len) { memcpy(c->buf, p, len); c->used = len; }
    return 1;
}
int CC_SHA256_Final(unsigned char *md, CC_SHA256_CTX *c) {
    uint64_t bits = c->bytes * 8;
    uint8_t pad = 0x80;
    CC_SHA256_Update(c, &pad, 1);
    uint8_t zero = 0;
    while (c->used != 56) CC_SHA256_Update(c, &zero, 1);
    uint8_t tail[8];
    for (int i = 0; i < 8; i++) tail[i] = (uint8_t)(bits >> (56 - 8 * i));
    c->bytes = 0;   // the padding updates are not message bytes
    CC_SHA256_Update(c, tail, 8);
    for (int i = 0; i < 8; i++) for (int j = 0; j < 4; j++) md[4 * i + j] = (uint8_t)(c->h[i] >> (24 - 8 * j));
    return 1;
}
unsigned char *CC_SHA256(const void *data, CC_LONG len, unsigned char *md) {
    CC_SHA256_CTX c;
    CC_SHA256_Init(&c); CC_SHA256_Update(&c, data, len); CC_SHA256_Final(md, &c);
    return md;
}

// ---- CoreFoundation dictionary (hle_nvn.c program-key side table) ----
#include <CoreFoundation/CoreFoundation.h>
const CFDictionaryKeyCallBacks kCFTypeDictionaryKeyCallBacks = {0};
const CFDictionaryValueCallBacks kCFTypeDictionaryValueCallBacks = {0};
static const void *const kPixelFormatKey = "pixel_format";
const void *kCVPixelBufferPixelFormatTypeKey = kPixelFormatKey;

#define DICT_CAP 32
struct cf_dict { void *keys[DICT_CAP], *values[DICT_CAP]; unsigned used; };
static struct cf_dict *cf_dicts_of(CFMutableDictionaryRef d) { return d; }

CFMutableDictionaryRef CFDictionaryCreateMutable(CFAllocatorRef alloc, uint32_t capacity, void *kcb, void *vcb) {
    (void)alloc; (void)capacity; (void)kcb; (void)vcb;
    struct cf_dict *d = calloc(1, sizeof *d);
    return d;
}
CFDictionaryRef CFDictionaryCreate(CFAllocatorRef alloc, const void **keys, const void **values, uint64_t count, void *kcb, void *vcb) {
    (void)alloc; (void)kcb; (void)vcb;
    struct cf_dict *d = calloc(1, sizeof *d);
    if (d) for (uint64_t i = 0; i < count; i++) { d->keys[d->used] = (void *)keys[i]; d->values[d->used] = (void *)values[i]; d->used++; }
    return d;
}
const void *CFDictionaryGetValue(CFMutableDictionaryRef d_, const void *key) {
    struct cf_dict *d = cf_dicts_of(d_);
    if (!d) return NULL;
    for (unsigned i = 0; i < d->used; i++) if (d->keys[i] == key) return d->values[i];
    return NULL;
}
void CFDictionarySetValue(CFMutableDictionaryRef d_, const void *key, const void *value) {
    struct cf_dict *d = cf_dicts_of(d_);
    if (!d) return;
    for (unsigned i = 0; i < d->used; i++)
        if (d->keys[i] == key) { d->values[i] = (void *)value; return; }
    if (d->used == DICT_CAP) {
        // the program-key table is small; a full one drops to a linear scan warning
        static int warned;
        if (!warned++) fprintf(stderr, "[RED] cf dict: %d entries is full, dropping one\n", DICT_CAP);
        return;
    }
    d->keys[d->used] = (void *)key; d->values[d->used] = (void *)value; d->used++;
}
void CFDictionaryRemoveValue(CFMutableDictionaryRef d_, const void *key) {
    struct cf_dict *d = cf_dicts_of(d_);
    if (!d) return;
    for (unsigned i = 0; i < d->used; i++)
        if (d->keys[i] == key) {
            if (i + 1 < d->used) { d->keys[i] = d->keys[d->used - 1]; d->values[i] = d->values[d->used - 1]; }
            d->used--;
            return;
        }
}
uint32_t CFDictionaryGetCount(CFMutableDictionaryRef d_) { struct cf_dict *d = cf_dicts_of(d_); return d ? d->used : 0; }
void CFDictionaryApplyFunction(CFMutableDictionaryRef d_, CFDictionaryApplierFunction fn, void *context) {
    struct cf_dict *d = cf_dicts_of(d_);
    if (!d) return;
    for (unsigned i = 0; i < d->used; i++) fn(d->keys[i], d->values[i], context);
}
void CFRelease(CFTypeRef o) { (void)o; }   // the tables' entries are owned elsewhere (hle_nvn.c allocates them)
CFTypeRef CFRetain(CFTypeRef o) { return o; }
static struct { int32_t v; } g_cfnums[64];
static unsigned g_nnum;
CFNumberRef CFNumberCreate(CFAllocatorRef alloc, int type, const void *value) {
    (void)alloc;
    if (type != kCFNumberSInt32Type || g_nnum >= sizeof g_cfnums / sizeof *g_cfnums) return NULL;
    memcpy(&g_cfnums[g_nnum].v, value, sizeof(int32_t));
    return &g_cfnums[g_nnum++];   // session-lifetime numbers: the movie decoder makes one per session
}
int CFNumberGetValue(CFNumberRef n, int type, void *value) {
    if (!n || type != kCFNumberSInt32Type) return 0;
    memcpy(value, &((struct { int32_t v; } *)n)->v, sizeof(int32_t));
    return 1;
}

// ---- clonefile / renamex_np (sys/clonefile.h): the save transactions' tree clone and atomic swap ----
#include <sys/clonefile.h>
#include <sys/xattr.h>
#include <sys/ioctl.h>
#include <linux/fs.h>
#include <dirent.h>

int clonefile(const char *src, const char *dst, int flags) {
    (void)flags;
    struct stat st;
    if (lstat(src, &st)) return -1;
    if (S_ISDIR(st.st_mode)) {
        if (mkdir(dst, st.st_mode & 0777)) return -1;
        DIR *d = opendir(src);
        if (!d) return -1;
        struct dirent *e;
        int bad = 0;
        while ((e = readdir(d))) {
            if (!strcmp(e->d_name, ".") || !strcmp(e->d_name, "..")) continue;
            char sp[1100], dp[1100];
            if (snprintf(sp, sizeof sp, "%s/%s", src, e->d_name) >= (int)sizeof sp || snprintf(dp, sizeof dp, "%s/%s", dst, e->d_name) >= (int)sizeof dp) { bad = ENAMETOOLONG; break; }
            if (clonefile(sp, dp, 0)) { bad = errno ? errno : EIO; break; }
        }
        closedir(d);
        return bad ? (errno = bad, -1) : 0;
    }
    if (!S_ISREG(st.st_mode)) { errno = EINVAL; return -1; }   // symlinks and specials: saves hold files and directories only
    int in = open(src, O_RDONLY), out = open(dst, O_WRONLY | O_CREAT | O_EXCL, st.st_mode & 0777);
    if (in < 0 || out < 0) { if (in >= 0) close(in); if (out >= 0) close(out); return -1; }
    // FICLONE first: a reflink keeps the copy zero-cost on btrfs / xfs / f2fs, as clonefile does on APFS
    if (ioctl(out, FICLONE, (void *)(intptr_t)in) == 0) { close(in); close(out); return 0; }
    char buf[1 << 16];
    ssize_t n;
    while ((n = read(in, buf, sizeof buf)) > 0)
        if (write(out, buf, (size_t)n) != n) { n = -1; break; }
    int err = n < 0 ? (errno ? errno : EIO) : 0;
    close(in); close(out);
    if (err) { unlink(dst); errno = err; return -1; }
    return 0;
}

int renamex_np(const char *a, const char *b, unsigned flags) {
    if (flags != RENAME_SWAP) { errno = EINVAL; return -1; }
    if (syscall(SYS_renameat2, AT_FDCWD, a, AT_FDCWD, b, (unsigned)(1u << 1)) == 0) return 0;   // RENAME_EXCHANGE
    return -1;   // RENAME_EXCHANGE since Linux 3.15; every Android device that runs the app has it
}

// ---- Darwin-shaped xattr calls → Linux's (see shim/sys/xattr.h) ----
ssize_t dm_getxattr(const char *path, const char *name, void *value, size_t size, u_int32_t position, int options) {
    (void)position;   // resource-fork offsets, which no guest file uses
    return (options & XATTR_NOFOLLOW ? lgetxattr : getxattr)(path, name, value, size);
}
int dm_setxattr(const char *path, const char *name, const void *value, size_t size, u_int32_t position, int options) {
    (void)position;
    return (options & XATTR_NOFOLLOW ? lsetxattr : setxattr)(path, name, value, size, 0);
}
int dm_removexattr(const char *path, const char *name, int options) {
    return (options & XATTR_NOFOLLOW ? lremovexattr : removexattr)(path, name);
}
