[app]
title = ScreenMirror
package.name = screenmirror
package.domain = org.example
source.dir = .
source.include_exts = py,png,jpg,kv,atlas
version = 0.1

# pillow dùng để encode JPEG, pyjnius để gọi API Android
requirements = python3,kivy,pyjnius,pillow

orientation = portrait
fullscreen = 0

android.permissions = INTERNET,ACCESS_NETWORK_STATE,FOREGROUND_SERVICE,POST_NOTIFICATIONS
android.api = 34
android.minapi = 26
android.ndk = 25b
android.archs = arm64-v8a

[buildozer]
log_level = 2
warn_on_root = 1
