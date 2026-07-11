import 'package:flutter/material.dart';
import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';

// ---------- theme colors ----------
const teal = Color(0xFF2A9D8F);
const tealBright = Color(0xFF3DD7C4);
const bg = Color(0xFF0D151C);
const panel = Color(0xFF14212B);
const card = Color(0xFF1A2A36);
const red = Color(0xFFE5484D);
const amber = Color(0xFFF4A261);

Color tierColor(String t) => t == '3' ? red : (t == '2' ? amber : teal);

// ---------- simple in-memory alert store ----------
class Alert {
  final String title, body, tier, distance, time;
  final String? imageUrl;
  Alert({required this.title, required this.body, required this.tier,
         required this.distance, required this.time, this.imageUrl});

  factory Alert.fromMessage(RemoteMessage m) {
    final d = m.data;
    final now = DateTime.now();
    final t = '${now.hour.toString().padLeft(2, '0')}:${now.minute.toString().padLeft(2, '0')}';
    return Alert(
      title: (m.notification?.title ?? d['title'] ?? 'CAPHY Alert').toString(),
      body: (m.notification?.body ?? d['body'] ?? '').toString(),
      tier: (d['tier'] ?? '').toString(),
      distance: (d['distance'] ?? '').toString(),
      time: t,
      imageUrl: d['image_url']?.toString(),
    );
  }
}

final ValueNotifier<List<Alert>> alertStore = ValueNotifier<List<Alert>>([]);
void addAlert(Alert a) => alertStore.value = [a, ...alertStore.value];

// ---------- Firebase background handler ----------
@pragma('vm:entry-point')
Future<void> _bgHandler(RemoteMessage message) async {
  await Firebase.initializeApp();
}

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await Firebase.initializeApp();
  FirebaseMessaging.onBackgroundMessage(_bgHandler);

  final messaging = FirebaseMessaging.instance;
  await messaging.requestPermission();
  await messaging.subscribeToTopic('caphy_alerts');
  final token = await messaging.getToken();
  debugPrint('FCM TOKEN: $token');

  FirebaseMessaging.onMessage.listen((m) => addAlert(Alert.fromMessage(m)));
  FirebaseMessaging.onMessageOpenedApp.listen((m) => addAlert(Alert.fromMessage(m)));

  runApp(const CaphyApp());
}

class CaphyApp extends StatelessWidget {
  const CaphyApp({super.key});
  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'CAPHY',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        useMaterial3: true,
        brightness: Brightness.dark,
        scaffoldBackgroundColor: bg,
        colorScheme: const ColorScheme.dark(primary: teal, secondary: tealBright),
      ),
      home: const LoginScreen(),
    );
  }
}

// ---------- Login ----------
class LoginScreen extends StatelessWidget {
  const LoginScreen({super.key});
  @override
  Widget build(BuildContext context) {
    final user = TextEditingController(text: 'admin');
    final pass = TextEditingController();
    InputDecoration dec(String label, IconData icon) => InputDecoration(
          labelText: label,
          prefixIcon: Icon(icon),
          filled: true,
          fillColor: card,
          border: OutlineInputBorder(
              borderRadius: BorderRadius.circular(10), borderSide: BorderSide.none),
        );
    return Scaffold(
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(28),
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              Container(
                width: 76, height: 76,
                decoration: BoxDecoration(color: teal, borderRadius: BorderRadius.circular(20)),
                child: const Icon(Icons.shield, color: Colors.black, size: 40),
              ),
              const SizedBox(height: 18),
              const Text('CAPHY',
                  style: TextStyle(fontSize: 32, fontWeight: FontWeight.bold, letterSpacing: 2)),
              const Text('Security in your pocket', style: TextStyle(color: teal)),
              const SizedBox(height: 34),
              TextField(controller: user, decoration: dec('Username', Icons.person)),
              const SizedBox(height: 14),
              TextField(controller: pass, obscureText: true, decoration: dec('Password', Icons.lock)),
              const SizedBox(height: 24),
              SizedBox(
                width: double.infinity,
                child: FilledButton(
                  style: FilledButton.styleFrom(
                      backgroundColor: teal, padding: const EdgeInsets.symmetric(vertical: 16)),
                  onPressed: () => Navigator.pushReplacement(
                      context, MaterialPageRoute(builder: (_) => const HomeScreen())),
                  child: const Text('Sign In',
                      style: TextStyle(color: Colors.black, fontWeight: FontWeight.bold)),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

// ---------- Home (Alerts / History tabs) ----------
class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key});
  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  int _tab = 0;

  Alert _sample() {
    final now = DateTime.now();
    final t = '${now.hour.toString().padLeft(2, '0')}:${now.minute.toString().padLeft(2, '0')}';
    return Alert(
      title: 'TIER 3 - Threat at Front Gate',
      body: 'Person detected 1.9 m from camera',
      tier: '3', distance: '1.9', time: t,
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text(_tab == 0 ? 'Alerts' : 'Event History'),
        backgroundColor: panel,
      ),
      body: const AlertListView(),
      floatingActionButton: FloatingActionButton(
        backgroundColor: teal,
        onPressed: () => addAlert(_sample()),
        child: const Icon(Icons.add_alert, color: Colors.black),
      ),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _tab,
        onDestinationSelected: (i) => setState(() => _tab = i),
        destinations: const [
          NavigationDestination(icon: Icon(Icons.notifications), label: 'Alerts'),
          NavigationDestination(icon: Icon(Icons.history), label: 'History'),
        ],
      ),
    );
  }
}

