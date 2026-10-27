// The virtual gamepad (DmGamepad.m's counterpart): Nintendo button positions, the same three
// layouts (2D simple, 3D Pro-Controller style, Tap), the same touch ownership rules and the same
// unit geometry (the layout constants of the iOS file, in u = min(height / 430, 1.3) units).
//   2D: ZL above L (teardrops toward the screen corner), ZR above R, − / + pills at the top centre,
//       the left stick bottom-left (fixed ring, dead zone), the A-B-X-Y diamond bottom-right with
//       finger-slide key switching.
//   3D: everything 2D plus the D-pad (inward of and below the left stick), an invisible right stick
//       (a touch landing on the right third of the screen holds it: centred where it lands, dragged
//       along past the stick radius) and L3 / R3 in the top row.
//   Tap: no control but the layout button; every other touch goes to the game's touch screen.
// Touches on no control go to the game (the same pushGame contract: the whole set, on every change).
// The layout button cycles, the choice is kept in preferences; the game profile's default comes from
// the packed meta (dm/meta.json's pad_layout, the DM_GAME_PAD_LAYOUT the iOS build compiles in).
// A connected physical controller hides the pad; every touch then goes to the game.
package org.nxnative.android

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.*
import android.text.TextPaint
import android.util.AttributeSet
import android.view.MotionEvent
import android.view.View
import androidx.core.graphics.component1
import androidx.core.graphics.component2
import org.json.JSONObject
import java.io.File
import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.hypot
import kotlin.math.min
import kotlin.math.sin

// Button bits (dm_input.h, nn::hid::NpadButton's order)
object Btn {
    const val A = 1L shl 0; const val B = 1L shl 1; const val X = 1L shl 2; const val Y = 1L shl 3
    const val LS = 1L shl 4; const val RS = 1L shl 5; const val L = 1L shl 6; const val R = 1L shl 7
    const val ZL = 1L shl 8; const val ZR = 1L shl 9; const val PLUS = 1L shl 10; const val MINUS = 1L shl 11
    const val LEFT = 1L shl 12; const val UP = 1L shl 13; const val RIGHT = 1L shl 14; const val DOWN = 1L shl 15
    const val SHAKE = 1L shl 63
    const val STICK_MAX = 32767
}

class DmGamepadView(context: Context, attrs: AttributeSet? = null) : View(context, attrs) {
    // Layout constants in u (x from the outer edge, the right half mirrors) - DmGamepad.m's table
    private val K_ZL_X = 44f; private val K_ZL_Y = 40f; private val K_ZL_R = 26f
    private val K_L_Y = 108f; private val K_L_R = 24f
    private val K_DROP_TIP = 1.45f
    private val K_FACE_X = 81f; private val K_PAD_Y = 82f
    private val K_FACE_GAP = 50f; private val K_FACE_R = 27f
    private val K_PILL_DX = 52f; private val K_PILL_Y = 22f; private val K_PILL_W = 50f; private val K_PILL_H = 28f
    private val K_SWAP_R = 15f
    private val K_STICK_X = 81f; private val K_STICK_R = 77f; private val K_KNOB_R = 30f
    private val K_STICK_GRAB = 1.4f
    private val K_FULL = 0.8f; private val K_DEAD = 0.1f
    private val K_LOW_X = 231f; private val K_LOW_Y = 64f
    private val K_DPAD_R = 48f; private val K_ARM_W = 17f; private val K_ARM_IN = 11f
    private val K_DPAD_GRAB = 1.3f
    private val K_RSTICK_R = 60f
    private val K_RSWIPE = 1f / 3
    private val K_CLICK_X = 102f; private val K_CLICK_R = 17f

    private val u get() = min(height / 430f, 1.3f)
    private var layoutMode = 0        // 0 = 2D, 1 = 3D, 2 = Tap
    var touchFeedback = true

    private val IDLE = Color.argb(178, 40, 44, 54)
    private val ON = Color.argb(224, 90, 170, 255)
    private val LINE = Color.argb(120, 180, 190, 210)
    private val TEXT = Color.argb(220, 235, 239, 247)

    // Touch state
    private class TouchState {
        var owner = C_NONE; var key = 0L; var stick = -1
        var faceBits = 0L; var dpadBits = 0L
        var x = 0f; var y = 0f
    }
    private val touches = HashMap<Int, TouchState>()
    private val gameTouches = HashMap<Int, Pair<Float, Float>>()   // finger slot → normalized point
    private var rStickCenter = Pair(0f, 0f)
    private var faceBits = 0L; private var dpadBits = 0L; private var btnBits = 0L
    private var swapHeld = false
    private var padHidden = false      // a physical controller is connected

