import 'dart:async';
import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';
import 'package:firebase_auth/firebase_auth.dart' as fb;
import 'package:cloud_firestore/cloud_firestore.dart' as fs;

/// Small persistent store for the server URL, auth token and username.
class Store {
  static late SharedPreferences _p;
  static Future<void> init() async {
    _p = await SharedPreferences.getInstance();
  }

  // No real default - '192.168.1.10' looked like a valid address but was
  // never actually anyone's laptop, so failed logins looked identical to a
  // wrong password with no clue that the real problem was the server
  // address. Empty string makes hasServerAddress below explicit instead.
  static String get baseUrl => _p.getString('baseUrl') ?? '';
  static set baseUrl(String v) => _p.setString('baseUrl', v.trim());
  static bool get hasServerAddress => baseUrl.trim().isNotEmpty;

  static String? get token => _p.getString('token');
  static set token(String? v) =>
      v == null ? _p.remove('token') : _p.setString('token', v);

  static String get user => _p.getString('user') ?? 'admin';
  static set user(String v) => _p.setString('user', v);

  // Firebase's email-link flow requires re-supplying the SAME email address
  // that requested the link, at the moment the link is opened - it can't be
  // recovered from the link itself (by design, so the link alone can't leak
  // an account). Persisted across app restarts, since the user typically
  // opens the email/link well after leaving the app.
  static String? get pendingEmailLinkAddress =>
      _p.getString('pendingEmailLinkAddress');
  static set pendingEmailLinkAddress(String? v) => v == null
      ? _p.remove('pendingEmailLinkAddress')
      : _p.setString('pendingEmailLinkAddress', v);

  // Which CAPHY desktop (device_id) this phone last paired with/connected
  // to - lets reconnectToPairedDevice() pick the right one for accounts
  // that own more than one desktop, and lets the onboarding screen skip
  // itself instantly on a warm start instead of waiting on a Firestore
  // round-trip every time the app opens.
  static String? get lastDeviceId => _p.getString('lastDeviceId');
  static set lastDeviceId(String? v) =>
      v == null ? _p.remove('lastDeviceId') : _p.setString('lastDeviceId', v);
}

/// Thin client over the CAPHY Flask JSON API.
class Api {
  /// Whether the CAPHY system is reachable at all (LAN OR cloud).
  ///
  /// IMPORTANT: only the authoritative state() poll is allowed to flip this
  /// to FALSE, because state() is the only call that checks BOTH the LAN
  /// fast-path AND the Firestore cloud fallback before giving up. Every
  /// other method here (stats/cameras/snapshot/...) only ever tries the LAN
  /// path, so on mobile data they all "fail" every cycle even though the
  /// system is perfectly reachable over the internet. Letting those flip
  /// the flag was the cause of the banner glitching on/off every couple of
  /// seconds: state() said "online (cloud)", then stats() said "offline
  /// (no LAN)", back and forth. Now they can only CONFIRM reachability (a
  /// LAN success means we're definitely up), never declare us down.
  static final ValueNotifier<bool> online = ValueNotifier<bool>(true);
  static String lastError = '';

  static void _reachable() {
    lastError = '';
    if (!online.value) online.value = true;
  }

  /// Record a failed request. Does NOT mark the app offline unless this is
  /// the authoritative state() check (authoritative: true) - see the online
  /// field doc above for why.
  static void _unreachable(Object e, {bool authoritative = false}) {
    lastError = e.toString();
    if (authoritative && online.value) online.value = false;
  }

  // ---------------------------------------------------------- connectivity
  //
  // Coarse "what can we reach right now" state, so the app can auto-switch
  // between cloud (from anywhere) and LAN (same Wi-Fi as the laptop, works
  // with NO internet) and tell the user which one they're on.
  //   'online'  - internet is up; cloud features (live video from anywhere,
  //               alerts, remote control) all work.
  //   'lan'     - NO internet, but the laptop IS reachable on the local
  //               network: live view (MJPEG) and controls still work; the
  //               from-anywhere cloud features are paused.
  //   'offline' - neither: nothing reachable.
  static final ValueNotifier<String> connectivityMode =
      ValueNotifier<String>('online');

  /// Is there real internet right now? Hits a couple of tiny, ultra-reliable
  /// "no content" endpoints (the ones Android itself uses for its captive-
  /// portal check) with a short timeout, so it reflects ACTUAL reachability,
  /// not just "Wi-Fi is connected" (which can be a router with no uplink).
  static Future<bool> hasInternet() async {
    for (final url in const [
      'https://www.gstatic.com/generate_204',
      'https://clients3.google.com/generate_204',
    ]) {
      try {
        final r = await http
            .get(Uri.parse(url))
            .timeout(const Duration(seconds: 4));
        if (r.statusCode == 204 || r.statusCode == 200) return true;
      } catch (_) {}
    }
    return false;
  }

