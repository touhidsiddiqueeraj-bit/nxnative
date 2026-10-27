// VideoToolbox/VideoToolbox.h shim (Android): hle_movie_avc.c's H.264 movie decoder over NDK
// MediaCodec. The host file was written for this port - its own comments describe MediaCodec's
// presentation-order output and it keeps the reorder queue itself - so the shim maps each
// VideoToolbox call onto its AMediaCodec counterpart (implementation: dm/android/jni/video_shim.c):
//   CMVideoFormatDescriptionCreateFromH264ParameterSets - parse SPS / PPS (width, height for the
//     caller's log line; the parameter sets also become the codec's csd-0 / csd-1)
//   VTDecompressionSessionCreate - AMediaCodec "video/avc" (NV12 / YUV420Flexible output; the RGBA
//     mode is converted in the shim, as VideoToolbox would convert with the stream's colour matrix)
//   CMBlockBuffer + CMSampleBuffer - the length-prefixed access unit (already the format MediaCodec
//     wants) wrapped for the decode call
//   VTDecompressionSessionDecodeFrame - synchronous: queue the access unit, drain the decoded
//     pictures, each handed to the host's callback with its pts (which the host's queue sorts)
#pragma once
#include <stdint.h>
#include <stddef.h>
#include "../CoreFoundation/CoreFoundation.h"

typedef int32_t OSStatus;
typedef uint32_t UInt32;
typedef int32_t SInt32;
typedef int64_t CMTimeValue;
typedef int32_t CMTimeScale;
#define noErr 0

// ---- CMTime (the caller only reads value / timescale; invalid marks unused slots) ----
typedef struct { int64_t value; int32_t timescale; int flags; uint64_t epoch; } CMTime;
static inline CMTime CMTimeMake(int64_t value, int32_t timescale) { CMTime t = {value, timescale, 0, 0}; return t; }
#define kCMTimeInvalid ((CMTime){0, 0, 1, 0})

// ---- pixel buffers ----
typedef struct { uint8_t *planes[3]; size_t stride[3]; uint64_t w, h, nplanes; void *mem; } CVPixelBuffer, *CVPixelBufferRef;
typedef void *CVImageBufferRef;
enum { kCVPixelFormatType_32BGRA = 0x42475241, kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange = 0x70423032 };
enum { kCVPixelBufferLock_ReadOnly = 1 };
CVPixelBufferRef CVPixelBufferRetain(CVPixelBufferRef pb);
void CVPixelBufferRelease(CVPixelBufferRef pb);
uint64_t CVPixelBufferGetWidth(CVPixelBufferRef pb);
uint64_t CVPixelBufferGetHeight(CVPixelBufferRef pb);
uint8_t *CVPixelBufferGetBaseAddress(CVPixelBufferRef pb);
uint8_t *CVPixelBufferGetBaseAddressOfPlane(CVPixelBufferRef pb, int plane);
size_t CVPixelBufferGetBytesPerRow(CVPixelBufferRef pb);
size_t CVPixelBufferGetBytesPerRowOfPlane(CVPixelBufferRef pb, int plane);
uint64_t CVPixelBufferGetWidthOfPlane(CVPixelBufferRef pb, int plane);
uint64_t CVPixelBufferGetHeightOfPlane(CVPixelBufferRef pb, int plane);
void CVPixelBufferLockBaseAddress(CVPixelBufferRef pb, int flags);
void CVPixelBufferUnlockBaseAddress(CVPixelBufferRef pb, int flags);

// ---- format descriptions ----
typedef struct { int64_t width, height; } CMVideoDimensions;
typedef struct { uint8_t sps[512], pps[512]; int nsps, npps; int64_t w, h; } CMVideoFormatDescription, *CMVideoFormatDescriptionRef;
CMVideoDimensions CMVideoFormatDescriptionGetDimensions(CMVideoFormatDescriptionRef fd);
OSStatus CMVideoFormatDescriptionCreateFromH264ParameterSets(void *allocator, size_t parameterSetCount, const uint8_t *const *parameterSetPointers,
                                                             const size_t *parameterSetSizes, int nalHeaderBytes, CMVideoFormatDescriptionRef *formatDescriptionOut);

// ---- block / sample buffers ----
typedef struct { uint8_t *data; size_t len; } CMBlockBuffer, *CMBlockBufferRef;
typedef struct { CMTime duration, presentationTimeStamp, decodeTimeStamp; } CMSampleTimingInfo;   // Darwin's field order
typedef struct { CMBlockBufferRef bb; CMVideoFormatDescriptionRef fd; CMSampleTimingInfo ti; size_t len; } CMSampleBuffer, *CMSampleBufferRef;
OSStatus CMBlockBufferCreateWithMemoryBlock(void *allocator, void *memory, size_t len, void *deallocator, void *context,
                                            size_t offset, size_t len2, int flags, CMBlockBufferRef *out);
OSStatus CMBlockBufferReplaceDataBytes(const void *src, CMBlockBufferRef bb, size_t offset, size_t len);
OSStatus CMSampleBufferCreateReady(void *allocator, CMBlockBufferRef bb, CMVideoFormatDescriptionRef fd, int nsamples, int nsampleTimings,
                                   const CMSampleTimingInfo *ti, int nsampleSizes, const size_t *sizes, CMSampleBufferRef *out);

// ---- the decode session ----
typedef struct OpaqueVTDecompressionSession *VTDecompressionSessionRef;
typedef uint32_t VTDecodeInfoFlags;
typedef void (*VTDecompressionOutputCallback)(void *decompressionOutputRefCon, void *sourceFrameRefCon, OSStatus status,
                                              VTDecodeInfoFlags infoFlags, CVImageBufferRef imageBuffer, CMTime presentationTimeStamp, CMTime presentationDuration);
typedef struct { VTDecompressionOutputCallback decompressionOutputCallback; void *decompressionOutputRefCon; } VTDecompressionOutputCallbackRecord;
OSStatus VTDecompressionSessionCreate(void *allocator, CMVideoFormatDescriptionRef fd, void *sessionOptions,
                                      void *destinationImageBufferAttributes, const VTDecompressionOutputCallbackRecord *cb, VTDecompressionSessionRef *sessionOut);
OSStatus VTDecompressionSessionInvalidate(VTDecompressionSessionRef session);
OSStatus VTDecompressionSessionWaitForAsynchronousFrames(VTDecompressionSessionRef session);
OSStatus VTDecompressionSessionDecodeFrame(VTDecompressionSessionRef session, CMSampleBufferRef sb, uint32_t flags, void *frameOptions, void *outputHandler);
