plugins {
    id("com.android.application") version "8.10.1"
    id("org.jetbrains.kotlin.android") version "2.0.21"
    id("com.google.gms.google-services") version "4.4.3"
}

android {
    namespace = "ci.sunrisespace.app"
    compileSdk = 36

    defaultConfig {
        applicationId = "ci.sunrisespace.app"      // identifiant définitif sur le Play Store : à ne plus changer après publication
        minSdk = 26
        targetSdk = 36                               // exigé par Google Play pour les nouvelles applications
        val code = System.getenv("VERSION_CODE")?.toIntOrNull() ?: 1
        versionCode = code
        versionName = "1.0.$code"
        // Adresse du site chargé dans l'application (à changer si vous changez de nom de domaine)
        buildConfigField("String", "APP_URL", "\"https://sunrise-space1992.onrender.com\"")
    }

    buildFeatures {
        buildConfig = true
    }

    // Clé de signature fixe pour les versions de test : chaque nouvelle version s'installe par-dessus l'ancienne
    // (sans elle, Android refuse la mise à jour). Elle n'est pas secrète : ne l'utilisez PAS pour le Play Store.
    signingConfigs {
        create("shared") {
            storeFile = file("sunrise-test.keystore")
            storePassword = "android"
            keyAlias = "sunrise"
            keyPassword = "android"
        }
    }
    buildTypes {
        getByName("debug") {
            signingConfig = signingConfigs.getByName("shared")
        }
    }

    sourceSets["main"].apply {
        manifest.srcFile("AndroidManifest.xml")
        java.setSrcDirs(listOf("kotlin"))
        res.setSrcDirs(listOf("res"))
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.activity:activity-ktx:1.10.1")
    implementation(platform("com.google.firebase:firebase-bom:33.16.0"))
    implementation("com.google.firebase:firebase-messaging")
}
