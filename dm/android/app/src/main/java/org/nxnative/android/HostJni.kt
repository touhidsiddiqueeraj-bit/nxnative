// The JNI surface of libdmhost.so (dm/android/jni/dm_jni.c). Loaded once before the game starts;
// every method is stable and named for what it hands over, not for how the host implements it.
package org.nxnative.android

import android.view.Surface

object HostJni {
    @JvmStatic external fun nativeBoot(front: Any, files: String, romfs: String?, aoc: String?, pso: String?): Int
    @JvmStatic external fun nativeSetSurface(surface: Surface?)
    @JvmStatic external fun nativeTouch(points: FloatArray)   // [id, x, y] triples, normalized [0,1)
    @JvmStatic external fun nativeInput(buttons: Long, lx: Int, ly: Int, rx: Int, ry: Int)   // the virtual gamepad (dm_input_set)
    @JvmStatic external fun nativePad(buttons: Long, lx: Int, ly: Int, rx: Int, ry: Int)     // physical controllers (dm_pad_set)
    @JvmStatic external fun nativePadPlayers(mask: Int)
    @JvmStatic external fun nativeFocus(ev: Int)   // 0 resign, 1 background, 2 foreground, 3 active
    @JvmStatic external fun nativeShutdown(why: String?)
    @JvmStatic external fun nativePresentCount(): Long
    @JvmStatic external fun nativeHapticInput(on: Int)
    @JvmStatic external fun nativeSetenv(key: String, value: String)   // the launcher applies its choices

    fun load() {
        System.loadLibrary("dmhost")
    }
}
