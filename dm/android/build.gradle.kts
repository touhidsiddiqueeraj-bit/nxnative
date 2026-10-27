// The Android app of nxnative (dm/android): the game shell - launcher, touch gamepad, controllers,
// saves, logs, rumble - around the host (libdmhost.so, built from dm/host + dm/gfx by jni/CMakeLists.txt).
// Games are packed into it by scripts/build_apk_from_nsp.py (the build_ipa_from_nsp.py counterpart);
// the CI workflow (.github/workflows/android.yml) builds the empty shell and releases packed APKs.
plugins {
    id("com.android.application") version "8.5.2" apply false
    id("org.jetbrains.kotlin.android") version "1.9.24" apply false
}
