import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_auth/firebase_auth.dart' as fb;
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:google_sign_in/google_sign_in.dart';
import 'package:app_links/app_links.dart';
import 'package:mobile_scanner/mobile_scanner.dart';
import 'api.dart';
import 'theme.dart';
import 'widgets.dart';
import 'home_tab.dart';
import 'alerts_tab.dart';
import 'live_tab.dart';
import 'me_tab.dart';

@pragma('vm:entry-point')
Future<void> _bgHandler(RemoteMessage message) async {
  // A notification payload is shown by the OS automatically when the app is
  // in the background/terminated, so nothing to do here.
}

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();

  // Draw behind the status and navigation bars instead of being boxed in by
  // them, and make both bars transparent so the dark theme runs edge to edge.
  // The live camera view goes further and hides them entirely.
  SystemChrome.setEnabledSystemUIMode(SystemUiMode.edgeToEdge);
  SystemChrome.setSystemUIOverlayStyle(const SystemUiOverlayStyle(
    statusBarColor: Colors.transparent,
    statusBarIconBrightness: Brightness.light,
    systemNavigationBarColor: Colors.transparent,
    systemNavigationBarIconBrightness: Brightness.light,
    systemNavigationBarDividerColor: Colors.transparent,
  ));

  await Store.init();

  bool loggedIn = false;
  try {
    await Firebase.initializeApp();

    // Firebase remembers the signed-in user across app restarts on its own
    // (separate from Store.token, which is just a cached copy of the ID
    // token for local-network requests). currentUser is the source of
    // truth for "is anyone logged in" now.
    final user = fb.FirebaseAuth.instance.currentUser;
    if (user != null) {
      // Firebase already has a locally cached, signed-in user at this
      // point - that's the actual source of truth. Treat that as logged
      // in FIRST, then try to opportunistically refresh the ID token.
      Store.user = user.email ?? '';
      loggedIn = true;

      // ID tokens expire (~1 hour) - refresh on startup so a phone that's
      // been closed for a while doesn't open to a stale/expired token.
      //
      // BUG FIX: this used to be the ONLY thing that set loggedIn = true,
      // and getIdToken(true) forces a network round-trip to Google. The
      // reported bug - exit the app on good Wi-Fi, turn off that
      // connection (or land on one with no real internet), reopen the
      // app - is exactly "cold start with no internet": Firebase's local
      // cache still has the user, but this forced refresh either throws
      // (falls to the outer catch, which is a second safety net - see
      // below) or can simply hang for a while on a connected-but-dead
      // network, since it never used a timeout. Wrapping it in its own
      // try/timeout and treating any failure here as "keep using the
      // last-known-good cached token, don't refresh it" means a phone
      // that was already validly signed in NEVER gets treated as logged
      // out just because this one optional refresh couldn't complete -
      // it only skips getting a newer token, which the app already does
      // routinely elsewhere as tokens naturally get used and refreshed.
      try {
        final freshToken = await user
            .getIdToken(true)
            .timeout(const Duration(seconds: 6));
        if (freshToken != null) {
          Store.token = freshToken;
        }
      } catch (_) {
        // Offline or slow network - keep whatever token is already cached
        // in Store (from the last time this succeeded) instead of losing
        // the signed-in state over an optional refresh.
      }
    }

    FirebaseMessaging.onBackgroundMessage(_bgHandler);
    await FirebaseMessaging.instance.requestPermission();

    // No more shared 'caphy_alerts' topic - every phone subscribing to the
    // same topic meant every phone got every household's alerts. Instead,
    // each phone registers its own FCM token with the paired laptop
    // (/api/fcm/register), which subscribes it to a topic scoped to just
    // that account + that device (see storage/firebase_push.py). Only
    // works if the laptop's address has been set - if not, this silently
    // no-ops and retries next time (e.g. after the user visits Settings).
    if (loggedIn && Store.hasServerAddress) {
      final fcmToken = await FirebaseMessaging.instance.getToken();
      if (fcmToken != null) {
        Api.rememberFcmToken(fcmToken);
        await Api.registerFcmToken(fcmToken);
      }
    }

    // If the OS rotates the token later, re-register it so the phone keeps
    // receiving alerts under the new token.
    FirebaseMessaging.instance.onTokenRefresh.listen((newToken) {
      Api.rememberFcmToken(newToken);
      if (Store.token != null && Store.hasServerAddress) {
        Api.registerFcmToken(newToken);
      }
    });
  } catch (_) {
    // Firebase not set up / offline — the app still works over local Wi-Fi
    // once the user has a cached Store.token from a previous session.
    loggedIn = Store.token != null;
  }
  runApp(CaphyApp(initiallyLoggedIn: loggedIn));
}

class CaphyApp extends StatefulWidget {
  final bool initiallyLoggedIn;
  const CaphyApp({super.key, this.initiallyLoggedIn = false});
  @override
  State<CaphyApp> createState() => _CaphyAppState();
}

class _CaphyAppState extends State<CaphyApp> {
  late bool _loggedIn = widget.initiallyLoggedIn;

  // Set when the app is opened via the emailed magic sign-in link (either
  // cold-started from it, or resumed while already running). LoginScreen
  // reads this to jump straight to completing the passwordless sign-in
  // instead of showing the normal email/password/Google form.
  String? _pendingSignInLink;

  StreamSubscription<Uri>? _linkSub;
  final _appLinks = AppLinks();

  @override
  void initState() {
    super.initState();
    _initDeepLinks();
  }

  @override
  void dispose() {
    _linkSub?.cancel();
    super.dispose();
  }

  Future<void> _initDeepLinks() async {
    // App opened cold, directly from tapping the link.
    try {
      final initial = await _appLinks.getInitialLink();
      if (initial != null) _handlePossibleSignInLink(initial.toString());
    } catch (_) {}

    // App was already running (backgrounded) when the link was tapped.
    _linkSub = _appLinks.uriLinkStream.listen((uri) {
      _handlePossibleSignInLink(uri.toString());
    });
  }

  void _handlePossibleSignInLink(String link) {
    if (Api.isSignInLink(link)) {
      setState(() => _pendingSignInLink = link);
    }
  }

  void _onLogin() {
    setState(() {
      _loggedIn = true;
      _pendingSignInLink = null;
    });
    _registerPushAfterLogin();
  }

