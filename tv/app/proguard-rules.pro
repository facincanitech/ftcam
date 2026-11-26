# WebView chama métodos por reflexão a partir do JS: mantém os que forem anotados
-keepclassmembers class * { @android.webkit.JavascriptInterface <methods>; }
-keep class br.com.facincanitech.ftcamtv.** { *; }
-dontwarn android.webkit.**
