# Notes (mobile configuration)

The configuration files of a small notes app for Android and iOS. There is no app code here, only the files a mobile build reads:

- `android/app/src/main/AndroidManifest.xml`: the Android manifest.
- `android/app/src/main/res/xml/network_security_config.xml`: the Android network security configuration.
- `android/app/build.gradle`: the SDK levels.
- `ios/App/Info.plist`: the iOS property list.
- `ios/App/App.entitlements`: the iOS entitlements.

The project is never built here: no Android SDK, Xcode, emulator, simulator or device is used. The files are meant to be read and parsed
as text.
