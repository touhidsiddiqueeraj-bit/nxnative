// The receiver screen: what the app shows while it holds no game (the template build). The PC
// companion raises it over USB the moment the phone is plugged in (`adb shell am start`), and
// ReceiverLink feeds it the computer's build - the stages, the shader counts, the install - so the
// player watches the game arrive on the phone they will play it on. Nothing here runs anything: the
// computer installs, the player taps the game's icon on the home screen (the receiver never launches
// a game itself).

package org.nxnative.android

import android.app.Activity
import android.graphics.Color
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.ScrollView
import android.widget.TextView

class ReceiverActivity : Activity() {
    private var status: TextView? = null
    private var detail: TextView? = null
    private var bar: ProgressBar? = null
    private var steps: TextView? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val pad = (18 * resources.displayMetrics.density).toInt()

        val column = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL; gravity = Gravity.CENTER_HORIZONTAL
            setBackgroundColor(Color.BLACK)
            setPadding(pad, pad * 2, pad, pad * 2)
        }

        column.addView(TextView(this).apply {
            text = getString(R.string.app_name); textSize = 32f; setTextColor(Color.WHITE)
            gravity = Gravity.CENTER; letterSpacing = 0.06f
        })
        column.addView(TextView(this).apply {
            text = getString(R.string.receiver_tagline); textSize = 13f; setTextColor(GRAY)
            gravity = Gravity.CENTER; setPadding(0, 0, 0, pad * 2)
        })

        status = TextView(this).apply {
            text = getString(R.string.receiver_waiting); textSize = 19f; setTextColor(Color.WHITE)
            gravity = Gravity.CENTER; setPadding(0, 0, 0, pad)
        }
        column.addView(status)

        bar = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply {
            max = 100; progress = 0; visibility = View.GONE
            layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT).apply { bottomMargin = pad / 2 }
        }
        column.addView(bar)

        detail = TextView(this).apply {
            textSize = 13f; setTextColor(GRAY); gravity = Gravity.CENTER; minLines = 2
        }
        column.addView(detail)

        steps = TextView(this).apply {
            text = getString(R.string.receiver_usb_steps); textSize = 13f; setTextColor(DIM)
            setLineSpacing(0f, 1.35f); setPadding(pad, pad * 2, pad, 0); gravity = Gravity.START
        }
        column.addView(steps)

        setContentView(ScrollView(this).apply {
            isFillViewport = true
            setBackgroundColor(Color.BLACK)
            addView(column, ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT))
        })
        window.addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)   // the build takes minutes; the screen stays up

        ReceiverLink.start(this)
    }

    override fun onStart() {
        super.onStart()
        ReceiverLink.onState = { runOnUiThread { render(it) } }
        render(ReceiverLink.state)   // catch up with whatever arrived while the screen was away
    }

    override fun onStop() {
        super.onStop()
        ReceiverLink.onState = null
    }

    private fun render(st: ReceiverLink.State) {
        val s = status ?: return
        val d = detail ?: return
        val b = bar ?: return
        val g = steps ?: return
        when (st) {
            is ReceiverLink.State.Waiting -> {
                s.text = getText(R.string.receiver_waiting); s.setTextColor(Color.WHITE)
                d.text = ""; d.visibility = View.GONE
                b.visibility = View.GONE
                g.visibility = View.VISIBLE
            }
            is ReceiverLink.State.Connected -> {
                s.text = getText(R.string.receiver_connected); s.setTextColor(GREEN)
                d.text = getText(R.string.receiver_connected_hint); d.visibility = View.VISIBLE
                b.visibility = View.GONE
                g.visibility = View.VISIBLE
            }
            is ReceiverLink.State.Building -> {
                s.text = st.stage; s.setTextColor(Color.WHITE)
                d.text = st.detail; d.visibility = if (st.detail.isEmpty()) View.GONE else View.VISIBLE
                b.visibility = View.VISIBLE
                b.isIndeterminate = st.pct == null
                if (st.pct != null) { b.isIndeterminate = false; b.progress = st.pct.coerceIn(0.0, 100.0).toInt() }
                g.visibility = View.GONE
            }
            is ReceiverLink.State.Installing -> {
                s.text = getString(R.string.receiver_installing, st.game); s.setTextColor(Color.WHITE)
                d.text = ""; d.visibility = View.GONE
                b.visibility = View.VISIBLE; b.isIndeterminate = true
                g.visibility = View.GONE
            }
            is ReceiverLink.State.Done -> {
                s.text = getString(R.string.receiver_installed, st.game); s.setTextColor(GREEN)
                d.text = ""; d.visibility = View.GONE
                b.visibility = View.GONE
                g.visibility = View.GONE
            }
            is ReceiverLink.State.Failed -> {
                s.text = getText(R.string.receiver_failed); s.setTextColor(RED)
                d.text = st.message; d.visibility = if (st.message.isEmpty()) View.GONE else View.VISIBLE
                b.visibility = View.GONE
                g.visibility = View.VISIBLE
            }
        }
    }

    companion object {
        private val GRAY = 0xFF9E9E9E.toInt()
        private val DIM = 0xFF8A8A8A.toInt()
        private val GREEN = 0xFF66BB6A.toInt()
        private val RED = 0xFFEF5350.toInt()
    }
}
