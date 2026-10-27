// Android front end (the Android implementation of front.h, the counterpart of front_mac.m / front_ios.m):
// the game's window is a SurfaceView whose ANativeWindow becomes the Vulkan swapchain (nvn_vk.c), the progress
// screen, the software keyboard and the save-failure notice are the app's Kotlin side reached over JNI
// (dm_jni.c: DmFront callbacks), and power state comes from the framework through the same bridge.
// The host runs on the big-stack thread dm_jni.c started; front_run runs the guest directly on the calling thread.
#include "front.h"
#include "dm.h"
#include "hle.h"
#include <pthread.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

// The JNI bridge (dm_jni.c) owns the window handle and the Kotlin callback object; this front end asks it
// to show things. Keeping the JNIEnv out of here means the host never links JNI headers directly.
void dm_front_set_window(void *native_window);            // dm_jni.c: the SurfaceView's ANativeWindow
void *dm_front_window(void);                              // nvn_vk.c reads it (the swapchain)
void dm_front_progress(const char *title, long done, long total);   // → ProgressOverlay (Kotlin)
int  dm_front_text_input(const char *title, const char *sub, const char *guide, const char *initial, int max_chars, char *out, int out_cap);   // → the dialog, blocking
void dm_front_save_failed(int err);                       // → the notice
void dm_front_focus(int ev);                              // app lifecycle → the host (focus / audio / gfx)
int  dm_front_thermal(void);                              // BatteryManager thermal status
int  dm_front_low_power(void);                             // PowerManager.isPowerSaveMode
double dm_front_brightness(void);                          // Settings.System screen brightness [0,1], -1 unknown
int  dm_front_screen_max_fps(void);                        // the display's refresh rate (Display.Mode)

void front_run(void (*guest)(void)) { guest(); }   // Already on dm_jni.c's big-stack thread

// Progress screen of the first-start prepare step (front.h front_progress): title NULL hides it.
// Routed to the app's overlay; nothing is shown without a window.
void front_progress(const char *title, long done, long total) { dm_front_progress(title, done, total); }

// Text input box (hle_swkbd.c): blocks the calling guest thread until the user confirms / cancels.
int front_text_input(const char *title, const char *sub, const char *guide, const char *initial, int max_chars, char *out, int out_cap) {
    return dm_front_text_input(title, sub, guide, initial, max_chars, out, out_cap);
}

// Save not written (hle_fs.c): tell the player without blocking the game thread.
void front_save_failed(int err) { dm_front_save_failed(err); }

// The highest refresh rate of the display in Hz (hle_nvn.c link pacing; 0 = not known)
int front_screen_max_fps(void) { return dm_front_screen_max_fps(); }
// A Vulkan swapchain picks its rate itself; nothing to switch (the Mac's mode change has no Android counterpart)
void front_screen_match(int fps) { (void)fps; }

// Power rows every 300 frames (hle_nvn.c reads these; the weak defaults return -1 on the Mac)
int dm_thermal_now(void) { return dm_front_thermal(); }
int dm_low_power_now(void) { return dm_front_low_power(); }
double dm_brightness_now(void) { return dm_front_brightness(); }
