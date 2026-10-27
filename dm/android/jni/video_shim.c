// VideoToolbox shim implementation (Android): see dm/android/jni/shim/VideoToolbox/VideoToolbox.h.
// hle_movie_avc.c's decoder over NDK MediaCodec: the parameter sets it kept become csd-0 / csd-1
// (Annex-B prefixed), the length-prefixed access units it builds are MediaCodec's preferred input
// shape, and its own reorder queue (sorted by pts, depth by profile) absorbs MediaCodec's
// presentation-order output - the file's comments describe exactly this mapping.
// Pixel output is YUV420Flexible: the shim copies it into a contiguous NV12 CVPixelBuffer (the host's
// NV12 path), or converts to BGRA when the session was created for it (the RGBA mode, as VideoToolbox
// would convert with the stream's colour matrix).
#include "shim_config.h"
#include <string.h>
#include <VideoToolbox/VideoToolbox.h>

// CFDictionaryCreate lives in sys_shim.c (one definition serves every caller)
#include <media/NdkMediaCodec.h>
#include <media/NdkMediaFormat.h>
// AMediaCodec_getOutputImage is NDK r28+; NDK 26 reads the output buffer and its layout from the format keys

// ---------------- pixel buffers ----------------
CVPixelBufferRef CVPixelBufferRetain(CVPixelBufferRef pb) { return pb; }   // the shim's buffers are heap blocks owned by the queue: the caller's queue owns the one reference
void CVPixelBufferRelease(CVPixelBufferRef pb) { if (pb) { free(pb->mem); free(pb); } }
uint64_t CVPixelBufferGetWidth(CVPixelBufferRef pb) { return pb ? pb->w : 0; }
uint64_t CVPixelBufferGetHeight(CVPixelBufferRef pb) { return pb ? pb->h : 0; }
uint8_t *CVPixelBufferGetBaseAddress(CVPixelBufferRef pb) { return pb ? pb->planes[0] : NULL; }
uint8_t *CVPixelBufferGetBaseAddressOfPlane(CVPixelBufferRef pb, int plane) { return pb && plane >= 0 && plane < 3 ? pb->planes[plane] : NULL; }
size_t CVPixelBufferGetBytesPerRow(CVPixelBufferRef pb) { return pb ? pb->stride[0] : 0; }
size_t CVPixelBufferGetBytesPerRowOfPlane(CVPixelBufferRef pb, int plane) { return pb && plane >= 0 && plane < 3 ? pb->stride[plane] : 0; }
uint64_t CVPixelBufferGetWidthOfPlane(CVPixelBufferRef pb, int plane) { return pb ? pb->w : 0; }
uint64_t CVPixelBufferGetHeightOfPlane(CVPixelBufferRef pb, int plane) { return pb && plane == 1 ? (pb->h + 1) / 2 : pb->h; }
void CVPixelBufferLockBaseAddress(CVPixelBufferRef pb, int flags) { (void)pb; (void)flags; }
void CVPixelBufferUnlockBaseAddress(CVPixelBufferRef pb, int flags) { (void)pb; (void)flags; }

