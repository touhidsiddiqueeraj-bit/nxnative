// mach-o/ldsyms.h shim (Android): the executable header Mach-O code passes to getsectiondata.
// On ELF there is no single header to name; the symbol exists so the call sites compile unchanged.
#pragma once
static const void *const _mh_execute_header = 0;
