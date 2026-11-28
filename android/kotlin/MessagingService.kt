package ci.sunrisespace.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.RingtoneManager
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage

/** Jeton Firebase du téléphone, gardé pour être donné au site. */
object Prefs {
    private const val FILE = "sunrise"

    fun token(ctx: Context): String =
        ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE).getString("fcm", "") ?: ""

    fun saveToken(ctx: Context, token: String) {
        ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE).edit().putString("fcm", token).apply()
    }
}

/** Notifications : « sonnerie d'appel » pour les demandes de course, notification normale pour le reste. */
object Notifier {
    const val CH_RING = "ride_requests_v1"
    const val CH_INFO = "info_v1"
    private const val RING_MS = 40_000L   // la sonnerie dure 40 secondes, ou jusqu'à l'ouverture de l'application

    fun ensureChannels(ctx: Context) {
        val nm = ctx.getSystemService(NotificationManager::class.java)
        val ringtone = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)
        val ring = NotificationChannel(CH_RING, ctx.getString(R.string.channel_ring), NotificationManager.IMPORTANCE_HIGH).apply {
            description = ctx.getString(R.string.channel_ring_desc)
            setSound(
                ringtone,
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_NOTIFICATION_RINGTONE)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                    .build()
            )
            enableVibration(true)
            vibrationPattern = longArrayOf(0, 700, 400, 700, 400, 700)
            lockscreenVisibility = Notification.VISIBILITY_PUBLIC
        }
        val info = NotificationChannel(CH_INFO, ctx.getString(R.string.channel_info), NotificationManager.IMPORTANCE_HIGH).apply {
            lockscreenVisibility = Notification.VISIBILITY_PUBLIC
        }
        nm.createNotificationChannel(ring)
        nm.createNotificationChannel(info)
    }

    fun show(ctx: Context, title: String, body: String, tag: String, ring: Boolean) {
        ensureChannels(ctx)
        val open = PendingIntent.getActivity(
            ctx, 0,
            Intent(ctx, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val builder = NotificationCompat.Builder(ctx, if (ring) CH_RING else CH_INFO)
            .setSmallIcon(R.drawable.ic_stat_sun)
            .setColor(0xFFFF7A1A.toInt())
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .setAutoCancel(true)
            .setContentIntent(open)
        if (ring) {
            builder.setCategory(NotificationCompat.CATEGORY_CALL).setTimeoutAfter(RING_MS)
        } else {
            builder.setCategory(NotificationCompat.CATEGORY_MESSAGE)
        }
        val notification = builder.build()
        if (ring) notification.flags = notification.flags or Notification.FLAG_INSISTENT   // le son se répète tant que la notification est là
        try {
            NotificationManagerCompat.from(ctx).notify(tag.hashCode(), notification)
        } catch (e: SecurityException) {
            // autorisation des notifications refusée
        }
    }

    fun cancel(ctx: Context, tag: String) {
        NotificationManagerCompat.from(ctx).cancel(tag.hashCode())
    }
}

class MessagingService : FirebaseMessagingService() {

    override fun onNewToken(token: String) {
        Prefs.saveToken(this, token)
        MainActivity.pushTokenToPage()
    }

    override fun onMessageReceived(message: RemoteMessage) {
        val d = message.data
        val tag = d["tag"] ?: "sunrise"
        when (d["type"]) {
            "stop" -> Notifier.cancel(this, tag)   // la course est prise ou annulée : on coupe la sonnerie
            else -> {
                if (MainActivity.visible) {        // application ouverte à l'écran : c'est elle qui fait sa sirène
                    MainActivity.notifyPage()
                    return
                }
                Notifier.show(this, d["title"] ?: getString(R.string.app_name), d["body"] ?: "", tag, d["type"] == "ring")
            }
        }
    }
}