    companion object {
        const val C_NONE = 0; const val C_STICK = 1; const val C_DPAD = 2; const val C_FACE = 3
        const val C_BTN = 4; const val C_SWAP = 5; const val C_GAME = 6
    }

    fun loadDefaultLayout() {
        val meta = File(context.filesDir, "meta.json")
        layoutMode = try { if (meta.isFile) JSONObject(meta.readText()).optInt("pad_layout", 0) else 0 } catch (e: Exception) { 0 }
        val saved = context.getSharedPreferences("dm", Context.MODE_PRIVATE).getInt("padLayout", -1)
        if (saved >= 0) layoutMode = saved
    }
    private fun saveLayout() = context.getSharedPreferences("dm", Context.MODE_PRIVATE).edit().putInt("padLayout", layoutMode).apply()

    fun setPadHidden(hidden: Boolean) {
        if (padHidden != hidden) { padHidden = hidden; releaseAll(); invalidate() }
    }

    private fun releaseAll() {
        touches.clear(); gameTouches.clear()
        faceBits = 0; dpadBits = 0; btnBits = 0; swapHeld = false
        publish()
    }

    /** On resign active: release everything (a key released in the background gets no "release"). */
    fun reset() = releaseAll()

    // ---- Geometry: centres in view coordinates ----
    private fun facePos(b: Long): PointF {
        val (cx, cy) = faceCenter()
        val ang = when (b) { Btn.A -> 0f; Btn.B -> Math.PI.toFloat() / 2; Btn.X -> -Math.PI.toFloat() / 2; else -> Math.PI.toFloat() }
        return PointF(cx + cos(ang) * K_FACE_GAP * u, cy + sin(ang) * K_FACE_GAP * u)
    }
    private fun faceCenter() = PointF(width - K_FACE_X * u, height - K_PAD_Y * u)
    private fun stickHome() = PointF(K_STICK_X * u, height - K_PAD_Y * u)
    private fun dpadCenter() = PointF(K_LOW_X * u, height - K_LOW_Y * u)
    private fun swapCenter() = PointF(width / 2f, K_PILL_Y * u + 2 * u)
    private fun isDrop(b: Long) = b == Btn.ZL || b == Btn.ZR || b == Btn.L || b == Btn.R
    private fun isClick(b: Long) = b == Btn.LS || b == Btn.RS
    private fun dropCenter(b: Long): PointF {
        val right = b == Btn.ZR || b == Btn.R
        val x = if (right) width - K_ZL_X * u else K_ZL_X * u
        return PointF(x, (if (b == Btn.ZL || b == Btn.ZR) K_ZL_Y else K_L_Y) * u)
    }
    private fun dropR(b: Long) = if (b == Btn.ZL || b == Btn.ZR) K_ZL_R else K_L_R
    private fun pillRect(b: Long): RectF {
        val left = width / 2f + (if (b == Btn.PLUS) 1 else -1) * K_PILL_DX * u - K_PILL_W * u / 2
        return RectF(left, K_PILL_Y * u - K_PILL_H * u / 2, left + K_PILL_W * u, K_PILL_Y * u + K_PILL_H * u / 2)
    }
    private fun clickCenter(b: Long): PointF {
        val right = b == Btn.RS
        return PointF(if (right) width - K_CLICK_X * u else K_CLICK_X * u, K_ZL_Y * u)
    }
    private val lockedBtns = longArrayOf(Btn.ZL, Btn.L, Btn.ZR, Btn.R, Btn.MINUS, Btn.PLUS, Btn.LS, Btn.RS)

    // ---- Drawing ----
    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val text = TextPaint(Paint.ANTI_ALIAS_FLAG).apply { color = TEXT; textAlign = Paint.Align.CENTER; typeface = Typeface.create(Typeface.DEFAULT, Typeface.BOLD) }

