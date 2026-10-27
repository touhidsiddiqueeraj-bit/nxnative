// mach-o/getsect.h shim (Android): the host's three registration tables live in Mach-O sections
// (hle.h / hle_nvn.c put every entry there with __attribute__((section)); hle.c, loader.c and
// hle_nvn.c read them back with getsectiondata). On ELF the same tables live in sections whose
// names are valid C identifiers (the __ELF__ branches of those macros), and the linker gives every
// such section __start_<name> / __stop_<name> symbols; this shim maps the Mach-O (segment, section)
// queries onto them. Only the host's own section names are known here; anything else returns NULL,
// as getsectiondata does for a missing section.
#pragma once
#include <stddef.h>
#include <stdint.h>

// ldsyms.h provides _mh_execute_header
const void *dm_getsectiondata_elf(const char *section, unsigned long *size);
static inline const void *getsectiondata(const void *image, const char *segment, const char *section, unsigned long *size) {
    (void)image; (void)segment;   // one image on Android: the loaded libdmhost.so, as on the Mac
    return dm_getsectiondata_elf(section, size);
}