  /// Register this phone's FCM token now that we have an auth token to send
  /// it with (/api/fcm/register requires a logged-in session AND the
  /// laptop's local address, since that route lives on Flask). If the
  /// server address isn't set yet, this just no-ops - it'll retry once the
  /// user sets it (e.g. from Settings/Me tab), since push is a local-pairing
  /// feature, not something identity alone can provide.
  Future<void> _registerPushAfterLogin() async {
    if (!Store.hasServerAddress) return;
    try {
      final fcmToken = await FirebaseMessaging.instance.getToken();
      if (fcmToken != null) {
        Api.rememberFcmToken(fcmToken);
        await Api.registerFcmToken(fcmToken);
      }
    } catch (_) {
      // Push registration failing should never block getting into the app.
    }
  }

  void _onLogout() {
    Api.logout();
    setState(() => _loggedIn = false);
  }

  @override
  Widget build(BuildContext context) {
    final theme = ThemeData(
      brightness: Brightness.dark,
      scaffoldBackgroundColor: cBg,
      primaryColor: cTeal,
      colorScheme: const ColorScheme.dark(
          primary: cTeal, secondary: cTeal2, surface: cPanel2),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: cBg,
        hintStyle: const TextStyle(color: cMuted),
        enabledBorder: OutlineInputBorder(
            borderRadius: BorderRadius.circular(10),
            borderSide: const BorderSide(color: cLine)),
        focusedBorder: OutlineInputBorder(
            borderRadius: BorderRadius.circular(10),
            borderSide: const BorderSide(color: cTeal)),
      ),
    );
    return MaterialApp(
      title: 'CAPHY',
      debugShowCheckedModeBanner: false,
      theme: theme,
      home: _loggedIn
          ? DeviceGate(onLogout: _onLogout)
          : ConnectSignInScreen(onConnected: _onLogin),
    );
  }
}

/// The ONLY entry screen now: no email/password/Google login. The laptop is
/// already signed into the account and shows a QR (its Connect screen); the
/// phone scans it and is silently signed in as that same account via a
/// one-time Firebase custom token (see Api.signInWithScannedToken). This is
/// both the sign-in AND the device pairing in a single scan - after this,
/// Firebase remembers the session, so the phone never has to scan again
/// unless the user logs out.
class ConnectSignInScreen extends StatefulWidget {
  final VoidCallback onConnected;
  const ConnectSignInScreen({super.key, required this.onConnected});
  @override
  State<ConnectSignInScreen> createState() => _ConnectSignInScreenState();
}

class _ConnectSignInScreenState extends State<ConnectSignInScreen> {
  final MobileScannerController _ctl = MobileScannerController();
  bool _busy = false;
  bool _scanning = false;
  String? _error;

  @override
  void dispose() {
    _ctl.dispose();
    super.dispose();
  }

  Future<void> _onDetect(BarcodeCapture capture) async {
    if (_busy) return;
    final raw =
        capture.barcodes.isNotEmpty ? capture.barcodes.first.rawValue : null;
    if (raw == null) return;

    final payload = Api.parseLinkQr(raw);
    if (payload == null) {
      setState(() => _error =
          'That isn\'t a CAPHY connect code. On your laptop open CAPHY → '
          'Settings → Connect Phone.');
      return;
    }

    setState(() {
      _busy = true;
      _error = null;
    });
    await _ctl.stop();

    final err = await Api.signInWithScannedToken(payload);
    if (!mounted) return;
    if (err != null) {
      setState(() {
        _busy = false;
        _error = err;
      });
      try {
        await _ctl.start();
      } catch (_) {}
      return;
    }
    widget.onConnected();
  }

  @override
  Widget build(BuildContext context) {
    if (!_scanning) return _intro();
    return Scaffold(
      backgroundColor: cBg,
      appBar: AppBar(
          backgroundColor: cPanel,
          title: const Text('Scan your laptop'),
          leading: IconButton(
              icon: const Icon(Icons.arrow_back, color: cText),
              onPressed: () => setState(() {
                    _scanning = false;
                    _error = null;
                  })),
      ),
      body: Column(children: [
        Padding(
          padding: const EdgeInsets.all(16),
          child: Text(
            'On your laptop, open CAPHY → Settings → Connect Phone, then point '
            'your camera at the QR code shown there.',
            style: const TextStyle(color: cMuted, fontSize: 13, height: 1.5),
          ),
        ),
        Expanded(
          child: Stack(fit: StackFit.expand, children: [
            MobileScanner(controller: _ctl, onDetect: _onDetect),
            Center(
              child: Container(
                width: 240,
                height: 240,
                decoration: BoxDecoration(
                  border:
                      Border.all(color: _error != null ? cRed : cTeal, width: 3),
                  borderRadius: BorderRadius.circular(16),
                ),
              ),
            ),
            if (_busy)
              Container(
                color: Colors.black54,
                child: const Center(
                    child: CircularProgressIndicator(color: cTeal)),
              ),
          ]),
        ),
        if (_error != null)
          Padding(
            padding: const EdgeInsets.all(16),
            child: Text(_error!,
                textAlign: TextAlign.center,
                style: const TextStyle(color: cRed, fontSize: 13)),
          ),
      ]),
    );
  }

  Widget _intro() {
    return Scaffold(
      backgroundColor: cBg,
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(28),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const Spacer(),
              const Center(child: CaphyLogo(size: 64)),
              const SizedBox(height: 16),
              const Center(
                child: Text('CAPHY',
                    style: TextStyle(
                        fontSize: 26,
                        fontWeight: FontWeight.bold,
                        letterSpacing: 3,
                        color: cText)),
              ),
              const SizedBox(height: 4),
              const Center(
                child: Text('AI SECURITY',
                    style: TextStyle(
                        fontSize: 11, letterSpacing: 3, color: cTeal2)),
              ),
              const SizedBox(height: 40),
              const Icon(Icons.qr_code_scanner, color: cTeal, size: 72),
              const SizedBox(height: 20),
              const Text('Connect to your laptop',
                  textAlign: TextAlign.center,
                  style: TextStyle(
                      color: cText, fontSize: 19, fontWeight: FontWeight.w600)),
              const SizedBox(height: 10),
              const Text(
                'No passwords. On the laptop running CAPHY, sign in once, then '
                'open Settings → Connect Phone and scan the QR code with this '
                'app. You\'ll be signed in and connected in one step.',
                textAlign: TextAlign.center,
                style: TextStyle(color: cMuted, fontSize: 13, height: 1.5),
              ),
              const Spacer(),
              SizedBox(
                height: 52,
                child: FilledButton.icon(
                  style: FilledButton.styleFrom(
                      backgroundColor: cTeal,
                      shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(12))),
                  onPressed: () => setState(() {
                    _scanning = true;
                    _error = null;
                  }),
                  icon: const Icon(Icons.qr_code_scanner, color: Colors.black),
                  label: const Text('Scan QR code',
                      style: TextStyle(
                          color: Colors.black,
                          fontSize: 15,
                          fontWeight: FontWeight.bold)),
                ),
              ),
              const SizedBox(height: 16),
            ],
          ),
        ),
      ),
    );
  }
}

