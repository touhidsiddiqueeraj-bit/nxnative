plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// The packed game's identity comes in as Gradle properties (dm/android/tools/build_apk.sh sets them;
// a bare build without them makes the empty shell - what the CI workflow ships as the template)
val appId = (findProperty("DM_APP_ID") as String?) ?: "org.nxnative.shell"
val appName = (findProperty("DM_APP_NAME") as String?) ?: "nxnative"
val appVersion = (findProperty("DM_APP_VERSION") as String?) ?: "1.0"
val staged = findProperty("DM_STAGED") as String?   // the staged assets folder (pack_apk.py's output)

android {
    namespace = "org.nxnative.android"
    compileSdk = 34
    ndkVersion = "26.3.11579264"

    defaultConfig {
        applicationId = appId
        minSdk = 29    // AAudio and AMediaCodec_getOutputImage; every device that meets the game's memory class
        targetSdk = 34
        versionCode = 1
        versionName = appVersion
        ndk {
            abiFilters += "arm64-v8a"
        }
        externalNativeBuild {
            cmake {
                // GAME comes from the environment (build_apk.sh exports it; the profile the host is built for)
                arguments += listOf("-DGAME=${System.getenv("GAME") ?: ""}")
            }
        }
    }

    externalNativeBuild {
        cmake {
            path = file("../jni/CMakeLists.txt")   // dm/android/jni/CMakeLists.txt (from app/)
            version = "3.22.1"
        }
    }

    // The staged game files join the assets (nothing game-sized ever lands in the repository tree;
    // a bare build without DM_STAGED packs the app's own resources only)
    if (staged != null) {
        sourceSets.getByName("main") {
            assets.srcDir(file(staged))
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false   // the JNI surface and the host's dlopen'd symbols stay whole
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    packaging {
        jniLibs {
            useLegacyPackaging = false
        }
        // The staged game files are already-stored zip entries inside the assets; resources.arsc and
        // the host library stay the build's own
        resources {
            excludes += listOf("META-INF/*.kotlin_module")
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.activity:activity-ktx:1.9.0")
    implementation("com.google.android.material:material:1.12.0")
}