class AlertListView extends StatelessWidget {
  const AlertListView({super.key});
  @override
  Widget build(BuildContext context) {
    return ValueListenableBuilder<List<Alert>>(
      valueListenable: alertStore,
      builder: (context, alerts, _) {
        if (alerts.isEmpty) {
          return const Center(
            child: Padding(
              padding: EdgeInsets.all(30),
              child: Text('No alerts yet.\nTap + to simulate one, or trigger a real threat.',
                  textAlign: TextAlign.center, style: TextStyle(color: Colors.white54)),
            ),
          );
        }
        return ListView.separated(
          padding: const EdgeInsets.all(16),
          itemCount: alerts.length,
          separatorBuilder: (_, __) => const SizedBox(height: 10),
          itemBuilder: (context, i) {
            final a = alerts[i];
            final c = tierColor(a.tier);
            return Card(
              color: card,
              child: ListTile(
                leading: CircleAvatar(
                    backgroundColor: c, child: const Icon(Icons.person, color: Colors.black)),
                title: Text(a.title, style: const TextStyle(fontWeight: FontWeight.bold)),
                subtitle: Text(a.body),
                trailing: Text(a.time, style: const TextStyle(color: Colors.white38)),
                onTap: () => Navigator.push(context,
                    MaterialPageRoute(builder: (_) => AlertDetailScreen(alert: a))),
              ),
            );
          },
        );
      },
    );
  }
}

// ---------- Alert Details ----------
class AlertDetailScreen extends StatelessWidget {
  final Alert alert;
  const AlertDetailScreen({super.key, required this.alert});

  Widget _noImage() => Container(
        height: 220,
        decoration: BoxDecoration(color: card, borderRadius: BorderRadius.circular(12)),
        child: const Center(child: Icon(Icons.image_not_supported, color: Colors.white24, size: 48)),
      );

  Widget _row(String k, String v, Color c) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 8),
        child: Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Text(k, style: const TextStyle(color: Colors.white54)),
            Text(v, style: TextStyle(color: c, fontWeight: FontWeight.bold, fontSize: 16)),
          ],
        ),
      );

  @override
  Widget build(BuildContext context) {
    final c = tierColor(alert.tier);
    return Scaffold(
      appBar: AppBar(title: const Text('Alert Details'), backgroundColor: panel),
      body: ListView(
        padding: const EdgeInsets.all(20),
        children: [
          if (alert.imageUrl != null && alert.imageUrl!.isNotEmpty)
            ClipRRect(
              borderRadius: BorderRadius.circular(12),
              child: Image.network(alert.imageUrl!, height: 220, fit: BoxFit.cover,
                  errorBuilder: (_, __, ___) => _noImage()),
            )
          else
            _noImage(),
          const SizedBox(height: 18),
          Text(alert.title, style: const TextStyle(fontSize: 20, fontWeight: FontWeight.bold)),
          const SizedBox(height: 6),
          Text(alert.body, style: const TextStyle(color: Colors.white70)),
          const SizedBox(height: 18),
          _row('Threat Tier', 'Tier ${alert.tier}', c),
          _row('Distance', '${alert.distance} m', Colors.white),
          _row('Time', alert.time, Colors.white),
          const SizedBox(height: 24),
          FilledButton(
            style: FilledButton.styleFrom(backgroundColor: teal),
            onPressed: () => Navigator.pop(context),
            child: const Text('Mark as Reviewed',
                style: TextStyle(color: Colors.black, fontWeight: FontWeight.bold)),
          ),
        ],
      ),
    );
  }
}