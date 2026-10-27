// sys/xattr.h shim (Android): Darwin's extended-attribute calls carry a position and an options
// word; Linux's are the plain 4-argument forms (and the l* variants when XATTR_NOFOLLOW is set,
// which is the only option the host uses). The header maps the signatures so hle_fs.c compiles
// unchanged; bionic's <sys/xattr.h> provides the real ones (include_next pulls it in).
#pragma once
#include_next <sys/xattr.h>
#include <sys/types.h>

#ifndef XATTR_NOFOLLOW
#define XATTR_NOFOLLOW 0x0001
#endif
#define XATTR_CREATE 0x0002
#define XATTR_REPLACE 0x0004
// Darwin spells the missing-attribute error ENOATTR; Linux says ENODATA (same number as ENOATTR on Darwin)
#ifndef ENOATTR
#define ENOATTR ENODATA
#endif

ssize_t dm_getxattr(const char *path, const char *name, void *value, size_t size, u_int32_t position, int options);
int dm_setxattr(const char *path, const char *name, const void *value, size_t size, u_int32_t position, int options);
int dm_removexattr(const char *path, const char *name, int options);

// Darwin's names → the shim's (position ignored: it is for resource forks, which the guest never reads)
#define getxattr(path, name, value, size, position, options) dm_getxattr(path, name, value, size, position, options)
#define setxattr(path, name, value, size, position, options) dm_setxattr(path, name, value, size, position, options)
#define removexattr(path, name, options) dm_removexattr(path, name, options)
