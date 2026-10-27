// libc passthrough table: C library functions consistent with AAPCS64 and with no struct layout dependency, connected directly to Apple libc. Goes into the swhle section.
#include "dm.h"
#include <math.h>
#include <ctype.h>
#include <time.h>
typedef struct { const char *name; void *fn; } hle_ent;
// The section name hle.h's HLE() macro uses (its __ELF__ branch): on ELF it must be a valid
// identifier for the linker's __start_ / __stop_ symbols (the Android shim's getsect maps the
// lookups); on Mach-O it is the __DATA segment section the committed walkers read
#if defined(__ELF__)
#define DM_LIB_SEC "swhle"
#else
#define DM_LIB_SEC "__DATA,swhle"
#endif
#define L(n) __attribute__((used, section(DM_LIB_SEC))) static const hle_ent hle_lib_##n = { #n, (void *)n };
L(memcpy) L(memset) L(memmove) L(memcmp) L(memchr) L(strlen) L(strnlen) L(strcmp) L(strncmp)
L(strcpy) L(strncpy) L(strcat) L(strncat) L(strchr) L(strrchr) L(strstr) L(strspn) L(strcspn)
L(strpbrk) L(strcasecmp) L(strncasecmp) L(atoi) L(atof) L(strtol) L(strtoul) L(strtoll) L(strtoull)
L(strtod) L(strtof) L(sqrtf) L(cbrtf) L(expf) L(acosf) L(cosf) L(sinf) L(atan2f) L(fmodf) L(cos)
L(sin) L(powf) L(atanf) L(tanf) L(asinf) L(log10) L(pow) L(logf) L(ldexpf) L(log10f) L(log)
L(ldexp) L(frexp) L(atan2) L(modff) L(exp2f) L(exp2) L(log2f) L(log2) L(lroundf)
L(mktime) L(timegm) L(gmtime_r) L(localtime_r) L(strftime)