    override fun onDraw(canvas: Canvas) {
        if (padHidden || layoutMode == 2) { drawSwap(canvas); return }
        val uu = u
        val pressed = faceBits or dpadBits or btnBits
        // shoulders: teardrops (circle + tangent tip toward the screen corner)
        for (b in longArrayOf(Btn.ZL, Btn.L, Btn.ZR, Btn.R)) {
            val c = dropCenter(b)
            paint.color = if (pressed and b != 0L) ON else IDLE
            canvas.drawPath(dropPath(c, dropR(b) * uu, if (b == Btn.ZR || b == Btn.R) -Math.PI / 4 else -3 * Math.PI / 4), paint)
            label(canvas, c, if (b == Btn.ZL) "ZL" else if (b == Btn.L) "L" else if (b == Btn.ZR) "ZR" else "R", 16f)
        }
        // − / + pills
        for (b in longArrayOf(Btn.MINUS, Btn.PLUS)) {
            val r = pillRect(b)
            paint.color = if (pressed and b != 0L) ON else IDLE
            canvas.drawRoundRect(r, r.height() / 2, r.height() / 2, paint)
            label(canvas, PointF(r.centerX(), r.centerY()), if (b == Btn.MINUS) "−" else "+", 18f)
        }
        // L3 / R3 (3D only)
        if (layoutMode == 1) for (b in longArrayOf(Btn.LS, Btn.RS)) {
            val c = clickCenter(b)
            paint.color = if (pressed and b != 0L) ON else IDLE
            canvas.drawCircle(c.x, c.y, K_CLICK_R * uu, paint)
            label(canvas, c, if (b == Btn.LS) "L3" else "R3", 13f)
        }
        // the diamond
        for (b in longArrayOf(Btn.A, Btn.B, Btn.X, Btn.Y)) {
            val c = facePos(b)
            paint.color = if (faceBits and b != 0L) ON else IDLE
            canvas.drawCircle(c.x, c.y, K_FACE_R * uu, paint)
            label(canvas, c, when (b) { Btn.A -> "A"; Btn.B -> "B"; Btn.X -> "X"; else -> "Y" }, 20f)
        }
        // the left stick (ring + knob)
        val h = stickHome()
        paint.style = Paint.Style.STROKE; paint.strokeWidth = 2f * uu; paint.color = LINE
        canvas.drawCircle(h.x, h.y, K_STICK_R * uu, paint)
        paint.style = Paint.Style.FILL
        val off = stickOffset()
        paint.color = if (touches.values.any { it.stick == 0 }) ON else IDLE
        canvas.drawCircle(h.x + off.first, h.y + off.second, K_KNOB_R * uu, paint)
        // the D-pad (3D): four arms
        if (layoutMode == 1) {
            val dc = dpadCenter()
            for ((b, ang) in listOf(Btn.UP to 0f, Btn.RIGHT to Math.PI.toFloat() / 2, Btn.DOWN to Math.PI.toFloat(), Btn.LEFT to -Math.PI.toFloat() / 2)) {
                paint.color = if (dpadBits and b != 0L) ON else IDLE
                canvas.drawPath(armPath(dc, K_DPAD_R * uu, K_ARM_W * uu, K_ARM_IN * uu, ang), paint)
            }
        }
        drawSwap(canvas)
    }

    private fun drawSwap(canvas: Canvas) {
        val c = swapCenter()
        paint.color = if (swapHeld) ON else IDLE
        canvas.drawCircle(c.x, c.y, K_SWAP_R * u, paint)
        text.textSize = 12f * u
        canvas.drawText(if (layoutMode == 0) "2D" else if (layoutMode == 1) "3D" else "Tap", c.x, c.y + text.textSize / 3, text)
    }

    private fun label(canvas: Canvas, c: PointF, s: String, fs: Float) {
        text.textSize = fs * u
        canvas.drawText(s, c.x, c.y + text.textSize / 3, text)
    }

    private fun dropPath(c: PointF, r: Float, ang: Double): Path {
        val d = r * K_DROP_TIP
        val th = Math.acos((r / d).toDouble())
        val tip = PointF((c.x + d * cos(ang)).toFloat(), (c.y + d * sin(ang)).toFloat())
        val p1 = PointF((c.x + r * cos(ang + th)).toFloat(), (c.y + r * sin(ang + th)).toFloat())
        val p2 = PointF((c.x + r * cos(ang - th)).toFloat(), (c.y + r * sin(ang - th)).toFloat())
        return Path().apply {
            moveTo(p2.x, p2.y)
            lineTo(tip.x, tip.y)
            lineTo(p1.x, p1.y)
            arcTo(RectF(c.x - r, c.y - r, c.x + r, c.y + r), Math.toDegrees(ang + th).toFloat(), Math.toDegrees(2 * Math.PI - 2 * th).toFloat(), false)
            close()
        }
    }

