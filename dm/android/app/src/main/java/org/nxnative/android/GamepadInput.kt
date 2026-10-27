// Physical controllers over Android's input stack (DmGamepad.m's GameController counterpart):
// key and motion events from InputDevice sources with a gamepad class map to the same bits the iOS
// build maps them to (by position: right = A, bottom = B, top = X, left = Y), sticks at ±32767,
// both thumbstick buttons, the D-pad and the menu / options buttons. A connected controller hides
// the virtual gamepad and keeps every button (D-pad and right stick included); players beyond the
// first are reported through dm_pad_set_player when the game asks for them (the host's npad logic,
// dm_input.h). Rumble for a connected controller goes through the haptics sink's per-player state.
package org.nxnative.android

import android.content.Context
import android.hardware.input.InputManager
import android.view.InputDevice
import android.view.KeyEvent
import android.view.MotionEvent

class GamepadInput(private val activity: MainActivity) : InputManager.InputDeviceListener {
    private val lock = Object()
    private var buttons = 0L
    private var lx = 0; private var ly = 0; private var rx = 0; private var ry = 0
    private var shakeOn = false
    private var devices = HashSet<Int>()

    companion object {
        // By position, as the iOS build maps GameController (right A, bottom B, top X, left Y)
        private val KEYMAP = mapOf(
            KeyEvent.KEYCODE_BUTTON_A to Btn.B, KeyEvent.KEYCODE_BUTTON_B to Btn.A,
            KeyEvent.KEYCODE_BUTTON_X to Btn.Y, KeyEvent.KEYCODE_BUTTON_Y to Btn.X,
            KeyEvent.KEYCODE_BUTTON_L1 to Btn.L, KeyEvent.KEYCODE_BUTTON_R1 to Btn.R,
            KeyEvent.KEYCODE_BUTTON_L2 to Btn.ZL, KeyEvent.KEYCODE_BUTTON_R2 to Btn.ZR,
            KeyEvent.KEYCODE_BUTTON_START to Btn.PLUS, KeyEvent.KEYCODE_BUTTON_SELECT to Btn.MINUS,
            KeyEvent.KEYCODE_BUTTON_THUMBL to Btn.LS, KeyEvent.KEYCODE_BUTTON_THUMBR to Btn.RS,
            KeyEvent.KEYCODE_DPAD_LEFT to Btn.LEFT, KeyEvent.KEYCODE_DPAD_UP to Btn.UP,
            KeyEvent.KEYCODE_DPAD_RIGHT to Btn.RIGHT, KeyEvent.KEYCODE_DPAD_DOWN to Btn.DOWN,
            KeyEvent.KEYCODE_BUTTON_MODE to Btn.MINUS,   // the home / mode button some pads have
        )
        private fun isGamepad(id: Int): Boolean =
            (InputDevice.getDevice(id)?.sources ?: 0) and (InputDevice.SOURCE_GAMEPAD or InputDevice.SOURCE_JOYSTICK) != 0
    }

    fun start() {
        val im = activity.getSystemService(Context.INPUT_SERVICE) as InputManager
        im.registerInputDeviceListener(this, null)
        refresh()
    }

    fun stop() {
        try {
            val im = activity.getSystemService(Context.INPUT_SERVICE) as InputManager
            im.unregisterInputDeviceListener(this)
        } catch (e: Exception) { }
    }

    private fun refresh() {
        synchronized(lock) { devices = InputDevice.getDeviceIds().filter { isGamepad(it) }.toHashSet() }
        activity.runOnUiThread { activity.padVisibility(devices.isNotEmpty()) }
        HostJni.nativePadPlayers(if (devices.isNotEmpty()) 0b11 else 0b01)   // player 1 always connected; a pad is player 2 when the host supports it
    }

    // ---- InputManager.InputDeviceListener: hotplug ----
    override fun onInputDeviceAdded(id: Int) { if (isGamepad(id)) refresh() }
    override fun onInputDeviceRemoved(id: Int) { if (isGamepad(id)) refresh() }
    override fun onInputDeviceChanged(id: Int) { if (isGamepad(id)) refresh() }

    // ---- Key events ----
    fun onKey(keyCode: Int, event: KeyEvent?, down: Boolean): Boolean {
        if (event == null || !isGamepad(event.deviceId)) return false
        val bit = KEYMAP[keyCode] ?: return false
        synchronized(lock) {
            buttons = if (down) buttons or bit else buttons and bit.inv()
        }
        publish()
        return true
    }

    // ---- Sticks and triggers (analog motion) ----
    fun onMotion(e: MotionEvent?): Boolean {
        if (e == null || e.action != MotionEvent.ACTION_MOVE) return false
        if (!isGamepad(e.deviceId)) return false
        fun axis(a: Int): Float = e.getAxisValue(a).coerceIn(-1f, 1f)
        synchronized(lock) {
            lx = (axis(MotionEvent.AXIS_X) * Btn.STICK_MAX).toInt()
            ly = (axis(MotionEvent.AXIS_Y) * Btn.STICK_MAX).toInt()
            rx = (axis(MotionEvent.AXIS_Z) * Btn.STICK_MAX).toInt()
            ry = (axis(MotionEvent.AXIS_RZ) * Btn.STICK_MAX).toInt()
        }
        publish()
        return true
    }

    /** The phone was shaken (ShakeSensor): the DM_BTN_SHAKE bit rides this path, where it counts. */
    fun setShake(on: Boolean) {
        synchronized(lock) { shakeOn = on }
        publish()
    }

    private fun publish() {
        synchronized(lock) {
            HostJni.nativePad(buttons or (if (shakeOn) Btn.SHAKE else 0L), lx, ly, rx, ry)
        }
    }
}