  /// Re-evaluate connectivity and update connectivityMode. Cheap enough to
  /// call every few seconds from the UI.
  static Future<void> refreshConnectivity() async {
    final results = await Future.wait([isLanReachable(), hasInternet()]);
    final lan = results[0];
    final net = results[1];
    final mode = net ? 'online' : (lan ? 'lan' : 'offline');
    if (connectivityMode.value != mode) connectivityMode.value = mode;
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

  // ------------------------------------------------------------- auth
  //
  // Login/signup talk DIRECTLY to Firebase Auth, not to the laptop's Flask
  // server. This is deliberate: identity should work from anywhere (mobile
  // data, a coffee shop, wherever), not require the phone to be on the same
  // Wi-Fi as the laptop. Only local features further down this file -
  // live view, arm/disarm, FCM registration - actually need Store.baseUrl,
  // because those genuinely are local-network operations.
  //
  // After Firebase confirms who the user is, we store their Firebase ID
  // token as Store.token. That token gets sent to the laptop (as a Bearer
  // header) on local-network requests, and the laptop's web/server.py
  // /api/fcm/register route resolves it back to the same user_uid the web
  // dashboard uses (see storage/firebase_push.py).

  static final fb.FirebaseAuth _auth = fb.FirebaseAuth.instance;

  /// Email+password sign-in via Firebase directly.
  /// Returns null on success, or a human-readable error otherwise.
  static Future<String?> login(String email, String pass) async {
    try {
      final cred = await _auth.signInWithEmailAndPassword(
          email: email.trim(), password: pass);
      final idToken = await cred.user?.getIdToken();
      if (idToken == null) return 'Could not get a sign-in token. Try again.';
      Store.token = idToken;
      Store.user = email;
      return null;
    } on fb.FirebaseAuthException catch (e) {
      return _friendlyAuthError(e);
    } catch (e) {
      return 'Sign-in error: $e';
    }
  }

  /// Email+password account creation via Firebase directly.
  static Future<String?> signup(String email, String password) async {
    try {
      final cred = await _auth.createUserWithEmailAndPassword(
          email: email.trim(), password: password);
      final idToken = await cred.user?.getIdToken();
      if (idToken == null) return 'Account created, but sign-in failed.';
      Store.token = idToken;
      Store.user = email;
      return null;
    } on fb.FirebaseAuthException catch (e) {
      return _friendlyAuthError(e);
    } catch (e) {
      return 'Sign-up error: $e';
    }
  }

  // Where Firebase sends the user after they tap the emailed magic link.
  // Must exactly match an Authorized Domain in Firebase Console ->
  // Authentication -> Settings, and the android:host in AndroidManifest.xml's
  // deep-link intent-filter, or the link either won't open the app or
  // Firebase will reject it as untrusted.
  static const _emailLinkUrl = 'https://caphy-c6b77.firebaseapp.com';

  /// Step 1 of passwordless sign-in: emails the user a magic sign-in link.
  /// Returns null on success, or an error message.
  static Future<String?> sendSignInLink(String email) async {
    try {
      final actionSettings = fb.ActionCodeSettings(
        url: _emailLinkUrl,
        handleCodeInApp: true,
        androidPackageName: 'com.caphy.app',
        androidInstallApp: true,
        androidMinimumVersion: '1',
      );
      await _auth.sendSignInLinkToEmail(
          email: email.trim(), actionCodeSettings: actionSettings);
      // Remember which email this link was sent to - needed to complete
      // sign-in when the link is opened, possibly after the app restarts.
      Store.pendingEmailLinkAddress = email.trim();
      return null;
    } on fb.FirebaseAuthException catch (e) {
      return _friendlyAuthError(e);
    } catch (e) {
      return 'Could not send sign-in link: $e';
    }
  }

  /// True if the given deep-link URL (from app startup or a resumed intent)
  /// is a Firebase email sign-in link.
  static bool isSignInLink(String link) => _auth.isSignInWithEmailLink(link);

  /// Step 2 of passwordless sign-in: called when the app is opened via the
  /// magic link. Completes the sign-in using the email remembered from step 1.
  /// Returns null on success, or an error message (including the case where
  /// no pending email was found - e.g. link opened on a different device
  /// than the one that requested it, which Firebase's flow doesn't support
  /// without asking the user to re-enter their email).
  static Future<String?> completeSignInWithLink(String link,
      {String? emailOverride}) async {
    final email = emailOverride ?? Store.pendingEmailLinkAddress;
    if (email == null) {
      return 'This link was requested on a different device. '
          'Enter your email again to continue.';
    }
    try {
      final cred =
          await _auth.signInWithEmailLink(email: email, emailLink: link);
      final idToken = await cred.user?.getIdToken();
      if (idToken == null) return 'Could not get a sign-in token. Try again.';
      Store.token = idToken;
      Store.user = email;
      Store.pendingEmailLinkAddress = null;
      return null;
    } on fb.FirebaseAuthException catch (e) {
      return _friendlyAuthError(e);
    } catch (e) {
      return 'Sign-in link error: $e';
    }
  }

  /// "Sign in with Google" - takes the Google ID token from google_sign_in
  /// and exchanges it for a Firebase credential directly (no trip through
  /// the laptop). Returns {'needs_password': bool} so the UI can offer the
  /// same "add a password too" nudge the web dashboard shows.
  static Future<Map<String, dynamic>?> loginWithGoogleToken(
      String googleIdToken) async {
    try {
      final credential = fb.GoogleAuthProvider.credential(idToken: googleIdToken);
      final userCred = await _auth.signInWithCredential(credential);
      final idToken = await userCred.user?.getIdToken();
      if (idToken == null) return null;

      Store.token = idToken;
      Store.user = userCred.user?.email ?? '';

      // additionalUserInfo.isNewUser tells us if this is their first time -
      // same signal the web's /set-password screen uses, just sourced from
      // Firebase directly instead of our own users table.
      final isNewUser = userCred.additionalUserInfo?.isNewUser ?? false;
      final hasPasswordProvider = userCred.user?.providerData
              .any((p) => p.providerId == 'password') ??
          false;

      return {
        'uid': userCred.user?.uid,
        'email': userCred.user?.email,
        'needs_password': isNewUser && !hasPasswordProvider,
      };
    } on fb.FirebaseAuthException catch (e) {
      lastError = _friendlyAuthError(e);
      return null;
    } catch (e) {
      lastError = 'Google sign-in error: $e';
      return null;
    }
  }

  static String _friendlyAuthError(fb.FirebaseAuthException e) {
    switch (e.code) {
      case 'user-not-found':
        return 'No account found for that email.';
      case 'wrong-password':
      case 'invalid-credential':
        return 'Incorrect email or password.';
      case 'email-already-in-use':
        return 'That email is already registered - try signing in instead.';
      case 'weak-password':
        return 'Password must be at least 6 characters.';
      case 'invalid-email':
        return 'That email address looks invalid.';
      case 'network-request-failed':
        return 'No internet connection - check your mobile data or Wi-Fi.';
      case 'invalid-action-code':
        return 'This sign-in link is invalid or has already been used. '
            'Request a new one.';
      case 'expired-action-code':
        return 'This sign-in link has expired. Request a new one.';
      default:
        return e.message ?? 'Authentication failed (${e.code}).';
    }
  }

  static void logout() {
    // Best-effort: tell the laptop to stop pushing to this phone. Fire and
    // forget - logout shouldn't hang waiting on the network.
    unregisterFcmToken();
    _auth.signOut();
    Store.token = null;
  }

  /// Sends this phone's FCM token to the paired laptop so alerts from THIS
  /// device/account reach THIS phone only (B5) - replaces the old shared
  /// 'caphy_alerts' topic every phone used to subscribe to.
  /// Call this once right after a successful login, and again whenever
  /// FirebaseMessaging.instance.onTokenRefresh fires.
  static Future<bool> registerFcmToken(String fcmToken) async {
    try {
      final r = await http
          .post(_u('/api/fcm/register'),
              headers: _h, body: jsonEncode({'fcm_token': fcmToken}))
          .timeout(const Duration(seconds: 10));
      if (r.statusCode == 200) {
        final j = jsonDecode(r.body);
        return j['success'] == true;
      }
    } catch (_) {}
    return false;
  }

  /// Call on logout so this phone stops receiving this laptop's alerts.
  static Future<bool> unregisterFcmToken() async {
    try {
      final fcmToken = _lastFcmToken;
      if (fcmToken == null) return false;
      final r = await http
          .post(_u('/api/fcm/unregister'),
              headers: _h, body: jsonEncode({'fcm_token': fcmToken}))
          .timeout(const Duration(seconds: 10));
      if (r.statusCode == 200) {
        final j = jsonDecode(r.body);
        return j['success'] == true;
      }
    } catch (_) {}
    return false;
  }

  /// Cached so unregisterFcmToken() can find it at logout time without
  /// asking Firebase again (that call can hang if there's no connectivity).
  static String? _lastFcmToken;
  static void rememberFcmToken(String token) => _lastFcmToken = token;

  static Future<List<dynamic>> alerts({int limit = 50}) async {
    // LAN fast-path first (instant, and works with zero internet).
    if (Store.hasServerAddress) {
      try {
        final r = await http
            .get(_u('/api/alerts', {'limit': limit}), headers: _h)
            .timeout(const Duration(seconds: 3));
        if (r.statusCode == 200) {
          _reachable();
          return jsonDecode(r.body) as List<dynamic>;
        }
      } catch (_) {
        // fall through to cloud
      }
    }
    // Cloud path: read alerts published to Firestore by the laptop, so the
    // list works from ANYWHERE (mobile data), not just on the laptop's LAN.
    return _alertsFromCloud(limit: limit);
  }

  /// Reads the account's alerts straight from Firestore (see the laptop's
  /// storage/cloud_alerts.py). Returns the SAME shape the UI already expects
  /// from /api/alerts, so nothing downstream needs to change.
  static Future<List<dynamic>> _alertsFromCloud({int limit = 50}) async {
    final uid = _auth.currentUser?.uid;
    if (uid == null) return [];
    try {
      final q = await _fs
          .collection('alerts')
          .where('owner_uid', isEqualTo: uid)
          .orderBy('created_at', descending: true)
          .limit(limit)
          .get();
      return q.docs.map((d) {
        final m = d.data();
        return {
          'id': m['alert_id'],
          'tier': m['tier'] ?? 1,
          'event': m['event'],
          'distance_m': m['distance_m'],
          'confidence': m['confidence'],
          'camera': m['camera'],
          'timestamp': m['timestamp'],
          // Cloud alerts carry a full https snapshot URL (Firebase Storage
          // signed URL); LAN alerts carry a path relative to the laptop.
          'snapshot': m['snapshot_url'],
          'snapshot_url': m['snapshot_url'],
          'has_video': m['has_video'] ?? false,
          'remote': true,
        };
      }).toList();
    } catch (e) {
      lastError = 'Could not load alerts: $e';
      return [];
    }
  }

  static Future<Map<String, dynamic>?> alert(int id) async {
    if (Store.hasServerAddress) {
      try {
        final r = await http
            .get(_u('/api/alert/$id'), headers: _h)
            .timeout(const Duration(seconds: 3));
        if (r.statusCode == 200) {
          _reachable();
          return jsonDecode(r.body) as Map<String, dynamic>;
        }
      } catch (_) {
        // fall through to cloud
      }
    }
    // Cloud fallback: find the alert doc for this alert_id in the account's
    // alerts. (device_id-agnostic - matches whichever laptop raised it.)
    final uid = _auth.currentUser?.uid;
    if (uid == null) return null;
    try {
      final q = await _fs
          .collection('alerts')
          .where('owner_uid', isEqualTo: uid)
          .where('alert_id', isEqualTo: id)
          .limit(1)
          .get();
      if (q.docs.isEmpty) return null;
      final m = q.docs.first.data();
      return {
        'id': m['alert_id'],
        'tier': m['tier'] ?? 1,
        'event': m['event'],
        'distance_m': m['distance_m'],
        'confidence': m['confidence'],
        'camera': m['camera'],
        'timestamp': m['timestamp'],
        'snapshot': m['snapshot_url'],
        'snapshot_url': m['snapshot_url'],
        'has_video': m['has_video'] ?? false,
        'remote': true,
      };
    } catch (_) {
      return null;
    }
  }

  static Future<List<dynamic>> stats() async {
    if (Store.hasServerAddress) {
      try {
        final r = await http
            .get(_u('/api/stats'), headers: _h)
            .timeout(const Duration(seconds: 3));
        if (r.statusCode == 200) {
          _reachable();
          return jsonDecode(r.body) as List<dynamic>;
        }
      } catch (_) {
        // fall through to cloud
      }
    }
    // Cloud fallback: derive lightweight per-camera stats from the heartbeat
    // snapshot so the Live screen's camera dots/labels work on mobile data.
    final cams = await _camerasFromCloud();
    return cams
        .map((c) => {
              'cam': c['cam'],
              'online': c['online'] == true,
              'camera_on': c['on'] != false,
              'motion': false,
              'person': false,
              'distance': '-',
            })
        .toList();
  }

  // ------------------------------------------------------------- pairing
  //
  // "Connect Device": the phone scans a QR code shown on the laptop's web
  // Settings page. The QR encodes {code, device_id, ip, port} - enough for
  // the phone to build a baseUrl and confirm the pairing directly against
  // that address, without ever typing an IP by hand. The pairing code
  // itself is short-lived and single-use (see /api/pairing/* in
  // web/server.py); the phone still proves ITS identity separately via its
  // own Firebase bearer token, so a scanned QR alone can't impersonate an
  // account.
  /// Parses a scanned QR payload. Returns null if it doesn't look like a
  /// CAPHY pairing code.
  static Map<String, dynamic>? parsePairingQr(String raw) {
    try {
      final m = jsonDecode(raw) as Map<String, dynamic>;
      if (m['code'] == null || m['ip'] == null || m['port'] == null) return null;
      return m;
    } catch (_) {
      return null;
    }
  }

  /// Parses a scan-to-connect QR (the v2 payload from the laptop's
  /// /api/pairing/link-token). Returns null if it isn't one.
  static Map<String, dynamic>? parseLinkQr(String raw) {
    try {
      final m = jsonDecode(raw) as Map<String, dynamic>;
      if (m['v'] == 2 && m['nonce'] != null && m['device_id'] != null) return m;
      return null;
    } catch (_) {
      return null;
    }
  }

  /// Scan-to-connect sign-in: the phone is NOT signed in yet. It scanned the
  /// laptop's QR, which carries only a nonce. That nonce points at a one-time
  /// Firebase custom token the laptop stashed in Firestore
  /// (phone_link_tokens/{nonce}); firestore.rules allows reading exactly that
  /// doc without auth (the unguessable nonce is the capability). We fetch the
  /// token, sign in with it as the laptop's account, remember the device +
  /// its LAN address, then delete the one-shot token. No email, no password,
  /// no Google button - just a scan.
  ///
  /// Returns null on success, or a human-readable error.
  static Future<String?> signInWithScannedToken(
      Map<String, dynamic> payload) async {
    final nonce = payload['nonce']?.toString();
    final deviceId = payload['device_id']?.toString();
    if (nonce == null || deviceId == null) {
      return 'That QR code is missing its connection details.';
    }
    try {
      final snap =
          await _fs.collection('phone_link_tokens').doc(nonce).get();
      final data = snap.data();
      if (data == null) {
        return 'This QR code has expired. On your laptop, open the connect '
            'screen again to show a fresh one.';
      }
      final expiresAt = data['expires_at'];
      if (expiresAt is num &&
          DateTime.now().millisecondsSinceEpoch / 1000 > expiresAt) {
        return 'This QR code has expired. Show a fresh one on your laptop.';
      }
      final customToken = data['custom_token']?.toString();
      if (customToken == null) {
        return 'That QR code is not a valid CAPHY connection code.';
      }

      await _auth.signInWithCustomToken(customToken);

      // Cache identity + fast-path LAN address for immediate use.
      final user = _auth.currentUser;
      if (user != null) {
        final t = await user.getIdToken();
        if (t != null) Store.token = t;
        Store.user = user.email ?? '';
      }
      Store.lastDeviceId = deviceId;
      final ip = payload['ip'];
      final port = payload['port'];
      if (ip != null && port != null) Store.baseUrl = 'http://$ip:$port';

      // Retire the one-shot token now that we're signed in (allowed by the
      // rules for the owner). Best-effort - expiry is the real backstop.
      try {
        await _fs.collection('phone_link_tokens').doc(nonce).delete();
      } catch (_) {}

      return null;
    } on fb.FirebaseAuthException catch (e) {
      return _friendlyAuthError(e);
    } catch (e) {
      return 'Could not connect: $e';
    }
  }

  /// Make `device` the active laptop this phone is controlling/viewing.
  /// Sets the remembered device id and, if that laptop is broadcasting a LAN
  /// address, the fast-path base URL too (cleared otherwise, so off-LAN we
  /// go straight to the cloud paths). Used by the multi-laptop picker.
  static void selectDevice(Map<String, dynamic> device) {
    Store.lastDeviceId = device['device_id']?.toString();
    final ip = device['last_lan_ip'];
    final port = device['last_lan_port'];
    if (ip != null && port != null) {
      Store.baseUrl = 'http://$ip:$port';
    } else {
      Store.baseUrl = '';
    }
  }

  /// Confirms pairing. Tries the scanned laptop's LAN address FIRST (fast
  /// path - works instantly when phone and laptop share WiFi, and needs
  /// nothing but the laptop itself, not even internet). If that fails
  /// (different network, laptop's LAN address unreachable for any
  /// reason), falls back to a Firestore-relayed confirmation: the phone
  /// writes a pairing_confirm_requests doc (any signed-in phone may
  /// create one, scoped to its own uid - see firestore.rules), the
  /// laptop's own background poll loop picks it up over the internet and
  /// completes the pairing via the Admin SDK, then this polls for that
  /// result. Either path ends the same way: Store.baseUrl gets set (LAN
  /// path only - the remote path has no LAN address to save) and
  /// Store.lastDeviceId is remembered either way.
  ///
  /// Returns null on success, or a human-readable error.
  static Future<String?> confirmPairing(Map<String, dynamic> payload) async {
    final ip = payload['ip'];
    final port = payload['port'];
    final code = payload['code'];
    final url = 'http://$ip:$port';

    try {
      final r = await http
          .post(Uri.parse('$url/api/pairing/confirm'),
              headers: _h, body: jsonEncode({'code': code}))
          .timeout(const Duration(seconds: 5));
      if (r.statusCode == 200) {
        final d = jsonDecode(r.body) as Map<String, dynamic>;
        if (d['success'] == true) {
          Store.baseUrl = url;
          Store.lastDeviceId = d['device_id']?.toString();
          return null;
        }
        return d['error']?.toString() ?? 'Pairing failed.';
      }
    } catch (_) {
      // Not on the same WiFi (or laptop unreachable for some other
      // reason) - fall through to the Firestore-relayed path below,
      // rather than surfacing this as a failure.
    }

    return _confirmPairingViaCloud(payload);
  }

  /// The "not on the same WiFi" pairing path - see confirmPairing()'s
  /// doc comment above for the full picture.
  static Future<String?> _confirmPairingViaCloud(
      Map<String, dynamic> payload) async {
    final uid = _auth.currentUser?.uid;
    if (uid == null) return 'Not signed in';
    final code = payload['code'];
    if (code == null) return 'That QR code is missing its pairing code.';

    try {
      final ref = await _fs.collection('pairing_confirm_requests').add({
        'code': code,
        'requested_by': uid,
        'created_at': fs.FieldValue.serverTimestamp(),
        'status': 'pending',
        'result': null,
      });

      // The laptop's poll loop checks every ~4s - give it up to ~20s to
      // notice, confirm, and write back, which comfortably covers one
      // round trip without leaving the user staring at a spinner forever.
      for (int i = 0; i < 20; i++) {
        await Future.delayed(const Duration(seconds: 1));
        final snap = await ref.get();
        final data = snap.data();
        if (data == null) continue;
        if (data['status'] == 'done') {
          final result = data['result'] as Map<String, dynamic>?;
          if (result != null && result['success'] == true) {
            Store.lastDeviceId = result['device_id']?.toString();
            // No LAN address known from this path - live view will use
            // WebRTC (see webrtc_view.dart) until the phone happens to be
            // on the laptop's WiFi, at which point the fast MJPEG path
            // picks up automatically once Store.baseUrl gets set (e.g.
            // by reconnectToPairedDevice() finding a fresh heartbeat).
            return null;
          }
          return result?['error']?.toString() ?? 'Pairing failed.';
        }
        if (data['status'] == 'error') {
          final result = data['result'] as Map<String, dynamic>?;
          return result?['error']?.toString() ?? 'Pairing failed.';
        }
      }
      return 'The laptop didn\'t respond in time. Make sure it\'s running '
          'and signed in, then try again.';
    } catch (e) {
      return 'Could not reach the pairing service: $e';
    }
  }

  // ---------------------------------------------------------- cloud device registry
  //
  // Reads devices/{id} straight from Firestore (allowed by firestore.rules
  // for the owning uid only - see the rules file). This is what makes
  // onboarding possible without ever having reached a laptop: right after
  // login, before Store.baseUrl even has a value, the phone can already
  // answer "does my account own a CAPHY desktop, and is it online."

  static final fs.FirebaseFirestore _fs = fs.FirebaseFirestore.instance;

  /// All CAPHY desktops this account owns. Empty list = show onboarding.
  static Future<List<Map<String, dynamic>>> myDevices() async {
    final uid = _auth.currentUser?.uid;
    if (uid == null) return [];
    try {
      final q = await _fs
          .collection('devices')
          .where('owner_uid', isEqualTo: uid)
          .get();
      return q.docs.map((d) {
        final m = Map<String, dynamic>.from(d.data());
        m['device_id'] = d.id;
        final lastSeen = m['last_seen'];
        if (lastSeen is fs.Timestamp) {
          final age = DateTime.now().difference(lastSeen.toDate());
          m['online'] = age.inSeconds <= 45;
        } else {
          m['online'] = false;
        }
        return m;
      }).toList();
    } catch (e) {
      lastError = 'Could not check paired devices: $e';
      return [];
    }
  }

  /// Tries to auto-connect to the best already-paired device: prefers the
  /// last device this phone actually paired with (Store.lastDeviceId), and
  /// only its LAN address if it looks recently online - otherwise leaves
  /// Store.baseUrl untouched so the UI can fall back to cloud commands.
  static Future<bool> reconnectToPairedDevice() async {
    final devices = await myDevices();
    if (devices.isEmpty) return false;

    Map<String, dynamic>? target;
    if (Store.lastDeviceId != null) {
      for (final d in devices) {
        if (d['device_id'] == Store.lastDeviceId) target = d;
      }
    }
    target ??= devices.first;

    final ip = target['last_lan_ip'];
    final port = target['last_lan_port'];
    Store.lastDeviceId = target['device_id']?.toString();
    if (ip != null && port != null) {
      Store.baseUrl = 'http://$ip:$port';
    }
    return true;
  }

  /// Quick reachability probe for the LAN fast-path: true if THIS phone's
  /// current network can actually reach Store.baseUrl right now. Local
  /// features (live view, arm/disarm, snapshot) should always prefer this
  /// path when it's up - it's faster and, importantly, still works with
  /// zero internet, which is core to CAPHY staying useful during an
  /// outage (edge AI keeps running on the laptop either way).
  static Future<bool> isLanReachable() async {
    if (!Store.hasServerAddress) return false;
    try {
      final r = await http.get(_u('/api/state'), headers: _h)
          .timeout(const Duration(seconds: 3));
      return r.statusCode == 200;
    } catch (_) {
      return false;
    }
  }

  /// Cloud fallback for arm/disarm/snapshot/siren when the phone can't
  /// reach the laptop's LAN address (different network / traveling).
  /// Enqueues a command doc in Firestore; the laptop's own poll loop
  /// (desktop_launcher.py's run_remote_command_listener) picks it up next
  /// time it checks in and executes it locally, then writes the result
  /// back. This never bypasses the laptop's own Flask API - the laptop
  /// just calls itself, so local and remote control share one code path.
  static Future<Map<String, dynamic>?> sendRemoteCommand(
      String deviceId, String type, {Map<String, dynamic>? args}) async {
    final uid = _auth.currentUser?.uid;
    if (uid == null) return null;
    try {
      final ref = await _fs
          .collection('commands')
          .doc(deviceId)
          .collection('queue')
          .add({
        'type': type,
        'args': args ?? {},
        'requested_by': uid,
        'created_at': fs.FieldValue.serverTimestamp(),
        'status': 'pending',
        'result': null,
      });

      // Poll for up to ~15s for the laptop to pick it up and finish -
      // its poll loop checks every ~4s, so this comfortably covers one
      // round trip without leaving the user staring at a spinner forever.
      for (int i = 0; i < 15; i++) {
        await Future.delayed(const Duration(seconds: 1));
        final snap = await ref.get();
        final data = snap.data();
        if (data != null && data['status'] != 'pending') {
          return Map<String, dynamic>.from(data);
        }
      }
      return {'status': 'timeout'};
    } catch (e) {
      lastError = 'Remote command failed: $e';
      return null;
    }
  }

  static Future<List<dynamic>> cameras() async {
    if (Store.hasServerAddress) {
      try {
        final r = await http
            .get(_u('/api/cameras'), headers: _h)
            .timeout(const Duration(seconds: 3));
        if (r.statusCode == 200) {
          _reachable();
          return jsonDecode(r.body) as List<dynamic>;
        }
      } catch (_) {
        // fall through to cloud
      }
    }
    // Cloud fallback: the camera list rides along in the laptop's heartbeat
    // state snapshot (devices/{id}.state.cameras), so the camera selector
    // shows on mobile data too.
    final cams = await _camerasFromCloud();
    return cams;
  }

  static Future<List<dynamic>> _camerasFromCloud() async {
    final deviceId = Store.lastDeviceId;
    if (deviceId == null) return [];
    try {
      final snap = await _fs.collection('devices').doc(deviceId).get();
      final data = snap.data();
      final st = data?['state'];
      if (st is Map && st['cameras'] is List) {
        return List<dynamic>.from(st['cameras'] as List);
      }
    } catch (_) {}
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
      // fall through to cloud
    }
    if (Store.lastDeviceId != null) {
      final result = await sendRemoteCommand(Store.lastDeviceId!, 'siren');
      if (result != null && result['status'] == 'done') return true;
    }
    return false;
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

  /// Send a spoken command/question to the CAPHY Assistant (v2 - talk/know/do
  /// via assistant_ai). Hits /api/assistant, which replaced the old
  /// /api/voice endpoint; that route no longer exists on the backend, so
  /// this must send {"text": ...} to match what api_assistant() reads.
  static Future<Map<String, dynamic>> voice(String command,
      {String lang = 'en'}) async {
    try {
      final r = await http.post(_u('/api/assistant'),
          headers: _h, body: jsonEncode({'text': command, 'lang': lang}));
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

  /// Full URL for a snapshot/video path returned by the API. Cloud alerts
  /// already carry a complete https:// Firebase Storage URL - pass those
  /// through untouched; only LAN paths (relative, like /snapshot/x.jpg) get
  /// the laptop's base URL + token prepended.
  static String mediaUrl(String path) {
    if (path.startsWith('http://') || path.startsWith('https://')) return path;
    return '${Store.baseUrl}$path?token=${Store.token}';
  }

  /// Delete a recording/snapshot on the PC after the phone has its own copy,
  /// so a phone-triggered capture ends up saved only on the phone.
  static Future<bool> deleteMedia(String name) async {
    try {
      final r = await http.post(_u('/api/media/delete'),
          headers: _h, body: jsonEncode({'name': name}));
      return r.statusCode == 200 && jsonDecode(r.body)['ok'] == true;
    } catch (_) {
      return false;
    }
  }

  // ------------------------------------------------------------- state

  /// Everything the UI needs to show what is ON or OFF right now.
  ///
  /// Tries the laptop's LAN address first (fast, instant, works even with
  /// zero internet). If that's not available - either no LAN address is
  /// known at all (e.g. paired over mobile data via the cloud pairing
  /// path, see _confirmPairingViaCloud) or the laptop just isn't reachable
  /// on this network right now - falls back to reading the same
  /// devices/{device_id} doc the desktop's heartbeat keeps fresh, so
  /// "connected over the internet, away from home" looks like a working
  /// system instead of the old "check your WiFi" LAN-only assumption.
  ///
  /// Returns null only when NEITHER path works: no LAN reply AND (no known
  /// device to check in the cloud OR that device hasn't heartbeated
  /// recently) - i.e. the laptop genuinely looks off/unreachable anywhere.
  static Future<Map<String, dynamic>?> state() async {
    if (Store.hasServerAddress) {
      try {
        // Short timeout: on mobile data the saved LAN address (192.168.x.x)
        // isn't routable, so this should give up fast and let the cloud
        // path answer - a long timeout here is what made the UI feel like
        // it was "hanging then reconnecting" every poll.
        final r = await http
            .get(_u('/api/state'), headers: _h)
            .timeout(const Duration(seconds: 3));
        if (r.statusCode == 200) {
          _reachable();
          return jsonDecode(r.body) as Map<String, dynamic>;
        }
      } catch (_) {
        // fall through to the cloud path below
      }
    }

    final cloud = await _stateFromCloud();
    if (cloud != null) {
      _reachable();
      return cloud;
    }
    _unreachable('Laptop unreachable on LAN and no recent cloud heartbeat',
        authoritative: true);
    return null;
  }

  /// Cloud fallback for state() - reads devices/{device_id} straight from
  /// Firestore so the phone can show "connected" the instant it's paired,
  /// with no LAN address required at all. The laptop's heartbeat publishes
  /// a small live-state snapshot (armed/camera_on/emergency/siren) into the
  /// same doc (see device_registry.heartbeat + the desktop heartbeat loop),
  /// so the remote view shows the REAL system state and the arm/disarm
  /// toggle sits in the right position - not a guess. "online" is derived
  /// from last_seen using the same 45s window the desktop registry uses.
  static Future<Map<String, dynamic>?> _stateFromCloud() async {
    final deviceId = Store.lastDeviceId;
    if (deviceId == null) return null;
    try {
      final snap =
          await _fs.collection('devices').doc(deviceId).get();
      final data = snap.data();
      if (data == null) return null;
      final lastSeen = data['last_seen'];
      bool online = false;
      if (lastSeen is fs.Timestamp) {
        online = DateTime.now().difference(lastSeen.toDate()).inSeconds <= 45;
      }
      if (!online) return null;

      final out = <String, dynamic>{'online': true, 'remote': true};
      final st = data['state'];
      if (st is Map) {
        out['armed'] = st['armed'] == true;
        out['camera_on'] = st['camera_on'] != false;
        out['emergency'] = st['emergency'] == true;
        out['siren'] = st['siren'] == true;
      }
      return out;
    } catch (_) {
      return null;
    }
  }

  /// Arm/disarm. Tries the laptop's LAN address first (fast, works even
  /// with zero internet) and only falls back to the Firestore cloud
  /// command queue (see sendRemoteCommand) if that fails AND we know
  /// which desktop to send it to - this is the "access from anywhere"
  /// path for when the phone isn't on the same WiFi as the laptop.
  static Future<bool?> setArmed(bool on) async {
    try {
      final r = await http
          .post(_u('/api/arm'), headers: _h, body: jsonEncode({'on': on}))
          .timeout(const Duration(seconds: 5));
      _reachable();
      if (r.statusCode == 200) return jsonDecode(r.body)['armed'] == true;
    } catch (e) {
      _unreachable(e);
    }

    if (Store.lastDeviceId != null) {
      final result = await sendRemoteCommand(
          Store.lastDeviceId!, on ? 'arm' : 'disarm');
      if (result != null && result['status'] == 'done') return on;
    }
    return null;
  }

  /// Turn the camera device on or off. Off releases it and stops detection.
  /// LAN-first, then the same Firestore command fallback as setArmed so it
  /// works over mobile data too.
  static Future<bool?> setCamera(bool on) async {
    try {
      final r = await http.post(_u('/api/camera/power'),
          headers: _h, body: jsonEncode({'on': on}));
      _reachable();
      if (r.statusCode == 200) return jsonDecode(r.body)['camera_on'] == true;
    } catch (e) {
      _unreachable(e);
    }

    if (Store.lastDeviceId != null) {
      final result = await sendRemoteCommand(
          Store.lastDeviceId!, on ? 'camera_on' : 'camera_off');
      if (result != null && result['status'] == 'done') return on;
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

    if (Store.lastDeviceId != null) {
      final result = await sendRemoteCommand(
          Store.lastDeviceId!, on ? 'emergency_on' : 'emergency_off');
      if (result != null && result['status'] == 'done') return on;
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

  /// Real-time alert push (Server-Sent Events). Emits an event the instant a
  /// new alert is confirmed on the system - no polling delay. Reconnects
  /// automatically if the connection drops (Wi-Fi hiccup, server restart,
  /// app backgrounded). Callers should still keep a slow fallback poll.
  static Stream<void> watchAlerts() {
    late final StreamController<void> controller;
    var active = true;

    Future<void> loop() async {
      while (active) {
        http.Client? client;
        try {
          client = http.Client();
          final req = http.Request('GET', _u('/api/alerts/stream'));
          req.headers.addAll(_h);
          final res = await client.send(req).timeout(const Duration(seconds: 10));
          if (res.statusCode == 200) {
            _reachable();
            await for (final chunk in res.stream.transform(utf8.decoder)) {
              if (!active) break;
              for (final line in const LineSplitter().convert(chunk)) {
                if (line.startsWith('data:') && !controller.isClosed) {
                  controller.add(null);
                }
              }
            }
          }
        } catch (e) {
          _unreachable(e);
        } finally {
          client?.close();
        }
        if (active) await Future.delayed(const Duration(seconds: 3));
      }
    }

    controller = StreamController<void>.broadcast(
      onListen: () => loop(),
      onCancel: () => active = false,
    );
    return controller.stream;
  }
}
