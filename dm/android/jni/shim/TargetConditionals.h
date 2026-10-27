// TargetConditionals shim for the Android build: the host's Apple platform guards resolve to the Mac branch
// (TARGET_OS_IPHONE 0), which is the branch the Android port follows (modules loaded from files, Mac audio
// descriptor, no run-time code generation assumptions beyond the rewriter's, which needs none of iOS's).
// TARGET_OS_OSX 0 keeps the iPhone's memory-cap default (no 24 GB desktop cap) and iOS behaviour elsewhere.
#pragma once
#define TARGET_OS_IPHONE 0
#define TARGET_OS_OSX 0
#define TARGET_OS_MAC 0
#define TARGET_OS_ANDROID 1