// ---------------- SPS: width / height (for the caller's log line and its buffer sizing) ----------------
typedef struct { const uint8_t *p; size_t n, bit; } br_t;
static int br_bit(br_t *b) { if (b->bit >= b->n * 8) return -1; int v = (b->p[b->bit >> 3] >> (7 - (b->bit & 7))) & 1; b->bit++; return v; }
static uint32_t br_u(br_t *b, int n) { uint32_t v = 0; for (int i = 0; i < n; i++) { int x = br_bit(b); if (x < 0) return v; v = v << 1 | x; } return v; }
static uint32_t br_ue(br_t *b) { int z = 0, x; while ((x = br_bit(b)) == 0 && z < 32) z++; if (x < 0) return 0; return (1u << z) - 1 + br_u(b, z); }
static int32_t br_se(br_t *b) { uint32_t k = br_ue(b); return (k & 1) ? (int32_t)((k + 1) / 2) : -(int32_t)(k / 2); }
static void br_skip_scaling(br_t *b, int size) {
    int last = 8, next = 8;
    for (int j = 0; j < size; j++) {
        if (next != 0) {
            int32_t se = br_se(b);
            next = (se + last + 256) % 256;
        }
        last = next ? next : last;
    }
}
static int sps_high(unsigned profile) {
    return profile == 100 || profile == 110 || profile == 122 || profile == 244 || profile == 44 || profile == 83 || profile == 86 ||
           profile == 118 || profile == 128 || profile == 138 || profile == 139 || profile == 134 || profile == 135;
}
// pic_width / pic_height in pixels, per the SPS parse order (7.3.2.1.1), with 4:2:0 cropping units.
static void sps_size(const uint8_t *sps, size_t n, int64_t *w, int64_t *h) {
    *w = *h = 0;
    br_t b = {sps, n, 0};
    unsigned profile = br_u(&b, 8);
    br_u(&b, 8);          // constraint flags + reserved
    br_u(&b, 8);          // level_idc
    (void)br_ue(&b);      // seq_parameter_set_id
    unsigned chroma = 1;  // 4:2:0 when the SPS states none (baseline / main, what the games use)
    if (sps_high(profile)) {
        chroma = br_ue(&b);
        if (chroma == 3) { (void)br_u(&b, 1); chroma = 0; }   // separate_colour_plane: cropping is on the luma grid
        (void)br_ue(&b); (void)br_ue(&b); (void)br_u(&b, 1);   // bit depths, qpprime
        if (br_u(&b, 1)) {   // seq_scaling_matrix_present
            for (int i = 0; i < (chroma == 0 ? 12 : 8); i++)
                if (br_u(&b, 1)) br_skip_scaling(&b, i < 6 ? 16 : 64);
        }
    }
    (void)br_ue(&b);      // log2_max_frame_num_minus4
    unsigned pom = br_ue(&b);
    if (pom == 0) (void)br_ue(&b);
    else if (pom == 1) {
        (void)br_u(&b, 1); (void)br_se(&b); (void)br_se(&b);
        unsigned cycles = br_ue(&b);
        for (unsigned i = 0; i < cycles; i++) (void)br_se(&b);
    }
    (void)br_ue(&b);      // max_num_ref_frames
    (void)br_u(&b, 1);    // gaps_in_frame_num_value_allowed
    uint64_t cw = (uint64_t)br_ue(&b) + 1, chh = (uint64_t)br_ue(&b) + 1;   // in macroblocks / map units
    unsigned frame_mbs_only = br_u(&b, 1);
    if (!frame_mbs_only) (void)br_u(&b, 1);   // mb_adaptive_frame_field
    (void)br_u(&b, 1);    // direct_8x8
    uint64_t left = 0, right = 0, top = 0, bottom = 0;
    if (br_u(&b, 1)) {   // frame_cropping
        left = br_ue(&b); right = br_ue(&b); top = br_ue(&b); bottom = br_ue(&b);
    }
    // 4:2:0 crop units: x = 2; y = 2 * (2 - frame_mbs_only). (Higher-chroma profiles would need their
    // own units; every game stream here is 4:2:0, the one the existing take path also assumes.)
    *w = (int64_t)(cw * 16 - (left + right) * 2);
    *h = (int64_t)(chh * 16 * (2 - frame_mbs_only) - (top + bottom) * 2 * (2 - frame_mbs_only));
    if (*w < 0) *w = 0;
    if (*h < 0) *h = 0;
}

