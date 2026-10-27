// AudioToolbox shim implementation (Android): see dm/android/jni/shim/AudioToolbox/AudioToolbox.h.
// The output unit over AAudio (the same pull model as RemoteIO) and the AAC converter over NDK
// MediaCodec in sync mode (one packet in, PCM out, as AudioConverterFillComplexBuffer is used).
#include "shim_config.h"
#include <string.h>
#include <AudioToolbox/AudioToolbox.h>
#include <aaudio/AAudio.h>
#include <media/NdkMediaCodec.h>
#include <media/NdkMediaFormat.h>

// ---------------- output unit (AAudio) ----------------
typedef struct au_unit {
    AAudioStream *stream;
    AURenderCallback render;   // the host's callback (hle_audio.c au_render)
    void *refcon;
    unsigned channels, rate;
} au_unit;

// AAudio's data callback carries no user pointer; a small table links streams back to their unit
// (two outputs are open at most, and they are made one at a time). The host's 200 ms watchdog
// handles a stream that stops calling back - on iOS it covers the audio-session takeover, on
// Android the same path covers a lost device.
enum { AU_SLOTS = 8 };
static struct { AAudioStream *s; au_unit *u; } g_au_slots[AU_SLOTS];
static pthread_mutex_t g_au_slot_mu = PTHREAD_MUTEX_INITIALIZER;

static void au_slot_add(AAudioStream *s, au_unit *u) {
    pthread_mutex_lock(&g_au_slot_mu);
    for (int i = 0; i < AU_SLOTS; i++) if (!g_au_slots[i].s) { g_au_slots[i].s = s; g_au_slots[i].u = u; break; }
    pthread_mutex_unlock(&g_au_slot_mu);
}
static void au_slot_del(AAudioStream *s) {
    pthread_mutex_lock(&g_au_slot_mu);
    for (int i = 0; i < AU_SLOTS; i++) if (g_au_slots[i].s == s) { g_au_slots[i].s = NULL; g_au_slots[i].u = NULL; }
    pthread_mutex_unlock(&g_au_slot_mu);
}
static au_unit *au_of(AAudioStream *s) {
    pthread_mutex_lock(&g_au_slot_mu);
    au_unit *u = NULL;
    for (int i = 0; i < AU_SLOTS; i++) if (g_au_slots[i].s == s) { u = g_au_slots[i].u; break; }
    pthread_mutex_unlock(&g_au_slot_mu);
    return u;
}

static aaudio_data_callback_result_t au_pull(AAudioStream *stream, void *user, void *data, int32_t frames) {
    (void)user;   // the unit is found through the slot table (au_slot_add)
    au_unit *u = au_of(stream);
    if (!u || !u->render) { memset(data, 0, (size_t)frames * 2 * (u ? u->channels : 2)); return AAUDIO_CALLBACK_RESULT_CONTINUE; }
    AudioBufferList bl = {1, {{u->channels, (UInt32)frames * 2 * u->channels, data}}};
    AudioUnitRenderActionFlags fl = {0};
    AudioTimeStamp ts = {frames, 0, 1};
    u->render(u->refcon, &fl, &ts, 0, (UInt32)frames, &bl);
    return AAUDIO_CALLBACK_RESULT_CONTINUE;
}

static AudioComponent g_out_component = (AudioComponent)1;

AudioComponent AudioComponentFindNext(AudioComponent inComponent, const AudioComponentDescription *inDesc) {
    (void)inComponent;
    if (inDesc && inDesc->componentType == kAudioUnitType_Output) return g_out_component;
    return NULL;
}
OSStatus AudioComponentInstanceNew(AudioComponent inComponent, AudioUnit *outUnit) {
    if (inComponent != g_out_component || !outUnit) return -1;
    au_unit *u = calloc(1, sizeof *u);
    if (!u) return -1;
    *outUnit = u;
    return noErr;
}
OSStatus AudioComponentInstanceDispose(AudioUnit inUnit) {
    au_unit *u = inUnit;
    if (!u) return noErr;
    if (u->stream) { au_slot_del(u->stream); AAudioStream_close(u->stream); }
    free(u);
    return noErr;
}
OSStatus AudioUnitInitialize(AudioUnit inUnit) { (void)inUnit; return noErr; }
OSStatus AudioUnitUninitialize(AudioUnit inUnit) { (void)inUnit; return noErr; }

