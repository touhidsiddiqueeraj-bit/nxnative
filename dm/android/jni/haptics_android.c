// Game rumble over the Android Vibrator (the haptics_apple.m counterpart): the host's hle_haptics
// pipeline (epochs, batching, per-player state) is finished work - this sink only adapts its snapshots.
// The Switch's two motors blend into one intensity / sharpness pair (dm_haptics_level, the same
// approximation Core Haptics gets on the iPhone), the app scales it with the launcher's
// "Rumble strength" and hands it to the phone's Vibrator or a connected controller's.
// Touch feedback (one short tick per virtual-key press edge) is separate: the app triggers it directly.
#include "dm.h"
#include "hle_haptics.h"
#include <pthread.h>
#include <string.h>

void dm_jni_haptic(int player, int on, float amp, float sharp);   // dm_jni.c → DmFront.haptic

static int g_input_on = 1;   // the launcher's "Touch feedback" switch (the app reads the same setting for its own ticks)
void dm_haptics_android_input(int on) { g_input_on = on; }
int dm_haptics_android_input_on(void) { return g_input_on; }

// The sink (hle_haptics.h): one ordered snapshot per state change, never called reentrantly, never
// on the sending thread's hot path. Rumble gain is applied by the app (the launcher's strength slider).
// Named for dm_jni.c's nativeBoot, which installs it (dm_haptics_init(dm_haptics_android_sink, 1)).
void dm_haptics_android_sink(dm_haptic_state st) {
    for (int m = 0; m < 2; m++) {
        dm_haptic_level lv = dm_haptics_level(&st.motor[m], 1, 1.f);
        float amp = lv.intensity, sharp = lv.sharpness;
        if (amp <= 0.001f) continue;
        dm_jni_haptic(st.player, 1, amp, sharp);
        return;   // the phone has one actuator: the first motor with output stands in for the pair
    }
    dm_jni_haptic(st.player, 0, 0.f, 0.f);   // silence for this player (the app cancels its effect)
}
