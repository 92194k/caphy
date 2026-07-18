import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

/// Small persistent store for the server URL, auth token and username.
class Store {
  static late SharedPreferences _p;
  static Future<void> init() async {
    _p = await SharedPreferences.getInstance();
  }

  static String get baseUrl => _p.getString('baseUrl') ?? 'http://192.168.1.10:5000';
  static set baseUrl(String v) => _p.setString('baseUrl', v.trim());

  static String? get token => _p.getString('token');
  static set token(String? v) =>
      v == null ? _p.remove('token') : _p.setString('token', v);

  static String get user => _p.getString('user') ?? 'admin';
  static set user(String v) => _p.setString('user', v);
}

/// Thin client over the CAPHY Flask JSON API.
class Api {
  /// Whether the last request reached the system.
  ///
  /// Every call updates this. Screens listen to it so an unreachable server
  /// looks like "can't connect" instead of "no data yet" - previously both
  /// showed an identical empty list, which is impossible to debug live.
  static final ValueNotifier<bool> online = ValueNotifier<bool>(true);
  static String lastError = '';

  static void _reachable() {
    lastError = '';
    if (!online.value) online.value = true;
  }

  static void _unreachable(Object e) {
    lastError = e.toString();
    if (online.value) online.value = false;
  }

  static Uri _u(String path, [Map<String, dynamic>? q]) {
    final base = Uri.parse('${Store.baseUrl}$path');
    if (q == null) return base;
    return base.replace(
        queryParameters: q.map((k, v) => MapEntry(k, '$v')));
  }

  static Map<String, String> get _h => {
        'Content-Type': 'application/json',
        if (Store.token != null) 'Authorization': 'Bearer ${Store.token}',
      };

  static Future<bool> login(String user, String pass) async {
    try {
      final r = await http
          .post(_u('/api/login'),
              headers: {'Content-Type': 'application/json'},
              body: jsonEncode({'username': user, 'password': pass}))
          .timeout(const Duration(seconds: 10));
      if (r.statusCode == 200) {
        final j = jsonDecode(r.body);
        if (j['ok'] == true) {
          Store.token = j['token'];
          Store.user = j['user'] ?? user;
          return true;
        }
      }
    } catch (_) {}
    return false;
  }

  static void logout() => Store.token = null;

  static Future<List<dynamic>> alerts({int limit = 50}) async {
    try {
      final r = await http.get(_u('/api/alerts', {'limit': limit}), headers: _h);
      _reachable();
      if (r.statusCode == 200) return jsonDecode(r.body) as List<dynamic>;
    } catch (e) {
      _unreachable(e);
    }
    return [];
  }

  static Future<Map<String, dynamic>?> alert(int id) async {
    try {
      final r = await http.get(_u('/api/alert/$id'), headers: _h);
      _reachable();
      if (r.statusCode == 200) return jsonDecode(r.body) as Map<String, dynamic>;
    } catch (e) {
      _unreachable(e);
    }
    return null;
  }

  static Future<List<dynamic>> stats() async {
    try {
      final r = await http.get(_u('/api/stats'), headers: _h);
      _reachable();
      if (r.statusCode == 200) return jsonDecode(r.body) as List<dynamic>;
    } catch (e) {
      _unreachable(e);
    }
    return [];
  }

  static Future<List<dynamic>> cameras() async {
    try {
      final r = await http.get(_u('/api/cameras'), headers: _h);
      _reachable();
      if (r.statusCode == 200) return jsonDecode(r.body) as List<dynamic>;
    } catch (e) {
      _unreachable(e);
    }
    return [];
  }

  static Future<bool> snapshot(int cam) async {
    try {
      final r = await http.post(_u('/api/snapshot/$cam'), headers: _h);
      _reachable();
      return r.statusCode == 200 && jsonDecode(r.body)['ok'] == true;
    } catch (e) {
      _unreachable(e);
      return false;
    }
  }

  static Future<bool> nightVision(int cam) async {
    try {
      final r = await http.post(_u('/api/nightvision/$cam'), headers: _h);
      return r.statusCode == 200 && jsonDecode(r.body)['on'] == true;
    } catch (_) {
      return false;
    }
  }

  static Future<bool> siren() async {
    try {
      final r = await http.post(_u('/api/siren'), headers: _h);
      return r.statusCode == 200 && jsonDecode(r.body)['on'] == true;
    } catch (_) {
      return false;
    }
  }

  /// Toggle recording. Returns the full response:
  ///   {recording: bool, video_url?: string}  (video_url present when stopped)
  static Future<Map<String, dynamic>> record(int cam) async {
    try {
      final r = await http.post(_u('/api/record/$cam'), headers: _h);
      _reachable();
      if (r.statusCode == 200) return jsonDecode(r.body) as Map<String, dynamic>;
    } catch (e) {
      _unreachable(e);
    }
    return {'recording': false};
  }