/// Sits between login and the dashboard. Authentication and device pairing
/// are deliberately two different questions - a valid login says nothing
/// about whether this account has ever connected a CAPHY desktop. This
/// widget answers that second question (via Api.myDevices(), a direct
/// Firestore read that needs no laptop reachable at all) and routes to
/// either the onboarding screen or straight into the dashboard.
class DeviceGate extends StatefulWidget {
  final VoidCallback onLogout;
  const DeviceGate({super.key, required this.onLogout});
  @override
  State<DeviceGate> createState() => _DeviceGateState();
}

class _DeviceGateState extends State<DeviceGate> {
  bool _checking = true;
  bool _hasDevice = false;

  @override
  void initState() {
    super.initState();
    _check();
  }

  Future<void> _check() async {
    Api.lastError = '';
    final devices = await Api.myDevices();
    // BUG FIX: myDevices() is a Firestore read - offline (or any other
    // network hiccup), it can't reach Firestore, catches the error, and
    // returns an EMPTY list, indistinguishable on its own from "this
    // account genuinely has zero paired devices". That made DeviceGate
    // send an already-paired phone back to the "scan to pair" onboarding
    // screen every time it lost signal, even though nothing about the
    // pairing had actually changed - then flip back once signal returned,
    // which looked exactly like "goes back to scanning, comes back signed
    // in later". myDevices() already records WHY it came back empty in
    // Api.lastError when it's a real fetch failure (vs. a legitimately
    // empty, no-error result) - if we have a remembered device from a
    // previous successful pairing (Store.lastDeviceId) AND this call
    // failed rather than genuinely returning zero, trust that we're still
    // paired and just couldn't refresh the list right now.
    final fetchFailed = Api.lastError.isNotEmpty;
    final hasDevice = devices.isNotEmpty ||
        (fetchFailed && Store.lastDeviceId != null);
    if (devices.isNotEmpty) {
      await Api.reconnectToPairedDevice();
    }
    if (!mounted) return;
    setState(() {
      _hasDevice = hasDevice;
      _checking = false;
    });
  }

  void _onPaired() {
    setState(() {
      _hasDevice = true;
      _checking = false;
    });
  }

  @override
  Widget build(BuildContext context) {
    if (_checking) {
      return const Scaffold(
        backgroundColor: cBg,
        body: Center(child: CircularProgressIndicator(color: cTeal)),
      );
    }
    if (!_hasDevice) {
      return OnboardingScreen(onLogout: widget.onLogout, onPaired: _onPaired);
    }
    return HomeShell(onLogout: widget.onLogout);
  }
}

/// "No CAPHY system has been connected to your account." Shown whenever a
/// valid, logged-in account has zero paired desktops - a brand-new user,
/// or someone signing in on a fresh phone for an account whose desktop
/// they haven't paired THIS device to yet. This must never look like an
/// error: it's the expected first-run state for any commercial smart-home
/// app before hardware (here: the laptop) is connected.
class OnboardingScreen extends StatelessWidget {
  final VoidCallback onLogout;
  final VoidCallback onPaired;
  const OnboardingScreen(
      {super.key, required this.onLogout, required this.onPaired});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: cBg,
      appBar: AppBar(
        backgroundColor: cPanel,
        title: const Text('Welcome to CAPHY'),
        actions: [
          IconButton(
              onPressed: onLogout,
              icon: const Icon(Icons.logout, color: cMuted))
        ],
      ),
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            children: [
              const Spacer(),
              const Icon(Icons.shield_outlined, color: cTeal, size: 72),
              const SizedBox(height: 20),
              const Text('No CAPHY system has been connected to your account.',
                  textAlign: TextAlign.center,
                  style: TextStyle(
                      color: cText, fontSize: 18, fontWeight: FontWeight.w600)),
              const SizedBox(height: 10),
              const Text(
                  'CAPHY runs its AI directly on your laptop\'s webcam - '
                  'no separate camera hardware needed. Connect one to get started.',
                  textAlign: TextAlign.center,
                  style: TextStyle(color: cMuted, fontSize: 13, height: 1.5)),
              const Spacer(),
              SizedBox(
                width: double.infinity,
                child: FilledButton.icon(
                  style: FilledButton.styleFrom(
                      backgroundColor: cTeal,
                      padding: const EdgeInsets.symmetric(vertical: 16)),
                  onPressed: () async {
                    final ok = await Navigator.of(context).push<bool>(
                        MaterialPageRoute(builder: (_) => const ConnectDeviceScreen()));
                    if (ok == true) onPaired();
                  },
                  icon: const Icon(Icons.qr_code_scanner, color: Colors.black),
                  label: const Text('Connect Existing Desktop',
                      style: TextStyle(
                          color: Colors.black, fontWeight: FontWeight.bold)),
                ),
              ),
              const SizedBox(height: 12),
              SizedBox(
                width: double.infinity,
                child: OutlinedButton.icon(
                  style: OutlinedButton.styleFrom(
                      side: const BorderSide(color: cLine),
                      padding: const EdgeInsets.symmetric(vertical: 16)),
                  onPressed: () => _showSetupGuide(context),
                  icon: const Icon(Icons.download_outlined, color: cText),
                  label: const Text('Set Up New Desktop',
                      style: TextStyle(color: cText, fontWeight: FontWeight.bold)),
                ),
              ),
              const SizedBox(height: 12),
              TextButton(
                onPressed: () => _showHowItWorks(context),
                child: const Text('Learn How CAPHY Works',
                    style: TextStyle(color: cMuted)),
              ),
              const SizedBox(height: 12),
            ],
          ),
        ),
      ),
    );
  }

  void _showSetupGuide(BuildContext context) {
    showDialog(
      context: context,
      builder: (_) => AlertDialog(
        backgroundColor: cPanel,
        title: const Text('Set Up a New Desktop', style: TextStyle(color: cText)),
        content: const Text(
            '1. Install CAPHY Desktop on the Windows laptop that will run '
            'your cameras.\n\n'
            '2. Open it and sign in with this same account.\n\n'
            '3. In the desktop app, go to Settings → Connect Device to '
            'generate a QR code.\n\n'
            '4. Come back here and tap "Connect Existing Desktop" to scan it.',
            style: TextStyle(color: cMuted, height: 1.6)),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(context),
              child: const Text('Got it', style: TextStyle(color: cTeal))),
        ],
      ),
    );
  }

  void _showHowItWorks(BuildContext context) {
    showDialog(
      context: context,
      builder: (_) => AlertDialog(
        backgroundColor: cPanel,
        title: const Text('How CAPHY Works', style: TextStyle(color: cText)),
        content: const Text(
            'CAPHY turns your laptop into a smart security camera. Its '
            'built-in webcam watches for motion, confirms it\'s really a '
            'person using on-device AI (Edge AI - your video never has to '
            'leave your laptop to be analyzed), then estimates how close '
            'they are before deciding whether to just log it, snapshot it, '
            'or trigger a full alert with siren and video.\n\n'
            'This app is the remote control: view alerts, arm/disarm, '
            'watch live video, and get instant push notifications - all '
            'tied to your account, from anywhere.',
            style: TextStyle(color: cMuted, height: 1.6)),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(context),
              child: const Text('Got it', style: TextStyle(color: cTeal))),
        ],
      ),
    );
  }
}

