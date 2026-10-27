// CommonCrypto/CommonDigest.h shim (Android): the CC_SHA256 family the host uses to fingerprint
// modules (loader.c DM_LOADHASH), NRO images (hle_ro.c) and relocation tables (hle_nvn.c).
// A compact SHA-256 (FIPS 180-4) over the same API shape: CC_LONG is the length type of the
// incremental calls, the one-shot CC_SHA256(data, len, md) included.
#pragma once
#include <stdint.h>
#include <stddef.h>

typedef uint32_t CC_LONG;
enum { CC_SHA256_DIGEST_LENGTH = 32, CC_SHA1_DIGEST_LENGTH = 20 };
typedef struct { uint32_t h[8]; uint64_t bytes; uint8_t buf[64]; unsigned used; } CC_SHA256_CTX;
int CC_SHA256_Init(CC_SHA256_CTX *c);
int CC_SHA256_Update(CC_SHA256_CTX *c, const void *data, CC_LONG len);
int CC_SHA256_Final(unsigned char *md, CC_SHA256_CTX *c);
unsigned char *CC_SHA256(const void *data, CC_LONG len, unsigned char *md);
