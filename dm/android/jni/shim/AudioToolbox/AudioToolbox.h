// AudioToolbox/AudioToolbox.h shim (Android): the two CoreAudio interfaces the host uses, over
// Android's native media stack. The API shapes are kept 1:1 so hle_audio.c and hle_movie.c compile
// unchanged; the implementation is in dm/android/jni/audio_shim.c.
//   AudioUnit (RemoteIO / DefaultOutput) - the output unit with a render callback. AAudio is the
//     same pull model: its data callback builds the AudioBufferList and calls the host's render
//     callback; property queries return kAudioObjectUnknown for the device (au_buffer returns early,
//     as on iOS, where AAudio's own buffer sizing stands in for the requested ~5 ms) and buffer-size
//     properties map onto the stream builder's frame settings.
//   AudioConverter (MPEG-4 AAC -> PCM16) - hle_movie.c's movie sound. AMediaCodec's AAC decoder in
//     sync mode: AudioConverterNew defers the codec to the first fill (the magic cookie with the
//     AudioSpecificConfig arrives in between), FillComplexBuffer queues the one packet the host's
//     feed callback hands over and copies the decoded PCM out. A decoder that cannot be made fails
//     the call, which the host already handles by outputting silence ([RED] movie: ...).
#pragma once
#include <stdint.h>
#include <stddef.h>

typedef int32_t OSStatus;
typedef uint32_t UInt32;
typedef int32_t SInt32;
typedef int64_t SInt64;
typedef uint16_t UInt16;
typedef uint64_t UInt64;
typedef float Float32;
typedef double Float64;
#define noErr 0
#include <pthread/qos.h>   // the QoS classes hle_audio.c's helper threads take (Apple's AudioToolbox chain pulls it in)

// ---- stream formats ----
enum { kAudioFormatLinearPCM = 0x6C70636D, kAudioFormatMPEG4AAC = 0x61636D70 };
enum { kLinearPCMFormatFlagIsSignedInteger = 2, kLinearPCMFormatFlagIsPacked = 4, kLinearPCMFormatFlagIsFloat = 1 };
typedef struct {
    Float64 mSampleRate; UInt32 mFormatID; UInt32 mFormatFlags;
    UInt32 mBytesPerPacket, mFramesPerPacket, mBytesPerFrame, mChannelsPerFrame, mBitsPerChannel, mReserved;
} AudioStreamBasicDescription;
typedef struct { Float32 mMinimum, mMaximum; } AudioValueRange;
typedef struct { UInt32 mNumberBuffers; struct { UInt32 mNumberChannels; UInt32 mDataByteSize; void *mData; } mBuffers[1]; } AudioBufferList;

// ---- audio units ----
typedef void *AudioUnit, *AudioComponent;
typedef uint32_t AudioUnitPropertyID, AudioUnitScope, AudioUnitElement, AudioDeviceID;
enum { kAudioUnitType_Output = 0x61756F75 };
enum { kAudioUnitSubType_DefaultOutput = 0x64656661, kAudioUnitSubType_RemoteIO = 0x72696F63 };
enum { kAudioUnitManufacturer_Apple = 0x6170706C };
enum { kAudioUnitScope_Global = 0, kAudioUnitScope_Input = 1, kAudioUnitScope_Output = 2 };
enum { kAudioUnitProperty_StreamFormat = 0x66000a, kAudioUnitProperty_SetRenderCallback = 0x66010a };
enum { kAudioOutputUnitProperty_CurrentDevice = 0x66000c };
enum { kAudioDevicePropertyBufferFrameSize = 0x660037, kAudioDevicePropertyBufferFrameSizeRange = 0x660038 };
#define kAudioObjectUnknown 0
typedef struct { uint32_t flags; } AudioUnitRenderActionFlags;
typedef struct { Float64 sampleTime, hostTime, rateScalar; } AudioTimeStamp;
typedef struct AudioConverter *AudioConverterRef;
typedef OSStatus (*AURenderCallback)(void *inRefCon, AudioUnitRenderActionFlags *ioActionFlags, const AudioTimeStamp *inTimeStamp,
                                     UInt32 inBusNumber, UInt32 inNumberFrames, AudioBufferList *ioData);
typedef struct { AURenderCallback inputProc; void *inputProcRefCon; } AURenderCallbackStruct;
typedef struct { UInt32 componentType, componentSubType, componentManufacturer, componentFlags, componentFlagsMask; } AudioComponentDescription;
typedef void (*AudioUnitPropertyListenerProc)(void *inUserData, AudioUnit inUnit, AudioUnitPropertyID inID, AudioUnitScope inScope, AudioUnitElement inElement);

AudioComponent AudioComponentFindNext(AudioComponent inComponent, const AudioComponentDescription *inDesc);
OSStatus AudioComponentInstanceNew(AudioComponent inComponent, AudioUnit *outUnit);
OSStatus AudioComponentInstanceDispose(AudioUnit inUnit);
OSStatus AudioUnitInitialize(AudioUnit inUnit);
OSStatus AudioUnitUninitialize(AudioUnit inUnit);
OSStatus AudioOutputUnitStart(AudioUnit inUnit);
OSStatus AudioOutputUnitStop(AudioUnit inUnit);
OSStatus AudioUnitSetProperty(AudioUnit inUnit, AudioUnitPropertyID inID, AudioUnitScope inScope, AudioUnitElement inElement,
                               const void *inData, UInt32 inDataSize);
OSStatus AudioUnitGetProperty(AudioUnit inUnit, AudioUnitPropertyID inID, AudioUnitScope inScope, AudioUnitElement inElement,
                              void *outData, UInt32 *ioDataSize);
OSStatus AudioUnitAddPropertyListener(AudioUnit inUnit, AudioUnitPropertyID inID, AudioUnitPropertyListenerProc inProc, void *inProcUserData);
OSStatus AudioUnitRemovePropertyListenerWithUserData(AudioUnit inUnit, AudioUnitPropertyID inID, AudioUnitPropertyListenerProc inProc, void *inProcUserData);

// ---- AAC converter (movie sound) ----
typedef struct { SInt64 mStartOffset; UInt32 mVariableFramesInPacket; UInt32 mDataByteSize; } AudioStreamPacketDescription;
enum { kAudioConverterDecompressionMagicCookie = 0x6D676963 };
typedef OSStatus (*AudioConverterComplexInputDataProc)(AudioConverterRef inAudioConverter, UInt32 *ioNumberDataPackets,
                                                        AudioBufferList *ioData, AudioStreamPacketDescription **outDataPacketDescription, void *inUserData);
OSStatus AudioConverterNew(const AudioStreamBasicDescription *inSourceFormat, const AudioStreamBasicDescription *inDestinationFormat, AudioConverterRef *outAudioConverter);
OSStatus AudioConverterDispose(AudioConverterRef inAudioConverter);
OSStatus AudioConverterSetProperty(AudioConverterRef inAudioConverter, AudioUnitPropertyID inPropertyID, UInt32 inPropertyDataSize, const void *inPropertyData);
OSStatus AudioConverterFillComplexBuffer(AudioConverterRef inAudioConverter, AudioConverterComplexInputDataProc inInputDataProc, void *inInputDataProcUserData,
                                         UInt32 *ioOutputDataPacketSize, AudioBufferList *ioOutputData, AudioStreamPacketDescription *outPacketDescription);
