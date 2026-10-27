// The prepare-step progress screen (front_ios.m's front_progress): a dark layer over the game and
// the pad, the title, a bar and a count, updated from the host's prepare thread through DmFront.
package org.nxnative.android

import android.app.Activity
import android.content.Context
import android.graphics.Color
import android.view.Gravity
import android.view.ViewGroup
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.TextView

class ProgressOverlay(context: Context) : FrameLayout(context) {
    interface Host { fun dmProgress(title: String?, done: Long, total: Long) }

    private val title: TextView
    private val bar: ProgressBar
    private val count: TextView

    init {
        setBackgroundColor(Color.argb(240, 8, 10, 14))
        visibility = GONE
        val box = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            setPadding(48, 24, 48, 24)
            setBackgroundColor(Color.argb(255, 20, 24, 32))
        }
        title = TextView(context).apply { setTextColor(Color.WHITE); textSize = 20f; gravity = Gravity.CENTER }
        bar = ProgressBar(context, null, android.R.attr.progressBarStyleHorizontal).apply {
            max = 1000; progress = 0
            layoutParams = LayoutParams(560, 16).apply { topMargin = 24 }
        }
        count = TextView(context).apply { setTextColor(Color.argb(255, 160, 170, 190)); textSize = 13f; gravity = Gravity.CENTER }
        box.addView(title)
        box.addView(bar)
        box.addView(count)
        val wrap = LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.CENTER)
        addView(box, wrap)
    }

    fun show(text: String?, done: Long, total: Long) {
        post {
            if (text == null) { visibility = GONE; return@post }
            visibility = VISIBLE
            title.text = text
            bar.max = if (total > 0) 1000 else 1
            bar.progress = if (total > 0) (1000 * done / total).toInt() else 0
            count.text = "%,d of %,d".format(done, total)
        }
    }
}
