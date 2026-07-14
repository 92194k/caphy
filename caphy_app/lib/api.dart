import 'dart:convert';
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
      if (r.statusCode == 200) return jsonDecode(r.body) as List<dynamic>;
    } catch (_) {}
    return [];
  }

  static Future<Map<String, dynamic>?> alert(int id) async {
    try {
      final r = await http.get(_u('/api/alert/$id'), headers: _h);
      if (r.statusCode == 200) return jsonDecode(r.body) as Map<String, dynamic>;
    } catch (_) {}
    return null;
  }

  static Future<List<dynamic>> stats() async {
    try {
      final r = await http.get(_u('/api/stats'), headers: _h);
      if (r.statusCode == 200) return jsonDecode(r.body) as List<dynamic>;
    } catch (_) {}
    return [];
  }

  static Future<List<dynamic>> cameras() async {
    try {
      final r = await http.get(_u('/api/cameras'), headers: _h);
      if (r.statusCode == 200) return jsonDecode(r.body) as List<dynamic>;
    } catch (_) {}
    return [];
  }

  static Future<bool> snapshot(int cam) async {
    try {
      final r = await http.post(_u('/api/snapshot/$cam'), headers: _h);
      return r.statusCode == 200 && jsonDecode(r.body)['ok'] == true;
    } catch (_) {
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

  static Future<bool> record(int cam) async {
    try {
      final r = await http.post(_u('/api/record/$cam'), headers: _h);
      return r.statusCode == 200 && jsonDecode(r.body)['recording'] == true;
    } catch (_) {
      return false;
    }
  }

  /// Single-frame live-view URL for a camera (poll this repeatedly).
  static String frameUrl(int cam) =>
      '${Store.baseUrl}/api/frame/$cam?token=${Store.token}';

  static Future<Map<String, dynamic>> voice(String command) async {
    try {
      final r = await http.post(_u('/api/voice'),
          headers: _h, body: jsonEncode({'command': command}));
      if (r.statusCode == 200) return jsonDecode(r.body) as Map<String, dynamic>;
    } catch (_) {}
    return {'ok': false, 'message': 'Could not reach the system'};
  }

  static Future<bool> renameCamera(int cam, String name) async {
    try {
      final r = await http.post(_u('/api/camera/$cam/name'),
          headers: _h, body: jsonEncode({'name': name}));
      return r.statusCode == 200 && jsonDecode(r.body)['ok'] == true;
    } catch (_) {
      return false;
    }
  }

  /// Live MJPEG stream URL (token in query so the <img>/stream can authenticate).
  static String streamUrl(int cam) =>
      '${Store.baseUrl}/video_feed/$cam?token=${Store.token}';

  /// Full URL for a snapshot/video path returned by the API.
  static String mediaUrl(String path) =>
      '${Store.baseUrl}$path?token=${Store.token}';
}
