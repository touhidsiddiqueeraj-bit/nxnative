// sys/clonefile.h shim (Android): Darwin's copy-on-write tree clone (hle_fs.c's save transactions)
// and its atomic directory swap, over Linux primitives:
//   clonefile(src, dst, flags) - a recursive tree copy; per file the FICLONE ioctl is tried first
//     (btrfs / xfs / f2fs reflink: the same zero-copy semantics APFS gives), a byte copy otherwise
//   renamex_np(a, b, RENAME_SWAP) - renameat2(RENAME_EXCHANGE): the two paths trade places in one
//     atomic step, exactly what the save Commit's swap-in needs
#pragma once
#include <stddef.h>

int clonefile(const char *src, const char *dst, int flags);   // flags: 0 (FICLONE is decided per file)
int renamex_np(const char *a, const char *b, unsigned flags);   // the only flag used is RENAME_SWAP
#define RENAME_SWAP 0x2u