// ---------------- format descriptions ----------------
CMVideoDimensions CMVideoFormatDescriptionGetDimensions(CMVideoFormatDescriptionRef fd) {
    CMVideoDimensions d = {fd ? fd->w : 0, fd ? fd->h : 0};
    return d;
}
OSStatus CMVideoFormatDescriptionCreateFromH264ParameterSets(void *allocator, size_t parameterSetCount, const uint8_t *const *parameterSetPointers,
                                                              const size_t *parameterSetSizes, int nalHeaderBytes, CMVideoFormatDescriptionRef *formatDescriptionOut) {
    (void)allocator; (void)nalHeaderBytes;
    if (!formatDescriptionOut || parameterSetCount < 2) return -1;
    CMVideoFormatDescriptionRef fd = calloc(1, sizeof *fd);
    if (!fd) return -1;
    for (size_t i = 0; i < parameterSetCount && i < 2; i++) {
        if (i == 0) { if (parameterSetSizes[0] > sizeof fd->sps) { free(fd); return -1; } memcpy(fd->sps, parameterSetPointers[0], parameterSetSizes[0]); fd->nsps = (int)parameterSetSizes[0]; }
        else { if (parameterSetSizes[1] > sizeof fd->pps) { free(fd); return -1; } memcpy(fd->pps, parameterSetPointers[1], parameterSetSizes[1]); fd->npps = (int)parameterSetSizes[1]; }
    }
    sps_size(fd->sps, (size_t)fd->nsps, &fd->w, &fd->h);
    if (fd->w <= 0 || fd->h <= 0) { free(fd); return -1; }
    *formatDescriptionOut = fd;
    return noErr;
}

// ---------------- block / sample buffers ----------------
OSStatus CMBlockBufferCreateWithMemoryBlock(void *allocator, void *memory, size_t len, void *deallocator, void *context,
                                            size_t offset, size_t len2, int flags, CMBlockBufferRef *out) {
    (void)allocator; (void)memory; (void)deallocator; (void)context; (void)offset; (void)flags;
    if (!out || (len && len != len2)) return -1;
    CMBlockBufferRef bb = malloc(sizeof *bb);
    if (!bb) return -1;
    bb->data = malloc(len ? len : 1);
    if (!bb->data) { free(bb); return -1; }
    bb->len = len;
    *out = bb;
    return noErr;
}
OSStatus CMBlockBufferReplaceDataBytes(const void *src, CMBlockBufferRef bb, size_t offset, size_t len) {
    if (!bb || !bb->data || offset > bb->len || len > bb->len - offset) return -1;
    memcpy(bb->data + offset, src, len);
    return noErr;
}
OSStatus CMSampleBufferCreateReady(void *allocator, CMBlockBufferRef bb, CMVideoFormatDescriptionRef fd, int nsamples, int nsampleTimings,
                                   const CMSampleTimingInfo *ti, int nsampleSizes, const size_t *sizes, CMSampleBufferRef *out) {
    (void)allocator; (void)nsamples; (void)nsampleSizes;
    if (!out || !bb || !fd) return -1;
    CMSampleBufferRef sb = malloc(sizeof *sb);
    if (!sb) return -1;
    sb->bb = bb; sb->fd = fd;
    sb->ti = ti && nsampleTimings ? ti[0] : (CMSampleTimingInfo){0};
    sb->len = sizes ? sizes[0] : 0;
    *out = sb;
    return noErr;
}

// ---------------- the decode session ----------------
struct OpaqueVTDecompressionSession {
    AMediaCodec *codec;
    VTDecompressionOutputCallbackRecord cb;
    unsigned w, h;
    int rgba;   // BGRA output (the RGBA mode): converted here from the decoder's NV12
};
static void session_drop(VTDecompressionSessionRef s) {
    if (s->codec) { AMediaCodec_stop(s->codec); AMediaCodec_delete(s->codec); s->codec = NULL; }
}

