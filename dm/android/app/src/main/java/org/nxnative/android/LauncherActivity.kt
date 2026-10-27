// The launcher: the settings screen shown before the game starts (Launcher.swift's counterpart).
// Its settings come from the APK's dm/launcher.json (dm/games/settings.py; extracted with the game
// files by MainActivity); each sets an environment variable the host reads when the game starts.
// All settings share one scrollable screen, grouped into sections; the left column shows the game's
// icon, name and version and the Play button; "Memory available" reads the ActivityManager (the
// largeHeap class the app runs in decides it, the iOS Increased Memory Limit's counterpart).
package org.nxnative.android

import android.app.Activity
import android.graphics.BitmapFactory
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.*
import java.io.File

class LauncherActivity : Activity() {
    companion object {
        const val RESULT_PLAYED = 10
    }

    private var model: LauncherSettings? = null
    private lateinit var memoryRow: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val m = LauncherSettings.load(this)
        if (m == null) { setResult(RESULT_PLAYED); finish(); return }
        model = m
        setContentView(buildUi(m))
    }

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()

    private fun buildUi(m: LauncherSettings): View {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setBackgroundColor(0xFF0b0d12.toInt())
        }
        // Left column: the game's icon, name, version, Play
        val left = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            setPadding(dp(16), dp(24), dp(16), dp(16))
        }
        val iconFile = File(filesDir, "icon.png")
        if (iconFile.isFile) {
            val bmp = BitmapFactory.decodeFile(iconFile.path)
            if (bmp != null) left.addView(ImageView(this).apply {
                setImageBitmap(bmp)
                scaleType = ImageView.ScaleType.FIT_CENTER
                layoutParams = LinearLayout.LayoutParams(dp(96), dp(96)).apply { bottomMargin = dp(12) }
            })
        }
        val name = packageManager.getApplicationInfo(packageName, 0).loadLabel(packageManager).toString()
        left.addView(TextView(this).apply {
            text = name; textSize = 22f; gravity = Gravity.CENTER
            setTextColor(0xFFE8ECF4.toInt()); setTypeface(typeface, android.graphics.Typeface.BOLD)
        })
        left.addView(TextView(this).apply {
            text = getString(R.string.app_name).let { "Version " + packageManager.getPackageInfo(packageName, 0).versionName }
            textSize = 14f; gravity = Gravity.CENTER; setTextColor(0xFF8A93A6.toInt())
            layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { bottomMargin = dp(20) }
        })
        left.addView(Button(this).apply {
            setText(R.string.play)
            textSize = 18f
            transformationMethod = null   // "Play", not "PLAY"
            setOnClickListener { play() }
            layoutParams = LinearLayout.LayoutParams(dp(220), ViewGroup.LayoutParams.WRAP_CONTENT)
        })
        memoryRow = TextView(this).apply {
            setTextColor(0xFF8A93A6.toInt()); textSize = 13f
            layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { topMargin = dp(24) }
        }
        left.addView(memoryRow)
        root.addView(left, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.MATCH_PARENT, 1f).apply { weight = 1f })

        // Right column: the settings, grouped by section
        val scroll = ScrollView(this)
        val list = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(8), dp(8), dp(16), dp(8)) }
        for (section in m.sections) {
            list.addView(TextView(this).apply {
                text = section; textSize = 13f; setTextColor(0xFF8A93A6.toInt())
                layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { topMargin = dp(12); bottomMargin = dp(4); leftMargin = dp(8) }
            })
            for (s in m.settings.filter { it.sectionTitle == section }) list.addView(row(m, s))
        }
        // Reset to tested defaults
        list.addView(Button(this).apply {
            setText(R.string.reset_defaults)
            transformationMethod = null
            setOnClickListener {
                m.reset()
                recreate()   // every row rebuilds from the model
            }
            layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { topMargin = dp(16) }
        })
        list.addView(TextView(this).apply {
            setText(R.string.reset_footer); textSize = 12f; setTextColor(0xFF6A7386.toInt())
            layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { leftMargin = dp(8) }
        })
        scroll.addView(list)
        root.addView(scroll, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.MATCH_PARENT, 2f))
        return root
    }

    private fun row(m: LauncherSettings, s: Setting): View {
        val locked = s.env in m.envFileKeys
        val pad = dp(14)
        return LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad / 2, pad, pad / 2)
            when (s.kind) {
                "choice" -> {
                    addView(TextView(this@LauncherActivity).apply { text = s.label; setTextColor(0xFFE8ECF4.toInt()); textSize = 15f })
                    if (locked) lockedNote(s) else addView(Spinner(this@LauncherActivity).apply {
                        adapter = ArrayAdapter<String>(this@LauncherActivity, android.R.layout.simple_spinner_dropdown_item, s.options!!.map { it.second })
                        setSelection(s.options.indexOfFirst { it.first == (m.values[s.env] ?: s.default) }.coerceAtLeast(0))
                        onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
                            override fun onItemSelected(p: AdapterView<*>?, v: View?, pos: Int, id: Long) { m.values[s.env] = s.options[pos].first }
                            override fun onNothingSelected(p: AdapterView<*>?) {}
                        }
                    })
                }
                "range" -> {
                    val header = LinearLayout(this@LauncherActivity).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
                    header.addView(TextView(this@LauncherActivity).apply { text = s.label; setTextColor(0xFFE8ECF4.toInt()); textSize = 15f; layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f) })
                    val pct = TextView(this@LauncherActivity).apply { setTextColor(0xFF8A93A6.toInt()) }
                    header.addView(pct)
                    addView(header)
                    if (locked) lockedNote(s) else addView(SeekBar(this@LauncherActivity).apply {
                        val lo = ((s.min ?: 0.0) * 100).toInt(); val hi = ((s.max ?: 1.0) * 100).toInt()
                        val st = ((s.step ?: 0.05) * 100).coerceAtLeast(1.0).toInt()
                        max = (hi - lo) / st
                        progress = (((m.values[s.env] ?: s.default).toDoubleOrNull() ?: s.min ?: 0.0) * 100 - lo).toInt() / st
                        setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
                            override fun onProgressChanged(seek: SeekBar?, v: Int, fromUser: Boolean) {
                                val valx = (lo + v * st) / 100.0
                                m.values[s.env] = String.format("%.2f", valx)
                                pct.text = "${(valx * 100).toInt().toDouble().roundToInt()} %"
                            }
                            override fun onStartTrackingTouch(seek: SeekBar?) {}
                            override fun onStopTrackingTouch(seek: SeekBar?) {}
                        })
                        pct.text = "${((lo + progress * st) / 100.0 * 100).toInt()} %"
                    })
                }
                else -> {   // toggle
                    addView(TextView(this@LauncherActivity).apply { text = s.label; setTextColor(0xFFE8ECF4.toInt()); textSize = 15f })
                    if (locked) lockedNote(s) else addView(Switch(this@LauncherActivity).apply {
                        isChecked = (m.values[s.env] ?: s.default) == s.onValue
                        setOnCheckedChangeListener { _, c -> m.values[s.env] = if (c) s.onValue else s.offValue }
                    })
                }
            }
        }
    }

    private fun LinearLayout.lockedNote(s: Setting) {
        if (s.note.isNotEmpty()) addView(TextView(this@LauncherActivity).apply { text = s.note; textSize = 12f; setTextColor(0xFF8A93A6.toInt()) })
        addView(TextView(this@LauncherActivity).apply { text = "Set by an env file"; textSize = 12f; setTextColor(0xFF6A7386.toInt()) })
    }

    private fun Double.roundToInt(): Int = Math.round(this).toInt()

    private fun play() {
        model?.apply()
        DmFront.rumbleGain = (model?.values?.get("DM_RUMBLE_GAIN")?.toFloatOrNull() ?: 1f).coerceIn(0f, 2f)
        setResult(RESULT_PLAYED)
        finish()
    }

    override fun onResume() {
        super.onResume()
        memoryUpdate()
        memoryHandler.sendEmptyMessage(0)
    }

    override fun onPause() {
        super.onPause()
        memoryHandler.removeMessages(0)
    }

    private val memoryHandler = object : android.os.Handler(mainLooper) {
        override fun handleMessage(msg: android.os.Message) {
            memoryUpdate()
            sendEmptyMessageDelayed(0, 2000)
        }
    }

    private fun memoryUpdate() {   // the ActivityManager's memory class, as os_proc_available_memory's counterpart
        val mi = android.app.ActivityManager.MemoryInfo()
        (getSystemService(ACTIVITY_SERVICE) as android.app.ActivityManager).getMemoryInfo(mi)
        memoryRow.text = "${getString(R.string.memory_available)}: ${human(mi.availMem)}"
    }

    private fun human(v: Long): String = when {
        v >= 1L shl 30 -> "%.1f GB".format(v / 1073741824.0)
        v >= 1L shl 20 -> "%.0f MB".format(v / 1048576.0)
        else -> "%.0f KB".format(v / 1024.0)
    }
}