    private fun armPath(c: PointF, R: Float, w: Float, inn: Float, ang: Float): Path {
        val v = arrayOf(PointF(-w, -R), PointF(w, -R), PointF(w, -(inn + w)), PointF(0f, -inn), PointF(-w, -(inn + w)))
        val cs = cos(ang); val sn = sin(ang)
        val r = v.map { PointF(c.x + it.x * cs - it.y * sn, c.y + it.x * sn + it.y * cs) }
        return Path().apply {
            moveTo(r[4].x, r[4].y)
            for (i in 0 until 5) lineTo(r[i].x, r[i].y)
            close()
        }
    }

    // ---- Touch handling ----
    private fun stickOffset(): Pair<Float, Float> {
        val t = touches.values.firstOrNull { it.stick == 0 } ?: return Pair(0f, 0f)
        val h = stickHome()
        val dx = t.x - h.x; val dy = t.y - h.y
        val rr = K_STICK_R * u
        val len = hypot(dx, dy)
        val cl = if (len > rr) rr / len else 1f
        return Pair(dx * cl, dy * cl)
    }

    private fun controlAt(x: Float, y: Float, touch: TouchState) {
        val uu = u
        // locked keys first (shoulders, − / +, the layout button, L3 / R3)
        for (b in lockedBtns) {
            if (layoutMode != 1 && (b == Btn.LS || b == Btn.RS)) continue
            if (isDrop(b)) { val c = dropCenter(b); if (hypot(x - c.x, y - c.y) <= dropR(b) * uu * 1.15f) { touch.owner = C_BTN; touch.key = b; return } }
            else if (isClick(b)) { val c = clickCenter(b); if (hypot(x - c.x, y - c.y) <= K_CLICK_R * uu * 1.3f) { touch.owner = C_BTN; touch.key = b; return } }
            else { val r = pillRect(b); if (r.contains(x, y)) { touch.owner = C_BTN; touch.key = b; return } }
        }
        val sc = swapCenter()
        if (hypot(x - sc.x, y - sc.y) <= K_SWAP_R * uu * 1.6f) { touch.owner = C_SWAP; return }
        if (layoutMode == 2) { touch.owner = C_GAME; return }
        // then whichever of the diamond, the D-pad and the left stick has the smallest distance / grab radius
        val h = stickHome()
        if (hypot(x - h.x, y - h.y) <= K_STICK_R * uu * K_STICK_GRAB) { touch.owner = C_STICK; touch.stick = 0; return }
        if (layoutMode == 1) {
            val d = dpadCenter()
            if (hypot(x - d.x, y - d.y) <= K_DPAD_R * uu * K_DPAD_GRAB) { touch.owner = C_DPAD; return }
            // the right stick: a touch in the right third on no control
            if (x > width * (1 - K_RSWIPE)) { touch.owner = C_STICK; touch.stick = 1; rStickCenter = Pair(x, y); return }
        }
        val fc = faceCenter()
        if (hypot(x - fc.x, y - fc.y) <= (K_FACE_GAP + K_FACE_R) * uu * 1.25f) { touch.owner = C_FACE; return }
        touch.owner = C_GAME
    }

    private fun faceKey(x: Float, y: Float): Long {
        var best = 0L; var bd = Float.MAX_VALUE
        for (b in longArrayOf(Btn.A, Btn.B, Btn.X, Btn.Y)) {
            val c = facePos(b)
            val d = hypot(x - c.x, y - c.y)
            if (d <= K_FACE_R * u * 1.3f && d < bd) { bd = d; best = b }
        }
        return best
    }

    private fun dpadBits(x: Float, y: Float): Long {
        val c = dpadCenter()
        val dx = x - c.x; val dy = y - c.y
        if (hypot(dx, dy) > K_DPAD_R * u * K_DPAD_GRAB) return 0
        val ang = atan2(dy, dx)
        val oct = Math.round(ang / (Math.PI / 4)).toInt() and 7
        return when (oct) {
            0 -> Btn.RIGHT; 1 -> Btn.RIGHT or Btn.DOWN; 2 -> Btn.DOWN; 3 -> Btn.DOWN or Btn.LEFT
            4 -> Btn.LEFT; 5 -> Btn.LEFT or Btn.UP; 6 -> Btn.UP; else -> Btn.UP or Btn.RIGHT
        }
    }

    private fun recompute() {
        faceBits = 0; dpadBits = 0; btnBits = 0
        for (t in touches.values) {
            when (t.owner) {
                C_BTN -> btnBits = btnBits or t.key
                C_FACE -> t.faceBits = if (t.key != 0L) t.key else 0L
                C_DPAD -> t.dpadBits = dpadBits(t.x, t.y)
            }
        }
        invalidate()
        publish()
    }