  /// Single-frame live-view URL for a camera (poll this repeatedly).
  static String frameUrl(int cam) =>
      '${Store.baseUrl}/api/frame/$cam?token=${Store.token}';

  /// The list of commands CAPHY understands, served from voice/intents.json.
  /// Fetched from the system so the app can never show a stale list.
  static Future<List<dynamic>> intents() async {
    try {
      final r = await http.get(_u('/api/intents'), headers: _h);
      if (r.statusCode == 200) {
        final m = jsonDecode(r.body) as Map<String, dynamic>;
        return (m['commands'] as List<dynamic>? ) ?? [];
      }
    } catch (_) {}
    return [];
  }

  /// Send a spoken command. Fixed commands only - CAPHY does not converse.
  /// Anything unrecognized comes back with ok:false and a "did not understand"
  /// reply, which the app speaks.
  static Future<Map<String, dynamic>> voice(String command,
      {String lang = 'en'}) async {
    try {
      final r = await http.post(_u('/api/voice'),
          headers: _h, body: jsonEncode({'command': command, 'lang': lang}));
      _reachable();
      if (r.statusCode == 200) return jsonDecode(r.body) as Map<String, dynamic>;
    } catch (e) {
      _unreachable(e);
    }
    return {'ok': false, 'reply': '', 'message': 'Could not reach the system'};
  }

  static Future<bool> renameCamera(int cam, String name) async {
    try {
      final r = await http.post(_u('/api/camera/$cam/name'),
          headers: _h, body: jsonEncode({'name': name}));
      _reachable();
      return r.statusCode == 200 && jsonDecode(r.body)['ok'] == true;
    } catch (e) {
      _unreachable(e);
      return false;
    }
  }

  /// Live MJPEG stream URL (token in query so the <img>/stream can authenticate).
  static String streamUrl(int cam) =>
      '${Store.baseUrl}/video_feed/$cam?token=${Store.token}';

  /// Full URL for a snapshot/video path returned by the API.
  static String mediaUrl(String path) =>
      '${Store.baseUrl}$path?token=${Store.token}';

  // ------------------------------------------------------------- state

  /// Everything the UI needs to show what is ON or OFF right now.
  /// Returns null when the system can't be reached.
  static Future<Map<String, dynamic>?> state() async {
    try {
      final r = await http
          .get(_u('/api/state'), headers: _h)
          .timeout(const Duration(seconds: 6));
      _reachable();
      if (r.statusCode == 200) return jsonDecode(r.body) as Map<String, dynamic>;
    } catch (e) {
      _unreachable(e);
    }
    return null;
  }

  static Future<bool?> setArmed(bool on) async {
    try {
      final r = await http.post(_u('/api/arm'),
          headers: _h, body: jsonEncode({'on': on}));
      _reachable();
      if (r.statusCode == 200) return jsonDecode(r.body)['armed'] == true;
    } catch (e) {
      _unreachable(e);
    }
    return null;
  }

  /// Turn the camera device on or off. Off releases it and stops detection.
  static Future<bool?> setCamera(bool on) async {
    try {
      final r = await http.post(_u('/api/camera/power'),
          headers: _h, body: jsonEncode({'on': on}));
      _reachable();
      if (r.statusCode == 200) return jsonDecode(r.body)['camera_on'] == true;
    } catch (e) {
      _unreachable(e);
    }
    return null;
  }

  static Future<bool?> setEmergency(bool on) async {
    try {
      final r = await http.post(_u('/api/emergency'),
          headers: _h, body: jsonEncode({'on': on}));
      _reachable();
      if (r.statusCode == 200) return jsonDecode(r.body)['emergency'] == true;
    } catch (e) {
      _unreachable(e);
    }
    return null;
  }

  /// Acknowledge one alert: hides it, keeps the row in the database.
  static Future<bool> dismissAlert(int id) async {
    try {
      final r = await http.post(_u('/api/alert/$id/dismiss'), headers: _h);
      _reachable();
      return r.statusCode == 200;
    } catch (e) {
      _unreachable(e);
      return false;
    }
  }

  static Future<bool> restoreAlert(int id) async {
    try {
      final r = await http.post(_u('/api/alert/$id/restore'), headers: _h);
      _reachable();
      return r.statusCode == 200;
    } catch (e) {
      _unreachable(e);
      return false;
    }
  }

  static Future<bool> dismissAllAlerts() async {
    try {
      final r = await http.post(_u('/api/alerts/dismiss_all'), headers: _h);
      _reachable();
      return r.statusCode == 200;
    } catch (e) {
      _unreachable(e);
      return false;
    }
  }
}