OSStatus AudioOutputUnitStart(AudioUnit inUnit) {
    au_unit *u = inUnit;
    if (!u || !u->render) return -1;
    if (u->stream) return noErr;
    AAudioStreamBuilder *b;
    if (AAudio_createStreamBuilder(&b) != AAUDIO_OK) return -1;
    AAudioStreamBuilder_setDirection(b, AAUDIO_DIRECTION_OUTPUT);
    AAudioStreamBuilder_setFormat(b, AAUDIO_FORMAT_PCM_I16);
    AAudioStreamBuilder_setSampleRate(b, u->rate ? u->rate : 48000);
    AAudioStreamBuilder_setChannelCount(b, u->channels ? u->channels : 2);
    AAudioStreamBuilder_setPerformanceMode(b, AAUDIO_PERFORMANCE_MODE_LOW_LATENCY);
    AAudioStreamBuilder_setDataCallback(b, au_pull, NULL);   // the unit is found by stream (the slot table below)
    aaudio_result_t r = AAudioStreamBuilder_openStream(b, &u->stream);
    AAudioStreamBuilder_delete(b);
    if (r != AAUDIO_OK || !u->stream) { u->stream = NULL; return -1; }
    au_slot_add(u->stream, u);
    u->rate = (unsigned)AAudioStream_getSampleRate(u->stream);   // the device's own rate: no resampler in between
    if (AAudioStream_requestStart(u->stream) != AAUDIO_OK) {
        au_slot_del(u->stream);
        AAudioStream_close(u->stream); u->stream = NULL;
        return -1;
    }
    return noErr;
}
OSStatus AudioOutputUnitStop(AudioUnit inUnit) {
    au_unit *u = inUnit;
    if (!u || !u->stream) return noErr;
    AAudioStream_requestStop(u->stream);
    au_slot_del(u->stream);
    AAudioStream_close(u->stream);   // the host's Start-after-Stop path makes a fresh stream, as it retries after a stall
    u->stream = NULL;
    return noErr;
}

OSStatus AudioUnitSetProperty(AudioUnit inUnit, AudioUnitPropertyID inID, AudioUnitScope inScope, AudioUnitElement inElement,
                              const void *inData, UInt32 inDataSize) {
    (void)inScope; (void)inElement;
    au_unit *u = inUnit;
    if (!u) return -1;
    if (inID == kAudioUnitProperty_StreamFormat && inDataSize >= sizeof(AudioStreamBasicDescription)) {
        const AudioStreamBasicDescription *f = inData;
        if (f->mChannelsPerFrame) u->channels = f->mChannelsPerFrame;
        if (f->mSampleRate) u->rate = (unsigned)f->mSampleRate;
        return noErr;
    }
    if (inID == kAudioUnitProperty_SetRenderCallback && inDataSize >= sizeof(AURenderCallbackStruct)) {
        const AURenderCallbackStruct *cb = inData;
        u->render = cb->inputProc; u->refcon = cb->inputProcRefCon;
        return noErr;
    }
    return noErr;   // buffer-size properties are the stream builder's on AAudio; accept and move on
}
OSStatus AudioUnitGetProperty(AudioUnit inUnit, AudioUnitPropertyID inID, AudioUnitScope inScope, AudioUnitElement inElement,
                              void *outData, UInt32 *ioDataSize) {
    (void)inUnit; (void)inScope; (void)inElement;
    if (inID == kAudioOutputUnitProperty_CurrentDevice && outData && ioDataSize && *ioDataSize >= sizeof(AudioDeviceID)) {
        *(AudioDeviceID *)outData = kAudioObjectUnknown;   // no per-device selection on Android: au_buffer returns early, as it does on iOS
        return noErr;
    }
    return -1;
}
OSStatus AudioUnitAddPropertyListener(AudioUnit inUnit, AudioUnitPropertyID inID, AudioUnitPropertyListenerProc inProc, void *inProcUserData) {
    (void)inUnit; (void)inID; (void)inProc; (void)inProcUserData; return noErr;   // accepted, never fires (see au_pull's comment)
}
OSStatus AudioUnitRemovePropertyListenerWithUserData(AudioUnit inUnit, AudioUnitPropertyID inID, AudioUnitPropertyListenerProc inProc, void *inProcUserData) {
    (void)inUnit; (void)inID; (void)inProc; (void)inProcUserData; return noErr;
}

