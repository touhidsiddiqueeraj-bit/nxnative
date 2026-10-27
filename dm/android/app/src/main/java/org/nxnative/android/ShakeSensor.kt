// Shaking the phone shakes the controller (DmGamepad.m's CoreMotion counterpart): the accelerometer
// at game speed, gravity followed by a slow low-pass, a sample whose acceleration beyond it exceeds
// DM_SHAKE_G (1.5 G by default) is a hit, and the controller counts as shaken until 120 ms after the
// last hit - a flick is one shake, shaking on keeps it going (the host finishes it as a whole cycle;
// the DM_BTN_SHAKE bit goes through the physical-controller path, where it counts).
package org.nxnative.android

import android.content.Context
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import kotlin.math.sqrt

class ShakeSensor(context: Context, private val onShake: (Boolean) -> Unit) : SensorEventListener {
    private val sm = context.getSystemService(Context.SENSOR_SERVICE) as SensorManager
    private var gx = 0.0; private var gy = 0.0; private var gz = 0.0
    private var init = false
    private var last = -1.0; private var peak = 0.0
    private var on = false
    private val threshold = (System.getenv("DM_SHAKE_G")?.toDoubleOrNull() ?: 0.0).let { if (it > 0) it else 1.5 }

    fun start() {
        val acc = sm.getDefaultSensor(Sensor.TYPE_ACCELEROMETER) ?: run {
            android.util.Log.i("nxnative", "front end: no accelerometer, shaking the phone does nothing")
            return
        }
        sm.registerListener(this, acc, SensorManager.SENSOR_DELAY_GAME)
        android.util.Log.i("nxnative", "front end: shaking the phone shakes the controller (threshold %.2f G)".format(threshold))
    }

    fun stop() { sm.unregisterListener(this) }

    override fun onSensorChanged(e: SensorEvent) {
        val (x, y, z) = Triple(e.values[0].toDouble(), e.values[1].toDouble(), e.values[2].toDouble())
        if (!init) { gx = x; gy = y; gz = z; init = true }
        val g = sqrt((x - gx) * (x - gx) + (y - gy) * (y - gy) + (z - gz) * (z - gz))
        gx += (x - gx) * 0.02; gy += (y - gy) * 0.02; gz += (z - gz) * 0.02   // about 0.8 s at 60 Hz
        val t = e.timestamp / 1e9
        if (g > threshold) { if (last < 0) peak = 0.0; last = t; if (g > peak) peak = g }
        val nowOn = last >= 0 && t - last < 0.12
        if (!nowOn && last >= 0) {
            android.util.Log.i("nxnative", "front end: phone shaken (peak %.1f G)".format(peak))
            last = -1.0
        }
        if (nowOn != on) {
            on = nowOn
            onShake(on)   // the DM_BTN_SHAKE bit rides the physical-controller path (dm_input.h); the
                          // merge with the touch gamepad's state happens in the host
        }
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) { }
}