// ==================== Login ====================
class LoginScreen extends StatefulWidget {
  final VoidCallback onLogin;
  // Set by CaphyApp when the app was opened via the emailed magic sign-in
  // link. When present, LoginScreen shows a "completing sign-in" state
  // instead of the normal form.
  final String? pendingSignInLink;
  const LoginScreen({super.key, required this.onLogin, this.pendingSignInLink});
  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  final _email = TextEditingController();
  final _pass = TextEditingController();
  final _passConfirm = TextEditingController();
  final _url = TextEditingController(text: Store.baseUrl);
  final _linkEmail = TextEditingController();  // used only for email-link mode

  bool _busy = false;
  bool _signUpMode = false;
  bool _showPass = false;         // show/hide toggle for the PASSWORD field
  bool _showPassConfirm = false;  // show/hide toggle for CONFIRM PASSWORD
  bool _emailLinkMode = false;   // showing the "email me a link" form
  bool _emailLinkSent = false;   // link sent, waiting for the user to tap it
  // Login/signup now go straight to Firebase (see api.dart) - no laptop IP
  // needed for that. The address is only needed afterward, for local
  // features (live view, arm/disarm), so it's collapsed by default again
  // and only nudged open if it's still empty once the user reaches the
  // dashboard's local features.
  bool _showServerField = !Store.hasServerAddress;
  String _err = '';

  @override
  void initState() {
    super.initState();
    if (widget.pendingSignInLink != null) {
      _emailLinkMode = true;
      // If we already know which email requested this link (same device,
      // same app install), complete sign-in immediately with no extra
      // taps. Otherwise fall through to asking the user to re-enter it.
      final knownEmail = Store.pendingEmailLinkAddress;
      if (knownEmail != null) {
        WidgetsBinding.instance
            .addPostFrameCallback((_) => _completeEmailLinkSignIn(knownEmail));
      }
    }
  }

  @override
  void didUpdateWidget(LoginScreen old) {
    super.didUpdateWidget(old);
    // App was already showing LoginScreen when the link arrived (warm
    // resume case) - CaphyApp rebuilds us with the new link.
    if (widget.pendingSignInLink != null &&
        widget.pendingSignInLink != old.pendingSignInLink) {
      setState(() => _emailLinkMode = true);
      final knownEmail = Store.pendingEmailLinkAddress;
      if (knownEmail != null) _completeEmailLinkSignIn(knownEmail);
    }
  }

  Future<void> _completeEmailLinkSignIn(String email) async {
    if (widget.pendingSignInLink == null) return;
    setState(() {
      _busy = true;
      _err = '';
    });
    final err = await Api.completeSignInWithLink(
        widget.pendingSignInLink!, emailOverride: email);
    if (!mounted) return;
    if (err == null) {
      widget.onLogin();
    } else {
      setState(() {
        _busy = false;
        _err = err;
      });
    }
  }

  Future<void> _sendEmailLink() async {
    if (_linkEmail.text.trim().isEmpty) {
      setState(() => _err = 'Enter your email first');
      return;
    }
    setState(() {
      _busy = true;
      _err = '';
    });
    final err = await Api.sendSignInLink(_linkEmail.text.trim());
    if (!mounted) return;
    setState(() {
      _busy = false;
      if (err == null) {
        _emailLinkSent = true;
      } else {
        _err = err;
      }
    });
  }

  // serverClientId MUST be the Web OAuth client (client_type: 3 in
  // google-services.json / the "Web application" client from Google Cloud
  // Console), NOT an Android client. Android's GoogleSignIn needs this to
  // know which server the idToken should be valid for - without it,
  // signIn() can complete the account picker but hand back a null or
  // unusable idToken, which is exactly what caused ApiException: 10 here.
  // Same Web Client ID already used for the web dashboard's Google button
  // (config.py's GOOGLE_CLIENT_ID).
  static const _webClientId =
      '790179915609-sujeq75jbavgsekof1vsk92prqiqpes9.apps.googleusercontent.com';

  final _google = GoogleSignIn(
    scopes: ['email'],
    serverClientId: _webClientId,
  );