// ---------------- AAC converter (AMediaCodec, sync mode) ----------------
struct AudioConverter {
    unsigned channels, rate;
    AMediaCodec *codec;
    uint8_t cookie[256]; size_t cookie_n;   // the ES descriptor with the AudioSpecificConfig
    int dead;
};

// The magic cookie is the MPEG-4 ES descriptor hle_movie.c builds (FFmpeg's shape): walk its
// tag/length descriptors to the AudioSpecificConfig (tag 5), which is MediaCodec's csd-0.
static const uint8_t *cookie_asc(const uint8_t *ck, size_t n, size_t *asc_n) {
    size_t i = 0;
    while (i + 2 <= n) {
        uint8_t tag = ck[i++];
        size_t len = 0; int more;
        do { if (i >= n) return NULL; more = ck[i] & 0x80; len = (len << 7) | (ck[i] & 0x7f); i++; } while (more);
        if (len > n - i) return NULL;
        if (tag == 5) { *asc_n = len; return ck + i; }
        if (tag == 3) i += 3;        // ES_ID (2) + flags (1)
        else if (tag == 4) i += 13;  // objectType &c: the fixed DecoderConfig body hle_movie.c writes
        else return NULL;
    }
    return NULL;
}

// AAC-LC AudioSpecificConfig from rate / channels, when no cookie was set (md_aac_open's fallback path)
static void asc_default(uint8_t *asc, unsigned rate, unsigned ch) {
    static const unsigned rates[] = {96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350};
    unsigned fi = 4;
    for (unsigned i = 0; i < sizeof rates / sizeof *rates; i++) if (rates[i] == rate) { fi = i; break; }
    asc[0] = (uint8_t)(2u << 3 | fi >> 1);   // AudioObjectType 2 (AAC-LC), top bit of the frequency index
    asc[1] = (uint8_t)((fi & 1) << 7 | (ch & 15) << 3);
}

static int aac_start(AudioConverterRef c) {
    if (c->codec) return 0;
    AMediaFormat *fmt = AMediaFormat_new();
    if (!fmt) return -1;
    AMediaFormat_setString(fmt, AMEDIAFORMAT_KEY_MIME, "audio/mp4a-latm");
    AMediaFormat_setInt32(fmt, AMEDIAFORMAT_KEY_SAMPLE_RATE, (int32_t)c->rate);
    AMediaFormat_setInt32(fmt, AMEDIAFORMAT_KEY_CHANNEL_COUNT, (int32_t)c->channels);
    size_t asc_n = 0;
    const uint8_t *asc = c->cookie_n ? cookie_asc(c->cookie, c->cookie_n, &asc_n) : NULL;
    if (asc && asc_n && asc_n <= 64) {
        AMediaFormat_setBuffer(fmt, "csd-0", asc, asc_n);
    } else {   // no cookie (or not parseable): AAC-LC from the format, MediaCodec derives the rest
        uint8_t lc[2];
        asc_default(lc, c->rate, c->channels);
        AMediaFormat_setBuffer(fmt, "csd-0", lc, 2);
    }
    AMediaCodec *codec = AMediaCodec_createDecoderByType("audio/mp4a-latm");
    media_status_t st = codec ? AMediaCodec_configure(codec, fmt, NULL, NULL, 0) : AMEDIA_ERROR_UNSUPPORTED;
    AMediaFormat_delete(fmt);
    if (st != AMEDIA_OK) { if (codec) AMediaCodec_delete(codec); return -1; }
    if (AMediaCodec_start(codec) != AMEDIA_OK) { AMediaCodec_delete(codec); return -1; }
    c->codec = codec;
    return 0;
}

OSStatus AudioConverterNew(const AudioStreamBasicDescription *inSourceFormat, const AudioStreamBasicDescription *inDestinationFormat, AudioConverterRef *outAudioConverter) {
    if (!inSourceFormat || !inDestinationFormat || !outAudioConverter) return -1;
    if (inSourceFormat->mFormatID != kAudioFormatMPEG4AAC || inDestinationFormat->mFormatID != kAudioFormatLinearPCM) return -1;
    AudioConverterRef c = calloc(1, sizeof *c);
    if (!c) return -1;
    c->channels = inSourceFormat->mChannelsPerFrame;
    c->rate = (unsigned)inSourceFormat->mSampleRate;
    if (!c->channels || c->channels > 8 || !c->rate) { free(c); return -1; }
    *outAudioConverter = c;
    return noErr;   // the codec itself is made on the first fill, when the cookie is in
}