OSStatus VTDecompressionSessionCreate(void *allocator, CMVideoFormatDescriptionRef formatDescription, void *sessionOptions,
                                      void *destinationImageBufferAttributes, const VTDecompressionOutputCallbackRecord *cb, VTDecompressionSessionRef *sessionOut) {
    (void)allocator; (void)sessionOptions;
    if (!formatDescription || !cb || !sessionOut) return -1;
    VTDecompressionSessionRef s = calloc(1, sizeof *s);
    if (!s) return -1;
    s->cb = *cb;
    s->w = (unsigned)(formatDescription->w > 0 ? formatDescription->w : 0);
    s->h = (unsigned)(formatDescription->h > 0 ? formatDescription->h : 0);
    if (destinationImageBufferAttributes) {
        CFNumberRef pf = CFDictionaryGetValue(destinationImageBufferAttributes, kCVPixelBufferPixelFormatTypeKey);
        int32_t v = 0;
        if (pf && CFNumberGetValue(pf, kCFNumberSInt32Type, &v) && v == kCVPixelFormatType_32BGRA) s->rgba = 1;
    }
    AMediaFormat *fmt = AMediaFormat_new();
    if (!fmt) { free(s); return -1; }
    AMediaFormat_setString(fmt, AMEDIAFORMAT_KEY_MIME, "video/avc");
    // csd: the parameter sets with Annex-B start codes, as MediaCodec takes them
    uint8_t csd[1030];
    csd[0] = csd[1] = csd[2] = 0; csd[3] = 1;
    memcpy(csd + 4, formatDescription->sps, (size_t)formatDescription->nsps);
    AMediaFormat_setBuffer(fmt, "csd-0", csd, 4 + (size_t)formatDescription->nsps);
    memcpy(csd + 4, formatDescription->pps, (size_t)formatDescription->npps);
    AMediaFormat_setBuffer(fmt, "csd-1", csd, 4 + (size_t)formatDescription->npps);
    s->codec = AMediaCodec_createDecoderByType("video/avc");
    media_status_t st = s->codec ? AMediaCodec_configure(s->codec, fmt, NULL, NULL, 0) : AMEDIA_ERROR_UNSUPPORTED;
    AMediaFormat_delete(fmt);
    if (st != AMEDIA_OK) { session_drop(s); free(s); return -1; }
    if (AMediaCodec_start(s->codec) != AMEDIA_OK) { session_drop(s); free(s); return -1; }
    *sessionOut = s;
    return noErr;
}
OSStatus VTDecompressionSessionInvalidate(VTDecompressionSessionRef session) {
    if (!session) return noErr;
    session_drop(session);
    free(session);
    return noErr;
}
OSStatus VTDecompressionSessionWaitForAsynchronousFrames(VTDecompressionSessionRef session) { (void)session; return noErr; }   // decoding is synchronous in this shim

// one decoded frame into a CVPixelBuffer: contiguous NV12 (Y then interleaved UV), the layout read
// from the output format's stride / slice-height keys (the semiplanar arrangement Qualcomm, Mali and
// virtuoso devices hand out; a planar colour format is reported and the frame skipped, the first
// case only). In RGBA mode a separate BGRA block follows the YUV one (VideoToolbox's conversion,
// BT.601 limited range - what the streams carry), and planes[0] points at it with a 4*w stride.
static CVPixelBufferRef nv12_frame(VTDecompressionSessionRef s, const uint8_t *data, size_t len, int32_t w, int32_t h, int32_t stride, int32_t vstride, int32_t color) {
    if (!data || !len || w <= 0 || h <= 0 || stride <= 0) return NULL;
    if (vstride < h) vstride = h;
    (void)color;   // COLOR_FormatYUV420Flexible's semiplanar shape: pixel stride 2 in the UV plane
    CVPixelBufferRef pb = calloc(1, sizeof *pb);
    if (!pb) return NULL;
    pb->w = (uint64_t)w; pb->h = (uint64_t)h; pb->nplanes = 2;
    size_t ys = (size_t)w, uvs = ys, uh = (size_t)((vstride + 1) / 2);
    size_t need = ys * (size_t)vstride + uvs * uh + (s->rgba ? (size_t)4 * (size_t)w * (size_t)h : 0) + 64;
    pb->mem = malloc(need);
    if (!pb->mem) { free(pb); return NULL; }
    uint8_t *y = pb->mem, *uv = y + ys * (size_t)vstride;
    for (int32_t r = 0; r < h; r++) memcpy(y + (size_t)r * ys, data + (size_t)r * (size_t)stride, (size_t)w);
    const uint8_t *suv = data + (size_t)vstride * (size_t)stride;   // the UV plane follows the Y plane, same stride
    for (size_t r = 0; r < (size_t)((h + 1) / 2); r++) memcpy(uv + r * uvs, suv + r * (size_t)stride, (size_t)w);
    pb->planes[0] = y; pb->planes[1] = uv;
    pb->stride[0] = pb->stride[1] = pb->stride[2] = ys;
    if (s->rgba) {
        uint8_t *bgra = uv + uvs * uh;
        for (int32_t r = 0; r < h; r++)
            for (int32_t c = 0; c < w; c++) {
                int Y = y[(size_t)r * ys + (size_t)c] - 16, U = uv[((size_t)r / 2) * uvs + 2 * ((size_t)c / 2)] - 128, V = uv[((size_t)r / 2) * uvs + 2 * ((size_t)c / 2) + 1] - 128;
                int R = (298 * Y + 409 * V + 128) >> 8, G = (298 * Y - 100 * U - 208 * V + 128) >> 8, B = (298 * Y + 516 * U + 128) >> 8;
                uint8_t *px = bgra + (size_t)r * 4 * (size_t)w + 4 * (size_t)c;
                px[0] = B < 0 ? 0 : B > 255 ? 255 : B; px[1] = G < 0 ? 0 : G > 255 ? 255 : G; px[2] = R < 0 ? 0 : R > 255 ? 255 : R; px[3] = 255;
            }
        pb->nplanes = 1;
        pb->planes[0] = bgra;
        pb->stride[0] = 4 * (size_t)w;
    }
    return pb;
}

