plugins {
    id("com.android.application")
    // The Flutter Gradle Plugin must be applied after the Android and Kotlin Gradle plugins.
    id("dev.flutter.flutter-gradle-plugin")
    id("com.google.gms.google-services")   // Firebase (push notifications)
}

android {
    namespace = "com.example.caphy_app"

    // compileSdk 36 - several plugins (app_links, flutter_tts, gal,
    // google_sign_in_android, mobile_scanner, shared_preferences_android,
    // speech_to_text) now require it. compileSdk is backward compatible,
    // so this doesn't change the minimum Android version CAPHY runs on
    // (that's minSdk below) - it only changes what SDK the app is BUILT
    // against.
    compileSdk = 36
    ndkVersion = flutter.ndkVersion

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    defaultConfig {
        // TODO: Specify your own unique Application ID (https://developer.android.com/studio/build/application-id.html).
        applicationId = "com.caphy.app"
        
        minSdk = flutter.minSdkVersion                // Firebase needs at least 23
        targetSdk = 35
        
        versionCode = flutter.versionCode
        versionName = flutter.versionName
    }

    buildTypes {
        release {
            // TODO: Add your own signing config for the release build.
            // Signing with the debug keys for now, so `flutter run --release` works.
            signingConfig = signingConfigs.getByName("debug")
        }
    }
}

kotlin {
    compilerOptions {
        jvmTarget = org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17
    }
}

flutter {
    source = "../.."
}
