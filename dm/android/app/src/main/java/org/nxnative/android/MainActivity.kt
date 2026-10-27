// The game activity (front_ios.m's DmRootVC counterpart): extracts the packed game files on first
// start, shows the launcher (LauncherActivity, the settings screen), then hosts the game surface
// (the host's Vulkan swapchain), the virtual gamepad over it, the frame-rate counter, the progress
// bar of the first-start prepare step, and the physical controllers. App lifecycle (focus, background)
// maps to the host's focus events; the screen stays on while the game runs.
package org.nxnative.android

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.graphics.Color
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.*
import android.widget.FrameLayout
import android.widget.TextView
import org.json.JSONObject
import java.io.File
import java.util.zip.ZipFile

class MainActivity : Activity(), ProgressOverlay.Host {
    private lateinit var root: FrameLayout
    private var gameView: SurfaceView? = null
    private var pad: DmGamepadView? = null
    private var fps: TextView? = null
    private var progress: ProgressOverlay? = null
    private var booted = false
    private var launcherDone = false
    private var controllers: GamepadInput? = null
    private var shake: ShakeSensor? = null

    companion object {
        const val LAUNCHER_REQUEST = 1
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        DmFront.attach(this)
        DmFront.activity = this

        root = FrameLayout(this)
        root.setBackgroundColor(Color.BLACK)
        setContentView(root)

        // The host library first: a failure here (a device the build does not fit) shows its reason
        // instead of crashing the process - the shell without it cannot run a game anyway
        try {
            HostJni.load()
        } catch (e: UnsatisfiedLinkError) {
            val hint = TextView(this).apply {
                text = getString(R.string.load_failed, e.message ?: e.javaClass.simpleName)
                setTextColor(Color.WHITE); gravity = Gravity.CENTER; setPadding(48, 48, 48, 48)
            }
            root.addView(hint)
            return
        }

        val files = filesDir.absolutePath
        extractPacked(files)
        val romfs = File(filesDir, "romfs").let { if (it.isDirectory) it.absolutePath else null }

        if (HostJni.nativeBoot(DmFront, files, romfs,
                File(filesDir, "aoc").takeIf { it.isDirectory }?.absolutePath,
                File(filesDir, "pso").takeIf { it.isDirectory }?.absolutePath) != 0) {
            val hint = TextView(this).apply {
                text = getText(R.string.no_game_files)
                setTextColor(Color.WHITE); gravity = Gravity.CENTER; setPadding(48, 48, 48, 48)
            }
            root.addView(hint)
            return
        }
        controllers = GamepadInput(this)
        // The launcher's defaults the packed meta carries (rumble strength, touch feedback)
        try {
            val meta = JSONObject(File(filesDir, "meta.json").readText())
            DmFront.rumbleGain = meta.optDouble("rumble_gain", 1.0).toFloat()
        } catch (e: Exception) { }
        showLauncher()
    }

    /** The packed game files (assets/dm/...), extracted once: romfs, modules, msl, font, aoc, pso, launcher.json, icon, meta. */
    private fun extractPacked(files: String) {
        val mark = File(filesDir, ".extracted")
        val version = try { packageManager.getPackageInfo(packageName, 0).versionName ?: "" } catch (e: Exception) { "" }
        if (mark.isFile && mark.readText() == version) return
        val am = assets
        val names = am.list("dm") ?: return
        if (names.isEmpty()) return
        for (entry in names) {
            val outDir = File(filesDir, entry.removeSuffix(".zip"))
            if (entry.endsWith(".zip")) {   // the big folders pack as zip (pack_apk.py): AAPT stores them whole
                val out = outDir.absolutePath
                ZipFile(File(outDir.parentFile, entry).let { _ -> extractZipAsset(am, "dm/$entry") }).use { zip ->
                    for (e in zip.entries()) {
                        val f = File(out, e.name)
                        if (e.isDirectory) f.mkdirs() else {
                            f.parentFile?.mkdirs()
                            zip.getInputStream(e).use { ins -> f.outputStream().use { ins.copyTo(it) } }
                        }
                    }
                }
            } else {
                val f = File(filesDir, entry)
                am.open("dm/$entry").use { ins -> f.outputStream().use { ins.copyTo(it) } }
            }
        }
        mark.writeText(version)
    }

    private fun extractZipAsset(am: android.content.res.AssetManager, path: String): File {
        val tmp = File.createTempFile("dmz", ".zip", cacheDir)
        am.open(path).use { ins -> tmp.outputStream().use { ins.copyTo(it) } }
        return tmp
    }