  Future<void> _submitEmail() async {
    setState(() {
      _busy = true;
      _err = '';
    });
    // Server address is saved if entered, but no longer REQUIRED here -
    // login/signup go straight to Firebase now (see api.dart). It's only
    // needed once the user reaches local features (live view, arm/disarm).
    if (_url.text.trim().isNotEmpty) Store.baseUrl = _url.text;

    if (_signUpMode) {
      if (_pass.text.length < 6) {
        setState(() {
          _busy = false;
          _err = 'Password must be at least 6 characters';
        });
        return;
      }
      if (_pass.text != _passConfirm.text) {
        setState(() {
          _busy = false;
          _err = 'Passwords do not match';
        });
        return;
      }
      final signupErr = await Api.signup(_email.text.trim(), _pass.text);
      if (!mounted) return;
      if (signupErr == null) {
        // Api.signup already left Store.token set (Firebase signs the user
        // in immediately on account creation) - go straight in, no separate
        // login step needed.
        widget.onLogin();
        return;
      }
      setState(() {
        _busy = false;
        _err = signupErr;
      });
      return;
    }

    final loginErr = await Api.login(_email.text.trim(), _pass.text);
    if (!mounted) return;
    if (loginErr == null) {
      widget.onLogin();
    } else {
      setState(() {
        _busy = false;
        _err = loginErr;
      });
    }
  }

