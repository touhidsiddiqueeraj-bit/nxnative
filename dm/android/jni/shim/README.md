# The Android shim layer

These headers stand in for the Apple frameworks and system interfaces the host's C files use, so
dm/host and dm/gfx compile unchanged on Android. Each header says what it maps onto and what its
limits are; the implementations are `../sys_shim.c` (Mach, GCD, CommonCrypto, CoreFoundation, the
ELF section tables), `../audio_shim.c` (AudioUnits over AAudio, the AAC converter over NDK
MediaCodec) and `../video_shim.c` (VideoToolbox H.264 over NDK MediaCodec).

The directory is first in the include path (`../CMakeLists.txt`), so `#include <mach/mach.h>` and
the other Apple paths resolve here before anything else. Nothing outside dm/android includes these.
