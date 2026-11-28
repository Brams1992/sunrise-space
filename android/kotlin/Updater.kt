package ci.sunrisespace.app

import android.app.AlertDialog
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import android.widget.Toast
import androidx.core.content.FileProvider
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest

/**
 * Mise à jour hors Play Store : au démarrage, l'application demande au serveur s'il existe une version plus récente,
 * la télécharge, vérifie son empreinte, puis ouvre l'installation Android (une seule confirmation à toucher).
 * Désactivé automatiquement si l'application a été installée depuis le Play Store (c'est lui qui s'en charge).
 */
object Updater {
    private const val CHECK_EVERY_MS = 10 * 60 * 1000L   // au plus une vérification toutes les 10 minutes
    @Volatile private var checking = false
    @Volatile private var downloading = false
    private var pendingFile: File? = null

    @Suppress("DEPRECATION")
    private fun fromPlayStore(c: Context): Boolean = try {
        val installer = if (Build.VERSION.SDK_INT >= 30) {
            c.packageManager.getInstallSourceInfo(c.packageName).installingPackageName
        } else {
            c.packageManager.getInstallerPackageName(c.packageName)
        }
        installer == "com.android.vending"
    } catch (e: Exception) {
        false
    }

    fun check(a: MainActivity) {
        if (checking || downloading || fromPlayStore(a)) return
        val prefs = a.getSharedPreferences("sunrise", Context.MODE_PRIVATE)
        val now = System.currentTimeMillis()
        if (now - prefs.getLong("upd_last", 0L) < CHECK_EVERY_MS) return
        checking = true
        Thread {
            try {
                val conn = URL(BuildConfig.APP_URL + "/api/app/version").openConnection() as HttpURLConnection
                conn.connectTimeout = 8000
                conn.readTimeout = 8000
                val json = JSONObject(conn.inputStream.bufferedReader().use { it.readText() })
                prefs.edit().putLong("upd_last", now).apply()
                if (json.optInt("versionCode", 0) > BuildConfig.VERSION_CODE) {
                    a.runOnUiThread { prompt(a, json) }
                }
            } catch (e: Exception) {
                // pas de réseau ou serveur endormi : on réessaiera à la prochaine ouverture
            } finally {
                checking = false
            }
        }.start()
    }

    private fun prompt(a: MainActivity, j: JSONObject) {
        if (a.isFinishing || a.isDestroyed) return
        val force = j.optBoolean("force", false)
        val notes = j.optString("notes", "")
        val mo = j.optLong("size", 0L) / 1048576.0
        val msg = "Version " + j.optString("versionName") + (if (mo > 0) " (%.1f Mo)".format(mo) else "") +
            (if (notes.isNotBlank()) "\n\n$notes" else "") +
            (if (force) "\n\nCette mise à jour est obligatoire." else "")
        val builder = AlertDialog.Builder(a)
            .setTitle("Nouvelle version disponible")
            .setMessage(msg)
            .setCancelable(!force)
            .setPositiveButton("Mettre à jour", null)   // le clic est géré plus bas pour garder la fenêtre ouverte
        if (!force) builder.setNegativeButton("Plus tard", null)
        val dialog = builder.create()
        dialog.show()
        dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = false
            download(a, j, dialog)
        }
    }

    private fun download(a: MainActivity, j: JSONObject, dialog: AlertDialog) {
        if (downloading) return
        downloading = true
        Thread {
            try {
                val dir = File(a.cacheDir, "updates")
                dir.mkdirs()
                dir.listFiles()?.forEach { it.delete() }
                val out = File(dir, "sunrise-space.apk")
                val conn = URL(BuildConfig.APP_URL + j.optString("url", "/app/sunrise-space.apk")).openConnection() as HttpURLConnection
                conn.connectTimeout = 15000
                conn.readTimeout = 30000
                val total = conn.contentLengthLong
                val digest = MessageDigest.getInstance("SHA-256")
                var done = 0L
                var lastPct = -1
                conn.inputStream.use { input ->
                    out.outputStream().use { output ->
                        val buf = ByteArray(32 * 1024)
                        while (true) {
                            val n = input.read(buf)
                            if (n < 0) break
                            output.write(buf, 0, n)
                            digest.update(buf, 0, n)
                            done += n
                            if (total > 0) {
                                val pct = (done * 100 / total).toInt()
                                if (pct != lastPct) {
                                    lastPct = pct
                                    a.runOnUiThread { dialog.setMessage("Téléchargement… $pct %") }
                                }
                            }
                        }
                    }
                }
                val sha = digest.digest().joinToString("") { "%02x".format(it) }
                val expected = j.optString("sha256", "")
                if (expected.isNotEmpty() && !sha.equals(expected, ignoreCase = true)) {
                    out.delete()
                    throw IllegalStateException("empreinte différente")
                }
                a.runOnUiThread {
                    dialog.dismiss()
                    install(a, out)
                }
            } catch (e: Exception) {
                a.runOnUiThread {
                    dialog.dismiss()
                    Toast.makeText(a, "Téléchargement impossible. Réessayez plus tard.", Toast.LENGTH_LONG).show()
                }
            } finally {
                downloading = false
            }
        }.start()
    }

    private fun install(a: MainActivity, file: File) {
        if (!a.packageManager.canRequestPackageInstalls()) {
            // Android demande d'abord d'autoriser cette application à installer des mises à jour
            pendingFile = file
            Toast.makeText(a, "Autorisez Sunrise Space à installer les mises à jour, puis revenez dans l'application.", Toast.LENGTH_LONG).show()
            a.startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:" + a.packageName)))
            return
        }
        val uri = FileProvider.getUriForFile(a, a.packageName + ".fileprovider", file)
        a.startActivity(
            Intent(Intent.ACTION_VIEW)
                .setDataAndType(uri, "application/vnd.android.package-archive")
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        )
    }

    /** Appelé quand l'utilisateur revient des réglages après avoir donné l'autorisation. */
    fun resumePending(a: MainActivity) {
        val f = pendingFile ?: return
        if (f.exists() && a.packageManager.canRequestPackageInstalls()) {
            pendingFile = null
            install(a, f)
        }
    }
}
