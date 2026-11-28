package ci.sunrisespace.app

import android.Manifest
import android.annotation.SuppressLint
import android.app.NotificationManager
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.drawable.ColorDrawable
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.view.ViewGroup
import android.webkit.GeolocationPermissions
import android.webkit.JavascriptInterface
import android.webkit.PermissionRequest
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import com.google.firebase.messaging.FirebaseMessaging

class MainActivity : ComponentActivity() {

    lateinit var web: WebView

    private var fileCallback: ValueCallback<Array<Uri>>? = null
    private var pendingGeo: Pair<String, GeolocationPermissions.Callback>? = null
    private var pendingMic: PermissionRequest? = null

    private val notifLauncher = registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    private val geoLauncher = registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { result ->
        pendingGeo?.let { (origin, cb) -> cb.invoke(origin, result.values.any { it }, false) }
        pendingGeo = null
    }

    private val micLauncher = registerForActivityResult(ActivityResultContracts.RequestPermission()) { ok ->
        val req = pendingMic
        pendingMic = null
        if (req != null) {
            if (ok) req.grant(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE)) else req.deny()
        }
    }

    private val fileLauncher = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { res ->
        fileCallback?.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(res.resultCode, res.data))
        fileCallback = null
    }

    private fun granted(p: String) = ContextCompat.checkSelfPermission(this, p) == PackageManager.PERMISSION_GRANTED

    inner class Bridge {
        @JavascriptInterface
        fun getToken(): String = Prefs.token(this@MainActivity)

        @JavascriptInterface
        fun version(): String = BuildConfig.VERSION_NAME
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        current = this

        val night = Color.parseColor("#0B1D3A")
        window.setBackgroundDrawable(ColorDrawable(night))
        WindowCompat.setDecorFitsSystemWindows(window, false)
        WindowCompat.getInsetsController(window, window.decorView).isAppearanceLightStatusBars = false

        Notifier.ensureChannels(this)
        if (Build.VERSION.SDK_INT >= 33 && !granted(Manifest.permission.POST_NOTIFICATIONS)) {
            notifLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
        }

        web = WebView(this).apply {
            setBackgroundColor(night)
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.mediaPlaybackRequiresUserGesture = false
            settings.setGeolocationEnabled(true)
            settings.allowFileAccess = false
            settings.userAgentString = settings.userAgentString + " SunriseApp/" + BuildConfig.VERSION_NAME
            addJavascriptInterface(Bridge(), "SunriseNative")

            webViewClient = object : WebViewClient() {
                override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                    val url = request.url
                    val host = Uri.parse(BuildConfig.APP_URL).host
                    if ((url.scheme == "https" || url.scheme == "http") && url.host == host) return false
                    try {
                        startActivity(Intent(Intent.ACTION_VIEW, url).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                    } catch (e: Exception) {
                        // aucune application pour ce lien
                    }
                    return true
                }

                override fun onPageFinished(view: WebView, url: String) {
                    pushTokenToPage()
                }

                override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                    if (request.isForMainFrame) view.loadData(offlineHtml(), "text/html", "UTF-8")
                }
            }

            webChromeClient = object : WebChromeClient() {
                override fun onGeolocationPermissionsShowPrompt(origin: String, callback: GeolocationPermissions.Callback) {
                    if (granted(Manifest.permission.ACCESS_FINE_LOCATION)) {
                        callback.invoke(origin, true, false)
                    } else {
                        pendingGeo = Pair(origin, callback)
                        geoLauncher.launch(arrayOf(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION))
                    }
                }

                override fun onPermissionRequest(request: PermissionRequest) {
                    runOnUiThread {
                        if (!request.resources.contains(PermissionRequest.RESOURCE_AUDIO_CAPTURE)) {
                            request.deny()
                        } else if (granted(Manifest.permission.RECORD_AUDIO)) {
                            request.grant(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
                        } else {
                            pendingMic = request
                            micLauncher.launch(Manifest.permission.RECORD_AUDIO)
                        }
                    }
                }

                override fun onShowFileChooser(
                    webView: WebView,
                    callback: ValueCallback<Array<Uri>>,
                    params: FileChooserParams
                ): Boolean {
                    fileCallback?.onReceiveValue(null)
                    fileCallback = callback
                    return try {
                        fileLauncher.launch(params.createIntent())
                        true
                    } catch (e: Exception) {
                        fileCallback = null
                        false
                    }
                }
            }
        }

        val root = FrameLayout(this)
        root.setBackgroundColor(night)
        root.addView(web, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        setContentView(root)

        // Android 15+ : l'application s'affiche sous les barres du système, on laisse la place
        ViewCompat.setOnApplyWindowInsetsListener(root) { v, insets ->
            val bars = insets.getInsets(
                WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout() or WindowInsetsCompat.Type.ime()
            )
            v.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            WindowInsetsCompat.CONSUMED
        }

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (web.canGoBack()) web.goBack() else moveTaskToBack(true)
            }
        })

        FirebaseMessaging.getInstance().token.addOnSuccessListener { token ->
            Prefs.saveToken(this, token)
            pushTokenToPage()
        }

        web.loadUrl(BuildConfig.APP_URL)
    }

    private fun offlineHtml(): String =
        """<html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head>
        <body style="margin:0;background:#0B1D3A;color:#fff;font-family:sans-serif;display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh;text-align:center;padding:24px">
        <h2>📶 Pas de connexion</h2><p>Vérifiez votre internet puis réessayez.</p>
        <button onclick="location.href='${BuildConfig.APP_URL}'" style="padding:14px 28px;font-size:16px;border:0;border-radius:12px;background:#FF7A1A;color:#fff">Réessayer</button>
        </body></html>"""

    override fun onStart() {
        super.onStart()
        visible = true
        // L'application est ouverte : les notifications de demande n'ont plus lieu d'être
        getSystemService(NotificationManager::class.java).cancelAll()
        Updater.check(this)
    }

    override fun onResume() {
        super.onResume()
        Updater.resumePending(this)
    }

    override fun onStop() {
        visible = false
        super.onStop()
    }

    override fun onDestroy() {
        if (current === this) current = null
        super.onDestroy()
    }

    companion object {
        @Volatile var current: MainActivity? = null
        @Volatile var visible = false

        fun pushTokenToPage() {
            current?.let { a -> a.runOnUiThread { a.web.evaluateJavascript("window.__onFcmToken&&window.__onFcmToken()", null) } }
        }

        fun notifyPage() {
            current?.let { a -> a.runOnUiThread { a.web.evaluateJavascript("window.__onNativePush&&window.__onNativePush()", null) } }
        }
    }
}