OSStatus AudioConverterSetProperty(AudioConverterRef inAudioConverter, AudioUnitPropertyID inPropertyID, UInt32 inPropertyDataSize, const void *inPropertyData) {
    if (!inAudioConverter || inPropertyID != kAudioConverterDecompressionMagicCookie) return -1;
    if (inPropertyDataSize > sizeof inAudioConverter->cookie) return -1;
    memcpy(inAudioConverter->cookie, inPropertyData, inPropertyDataSize);
    inAudioConverter->cookie_n = inPropertyDataSize;
    return noErr;
}

OSStatus AudioConverterDispose(AudioConverterRef inAudioConverter) {
    if (!inAudioConverter) return -1;
    if (inAudioConverter->codec) { AMediaCodec_stop(inAudioConverter->codec); AMediaCodec_delete(inAudioConverter->codec); }
    free(inAudioConverter);
    return noErr;
}

OSStatus AudioConverterFillComplexBuffer(AudioConverterRef c, AudioConverterComplexInputDataProc inInputDataProc, void *inInputDataProcUserData,
                                          UInt32 *ioOutputDataPacketSize, AudioBufferList *ioOutputData, AudioStreamPacketDescription *outPacketDescription) {
    (void)outPacketDescription;
    if (!c || !inInputDataProc || !ioOutputDataPacketSize || !ioOutputData) return -1;
    if (!c->codec && c->dead) { *ioOutputDataPacketSize = 0; return -1; }
    if (!c->codec && aac_start(c)) { c->dead = 1; *ioOutputDataPacketSize = 0; return -1; }   // the host logs this once and outputs silence
    // one AAC access unit in (the host's feed hands over at most one packet)
    UInt32 packets = 1;
    AudioBufferList in = {0};
    AudioStreamPacketDescription *pd = NULL;
    OSStatus st = inInputDataProc(c, &packets, &in, &pd, inInputDataProcUserData);
    if (st || !packets || !in.mNumberBuffers || !in.mBuffers[0].mData) {
        *ioOutputDataPacketSize = 0;
        return st ? noErr : 1;   // no packet: nothing to decode, as the host's feed signals it
    }
    ssize_t ib = AMediaCodec_dequeueInputBuffer(c->codec, 0);
    if (ib < 0) ib = AMediaCodec_dequeueInputBuffer(c->codec, 10000000);   // 10 ms: the stream is fresh, it takes one
    if (ib < 0) return -1;
    size_t cap = 0;
    uint8_t *dst = AMediaCodec_getInputBuffer(c->codec, ib, &cap);
    if (!dst || in.mBuffers[0].mDataByteSize > cap) { AMediaCodec_queueInputBuffer(c->codec, ib, 0, 0, 0, 0); return -1; }
    memcpy(dst, in.mBuffers[0].mData, in.mBuffers[0].mDataByteSize);
    AMediaCodec_queueInputBuffer(c->codec, ib, 0, in.mBuffers[0].mDataByteSize, 0, 0);
    // decoded PCM out (the decoder's start-up delay may give none for the first packet: frames 0)
    UInt32 got = 0;
    AMediaCodecBufferInfo info;
    ssize_t ob = AMediaCodec_dequeueOutputBuffer(c->codec, &info, 0);
    if (ob == AMEDIACODEC_INFO_OUTPUT_FORMAT_CHANGED) ob = AMediaCodec_dequeueOutputBuffer(c->codec, &info, 0);
    if (ob >= 0) {
        size_t sz = 0;
        uint8_t *src = AMediaCodec_getOutputBuffer(c->codec, ob, &sz);
        if (src && info.size > 0) {
            size_t bytes = (size_t)info.size;
            if (bytes > ioOutputData->mBuffers[0].mDataByteSize) bytes = ioOutputData->mBuffers[0].mDataByteSize;
            memcpy(ioOutputData->mBuffers[0].mData, src, bytes);
            got = (UInt32)(bytes / (2 * c->channels));
        }
        AMediaCodec_releaseOutputBuffer(c->codec, ob, false);
    }
    *ioOutputDataPacketSize = got;
    return got ? noErr : 1;
}