  Future<void> _submitGoogle() async {
    setState(() {
      _busy = true;
      _err = '';
    });
    if (_url.text.trim().isNotEmpty) Store.baseUrl = _url.text;

    try {
      // Clear any stale cached session first - a half-broken previous
      // attempt (e.g. from before serverClientId was set correctly) can
      // otherwise get silently reused and keep failing the same way.
      await _google.signOut();

      final account = await _google.signIn();
      if (account == null) {
        // user cancelled the picker
        setState(() => _busy = false);
        return;
      }
      final auth = await account.authentication;
      final idToken = auth.idToken;
      if (idToken == null) {
        setState(() {
          _busy = false;
          _err = 'Google did not return an ID token. '
              'Confirm serverClientId is the Web client, not Android.';
        });
        return;
      }

      final result = await Api.loginWithGoogleToken(idToken);
      if (!mounted) return;
      if (result != null) {
        widget.onLogin();
        // Same "set a password" nudge the web dashboard shows first-time
        // Google users - keeps both login methods usable for this account.
        if (result['needs_password'] == true) {
          _showSetPasswordHint();
        }
      } else {
        setState(() {
          _busy = false;
          _err = Api.lastError.isNotEmpty
              ? Api.lastError
              : 'Google sign-in failed - please try again.';
        });
      }
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _busy = false;
        _err = 'Google sign-in error: $e';
      });
    }
  }

  void _showSetPasswordHint() {
    // Non-blocking - same "Skip for now" spirit as the web's /set-password
    // page. Full password-setting UI can live in me_tab.dart later; for now
    // this just makes sure the user knows the option exists.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
        content: Text('Signed in with Google. '
            'Add a password anytime in Settings to also sign in by email.'),
        duration: Duration(seconds: 4),
      ));
    });
  }

  InputDecoration _fieldDecoration(String hint) => InputDecoration(
        hintText: hint,
        hintStyle: const TextStyle(color: cDim, fontSize: 13.5),
        filled: true,
        fillColor: cBg,
        contentPadding: const EdgeInsets.symmetric(horizontal: 14, vertical: 13),
        border: OutlineInputBorder(
            borderRadius: BorderRadius.circular(10),
            borderSide: const BorderSide(color: cLine)),
        enabledBorder: OutlineInputBorder(
            borderRadius: BorderRadius.circular(10),
            borderSide: const BorderSide(color: cLine)),
        focusedBorder: OutlineInputBorder(
            borderRadius: BorderRadius.circular(10),
            borderSide: const BorderSide(color: cTeal, width: 1.4)),
      );

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: cBg,
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.symmetric(horizontal: 22, vertical: 28),
            child: Container(
              constraints: const BoxConstraints(maxWidth: 400),
              padding: const EdgeInsets.fromLTRB(24, 30, 24, 24),
              decoration: BoxDecoration(
                  color: cPanel2,
                  borderRadius: BorderRadius.circular(18),
                  border: Border.all(color: cLine)),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  const Center(child: CaphyLogo(size: 56)),
                  const SizedBox(height: 12),
                  const Center(
                      child: Text('CAPHY',
                          style: TextStyle(
                              fontSize: 25,
                              fontWeight: FontWeight.bold,
                              letterSpacing: 3,
                              color: cText))),
                  const SizedBox(height: 2),
                  const Center(
                      child: Text('AI SECURITY',
                          style: TextStyle(
                              fontSize: 10.5, letterSpacing: 3, color: cTeal2))),
                  const SizedBox(height: 28),

                  if (_emailLinkMode) ..._buildEmailLinkForm() else ...[

                  Center(
                    child: Text(_signUpMode ? 'Create your account' : 'Welcome back',
                        style: const TextStyle(
                            color: cText,
                            fontWeight: FontWeight.w700,
                            fontSize: 16)),
                  ),
                  const SizedBox(height: 18),

                  // ---- Google Sign-In (primary, matches the web dashboard) ----
                  SizedBox(
                    height: 48,
                    child: OutlinedButton(
                      onPressed: _busy ? null : _submitGoogle,
                      style: OutlinedButton.styleFrom(
                          backgroundColor: Colors.white,
                          side: BorderSide.none,
                          shape: RoundedRectangleBorder(
                              borderRadius: BorderRadius.circular(10)),
                          padding: const EdgeInsets.symmetric(horizontal: 12)),
                      child: Row(
                        mainAxisAlignment: MainAxisAlignment.center,
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          const _GoogleLogo(size: 18),
                          const SizedBox(width: 10),
                          Text(
                            _busy ? 'Signing in...' : 'Continue with Google',
                            style: const TextStyle(
                                color: Color(0xFF3C4043),
                                fontSize: 14,
                                fontWeight: FontWeight.w600),
                          ),
                        ],
                      ),
                    ),
                  ),

                  const SizedBox(height: 18),
                  Row(children: [
                    Expanded(child: Divider(color: cLine.withValues(alpha: 0.7))),
                    Padding(
                        padding: const EdgeInsets.symmetric(horizontal: 12),
                        child: Text('OR',
                            style: TextStyle(
                                color: cMuted.withValues(alpha: 0.8),
                                fontSize: 10.5,
                                letterSpacing: 1))),
                    Expanded(child: Divider(color: cLine.withValues(alpha: 0.7))),
                  ]),
                  const SizedBox(height: 18),

                  // ---- Email + password ----
                  _label('EMAIL'),
                  TextField(
                      controller: _email,
                      keyboardType: TextInputType.emailAddress,
                      style: const TextStyle(color: cText, fontSize: 14),
                      decoration: _fieldDecoration('you@example.com')),
                  const SizedBox(height: 14),
                  _label('PASSWORD'),
                  TextField(
                      controller: _pass,
                      obscureText: !_showPass,
                      style: const TextStyle(color: cText, fontSize: 14),
                      decoration: _fieldDecoration(
                              _signUpMode ? 'Min. 6 characters' : '••••••••')
                          .copyWith(
                        suffixIcon: IconButton(
                          icon: Icon(
                              _showPass
                                  ? Icons.visibility_off_outlined
                                  : Icons.visibility_outlined,
                              color: cMuted,
                              size: 20),
                          onPressed: () =>
                              setState(() => _showPass = !_showPass),
                        ),
                      )),
                  if (_signUpMode) ...[
                    const SizedBox(height: 14),
                    _label('CONFIRM PASSWORD'),
                    TextField(
                        controller: _passConfirm,
                        obscureText: !_showPassConfirm,
                        style: const TextStyle(color: cText, fontSize: 14),
                        decoration: _fieldDecoration('••••••••').copyWith(
                          suffixIcon: IconButton(
                            icon: Icon(
                                _showPassConfirm
                                    ? Icons.visibility_off_outlined
                                    : Icons.visibility_outlined,
                                color: cMuted,
                                size: 20),
                            onPressed: () => setState(
                                () => _showPassConfirm = !_showPassConfirm),
                          ),
                        )),
                  ],

                  if (_err.isNotEmpty)
                    Padding(
                        padding: const EdgeInsets.only(top: 14),
                        child: Container(
                          padding: const EdgeInsets.symmetric(
                              horizontal: 12, vertical: 10),
                          decoration: BoxDecoration(
                              color: cRed.withValues(alpha: 0.1),
                              borderRadius: BorderRadius.circular(8),
                              border: Border.all(
                                  color: cRed.withValues(alpha: 0.35))),
                          child: Text(_err,
                              style: const TextStyle(
                                  color: cRed, fontSize: 12.5, height: 1.3)),
                        )),

                  const SizedBox(height: 20),
                  SizedBox(
                    height: 48,
                    child: FilledButton(
                      style: FilledButton.styleFrom(
                          backgroundColor: cTeal,
                          shape: RoundedRectangleBorder(
                              borderRadius: BorderRadius.circular(10))),
                      onPressed: _busy ? null : _submitEmail,
                      child: _busy
                          ? const SizedBox(
                              height: 18,
                              width: 18,
                              child: CircularProgressIndicator(
                                  strokeWidth: 2, color: Colors.black))
                          : Text(
                              _signUpMode ? 'Create Account' : 'Sign In',
                              style: const TextStyle(
                                  color: Colors.black,
                                  fontSize: 14.5,
                                  fontWeight: FontWeight.bold)),
                    ),
                  ),

                  const SizedBox(height: 16),
                  Center(
                    child: TextButton(
                      onPressed: _busy
                          ? null
                          : () => setState(() {
                                _signUpMode = !_signUpMode;
                                _err = '';
                              }),
                      style: TextButton.styleFrom(
                          padding: const EdgeInsets.symmetric(
                              horizontal: 8, vertical: 4),
                          minimumSize: Size.zero,
                          tapTargetSize: MaterialTapTargetSize.shrinkWrap),
                      child: Text(
                        _signUpMode
                            ? 'Already have an account?  Sign in'
                            : "Don't have an account?  Sign up",
                        style: const TextStyle(color: cTeal2, fontSize: 12.5),
                      ),
                    ),
                  ),

                  const SizedBox(height: 4),
                  Center(
                    child: TextButton(
                      onPressed: _busy
                          ? null
                          : () => setState(() {
                                _emailLinkMode = true;
                                _emailLinkSent = false;
                                _err = '';
                                _linkEmail.text = _email.text;
                              }),
                      style: TextButton.styleFrom(
                          padding: const EdgeInsets.symmetric(
                              horizontal: 8, vertical: 4),
                          minimumSize: Size.zero,
                          tapTargetSize: MaterialTapTargetSize.shrinkWrap),
                      child: const Text(
                        'Or sign in with an email link (no password)',
                        style: TextStyle(color: cMuted, fontSize: 11.5),
                      ),
                    ),
                  ),

                  ], // end of the non-email-link form branch

                  const SizedBox(height: 6),
                  Divider(color: cLine.withValues(alpha: 0.5), height: 1),
                  const SizedBox(height: 10),

                  // ---- Server address (advanced, tucked away) ----
                  // Still needed for local Wi-Fi live view/control, which
                  // talks straight to the laptop's Flask server rather than
                  // through Firebase - but not something most users ever
                  // need to touch, so it's hidden by default.
                  Center(
                    child: TextButton.icon(
                      onPressed: _busy
                          ? null
                          : () => setState(() => _showServerField = !_showServerField),
                      style: TextButton.styleFrom(
                          padding: const EdgeInsets.symmetric(
                              horizontal: 8, vertical: 4),
                          minimumSize: Size.zero,
                          tapTargetSize: MaterialTapTargetSize.shrinkWrap),
                      icon: Icon(
                          _showServerField
                              ? Icons.expand_less
                              : Icons.expand_more,
                          size: 16,
                          color: cMuted),
                      label: const Text('Advanced: server address',
                          style: TextStyle(color: cMuted, fontSize: 11.5)),
                    ),
                  ),
                  if (_showServerField) ...[
                    const SizedBox(height: 10),
                    _label('SERVER ADDRESS (LOCAL WI-FI)'),
                    TextField(
                        controller: _url,
                        style: const TextStyle(color: cText, fontSize: 13),
                        decoration:
                            _fieldDecoration('http://192.168.1.10:5000')),
                  ],
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }

  /// Passwordless email-link form. Three states:
  ///   1. Enter email, tap "Send sign-in link"
  ///   2. "Check your email" waiting state
  ///   3. (Automatic, no UI) - app reopens via the link, completes sign-in
  List<Widget> _buildEmailLinkForm() {
    if (widget.pendingSignInLink != null && Store.pendingEmailLinkAddress == null) {
      // Link opened on a different device/install than the one that sent
      // it - Firebase requires re-confirming the email in this case.
      return [
        const Text('Confirm your email',
            style: TextStyle(color: cText, fontWeight: FontWeight.w700, fontSize: 16)),
        const SizedBox(height: 8),
        const Text(
          'This sign-in link was opened on a different device. '
          'Enter the same email you used to request it.',
          style: TextStyle(color: cMuted, fontSize: 12.5, height: 1.4),
        ),
        const SizedBox(height: 16),
        _label('EMAIL'),
        TextField(
            controller: _linkEmail,
            keyboardType: TextInputType.emailAddress,
            style: const TextStyle(color: cText, fontSize: 14),
            decoration: _fieldDecoration('you@example.com')),
        if (_err.isNotEmpty) _errorBox(),
        const SizedBox(height: 20),
        SizedBox(
          height: 48,
          child: FilledButton(
            style: FilledButton.styleFrom(
                backgroundColor: cTeal,
                shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10))),
            onPressed: _busy
                ? null
                : () => _completeEmailLinkSignIn(_linkEmail.text.trim()),
            child: _busy
                ? const SizedBox(
                    height: 18,
                    width: 18,
                    child: CircularProgressIndicator(strokeWidth: 2, color: Colors.black))
                : const Text('Continue',
                    style: TextStyle(
                        color: Colors.black, fontSize: 14.5, fontWeight: FontWeight.bold)),
          ),
        ),
      ];
    }

    if (_emailLinkSent) {
      return [
        const Icon(Icons.mark_email_read_outlined, color: cTeal2, size: 40),
        const SizedBox(height: 14),
        const Text('Check your email',
            style: TextStyle(color: cText, fontWeight: FontWeight.w700, fontSize: 16)),
        const SizedBox(height: 8),
        Text(
          'We sent a sign-in link to ${_linkEmail.text.trim()}. '
          'Open it on this phone to finish signing in.',
          style: const TextStyle(color: cMuted, fontSize: 12.5, height: 1.4),
        ),
        const SizedBox(height: 20),
        Center(
          child: TextButton(
            onPressed: () => setState(() {
              _emailLinkMode = false;
              _emailLinkSent = false;
              _err = '';
            }),
            child: const Text('Back to sign in',
                style: TextStyle(color: cTeal2, fontSize: 12.5)),
          ),
        ),
      ];
    }

    return [
      const Text('Sign in with email link',
          style: TextStyle(color: cText, fontWeight: FontWeight.w700, fontSize: 16)),
      const SizedBox(height: 8),
      const Text(
        "No password needed - we'll email you a link to sign in.",
        style: TextStyle(color: cMuted, fontSize: 12.5, height: 1.4),
      ),
      const SizedBox(height: 16),
      _label('EMAIL'),
      TextField(
          controller: _linkEmail,
          keyboardType: TextInputType.emailAddress,
          style: const TextStyle(color: cText, fontSize: 14),
          decoration: _fieldDecoration('you@example.com')),
      if (_err.isNotEmpty) _errorBox(),
      const SizedBox(height: 20),
      SizedBox(
        height: 48,
        child: FilledButton(
          style: FilledButton.styleFrom(
              backgroundColor: cTeal,
              shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10))),
          onPressed: _busy ? null : _sendEmailLink,
          child: _busy
              ? const SizedBox(
                  height: 18,
                  width: 18,
                  child: CircularProgressIndicator(strokeWidth: 2, color: Colors.black))
              : const Text('Send sign-in link',
                  style: TextStyle(
                      color: Colors.black, fontSize: 14.5, fontWeight: FontWeight.bold)),
        ),
      ),
      const SizedBox(height: 14),
      Center(
        child: TextButton(
          onPressed: _busy
              ? null
              : () => setState(() {
                    _emailLinkMode = false;
                    _err = '';
                  }),
          child: const Text('Back to sign in',
              style: TextStyle(color: cTeal2, fontSize: 12.5)),
        ),
      ),
    ];
  }

  Widget _errorBox() => Padding(
      padding: const EdgeInsets.only(top: 14),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
        decoration: BoxDecoration(
            color: cRed.withValues(alpha: 0.1),
            borderRadius: BorderRadius.circular(8),
            border: Border.all(color: cRed.withValues(alpha: 0.35))),
        child: Text(_err,
            style: const TextStyle(color: cRed, fontSize: 12.5, height: 1.3)),
      ));

  Widget _label(String t) => Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Text(t,
          style: const TextStyle(
              color: cMuted, fontSize: 10.5, letterSpacing: 0.5)));
}

