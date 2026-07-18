import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'api.dart';
import 'theme.dart';
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
  try {
    await Firebase.initializeApp();
    FirebaseMessaging.onBackgroundMessage(_bgHandler);
    await FirebaseMessaging.instance.requestPermission();
    await FirebaseMessaging.instance.subscribeToTopic('caphy_alerts');
  } catch (_) {
    // Firebase not set up / offline — the app still works over local Wi-Fi.
  }
  runApp(const CaphyApp());
}

class CaphyApp extends StatefulWidget {
  const CaphyApp({super.key});
  @override
  State<CaphyApp> createState() => _CaphyAppState();
}

class _CaphyAppState extends State<CaphyApp> {
  bool _loggedIn = Store.token != null;

  void _onLogin() => setState(() => _loggedIn = true);
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
          ? HomeShell(onLogout: _onLogout)
          : LoginScreen(onLogin: _onLogin),
    );
  }
}

// ==================== Login ====================
class LoginScreen extends StatefulWidget {
  final VoidCallback onLogin;
  const LoginScreen({super.key, required this.onLogin});
  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  final _url = TextEditingController(text: Store.baseUrl);
  final _user = TextEditingController(text: 'admin');
  final _pass = TextEditingController();
  bool _busy = false;
  String _err = '';

  Future<void> _submit() async {
    setState(() {
      _busy = true;
      _err = '';
    });
    Store.baseUrl = _url.text;
    final ok = await Api.login(_user.text.trim(), _pass.text);
    if (!mounted) return;
    if (ok) {
      widget.onLogin();
    } else {
      setState(() {
        _busy = false;
        _err = 'Login failed - check the server address, Wi-Fi, and password.';
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Center(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(28),
          child: Container(
            constraints: const BoxConstraints(maxWidth: 380),
            padding: const EdgeInsets.all(26),
            decoration: BoxDecoration(
                color: cPanel2,
                borderRadius: BorderRadius.circular(16),
                border: Border.all(color: cLine)),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                const Center(child: CaphyLogo(size: 56)),
                const SizedBox(height: 10),
                const Center(
                    child: Text('CAPHY',
                        style: TextStyle(
                            fontSize: 26,
                            fontWeight: FontWeight.bold,
                            letterSpacing: 3,
                            color: cText))),
                const Center(
                    child: Text('AI SECURITY',
                        style: TextStyle(
                            fontSize: 11, letterSpacing: 3, color: cTeal2))),
                const SizedBox(height: 24),
                _label('SERVER ADDRESS'),
                TextField(
                    controller: _url,
                    style: const TextStyle(color: cText),
                    decoration: const InputDecoration(
                        hintText: 'http://192.168.1.10:5000')),
                const SizedBox(height: 14),
                _label('USERNAME'),
                TextField(
                    controller: _user, style: const TextStyle(color: cText)),
                const SizedBox(height: 14),
                _label('PASSWORD'),
                TextField(
                    controller: _pass,
                    obscureText: true,
                    style: const TextStyle(color: cText)),
                if (_err.isNotEmpty)
                  Padding(
                      padding: const EdgeInsets.only(top: 12),
                      child: Text(_err,
                          style: const TextStyle(color: cRed, fontSize: 12))),
                const SizedBox(height: 20),
                FilledButton(
                  style: FilledButton.styleFrom(
                      backgroundColor: cTeal,
                      padding: const EdgeInsets.symmetric(vertical: 14)),
                  onPressed: _busy ? null : _submit,
                  child: _busy
                      ? const SizedBox(
                          height: 18,
                          width: 18,
                          child: CircularProgressIndicator(
                              strokeWidth: 2, color: Colors.black))
                      : const Text('Sign In',
                          style: TextStyle(
                              color: Colors.black,
                              fontWeight: FontWeight.bold)),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  Widget _label(String t) => Padding(
      padding: const EdgeInsets.only(bottom: 5),
      child: Text(t, style: const TextStyle(color: cMuted, fontSize: 11)));
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

  @override
  void initState() {
    super.initState();
    // foreground alert -> quick in-app banner, tap to open the alert
    FirebaseMessaging.onMessage.listen((m) {
      final n = m.notification;
      if (n != null && mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          backgroundColor: cPanel,
          content: Text('${n.title ?? "Alert"} — ${n.body ?? ""}',
              style: const TextStyle(color: cText)),
          action: SnackBarAction(
            label: 'View',
            textColor: cTeal2,
            onPressed: () => _openAlert(m.data['alert_id']),
          ),
        ));
      }
    });
    // tapped a notification while the app was in the background
    FirebaseMessaging.onMessageOpenedApp.listen((m) => _openAlert(m.data['alert_id']));
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
      body: IndexedStack(index: _i, children: tabs),
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
