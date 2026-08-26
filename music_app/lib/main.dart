import 'dart:ui';

import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'app.dart';
import 'config.dart';

// Must be a top-level function — runs in a separate isolate when the app is
// terminated or in the background.
@pragma('vm:entry-point')
Future<void> _backgroundMessageHandler(RemoteMessage message) async {
  // FCM displays the notification automatically when a notification payload
  // is present.  Handle data-only background messages here if needed.
}

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();

  // Catch Flutter framework errors (widget build, rendering, etc.)
  FlutterError.onError = (FlutterErrorDetails details) {
    FlutterError.presentError(details);
  };

  // Catch uncaught async errors (isolate-level) — prevents hard Android crash.
  PlatformDispatcher.instance.onError = (error, stack) {
    // Log here if you add a crash-reporting SDK later.
    return true; // true = error handled, don't crash
  };

  // Firebase must be initialized before any firebase_messaging API is called.
  await Firebase.initializeApp(options: AppConfig.firebaseOptions);
  FirebaseMessaging.onBackgroundMessage(_backgroundMessageHandler);

  SystemChrome.setSystemUIOverlayStyle(
    const SystemUiOverlayStyle(
      statusBarColor: Colors.transparent,
      statusBarIconBrightness: Brightness.dark,
    ),
  );
  await SystemChrome.setPreferredOrientations([DeviceOrientation.portraitUp]);
  runApp(const SoundwavesApp());
}