/// Real 4-color Google "G" logo, drawn with CustomPaint - no image asset
/// needed, no janky single-letter placeholder. Matches Google's brand mark.
class _GoogleLogo extends StatelessWidget {
  final double size;
  const _GoogleLogo({this.size = 18});
  @override
  Widget build(BuildContext context) =>
      SizedBox(width: size, height: size, child: CustomPaint(painter: _GoogleLogoPainter()));
}

class _GoogleLogoPainter extends CustomPainter {
  @override
  void paint(Canvas canvas, Size size) {
    final s = size.width;
    final r = s / 2;
    final center = Offset(r, r);
    final strokeWidth = s * 0.22;
    final radius = r - strokeWidth / 2;

    Paint arcPaint(Color c) => Paint()
      ..color = c
      ..style = PaintingStyle.stroke
      ..strokeWidth = strokeWidth
      ..strokeCap = StrokeCap.butt;

    // Four arcs approximating the Google "G" mark colors.
    canvas.drawArc(Rect.fromCircle(center: center, radius: radius),
        _deg(-45), _deg(90), false, arcPaint(const Color(0xFF4285F4))); // blue
    canvas.drawArc(Rect.fromCircle(center: center, radius: radius),
        _deg(45), _deg(90), false, arcPaint(const Color(0xFF34A853))); // green
    canvas.drawArc(Rect.fromCircle(center: center, radius: radius),
        _deg(135), _deg(90), false, arcPaint(const Color(0xFFFBBC05))); // yellow
    canvas.drawArc(Rect.fromCircle(center: center, radius: radius),
        _deg(225), _deg(70), false, arcPaint(const Color(0xFFEA4335))); // red

    // Horizontal bar (the crossbar of the "G").
    final barPaint = Paint()..color = const Color(0xFF4285F4);
    canvas.drawRect(
        Rect.fromLTWH(r, r - strokeWidth / 2, r * 0.92, strokeWidth), barPaint);
  }

  double _deg(double d) => d * (3.1415926535 / 180);

  @override
  bool shouldRepaint(covariant CustomPainter oldDelegate) => false;
}