    private fun publish() {
        // Touches that hold no control go to the game's touch screen
        val pts = ArrayList<Float>()
        for ((slot, p) in gameTouches) { pts.add(slot.toFloat()); pts.add(p.first); pts.add(p.second) }
        HostJni.nativeTouch(pts.toFloatArray())
        // Sticks
        var lx = 0; var ly = 0; var rx = 0; var ry = 0
        for (t in touches.values) {
            if (t.owner != C_STICK || t.stick < 0) continue
            if (t.stick == 0) {
                val h = stickHome()
                lx = axis(t.x - h.x); ly = axis(t.y - h.y)
            } else {
                rx = axis(t.x - rStickCenter.first); ry = axis(t.y - rStickCenter.second)
            }
        }
        HostJni.nativeInput(faceBits or dpadBits or btnBits, lx, ly, rx, ry)
    }

    private fun axis(d: Float): Int {
        val rr = K_STICK_R * u
        val dead = K_DEAD * rr
        val full = K_FULL * rr
        val v = d.coerceIn(-rr, rr)
        if (abs(v) < dead) return 0
        val scaled = ((abs(v) - dead) / (full - dead)).coerceIn(0f, 1f) * (if (v < 0) -1 else 1)
        return (scaled * Btn.STICK_MAX).toInt()
    }

    private fun gameSlotsRebuild() {   // touches that hold no control: their set is pushed whole
        gameTouches.clear()
        for ((id, t) in touches) if (t.owner == C_GAME && t.key >= 0) gameTouches[id] = Pair(t.x / width, t.y / height)
    }

    @SuppressLint("ClickableViewAccessibility")
    override fun onTouchEvent(e: MotionEvent): Boolean {
        when (e.actionMasked) {
            MotionEvent.ACTION_DOWN, MotionEvent.ACTION_POINTER_DOWN -> {
                for (i in 0 until e.pointerCount) {
                    if (e.findPointerIndex(e.getPointerId(i)) < 0) continue
                    val idx = i
                    val id = e.getPointerId(idx)
                    if (touches.containsKey(id)) continue
                    val t = TouchState()
                    t.x = e.getX(idx); t.y = e.getY(idx)
                    controlAt(t.x, t.y, t)
                    if (padHidden) { t.owner = C_GAME; t.key = 0 }
                    if (t.owner == C_GAME) { val slot = freeSlot(); t.key = slot.toLong(); gameTouches[id] = Pair(t.x / width, t.y / height) }
                    if (t.owner == C_BTN && touchFeedback) performHapticFeedback(android.view.HapticFeedbackConstants.VIRTUAL_KEY)
                    touches[id] = t
                }
                gameSlotsRebuild(); recompute()
            }
            MotionEvent.ACTION_MOVE -> {
                for (i in 0 until e.pointerCount) {
                    val id = e.getPointerId(i)
                    val t = touches[id] ?: continue
                    t.x = e.getX(i); t.y = e.getY(i)
                    if (t.owner == C_FACE) t.key = faceKey(t.x, t.y)
                }
                gameSlotsRebuild(); recompute()
            }
            MotionEvent.ACTION_UP, MotionEvent.ACTION_POINTER_UP -> {
                for (i in intArrayOf(e.actionIndex)) {
                    val id = e.getPointerId(i)
                    val t = touches.remove(id) ?: continue
                    if (t.owner == C_SWAP && hypot(t.x - swapCenter().x, t.y - swapCenter().y) < K_SWAP_R * u * 2) nextLayout()
                }
                gameSlotsRebuild(); recompute()
            }
            MotionEvent.ACTION_CANCEL -> { releaseAll() }
        }
        return true
    }

    private fun freeSlot(): Int {   // the lowest free finger slot 0–15 (hle_touch.c's contract)
        val used = touches.values.filter { it.owner == C_GAME }.map { it.key.toInt() }.toHashSet()
        for (s in 0 until 16) if (s !in used) return s
        return 15
    }

    private fun nextLayout() {
        // Tap only in handheld mode (the only mode in which the game reads the touch screen); out of
        // it the cycle skips it - dm_opmode comes from the host, so the packed meta's opmode stands in
        val handheld = try { JSONObject(File(context.filesDir, "meta.json").readText()).optInt("opmode", 0) == 0 } catch (e: Exception) { true }
        layoutMode = when (layoutMode) { 0 -> 1; 1 -> if (handheld) 2 else 0; else -> 0 }
        saveLayout()
        releaseAll()
    }
}
