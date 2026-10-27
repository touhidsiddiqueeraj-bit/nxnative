// The software keyboard (hle_swkbd.c's dialog, front_text_input's host): a title, a hint line and
// an input field with the guide as placeholder; OK returns the text (and true to the guest thread),
// Cancel returns null. maxChars caps the field (the host truncates in UTF-16 units itself).
package org.nxnative.android

import android.app.AlertDialog
import android.content.Context
import android.text.InputType
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView

class TextInputDialog(
    context: Context, title: String, sub: String, guide: String, initial: String,
    maxChars: Int, private val done: (String?) -> Unit,
) : AlertDialog.Builder(context) {
    init {
        setTitle(title.ifEmpty { null })
        val box = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 8, 48, 8)
        }
        if (sub.isNotEmpty()) box.addView(TextView(context).apply { text = sub; setPadding(0, 0, 0, 12) })
        val field = EditText(context).apply {
            setText(initial)
            hint = guide
            inputType = InputType.TYPE_CLASS_TEXT
            if (maxChars > 0) filters = arrayOf(android.text.InputFilter.LengthFilter(maxChars))
        }
        box.addView(field)
        setView(box)
        setPositiveButton(R.string.ok) { _, _ -> done(field.text.toString()) }
        setNegativeButton(R.string.cancel) { _, _ -> done(null) }
        setOnCancelListener { done(null) }
        // The guest clock keeps running meanwhile (hle_swkbd.c): the dialog must not freeze anything
        // behind it, and the game's frame loop continues under it, as on the Switch.
    }
}