// ==================== Shell (4 tabs) ====================
class HomeShell extends StatefulWidget {
  final VoidCallback onLogout;
  const HomeShell({super.key, required this.onLogout});
  @override
  State<HomeShell> createState() => _HomeShellState();
}

class _HomeShellState extends State<HomeShell> {
  int _i = 0;
  Timer? _alertPoll;
  Timer? _connPoll;
  int _lastSeenId = 0;      // highest alert id we've already popped
  bool _popupOpen = false;  // don't stack popups
  bool _primed = false;     // skip the first poll so old alerts don't pop

  @override
  void initState() {
    super.initState();
    // Poll for new alerts and pop them up automatically - works over local
    // Wi-Fi without needing Firebase. No manual refresh required.
    _alertPoll = Timer.periodic(const Duration(seconds: 3), (_) => _checkAlerts());
    _checkAlerts();

    // Auto-detect internet vs LAN-only vs offline, every few seconds, so the
    // app can switch to the local network the moment the internet drops and
    // tell the user (see ModeBanner).
    Api.refreshConnectivity();
    _connPoll = Timer.periodic(
        const Duration(seconds: 6), (_) => Api.refreshConnectivity());

    // If Firebase is set up, a tapped background notification still opens it.
    FirebaseMessaging.onMessageOpenedApp
        .listen((m) => _openAlert(m.data['alert_id']));
  }

  @override
  void dispose() {
    _alertPoll?.cancel();
    _connPoll?.cancel();
    super.dispose();
  }

  Future<void> _checkAlerts() async {
    final list = await Api.alerts(limit: 5);
    if (!mounted || list.isEmpty) return;
    final newest = list.first;
    final id = asInt(newest['id']);
    if (!_primed) {
      // first run: remember the newest without popping, so we only pop alerts
      // that arrive AFTER the app opened.
      _primed = true;
      _lastSeenId = id;
      return;
    }
    if (id > _lastSeenId && !_popupOpen) {
      _lastSeenId = id;
      // Alert-fatigue: only Tier 3 pops an in-app banner. Tier 1 & 2 are
      // still saved and visible in the Alerts tab, they just don't interrupt.
      if (asInt(newest['tier'], 1) >= 3) {
        _showAlertBanner(newest);
      }
    }
  }

  /// Non-blocking banner at the TOP of the screen. The app stays fully usable
  /// while it's up. Auto-dismisses; has Acknowledge and View.
  void _showAlertBanner(dynamic a) {
    final id = asInt(a['id']);
    final tier = asInt(a['tier'], 1);
    final overlay = Overlay.of(context);
    _popupOpen = true;

    late OverlayEntry entry;
    Timer? auto;
    void close() {
      auto?.cancel();
      if (entry.mounted) entry.remove();
      _popupOpen = false;
    }

    entry = OverlayEntry(
      builder: (ctx) => Positioned(
        top: MediaQuery.of(ctx).padding.top + 10,
        left: 12,
        right: 12,
        child: Material(
          color: Colors.transparent,
          child: Container(
            padding: const EdgeInsets.fromLTRB(14, 12, 10, 12),
            decoration: BoxDecoration(
              color: cPanel,
              borderRadius: BorderRadius.circular(14),
              border: Border.all(color: tierColor(tier)),
              boxShadow: [
                BoxShadow(
                    color: Colors.black.withValues(alpha: 0.45),
                    blurRadius: 16,
                    offset: const Offset(0, 5))
              ],
            ),
            child: Row(children: [
              Icon(Icons.warning_amber, color: tierColor(tier), size: 22),
              const SizedBox(width: 10),
              Expanded(
                child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text('New Alert · Tier $tier',
                          style: const TextStyle(
                              color: cText,
                              fontWeight: FontWeight.w700,
                              fontSize: 13.5)),
                      Text(
                        'Person detected'
                        '${a['camera'] != null ? ' on ${a['camera']}' : ''}'
                        '${a['distance_m'] != null ? ' · ${a['distance_m']} m' : ''}',
                        style: const TextStyle(color: cMuted, fontSize: 12),
                      ),
                    ]),
              ),
              IconButton(
                onPressed: close,
                icon: const Icon(Icons.close, color: cMuted, size: 18),
                tooltip: 'Dismiss',
                constraints: const BoxConstraints(),
                padding: const EdgeInsets.all(6),
              ),
              const SizedBox(width: 2),
              FilledButton(
                onPressed: () {
                  close();
                  _openAlert(id);
                },
                style: FilledButton.styleFrom(
                    backgroundColor: cTeal,
                    minimumSize: const Size(0, 34),
                    padding: const EdgeInsets.symmetric(horizontal: 16)),
                child: const Text('View',
                    style: TextStyle(color: Colors.black, fontSize: 12.5)),
              ),
            ]),
          ),
        ),
      ),
    );
    overlay.insert(entry);
    auto = Timer(const Duration(seconds: 6), close);
  }

  void _openAlert(dynamic id) {
    final aid = int.tryParse('${id ?? ''}');
    if (aid == null || !mounted) return;
    Navigator.of(context).push(
        MaterialPageRoute(builder: (_) => AlertDetailScreen(id: aid)));
  }

  @override
  Widget build(BuildContext context) {
    final tabs = [
      const HomeTab(),
      const AlertsTab(),
      const LiveTab(),
      MeTab(onLogout: widget.onLogout),
    ];
    return Scaffold(
      body: SafeArea(
        bottom: false,
        child: Column(children: [
          // App-wide connectivity banner: auto-switches to LAN / shows
          // offline the moment internet drops (see ModeBanner).
          const ModeBanner(),
          Expanded(child: IndexedStack(index: _i, children: tabs)),
        ]),
      ),
      bottomNavigationBar: NavigationBar(
        backgroundColor: cPanel,
        indicatorColor: cTeal.withValues(alpha: 0.25),
        selectedIndex: _i,
        onDestinationSelected: (v) => setState(() => _i = v),
        destinations: const [
          NavigationDestination(icon: Icon(Icons.home_outlined), label: 'Home'),
          NavigationDestination(
              icon: Icon(Icons.notifications_outlined), label: 'Alerts'),
          NavigationDestination(
              icon: Icon(Icons.videocam_outlined), label: 'Live'),
          NavigationDestination(icon: Icon(Icons.person_outline), label: 'Me'),
        ],
      ),
    );
  }
}
