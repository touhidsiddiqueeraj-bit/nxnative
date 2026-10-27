// CoreFoundation/CoreFoundation.h shim (Android): the CF types the host's C files touch.
//   hle_nvn.c        - a pointer-keyed CFMutableDictionary side table for program keys
//   hle_movie_avc.c  - CFDictionary / CFNumber to pass the pixel format to VideoToolbox's session
//                      creation (dm/android/jni/video_shim.c), and CFRelease on the session's objects
// The implementations are in dm/android/jni/cf_shim.c. Everything else CF in the tree lives in
// Apple-platform .m files the Android build excludes.
#pragma once
#include <stddef.h>
#include <stdint.h>

typedef void *CFTypeRef, *CFMutableDictionaryRef, *CFDictionaryRef, *CFNumberRef, *CFAllocatorRef, *CFStringRef;
typedef const void *CFTypeRefConst;
typedef unsigned long CFIndex;
enum { kCFNumberSInt32Type = 3 };
typedef void (*CFDictionaryApplierFunction)(const void *key, const void *value, void *context);
typedef struct { int dummy; } CFDictionaryKeyCallBacks, CFDictionaryValueCallBacks;
extern const CFDictionaryKeyCallBacks kCFTypeDictionaryKeyCallBacks;
extern const CFDictionaryValueCallBacks kCFTypeDictionaryValueCallBacks;
extern const void *kCVPixelBufferPixelFormatTypeKey;

CFMutableDictionaryRef CFDictionaryCreateMutable(CFAllocatorRef alloc, uint32_t capacity, void *kcb, void *vcb);
CFDictionaryRef CFDictionaryCreate(CFAllocatorRef alloc, const void **keys, const void **values, uint64_t count, void *kcb, void *vcb);
const void *CFDictionaryGetValue(CFMutableDictionaryRef d, const void *key);
void CFDictionarySetValue(CFMutableDictionaryRef d, const void *key, const void *value);
void CFDictionaryRemoveValue(CFMutableDictionaryRef d, const void *key);
uint32_t CFDictionaryGetCount(CFMutableDictionaryRef d);
void CFDictionaryApplyFunction(CFMutableDictionaryRef d, CFDictionaryApplierFunction fn, void *context);
void CFRelease(CFTypeRef o);
CFTypeRef CFRetain(CFTypeRef o);
CFNumberRef CFNumberCreate(CFAllocatorRef alloc, int type, const void *value);
int CFNumberGetValue(CFNumberRef n, int type, void *value);
