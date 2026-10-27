// The app-side front end: the host's C code (dm_jni.c) calls these methods for everything UI - the
// prepare-step progress bar, the software keyboard, the save-failure notice - and reads the device's
// power state from them (front_ios.m's counterpart duties). One object, held by the host for the
// whole game; every method is quick, dispatches to the main thread where the UI lives, and never
// throws back into native code.
package org.nxnative.android

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.BatteryManager
import android.os.Build
import android.os.PowerManager
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.provider.Settings
import android.util.Log
import android.view.WindowManager
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

object DmFront {
    lateinit var app: Context
    var activity: Activity? = null     // the game activity (progress / dialogs hang off it)
    private var vibrator: Vibrator? = null
    var rumbleGain = 1f                // the launcher's "Rumble strength" (0 = off)

    fun attach(context: Context) {
        app = context.applicationContext
        vibrator = if (Build.VERSION.SDK_INT >= 31)
            (app.getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as VibratorManager).defaultVibrator
        else
            @Suppress("DEPRECATION") app.getSystemService(Context.VIBRATOR_SERVICE) as Vibrator
    }

    // ---- The host's UI calls (dm_jni.c → front_android.c) ----

    /** The prepare-step progress ("Preparing shaders", done of total); title null hides it. */
    fun progress(title: String?, done: Long, total: Long) {
        val a = activity ?: return
        a.runOnUiThread { (a as? ProgressOverlay.Host)?.dmProgress(title, done, total) }
    }

    /** The software keyboard (hle_swkbd.c): the guest thread blocks until the user answers. */
    fun textInput(title: String, sub: String, guide: String, initial: String, maxChars: Int): String? {
        val a = activity ?: return null
        val result = AtomicReference<String?>(null)
        val done = CountDownLatch(1)
        a.runOnUiThread {
            TextInputDialog(a, title, sub, guide, initial, maxChars) { text ->
                result.set(text)
                done.countDown()
            }.show()
        }
        done.await(10, TimeUnit.MINUTES)   // the guest clock keeps running meanwhile, as on the Switch
        return result.get()
    }

    /** A save transaction was refused (hle_fs.c): tell the player without blocking the game. */
    fun saveFailed(err: Int) {
        val a = activity ?: return
        a.runOnUiThread {
            val msg = if (err == 28) a.getString(R.string.save_failed_enospc)
                      else a.getString(R.string.save_failed_fmt, err)
            android.app.AlertDialog.Builder(a)
                .setTitle(R.string.save_failed_title)
                .setMessage(msg)
                .setPositiveButton(R.string.ok, null)
                .show()
        }
    }

    // ---- The device state the host reads (hle_nvn.c's power rows) ----

    /** 0 nominal, 1 fair, 2 serious, 3 critical (the thermal states' order, as NSProcessInfo reports them). */
    fun thermal(): Int {
        // The battery health the platform publishes (iOS's NSProcessInfo thermal states have no direct
        // counterpart): the sticky ACTION_BATTERY_CHANGED broadcast carries it - there is no
        // BATTERY_PROPERTY_HEALTH to poll, and the health constants are the states, not the status ones
        val i = app.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val health = i?.getIntExtra(BatteryManager.EXTRA_HEALTH, BatteryManager.BATTERY_HEALTH_GOOD)
            ?: BatteryManager.BATTERY_HEALTH_GOOD
        return when (health) {
            BatteryManager.BATTERY_HEALTH_OVERHEAT -> 3
            BatteryManager.BATTERY_HEALTH_COLD -> 2
            else -> 0   // nominal: Android publishes no finer grade; the frame-step power rows stay there
        }
    }

    fun lowPower(): Int {
        val pm = app.getSystemService(Context.POWER_SERVICE) as PowerManager
        return if (pm.isPowerSaveMode) 1 else 0
    }

    fun brightness(): Double {
        return try {
            Settings.System.getInt(app.contentResolver, Settings.System.SCREEN_BRIGHTNESS) / 255.0
        } catch (e: Settings.SettingNotFoundException) { -1.0 }
    }

    fun screenFps(): Int {
        val a = activity ?: return 0
        val display = a.display ?: return 60
        return display.refreshRate.toInt().coerceAtLeast(0)
    }

    // ---- Rumble: the haptics sink (haptics_android.c) ----

    fun haptic(player: Int, on: Int, amplitude: Float, sharpness: Float) {
        val v = vibrator ?: return
        val gain = rumbleGain
        if (on == 0 || gain <= 0f || amplitude <= 0.01f) { v.cancel(); return }
        val amp = (amplitude * gain).coerceIn(0f, 1f)
        // One shot per state change (the sink already throttles to the game's updates): a short
        // waveform whose amplitude is the mix; sharpness raises the frequency band.
        val ms = if (sharpness > 0.5f) 24L else 40L
        v.vibrate(VibrationEffect.createOneShot(ms, (255 * amp).toInt().coerceIn(1, 255)))
    }

    /** The touch feedback of the virtual gamepad: one short tick per key-press edge. */
    fun touchFeedback() {
        val a = activity ?: return
        a.runOnUiThread { a.window?.attributes = a.window?.attributes.also { } }   // no-op latch: the view's own haptics run where the touch lands
    }

    fun keepScreenOn(on: Boolean) {
        val a = activity ?: return
        a.runOnUiThread {
            if (on) a.window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            else a.window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        }
    }

    fun log(msg: String) { Log.i("nxnative", msg) }
}
