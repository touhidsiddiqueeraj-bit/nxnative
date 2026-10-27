// Launcher settings, validation and persistence (LauncherSettings.swift's counterpart): the same
// dm/launcher.json the iOS app reads (dm/games/settings.py writes it when packing), the same rules -
// a variable an env file set (DM_ENV_FILE_KEYS, filled by dm_jni.c at boot) is shown but kept, a
// saved choice the game no longer offers falls back to its default, and only a choice that differs
// from the game's default is stored. SharedPreferences stands in for UserDefaults.
package org.nxnative.android

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

data class Setting(
    val env: String, val kind: String, val label: String, val note: String, val default: String,
    val section: String?, val options: List<Pair<String, String>>?,      // choice: (value, label)
    val min: Double?, val max: Double?, val step: Double?,                 // range
    val on: String?, val off: String?,                                     // toggle
) {
    val sectionTitle get() = section ?: "Game"
    val onValue get() = on ?: "1"
    val offValue get() = off ?: "0"
    fun isDefault(v: String): Boolean =       // a range compares as numbers (a slider writes 1.00 for 1.0)
        if (kind == "range") (v.toDoubleOrNull() ?: Double.NaN).let { a -> (default.toDoubleOrNull() ?: Double.NaN).let { b -> !a.isNaN() && !b.isNaN() && Math.abs(a - b) < 1e-6 } } else v == default
    fun accepts(v: String): Boolean = when (kind) {   // a saved choice the game still offers
        "choice" -> options?.any { it.first == v } ?: false
        "range" -> v.toDoubleOrNull()?.let { n -> n.isFinite() && min != null && max != null && n >= min && n <= max } ?: false
        "toggle" -> v == onValue || v == offValue
        else -> true
    }

    companion object {
        fun fromJson(o: JSONObject): Setting = Setting(
            env = o.getString("env"), kind = o.getString("kind"), label = o.getString("label"),
            note = if (o.has("note")) o.getString("note") else "", default = o.getString("default"),
            section = if (o.has("section") && !o.isNull("section")) o.getString("section") else null,
            options = if (o.has("options") && !o.isNull("options")) (0 until o.getJSONArray("options").length()).map {
                val pair = o.getJSONArray("options").getJSONArray(it)
                Pair(pair.getString(0), pair.getString(1))
            } else null,
            min = if (o.has("min") && !o.isNull("min")) o.optDouble("min") else null,
            max = if (o.has("max") && !o.isNull("max")) o.optDouble("max") else null,
            step = if (o.has("step") && !o.isNull("step")) o.optDouble("step") else null,
            on = if (o.has("on") && !o.isNull("on")) o.getString("on") else null,
            off = if (o.has("off") && !o.isNull("off")) o.getString("off") else null,
        )
    }
}

class LauncherSettings(context: Context, val settings: List<Setting>, val envFileKeys: Set<String>) {
    private val prefs = context.getSharedPreferences("launcher", Context.MODE_PRIVATE)
    val values = HashMap<String, String>()

    init {
        for (s in settings) {
            if (s.env in envFileKeys) { System.getenv(s.env)?.let { values[s.env] = it }; continue }
            val saved = prefs.getString("launcher.${s.env}", null)
            values[s.env] = if (saved != null && s.accepts(saved)) saved else s.default
        }
    }

    val sections: List<String>
        get() = settings.fold(mutableListOf()) { acc, s -> if (!acc.contains(s.sectionTitle)) acc.add(s.sectionTitle); acc }

    fun changed(): Boolean = settings.any { it.env !in envFileKeys && !it.isDefault(values[it.env] ?: it.default) }

    /** Reset every launcher choice to the game's defaults; an env file's values stay. */
    fun reset() {
        for (s in settings) {
            prefs.edit().remove("launcher.${s.env}").apply()
            if (s.env !in envFileKeys) values[s.env] = s.default
        }
    }

    /** Before the game starts: every setting not set by an env file goes into the environment. */
    fun apply() {
        for (s in settings) {
            val v = values[s.env] ?: s.default
            if (s.env in envFileKeys) continue   // kept at what the env file set
            if (s.isDefault(v)) prefs.edit().remove("launcher.${s.env}").apply()
            else prefs.edit().putString("launcher.${s.env}", v).apply()
            HostJni.nativeSetenv(s.env, v)
        }
    }

    companion object {
        /** The launcher.json the APK was packed with, from <filesDir>/launcher.json; null = no launcher. */
        fun load(context: Context): LauncherSettings? {
            val file = File(context.filesDir, "launcher.json")
            if (!file.isFile) return null
            return try {
                val settings = ArrayList<Setting>()
                val arr: JSONArray = JSONObject(file.readText()).getJSONArray("settings")
                for (i in 0 until arr.length()) settings.add(Setting.fromJson(arr.getJSONObject(i)))
                if (settings.isEmpty()) null
                else LauncherSettings(context, settings, System.getenv("DM_ENV_FILE_KEYS")
                    ?.split(' ')?.filter { it.isNotEmpty() }?.toSet() ?: emptySet())
            } catch (e: Exception) {
                android.util.Log.w("nxnative", "launcher.json cannot be read: ${e.message}")
                null
            }
        }
    }
}
