// The receiver link: the phone's half of the PC companion's USB connection. The companion reaches it
// through `adb forward tcp:<port> localabstract:nxnative` and speaks one JSON object a line: it
// announces itself (hello), streams the build (status: the stages, the shader counts, the install),
// and pings every couple of seconds so both sides know the moment the cable or the app goes away -
// either side closing drops the screen back to "waiting for the computer", and the companion tries
// again on its own. The link is the screen's content, never a step the build or the install waits on.

package org.nxnative.android

import android.content.Context
import android.net.LocalServerSocket
import android.net.LocalSocket
import android.os.Handler
import android.os.Looper
import org.json.JSONObject
import java.io.BufferedReader
import java.io.File
import java.io.InputStreamReader
import kotlin.concurrent.thread

object ReceiverLink {
    const val NAME = "nxnative"    // localabstract:nxnative, the name adb forward pairs with

    sealed class State {
        object Waiting : State()                                                     // no computer on the other end
        object Connected : State()                                                   // plugged in, waiting for the pick
        data class Building(val stage: String, val pct: Double?, val detail: String) : State()
        data class Installing(val game: String) : State()
        data class Done(val game: String) : State()
        data class Failed(val message: String) : State()
    }

    @Volatile var onState: ((State) -> Unit)? = null    // always called on the main thread
    @Volatile var state: State = State.Waiting         // the last thing posted, for a re-render on start
    private val main = Handler(Looper.getMainLooper())
    private var started = false
    @Volatile private var running = false
    @Volatile private var appContext: Context? = null
    @Volatile private var lastBeat = 0L               // the serve loop writes it, the watchdog reads it

    /** Idempotent: the first ReceiverActivity starts the listener, and it outlives the screen - the
     *  companion can raise the receiver again (adb shell am start) without the app being opened. */
    fun start(ctx: Context) {
        appContext = ctx.applicationContext
        synchronized(this) {
            if (started) return
            started = true; running = true
        }
        thread(name = "receiver-link") { listen() }
    }

    private fun listen() {
        while (running) {
            try {
                LocalServerSocket(NAME).use { server ->
                    while (running) {
                        val client = server.accept() ?: break   // the socket closed under us
                        serve(client)
                        post(State.Waiting)
                    }
                }
            } catch (e: Exception) { /* another nxnative app holds the name, or the socket died */ }
            if (!running) break
            Thread.sleep(1000)    // auto redetect: back on the air shortly
        }
    }

    /** One computer at a time: a watchdog closes the client when the pings stop, which also unblocks
     *  the read below so the next connection (a reconnecting companion) can be accepted. */
    private fun serve(client: LocalSocket) {
        var open = true
        lastBeat = System.currentTimeMillis()
        val watchdog = thread(name = "receiver-watchdog") {
            while (open) {
                if (System.currentTimeMillis() - lastBeat > 8000) {   // the computer went quiet: cable, app or companion
                    open = false
                    try { client.close() } catch (e: Exception) { }
                    break
                }
                Thread.sleep(250)
            }
        }
        try {
            val ins = BufferedReader(InputStreamReader(client.inputStream, Charsets.UTF_8))
            val out = client.outputStream
            val hasGame = appContext?.let { File(it.filesDir, "romfs").isDirectory } ?: false
            write(out, JSONObject().put("event", "ready").put("hasGame", hasGame))
            post(State.Connected)
            while (open && running) {
                val line = ins.readLine() ?: break
                lastBeat = System.currentTimeMillis()
                val msg = try { JSONObject(line) } catch (e: Exception) { null } ?: continue
                when (msg.optString("event")) {
                    "ping" -> if (!write(out, JSONObject().put("event", "pong"))) break
                    "hello" -> post(State.Connected)
                    "status" -> parseStatus(msg)?.let { post(it) }
                }
            }
        } catch (e: Exception) {
        } finally {
            open = false
            try { client.close() } catch (e: Exception) { }
            watchdog.join(1000)
        }
    }

    private fun parseStatus(m: JSONObject): State? {
        val pct: Double? = if (m.has("pct") && !m.isNull("pct")) m.optDouble("pct") else null
        return when (m.optString("state")) {
            "building" -> State.Building(m.optString("stage"), pct, m.optString("detail"))
            "installing" -> State.Installing(m.optString("game"))
            "done" -> State.Done(m.optString("game"))
            "error" -> State.Failed(m.optString("message"))
            else -> null
        }
    }

    private fun write(out: java.io.OutputStream, obj: JSONObject): Boolean = try {
        out.write((obj.toString() + "\n").toByteArray(Charsets.UTF_8)); out.flush(); true
    } catch (e: Exception) { false }

    private fun post(st: State) {
        state = st
        val l = onState ?: return
        main.post { if (onState === l) l(st) }
    }
}