    private fun showLauncher() {
        if (System.getenv("DM_LAUNCHER") == "0") { startGame(); return }
        if (!File(filesDir, "launcher.json").isFile) { startGame(); return }
        startActivityForResult(Intent(this, LauncherActivity::class.java), LAUNCHER_REQUEST)
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == LAUNCHER_REQUEST && !launcherDone) { launcherDone = true; startGame() }
    }

    @SuppressLint("ClickableViewAccessibility")
    private fun startGame() {
        if (booted) return
        booted = true

        val surface = SurfaceView(this)
        surface.holder.setKeepScreenOn(true)
        gameView = surface
        root.addView(surface, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))

        val gamepad = DmGamepadView(this)
        gamepad.loadDefaultLayout()
        pad = gamepad
        root.addView(gamepad, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))

        // Frame-rate counter (front_ios.m): on when the APK was packed with it (meta.show_fps) or
        // DM_SHOW_FPS is set; a reading each second, none while in the background
        val showFps = System.getenv("DM_SHOW_FPS")?.toIntOrNull() == 1 ||
                (try { JSONObject(File(filesDir, "meta.json").readText()).optBoolean("show_fps", false) } catch (e: Exception) { false })
        if (showFps) {
            val label = TextView(this).apply {
                text = "-- fps"; setTextColor(Color.WHITE); textSize = 13f
                setBackgroundColor(Color.argb(115, 0, 0, 0)); setPadding(16, 6, 16, 6)
            }
            fps = label
            root.addView(label, FrameLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.TOP or Gravity.START).apply {
                leftMargin = 180; topMargin = 40
            })
            var n0 = HostJni.nativePresentCount(); var t0 = System.nanoTime()
            val h = Handler(Looper.getMainLooper())
            h.post(object : Runnable {
                override fun run() {
                    val n = HostJni.nativePresentCount(); val t = System.nanoTime()
                    val dt = (t - t0) / 1e9
                    if (dt >= 1.5 || (t - t0) < 0) {   // a longer span is the time in the background: no reading
                        if (dt in 0.5..1.5) label.text = "%.0f fps".format((n - n0) / dt)
                        n0 = n; t0 = t
                    }
                    h.postDelayed(this, 1000)
                }
            })
        }

        progress = ProgressOverlay(this)
        root.addView(progress, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))

        surface.holder.addCallback(object : SurfaceHolder.Callback {
            override fun surfaceCreated(holder: SurfaceHolder) { HostJni.nativeSetSurface(holder.surface) }
            override fun surfaceChanged(holder: SurfaceHolder, format: Int, width: Int, height: Int) { }
            override fun surfaceDestroyed(holder: SurfaceHolder) { HostJni.nativeSetSurface(null) }
        })

        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        // Edge gestures need two swipes, to avoid accidental exits (the iOS safe-edge counterpart)
        if (android.os.Build.VERSION.SDK_INT >= 30) window.insetsController?.setSystemBarsBehavior(WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE)
        controllers?.start()
        shake = ShakeSensor(this) { on -> controllers?.setShake(on) }.also { it.start() }
    }

    // ---- ProgressOverlay.Host: the prepare-step bar over the game ----
    override fun dmProgress(title: String?, done: Long, total: Long) {
        progress?.show(title, done, total)
    }

    // ---- Physical controllers: key / motion events the game view sees ----
    override fun onKeyDown(keyCode: Int, event: KeyEvent?): Boolean {
        if (controllers?.onKey(keyCode, event, true) == true) return true
        return super.onKeyDown(keyCode, event)
    }
    override fun onKeyUp(keyCode: Int, event: KeyEvent?): Boolean {
        if (controllers?.onKey(keyCode, event, false) == true) return true
        return super.onKeyUp(keyCode, event)
    }
    override fun onGenericMotionEvent(e: MotionEvent?): Boolean {
        if (controllers?.onMotion(e) == true) return true
        return super.onGenericMotionEvent(e)
    }
    fun padVisibility(hidden: Boolean) { pad?.setPadHidden(hidden) }

    // ---- App lifecycle → the host's focus events (front_ios_focus_event's table) ----
    override fun onPause() { super.onPause(); if (booted) HostJni.nativeFocus(0) }
    override fun onStop() { super.onStop(); shake?.stop(); if (booted) HostJni.nativeFocus(1) }
    override fun onStart() { super.onStart(); shake?.start(); if (booted) HostJni.nativeFocus(2) }
    override fun onResume() { super.onResume(); if (booted) HostJni.nativeFocus(3) }

    override fun onDestroy() {
        DmFront.activity = null
        if (isFinishing && booted) HostJni.nativeShutdown("activity finished")
        super.onDestroy()
    }
}