OSStatus VTDecompressionSessionDecodeFrame(VTDecompressionSessionRef session, CMSampleBufferRef sb, uint32_t flags, void *frameOptions, void *outputHandler) {
    (void)flags; (void)frameOptions; (void)outputHandler;
    if (!session || !session->codec || !sb || !sb->bb || !sb->bb->data) return -1;
    ssize_t ib = AMediaCodec_dequeueInputBuffer(session->codec, 100000000);   // 100 ms: one input is free again by now in this synchronous loop
    if (ib < 0) return -1;
    size_t cap = 0;
    uint8_t *dst = AMediaCodec_getInputBuffer(session->codec, ib, &cap);
    if (!dst || sb->len > cap) { AMediaCodec_queueInputBuffer(session->codec, ib, 0, 0, 0, 0); return -1; }
    memcpy(dst, sb->bb->data, sb->len);
    AMediaCodec_queueInputBuffer(session->codec, ib, 0, sb->len, (uint64_t)sb->ti.presentationTimeStamp.value, 0);
    // drain what is ready without waiting: the host's take loop pulls the rest (mavc_take / flush)
    AMediaCodecBufferInfo info;
    for (;;) {
        ssize_t ob = AMediaCodec_dequeueOutputBuffer(session->codec, &info, 0);
        if (ob == AMEDIACODEC_INFO_OUTPUT_FORMAT_CHANGED) continue;
        if (ob < 0) break;   // nothing ready now: the host's take loop pulls the rest (mavc_take / flush)
        do {
            if (info.size <= 0) break;
            AMediaFormat *fmt = AMediaCodec_getBufferFormat(session->codec, (size_t)ob);
            int32_t w = (int32_t)session->w, h = (int32_t)session->h, stride = w, vstride = h, color = 0;
            if (fmt) {
                AMediaFormat_getInt32(fmt, "width", &w);
                AMediaFormat_getInt32(fmt, "height", &h);
                AMediaFormat_getInt32(fmt, "stride", &stride);
                AMediaFormat_getInt32(fmt, "slice-height", &vstride);
                AMediaFormat_getInt32(fmt, "color-format", &color);
                AMediaFormat_delete(fmt);
            }
            if (w <= 0 || h <= 0) { w = (int32_t)session->w; h = (int32_t)session->h; }
            if (stride < w) stride = w;
            if (vstride < h) vstride = h;
            size_t sz = 0;
            const uint8_t *src = AMediaCodec_getOutputBuffer(session->codec, (size_t)ob, &sz);
            CVPixelBufferRef pb = nv12_frame(session, src, sz, w, h, stride, vstride, color);
            if (pb) {
                CMTime pts = {info.presentationTimeUs * 1000ll, 1000000, 0, 0}, dur = {0, 0, 0, 0};
                session->cb.decompressionOutputCallback(session->cb.decompressionOutputRefCon, NULL, noErr, 0, pb, pts, dur);
            }
        } while (0);
        AMediaCodec_releaseOutputBuffer(session->codec, ob, false);
    }
    return noErr;
}
