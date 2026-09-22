import 'dart:async';
import 'dart:convert';
import 'dart:io' show RawDatagramSocket, InternetAddress, RawSocketEvent;
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

  // The name captured at signup (or loaded from Firebase Auth's
  // displayName on login) - shown in the app header/profile.
  static String get displayName => _p.getString('displayName') ?? '';
  static set displayName(String v) => _p.setString('displayName', v);

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
  //   'reconnected' - kept ONLY as a brief, auto-clearing transitional
  //               value for a split second while refreshConnectivity()
  //               re-verifies after coming back from 'lan'/'offline' -
  //               NOT a state the user has to act on anymore. Previously
  //               this required an explicit tap on "Reconnect" before
  //               settling on 'online'; per explicit user feedback that
  //               was too slow/manual for a security app that should just
  //               recover on its own, refreshConnectivity() below now
  //               finishes that verification and moves straight to
  //               'online' automatically, same poll cycle. The banner
  //               widget can still show a brief teal flash if it catches
  //               this value mid-transition, but nothing blocks on a tap.
  static final ValueNotifier<String> connectivityMode =
      ValueNotifier<String>('online');

  /// True only when the most recent refreshConnectivity() actually got a
  /// live LAN response from the laptop. Unlike connectivityMode (which
  /// collapses "LAN+internet" and "cloud-only, mobile data" into the same
  /// 'online' string), this tells the action methods below (setArmed,
  /// siren, snapshot, etc) whether attempting the direct LAN HTTP call is
  /// even worth it. Defaults to true so a cold app start (before the first
  /// connectivity probe completes) still tries LAN first, same as before -
  /// this only skips the doomed attempt once we've actually confirmed we're
  /// off-LAN.
  static bool lastLanOk = true;

  /// Kept for compatibility with any UI that still calls this on a tap
  /// (e.g. if the user taps the teal bar during its brief auto-clearing
  /// flash) - just forces an immediate re-check instead of waiting for the
  /// next poll cycle. No longer required for the state to reach 'online';
  /// refreshConnectivity() does that on its own now.
  static Future<void> acknowledgeReconnect() async {
    await refreshConnectivity();
  }

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
  ///
  /// Three genuinely different signals feed this, per the actual required
  /// behavior:
  ///   - lan   : is the laptop reachable RIGHT NOW on this local network?
  ///   - net   : does the PHONE have real internet access at all?
  ///   - cloud : is the laptop ITSELF reachable through Firestore (i.e. it
  ///             has a recent heartbeat)? This is NOT the same thing as
  ///             "phone has internet" - the phone could have perfectly
  ///             good internet while the laptop is off, or the laptop
  ///             could be up but its own connection to Firebase is down.
  ///             Only checking `net` (as this used to do) meant "internet
  ///             restored on the phone" could show 'online'/'reconnected'
  ///             even when the laptop was genuinely unreachable by any
  ///             path - the opposite of what "Can't reach your laptop"
  ///             is supposed to mean.
  ///
  /// Internet coming back after 'lan'/'offline' lands on 'reconnected'
  /// instead of jumping straight to 'online' - see the field doc above for
  /// why. Every other transition (including 'reconnected' -> 'lan'/
  /// 'offline' if it drops again before being acknowledged) behaves as
  /// before.
  static bool _refreshConnectivityInFlight = false;

  static Future<void> refreshConnectivity() async {
    // Guard against overlapping calls. Without this, a caller that polls on
    // a fixed Timer.periodic (see main.dart, every 3s) can stack up MANY
    // concurrent refreshConnectivity() calls when offline: isLanReachable()
    // alone can take up to ~18s worst-case fully offline (LAN probe timeout
    // + Firestore fetch timeout + a second probe + UDP broadcast discovery
    // timeout + a third probe, each with its own multi-second timeout,
    // chained sequentially). Every 3-second tick that fires before the
    // previous call finishes starts ANOTHER full chain on top of the ones
    // still running, and none of them ever get cancelled - the number of
    // in-flight network attempts (Firestore reads, UDP sockets, HTTP
    // probes) grows without bound the whole time the phone stays offline.
    // That unbounded pile-up is what actually presented as "the app hangs
    // forever" when testing the fully-offline scenario - not any single
    // call being stuck, but more and more of them competing for the same
    // network/CPU resources with no end in sight. Skipping a tick when the
    // previous one hasn't finished yet keeps this to exactly one call in
    // flight at a time, so it settles down (correctly) even fully offline
    // instead of getting progressively worse.
    if (_refreshConnectivityInFlight) return;
    _refreshConnectivityInFlight = true;
    try {
      await _refreshConnectivityOnce();
    } finally {
      _refreshConnectivityInFlight = false;
    }
  }

  static Future<void> _refreshConnectivityOnce() async {
    final results = await Future.wait(
        [isLanReachable(), hasInternet(), _cloudReachable()]);
    final lan = results[0];
    final net = results[1];
    final cloud = results[2];
    final current = connectivityMode.value;
    lastLanOk = lan;

    if (lan) {
      // Laptop directly reachable on this network - this ALONE already
      // proves the laptop is up and working, full stop. Only the phone's
      // own internet (`net`) matters on top of that to decide "fully
      // online" vs "local-only" - `cloud` must NOT gate this branch.
      //
      // Bug this fixes: `cloud` comes from the laptop's Firestore
      // heartbeat doc being fresh within the last 45s. That heartbeat can
      // lag, or briefly miss a beat, for reasons that have nothing to do
      // with whether the laptop is actually reachable - and when it does,
      // the OLD code here required `net && cloud` even though `lan` was
      // already true, so a phone on the normal home Wi-Fi with working
      // internet could still get stuck on the amber "Offline mode" bar
      // just because the cloud heartbeat hadn't ticked recently enough.
      // Since we already have a live, direct, just-this-second response
      // from the laptop over LAN, that's strictly better evidence than a
      // 45-second-old cloud heartbeat - there's nothing left for `cloud`
      // to prove here.
      if (net) {
        if (current != 'online') {
          connectivityMode.value = 'online';
          _flushPendingDeletesOnReconnect(current);
        }
      } else if (current != 'lan') {
        connectivityMode.value = 'lan';
        _flushPendingDeletesOnReconnect(current);
      }
      return;
    }

    if (net && cloud) {
      // No LAN, but the laptop IS reachable through the cloud (Remote
      // Online Mode - different networks, or mobile data).
      if (current != 'online') {
        connectivityMode.value = 'online';
        _flushPendingDeletesOnReconnect(current);
      }
      return;
    }

    // Neither LAN nor a cloud-reachable laptop: genuinely unreachable by
    // any path. This is the ONE clean red state - it no longer matters
    // whether the phone's own internet is up or down, because that was
    // never the actual question ("can we reach the laptop").
    if (current != 'offline') connectivityMode.value = 'offline';
  }

  // Fire-and-forget: whenever connectivity moves INTO a mode that can
  // actually reach the laptop (from a worse mode - offline, or unset at
  // startup), retry any deletes that got queued while unreachable. Never
  // awaited from the connectivity check itself - a slow/stuck delete
  // retry must not delay or block the connectivity state transition that
  // triggered it.
  static void _flushPendingDeletesOnReconnect(String previousMode) {
    if (previousMode == 'offline' || previousMode == '') {
      // ignore: discarded_futures
      flushPendingDeletes();
    }
  }

  /// Is the laptop reachable through Firestore right now (recent
  /// heartbeat)? Reuses the exact same "online" derivation state() already
  /// uses for its cloud fallback, just without needing the full state
  /// payload - this is purely a reachability check for the connectivity
  /// banner.
  static Future<bool> _cloudReachable() async {
    final cloud = await _stateFromCloud();
    return cloud != null;
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
      Store.displayName = cred.user?.displayName ?? '';
      return null;
    } on fb.FirebaseAuthException catch (e) {
      return _friendlyAuthError(e);
    } catch (e) {
      return 'Sign-in error: $e';
    }
  }

  /// Email+password account creation via Firebase directly.
  /// `name` (optional) is saved as the Firebase Auth displayName, shown
  /// in the app header/profile.
  static Future<String?> signup(String email, String password, {String? name}) async {
    try {
      final cred = await _auth.createUserWithEmailAndPassword(
          email: email.trim(), password: password);
      if (name != null && name.trim().isNotEmpty) {
        await cred.user?.updateDisplayName(name.trim());
        await cred.user?.reload();
      }
      final idToken = await cred.user?.getIdToken();
      if (idToken == null) return 'Account created, but sign-in failed.';
      Store.token = idToken;
      Store.user = email;
      Store.displayName = name?.trim() ?? '';
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

  // Firebase ID tokens (Store.token) expire after 1 hour. Previously the
  // ONLY place that ever force-refreshed one was app boot
  // (main.dart's _boot(), getIdToken(true)) - so any session left open
  // (or just backgrounded/foregrounded) past an hour started silently
  // 401-ing on every LAN call. The server (web/server.py's guard()) then
  // rejects the request, api.dart's LAN branch here sees a non-200 and
  // falls through to the Firestore cloud path, which comes back [] for
  // any alert that hasn't been cloud-published - net effect: alerts
  // "disappear" from Home/Alerts even though the laptop, the LAN, and
  // the DB are all completely fine. This is the root cause of that.
  //
  // Fix has two parts: (1) a periodic background refresh so the token
  // rarely goes stale in the first place (see startTokenAutoRefresh
  // below), and (2) this helper, which detects a 401 on any LAN call,
  // force-refreshes once, and retries - so even a token that DID expire
  // self-heals on the very next tap instead of quietly falling back to
  // an incomplete cloud cache.
  static Future<bool> _refreshToken() async {
    try {
      final user = _auth.currentUser;
      if (user == null) return false;
      final fresh = await user.getIdToken(true)
          .timeout(const Duration(seconds: 8));
      if (fresh == null) return false;
      Store.token = fresh;
      return true;
    } catch (_) {
      return false;
    }
  }

  static Timer? _tokenRefreshTimer;

  /// Call once after login/boot (see main.dart). Proactively force-refreshes
  /// the Firebase ID token every 45 minutes so it never sits stale for the
  /// full 1-hour lifetime while the app stays open - the 401-retry in
  /// alerts() (and callers can add the same pattern elsewhere) is the
  /// safety net, this is the thing that stops it from being needed most of
  /// the time.
  static void startTokenAutoRefresh() {
    _tokenRefreshTimer?.cancel();
    _tokenRefreshTimer =
        Timer.periodic(const Duration(minutes: 45), (_) => _refreshToken());
  }

  static void stopTokenAutoRefresh() {
    _tokenRefreshTimer?.cancel();
    _tokenRefreshTimer = null;
  }

  static StreamSubscription<fb.User?>? _authRefreshSub;

  /// Belt-and-suspenders: rather than adding startTokenAutoRefresh() to
  /// every individual sign-in method above (login, signup, magic link,
  /// Google, QR pairing - easy to miss one), listen to Firebase's own
  /// auth-state stream once and start/stop the refresh timer from there.
  /// Call once, early (main.dart, right after Firebase.initializeApp()).
  static void watchAuthForTokenRefresh() {
    _authRefreshSub?.cancel();
    _authRefreshSub = _auth.authStateChanges().listen((user) {
      if (user != null) {
        startTokenAutoRefresh();
      } else {
        stopTokenAutoRefresh();
      }
    });
  }

  static Future<http.Response> _getAlertsOnce(int limit) {
    return http
        .get(_u('/api/alerts', {'limit': limit}), headers: _h)
        // Was 3s - too tight for this endpoint specifically (it does a
        // real DB query, unlike the live-view frame/stream requests which
        // are effectively instant), so a normal laptop-side query that
        // takes a bit longer under load was silently timing out here and
        // falling through to the cloud path below - which then came back
        // empty for any alert not yet synced to Firestore, even though the
        // phone showed "online" (that flag only reflects LAN/internet
        // reachability, not this specific call's success) and the laptop's
        // own dashboard had the data the whole time. Bumped to a more
        // realistic ceiling.
        .timeout(const Duration(seconds: 8));
  }

  static Future<List<dynamic>> alerts({int limit = 50}) async {
    // LAN fast-path first (instant, and works with zero internet).
    if (Store.hasServerAddress) {
      try {
        var r = await _getAlertsOnce(limit);
        if (r.statusCode == 401 && await _refreshToken()) {
          // Stale token, now refreshed - retry once with the new one
          // before giving up on the LAN path entirely.
          r = await _getAlertsOnce(limit);
        } else if (r.statusCode != 200) {
          // Store.baseUrl is a CACHED laptop address - if the laptop's IP
          // changed (new network, DHCP renewal, sleep/wake) that cache goes
          // stale and this request fails FOREVER even though phone and
          // laptop are on the same LAN and the laptop is fine. isLanReachable()
          // already has the self-healing logic to find the laptop's current
          // address (see its doc comment) and adopts it into Store.baseUrl
          // when it works - every OTHER LAN call already benefits from that
          // indirectly by calling isLanReachable() first via
          // refreshConnectivity(), but alerts() was hitting the URL
          // directly with no such check, so a stale cache here specifically
          // meant this call alone kept silently failing (-> empty cloud
          // fallback) even after the rest of the app self-healed and showed
          // "online" again. Try the same self-heal here, then retry once.
          if (await isLanReachable()) {
            r = await _getAlertsOnce(limit);
          }
        }
        if (r.statusCode == 200) {
          _reachable();
          return jsonDecode(r.body) as List<dynamic>;
        }
        // Non-200 (e.g. 401 expired token, 500 on the laptop) previously
        // fell through to cloud with zero trace of what actually happened -
        // now at least visible via lastError for debugging "why is this
        // empty" reports.
        lastError = '/api/alerts returned HTTP ${r.statusCode}';
      } catch (e) {
        // Timeout, connection refused, malformed response, etc. - record
        // it instead of silently falling through with no trace, same
        // reasoning as above.
        lastError = '/api/alerts LAN request failed: $e';
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
      // Explicit timeout - same fix as myDevices() above and for the same
      // reason: with no internet, the Firestore SDK's own internal
      // retry/backoff (not bounded by anything in THIS code) can sit here
      // far longer than feels like "loading" should ever take. This call
      // runs every few seconds from _checkAlerts()'s periodic timer, so an
      // unbounded call here compounds with the same overlapping-calls risk
      // just fixed in refreshConnectivity()/​_refreshLanProbe() above.
      final q = await _fs
          .collection('alerts')
          .where('owner_uid', isEqualTo: uid)
          .orderBy('created_at', descending: true)
          .limit(limit)
          .get()
          .timeout(const Duration(seconds: 4));

      // Reconciliation against hard-delete tombstones (see
      // storage/database.py's deleted_alerts table and cloud_alerts.py's
      // delete_alert()/_write_tombstone()). Off-LAN, this alerts
      // collection is the ONLY thing the phone reads - if a delete's
      // direct doc-removal write to Firestore failed for any reason
      // (laptop briefly offline mid-delete, a transient Firestore error)
      // while the tombstone write succeeded (or vice versa - either can
      // independently fail), this is what stops a deleted alert from
      // reappearing forever for a phone reading cross-network. Best-
      // effort and bounded by its own short timeout - a failure here
      // just means tombstone filtering is skipped for this one refresh,
      // not that the whole alerts load fails.
      Set<dynamic> deletedIds = {};
      try {
        final tq = await _fs
            .collection('deleted_alerts')
            .where('owner_uid', isEqualTo: uid)
            .limit(2000)
            .get()
            .timeout(const Duration(seconds: 3));
        deletedIds = tq.docs.map((d) => d.data()['alert_id']).toSet();
      } catch (_) {
        // Tombstone fetch failing is not fatal - see comment above.
      }

      return q.docs
          .map((d) {
            final m = d.data();
            return {
              'id': m['alert_id'],
              'tier': m['tier'] ?? 1,
              'event': m['event'],
              'distance_m': m['distance_m'],
              'confidence': m['confidence'],
              'camera': m['camera'],
              'timestamp': m['timestamp'],
              // Cloud alerts carry a full https snapshot URL (Firebase
              // Storage signed URL); LAN alerts carry a path relative to
              // the laptop.
              'snapshot': m['snapshot_url'],
              'snapshot_url': m['snapshot_url'],
              'has_video': m['has_video'] ?? false,
              'remote': true,
            };
          })
          .where((a) => !deletedIds.contains(a['id']))
          .toList();
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
      // Without an explicit timeout, a fully offline phone can sit here for
      // as long as the Firestore SDK's own internal retry/backoff takes to
      // give up - which is this call's OWN app-level bound, not something
      // that depends on any other network code in the app. This is what
      // was making the very first screen after login (DeviceGate) hang
      // indefinitely with no internet, when the app should have opened
      // straight into local/offline mode using the cached lastDeviceId.
      final q = await _fs
          .collection('devices')
          .where('owner_uid', isEqualTo: uid)
          .get()
          .timeout(const Duration(seconds: 4));
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
  ///
  /// SELF-HEALING: Store.baseUrl is a CACHED address from whenever pairing
  /// last happened. If the laptop's IP changes later (new network, DHCP
  /// lease renewal, sleep/wake) that cache goes stale and every future
  /// probe fails forever - even though the phone and laptop are genuinely
  /// on the same LAN and the laptop is happily publishing its new address
  /// to Firestore on every heartbeat. So: if the cached address fails,
  /// before giving up, pull the laptop's latest last_lan_ip/last_lan_port
  /// from its Firestore device doc and try THAT instead. If it works,
  /// adopt it as the new Store.baseUrl so the fast path stays fast on
  /// every subsequent call - this one retry is what makes a changed
  /// laptop IP self-correct within one connectivity poll instead of
  /// requiring the user to notice, dig into settings, and re-pair.
  static Future<bool> isLanReachable() async {
    if (await _probeCurrentBaseUrl()) return true;

    final fresh = await _fetchFreshLanAddress();
    if (fresh != null) {
      final candidate = 'http://${fresh['ip']}:${fresh['port']}';
      if (candidate != Store.baseUrl) {
        final previous = Store.baseUrl;
        Store.baseUrl = candidate;
        if (await _probeCurrentBaseUrl()) return true;
        Store.baseUrl = previous; // didn't help - don't thrash a working cache for nothing
      }
    }

    // Firestore's last_lan_ip is only as fresh as the laptop's last
    // successful heartbeat - which itself requires the LAPTOP to have
    // internet. In the "phone hotspot, laptop connected, mobile data off"
    // scenario, the laptop ALSO has zero internet, so it can never publish
    // a fresh address there either - the fallback above has nothing
    // current to read. A plain UDP broadcast on the local network needs
    // no internet on either side, so it's the last resort that actually
    // covers that exact case.
    final discovered = await _discoverLanAddressViaBroadcast();
    if (discovered == null) return false;
    final candidate = 'http://${discovered['ip']}:${discovered['port']}';
    if (candidate == Store.baseUrl) return false;
    final previous = Store.baseUrl;
    Store.baseUrl = candidate;
    if (await _probeCurrentBaseUrl()) return true;
    Store.baseUrl = previous;
    return false;
  }

  static Future<bool> _probeCurrentBaseUrl() async {
    if (!Store.hasServerAddress) return false;
    try {
      final r = await http.get(_u('/api/state'), headers: _h)
          .timeout(const Duration(seconds: 3));
      return r.statusCode == 200;
    } catch (_) {
      return false;
    }
  }

  /// Reads last_lan_ip/last_lan_port for the currently-paired device
  /// straight from Firestore - the laptop keeps this fresh on every
  /// heartbeat regardless of whether the phone can currently reach it.
  /// Requires internet on THIS phone (to reach Firestore) and requires the
  /// laptop to have had internet recently enough to have published a fresh
  /// value - see _discoverLanAddressViaBroadcast() for the zero-internet
  /// fallback when neither condition holds.
  static Future<Map<String, dynamic>?> _fetchFreshLanAddress() async {
    final deviceId = Store.lastDeviceId;
    if (deviceId == null) return null;
    try {
      final snap = await _fs.collection('devices').doc(deviceId).get()
          .timeout(const Duration(seconds: 4));
      final data = snap.data();
      final ip = data?['last_lan_ip'];
      final port = data?['last_lan_port'];
      if (ip == null || port == null) return null;
      return {'ip': ip, 'port': port};
    } catch (_) {
      return null;
    }
  }

  static const int _lanDiscoveryPort = 58471;   // must match web/server.py's LAN_DISCOVERY_PORT
  static const String _lanDiscoveryMagic = 'CAPHY_DISCOVER_V1';

  /// Broadcasts a "where are you?" UDP packet on the local network and
  /// waits briefly for the laptop's reply. Needs NO internet on either
  /// side - this is what actually covers "phone hotspot, laptop connected,
  /// mobile data off", which Firestore-based discovery structurally cannot
  /// solve (both sides would need internet to use Firestore at all).
  static Future<Map<String, dynamic>?> _discoverLanAddressViaBroadcast() async {
    RawDatagramSocket? socket;
    try {
      socket = await RawDatagramSocket.bind(InternetAddress.anyIPv4, 0)
          .timeout(const Duration(seconds: 2));
      socket.broadcastEnabled = true;
      socket.send(utf8.encode(_lanDiscoveryMagic),
          InternetAddress('255.255.255.255'), _lanDiscoveryPort);

      final completer = Completer<Map<String, dynamic>?>();
      late final StreamSubscription sub;
      sub = socket.listen((event) {
        if (event != RawSocketEvent.read) return;
        final dg = socket!.receive();
        if (dg == null) return;
        try {
          final data = jsonDecode(utf8.decode(dg.data)) as Map<String, dynamic>;
          if (data['ip'] != null && data['port'] != null && !completer.isCompleted) {
            completer.complete(data);
          }
        } catch (_) {}
      });

      final result = await completer.future
          .timeout(const Duration(seconds: 3), onTimeout: () => null);
      await sub.cancel();
      return result;
    } catch (_) {
      return null;
    } finally {
      socket?.close();
    }
  }

  /// Cloud fallback for arm/disarm/snapshot/siren/acknowledge when the
  /// phone can't reach the laptop's LAN address (different network /
  /// traveling). Enqueues a command doc in Firestore; the laptop's realtime
  /// listener (web/server.py's _start_command_listener /
  /// device_registry.watch_pending_commands) picks it up the instant
  /// Firestore pushes the change - no fixed poll interval on that side
  /// anymore - and executes it locally, then writes the result back. This
  /// never bypasses the laptop's own Flask API - the laptop just calls
  /// itself, so local and remote control share one code path.
  ///
  /// This also no longer polls on the phone side: it attaches a
  /// .snapshots() listener to the command doc (same pattern already used
  /// for WebRTC call signaling in webrtc_call.dart) and resolves the very
  /// moment the laptop's status write arrives, instead of waiting for the
  /// next poll tick. onStateChange (optional) is invoked with each
  /// intermediate status ('pending' -> 'executing' -> 'done'/'error') so
  /// callers can drive an optimistic "Arming..." -> "Armed" UI without
  /// polling themselves.
  static Future<Map<String, dynamic>?> sendRemoteCommand(
    String deviceId,
    String type, {
    Map<String, dynamic>? args,
    void Function(String status)? onStateChange,
    Duration timeout = const Duration(seconds: 15),
  }) async {
    final uid = _auth.currentUser?.uid;
    if (uid == null) return null;

    fs.DocumentReference<Map<String, dynamic>>? ref;
    StreamSubscription<fs.DocumentSnapshot<Map<String, dynamic>>>? sub;
    Timer? timeoutTimer;
    final completer = Completer<Map<String, dynamic>?>();

    void finish(Map<String, dynamic>? result) {
      if (completer.isCompleted) return;
      timeoutTimer?.cancel();
      sub?.cancel();
      completer.complete(result);
    }

    try {
      ref = await _fs
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
      debugPrint('[CAPHY] sendRemoteCommand: wrote ${ref.path} type=$type');
    } catch (e) {
      debugPrint('[CAPHY] sendRemoteCommand: write FAILED: $e');
      lastError = 'Remote command failed: $e';
      return null;
    }

    // Single listener per in-flight command, cancelled the moment it
    // resolves or times out - never left dangling.
    sub = ref.snapshots().listen((snap) {
      final data = snap.data();
      if (data == null) {
        debugPrint('[CAPHY] sendRemoteCommand: snapshot with null data for ${ref?.path}');
        return;
      }
      final status = data['status'] as String? ?? 'pending';
      debugPrint('[CAPHY] sendRemoteCommand: snapshot status=$status for ${ref?.path}');
      onStateChange?.call(status);
      if (status != 'pending' && status != 'executing') {
        finish(Map<String, dynamic>.from(data));
      }
    }, onError: (e) {
      debugPrint('[CAPHY] sendRemoteCommand: listener onError: $e');
      lastError = 'Remote command listener error: $e';
      finish(null);
    });

    timeoutTimer = Timer(timeout, () {
      debugPrint('[CAPHY] sendRemoteCommand: TIMED OUT waiting for ${ref?.path}');
      finish({'status': 'timeout'});
    });

    return completer.future;
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
    if (lastLanOk) {
      try {
        final r = await http.post(_u('/api/snapshot/$cam'), headers: _h)
            .timeout(const Duration(seconds: 5));
        _reachable();
        return r.statusCode == 200 && jsonDecode(r.body)['ok'] == true;
      } catch (e) {
        _unreachable(e);
        // Previously had no cross-network fallback at all (unlike
        // arm/disarm/siren/dismiss) - snapshot silently failed whenever the
        // phone wasn't on the laptop's WiFi. Same fallback pattern as the
        // others now.
      }
    }
    if (Store.lastDeviceId != null) {
      // Must pass the actual camera index - the laptop's route_map
      // (_execute_remote_command in web/server.py) previously ignored this
      // and always hit camera 0, so snapshotting camera 1+ cross-network
      // silently snapshotted the wrong camera.
      final result = await sendRemoteCommand(Store.lastDeviceId!, 'snapshot',
          args: {'cam': cam});
      if (result != null && result['status'] == 'done') return true;
    }
    return false;
  }

  /// Toggle night vision for a camera. Returns the new on/off state, or
  /// null if the command could not be delivered at all (mirrors siren()'s
  /// true/false/null shape - see its comment - false and "failed" must not
  /// be conflated, or successfully turning night vision OFF would show as
  /// an error).
  ///
  /// Was the ONE control button on the whole Live tab with no timeout at
  /// all on its network call and no cross-network/cloud fallback -
  /// arm/disarm, siren, snapshot, and record all already had a timeout +
  /// fallback (see siren()/record() above). On a slow connection or with
  /// the laptop briefly unreachable, this call could hang indefinitely -
  /// no timeout meant nothing ever gave up - and the caller (live_tab.dart)
  /// had no pending/cancel state for this button either, so a slow tap
  /// looked and felt exactly like the whole app freezing: no spinner, no
  /// error, nothing tappable-feeling, until the OS-level socket eventually
  /// gave up on its own (which is why it "recovered after a delay" instead
  /// of ever actually crashing). Same timeout-then-cloud-fallback pattern
  /// as siren()/record() now, plus live_tab.dart got its own pending/
  /// cancel-on-retap handling to match every other button there.
  static Future<bool?> nightVision(int cam) async {
    if (lastLanOk) {
      try {
        final r = await http.post(_u('/api/nightvision/$cam'), headers: _h)
            .timeout(const Duration(seconds: 5));
        _reachable();
        if (r.statusCode == 200) return jsonDecode(r.body)['on'] == true;
      } catch (e) {
        _unreachable(e);
        // fall through to cloud
      }
    }
    if (Store.lastDeviceId != null) {
      final result = await sendRemoteCommand(
          Store.lastDeviceId!, 'nightvision', args: {'cam': cam});
      if (result != null && result['status'] == 'done') {
        final body = result['result'];
        if (body is Map && body.containsKey('on')) return body['on'] == true;
        return true; // delivered but shape unknown - treat as success, state unclear
      }
    }
    return null;
  }

  /// Toggles the manual siren override. Returns the NEW siren state (true =
  /// now sounding, false = now silent) on success, or null if the command
  /// could not be delivered at all.
  ///
  /// Was Future<bool>, collapsing "the siren is now OFF" and "the request
  /// failed" into the same `false` return value - the caller (live_tab.dart)
  /// could not tell them apart, so successfully turning the siren OFF
  /// showed a red "Could not reach CAPHY - try again" error toast, which
  /// is exactly backwards. null now means "truly failed", false means
  /// "succeeded, and the siren is off" - the two are no longer conflated.
  static Future<bool?> siren() async {
    if (lastLanOk) {
      try {
        // Timeout added - this direct call previously had NONE, so on mobile
        // data (laptop unreachable at its LAN IP) it could hang far longer
        // than expected before ever falling through to the cloud command
        // path below, which is exactly what a "stuck forever" button looks
        // like from the outside. Now also skipped entirely when we already
        // know we're off-LAN, instead of always eating the 5s timeout first.
        final r = await http.post(_u('/api/siren'), headers: _h)
            .timeout(const Duration(seconds: 5));
        if (r.statusCode == 200) return jsonDecode(r.body)['on'] == true;
      } catch (_) {
        // fall through to cloud
      }
    }
    if (Store.lastDeviceId != null) {
      final result = await sendRemoteCommand(Store.lastDeviceId!, 'siren');
      if (result != null && result['status'] == 'done') {
        final body = result['result'];
        if (body is Map && body.containsKey('on')) return body['on'] == true;
        return true; // delivered but shape unknown - treat as success, state unclear
      }
    }
    return null;
  }

  /// Toggle recording. Returns the full response:
  ///   {recording: bool, video_url?: string}  (video_url present when stopped)
  ///
  /// Previously had NO timeout and NO cross-network fallback at all -
  /// unlike every other action button (arm/disarm/siren/snapshot), Record
  /// simply did nothing when the phone wasn't on the laptop's WiFi. Same
  /// timeout-then-cloud-fallback pattern as the others now.
  static Future<Map<String, dynamic>> record(int cam) async {
    if (lastLanOk) {
      try {
        final r = await http.post(_u('/api/record/$cam'), headers: _h)
            .timeout(const Duration(seconds: 5));
        _reachable();
        if (r.statusCode == 200) return jsonDecode(r.body) as Map<String, dynamic>;
      } catch (e) {
        _unreachable(e);
        // fall through to cloud
      }
    }
    if (Store.lastDeviceId != null) {
      final result = await sendRemoteCommand(Store.lastDeviceId!, 'record',
          args: {'cam': cam});
      if (result != null && result['status'] == 'done') {
        final body = result['result'];
        if (body is Map) return Map<String, dynamic>.from(body);
      }
    }
    return {'recording': false};
  }

  /// Sends one line of text to CAPHY's voice assistant (v2) and returns its
  /// classified response - {"type": "talk"|"know"|"do"|"clarify"|"error",
  /// "reply": "...", ...}. This is the text-only round trip; wake-word/STT
  /// capture is a separate, not-yet-built layer that will call this same
  /// method once transcription is solid on its own.
  /// Voice/chat assistant. The AI itself (Groq) is ALWAYS called server-
  /// side from web/server.py's /api/assistant, never from the phone
  /// directly - the Groq API key is a real billed secret (same reasoning
  /// as why the WebRTC TURN relay key is never embedded in the app
  /// either: a decompiled APK could extract it and run up usage on the
  /// account). That part doesn't change cross-network.
  ///
  /// What DOES change is the transport: same-WiFi tries a direct HTTP call
  /// first (fast, works with zero internet too), and now falls back to
  /// the same Firestore command queue every other action (arm/disarm/
  /// siren/acknowledge) already uses when that direct call fails - the
  /// laptop's realtime command listener picks it up and calls its own
  /// /api/assistant internally (see the "assistant" entry in
  /// _execute_remote_command's route_map, web/server.py), then relays the
  /// full parsed reply back. Previously this had NO fallback at all,
  /// which is why "arm the system" spoken through voice failed
  /// cross-network even though the exact same action via the Arm BUTTON
  /// worked fine - this brings voice-issued direct commands in line with
  /// every other remote action. Open-ended talk/know questions still
  /// genuinely require the laptop to be reachable (it's still doing the
  /// Groq call), so those still fail if the laptop itself is off/down -
  /// but now they fail through the SAME reliable path as everything else,
  /// not a dead end.
  /// history: recent prior turns as [{"role": "user"|"assistant", "text": "..."}],
  /// oldest first - sent so the server can build a real back-and-forth
  /// conversation for Groq instead of treating every message as the first
  /// one ever asked (see assistant_ai/router.py's _build_messages). Optional
  /// and capped by the caller (ask_caphy_screen.dart keeps only the last
  /// few turns) - this endpoint itself also caps it server-side as a
  /// backstop.
  static Future<Map<String, dynamic>> assistant(String text,
      {List<Map<String, String>>? history}) async {
    final body = <String, dynamic>{'text': text};
    if (history != null && history.isNotEmpty) body['history'] = history;
    try {
      // Short timeout on the direct attempt, same reasoning as state()'s
      // 3s timeout above: on mobile data the saved LAN address isn't
      // routable at all, so this can only ever time out - the old 8s
      // timeout here meant every single cross-network voice request spent
      // a full 8 seconds failing before the (also slow, Groq-backed) cloud
      // fallback even started, which is a huge chunk of exactly the
      // "voice assistant is too slow" complaint. Same-WiFi replies are
      // typically well under a second, so 4s is still generous there while
      // cutting the cross-network dead-wait by more than half.
      final r = await http
          .post(_u('/api/assistant'), headers: _h, body: jsonEncode(body))
          .timeout(const Duration(seconds: 4));
      _reachable();
      if (r.statusCode == 200) return jsonDecode(r.body) as Map<String, dynamic>;
    } catch (e) {
      _unreachable(e);
      // fall through to the cloud command queue below
    }

    if (Store.lastDeviceId != null) {
      // Must be longer than the laptop's own internal timeout for this
      // command type (20s - see _execute_remote_command's route_map in
      // web/server.py, which needs headroom for the Groq call itself) or
      // the phone could give up and show "can't reach the laptop" right
      // as the laptop was about to finish and write the real reply.
      final result = await sendRemoteCommand(
          Store.lastDeviceId!, 'assistant',
          args: {'text': text}, timeout: const Duration(seconds: 25));
      if (result != null && result['status'] == 'done') {
        final r = result['result'];
        if (r is Map<String, dynamic>) return r;
      }
    }

    return {
      'type': 'error',
      'reply': "Can't reach your CAPHY laptop right now, so I can't answer "
          "that. Make sure it's powered on and running CAPHY, then try again.",
    };
  }

  /// Single-frame live-view URL for a camera (poll this repeatedly).
  static String frameUrl(int cam) =>
      '${Store.baseUrl}/api/frame/$cam?token=${Store.token}';

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
          await _fs.collection('devices').doc(deviceId).get()
              .timeout(const Duration(seconds: 4));
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
    // Skip the direct-LAN attempt entirely when the last connectivity probe
    // already confirmed we're off-LAN (mobile data, different network) -
    // otherwise every cross-network tap eats a full 5s dead timeout before
    // even starting the Firestore command queue below, which is what
    // actually produced the reported "6-7 second" button lag.
    if (lastLanOk) {
      try {
        final r = await http
            .post(_u('/api/arm'), headers: _h, body: jsonEncode({'on': on}))
            .timeout(const Duration(seconds: 5));
        _reachable();
        if (r.statusCode == 200) return jsonDecode(r.body)['armed'] == true;
      } catch (e) {
        _unreachable(e);
      }
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
    if (lastLanOk) {
      try {
        final r = await http.post(_u('/api/camera/power'),
                headers: _h, body: jsonEncode({'on': on}))
            .timeout(const Duration(seconds: 5));
        _reachable();
        if (r.statusCode == 200) return jsonDecode(r.body)['camera_on'] == true;
      } catch (e) {
        _unreachable(e);
      }
    }

    if (Store.lastDeviceId != null) {
      final result = await sendRemoteCommand(
          Store.lastDeviceId!, on ? 'camera_on' : 'camera_off');
      if (result != null && result['status'] == 'done') return on;
    }
    return null;
  }

  static Future<bool?> setEmergency(bool on) async {
    if (lastLanOk) {
      try {
        final r = await http.post(_u('/api/emergency'),
                headers: _h, body: jsonEncode({'on': on}))
            .timeout(const Duration(seconds: 5));
        _reachable();
        if (r.statusCode == 200) return jsonDecode(r.body)['emergency'] == true;
      } catch (e) {
        _unreachable(e);
      }
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
    if (lastLanOk) {
      try {
        final r = await http.post(_u('/api/alert/$id/dismiss'), headers: _h)
            .timeout(const Duration(seconds: 5));
        _reachable();
        if (r.statusCode == 200) return true;
      } catch (e) {
        _unreachable(e);
        // fall through to cloud - see the siren()/arm() pattern above. This
        // was previously MISSING here entirely, unlike every other remote
        // action: on a different network than the laptop (phone on mobile
        // data, laptop on Wi-Fi), the direct call above always fails, this
        // returned false with no fallback, and the caller's failure handler
        // re-loaded the whole alert list - which is exactly what looked
        // like "Acknowledge keeps reloading / isn't working" cross-network.
      }
    }
    if (Store.lastDeviceId != null) {
      final result = await sendRemoteCommand(
          Store.lastDeviceId!, 'dismiss_alert', args: {'id': id});
      if (result != null && result['status'] == 'done') return true;
    }
    return false;
  }

  // ---- Offline delete queue -------------------------------------------
  // A delete that fails outright (phone genuinely offline, laptop
  // unreachable AND no paired device to route a remote command to) used
  // to just fail silently from the user's perspective - the alert stayed
  // in the list with no indication it would ever actually go away, and
  // the user had to remember to retry it themselves later. This is a real
  // persisted queue (survives app restarts, not just in-memory) that a
  // failed delete gets added to, and that gets drained automatically the
  // next time the app has ANY working connection - the user taps delete
  // once, ever, regardless of how many attempts it actually takes.
  static const _pendingDeletesKey = 'pendingDeleteAlertIds';

  static List<int> get _pendingDeletes {
    final raw = Store._p.getStringList(_pendingDeletesKey) ?? const [];
    return raw.map(int.tryParse).whereType<int>().toList();
  }

  static Future<void> _setPendingDeletes(List<int> ids) => Store._p.setStringList(
      _pendingDeletesKey, ids.map((e) => e.toString()).toList());

  static Future<void> _queuePendingDelete(int id) async {
    final ids = _pendingDeletes;
    if (!ids.contains(id)) {
      ids.add(id);
      await _setPendingDeletes(ids);
    }
  }

  static Future<void> _unqueuePendingDelete(int id) async {
    final ids = _pendingDeletes;
    if (ids.remove(id)) await _setPendingDeletes(ids);
  }

  /// True if `id` has a delete queued but not yet confirmed - the alert
  /// list UI uses this to grey out / show "deleting..." on a row that's
  /// waiting for connectivity, instead of it looking like the delete tap
  /// did nothing at all.
  static bool isDeletePending(int id) => _pendingDeletes.contains(id);

  /// Drains the offline delete queue: retries every still-pending delete
  /// id via the SAME path deleteAlert() itself uses (LAN, then cross-
  /// network command), removing each from the queue only on confirmed
  /// success. Call this whenever connectivity is regained - api.dart's
  /// connectivity listener and alerts_tab.dart's periodic refresh both
  /// do. Safe to call anytime (including with an empty queue, or while
  /// fully offline - each attempt just re-fails and stays queued).
  static Future<void> flushPendingDeletes() async {
    final ids = _pendingDeletes;
    for (final id in ids) {
      final ok = await _deleteAlertAttempt(id);
      if (ok) await _unqueuePendingDelete(id);
    }
  }

  /// Permanently deletes an alert (and its snapshot/video on the laptop) -
  /// different from dismissAlert(), which only hides it. Same LAN-then-
  /// cross-network-queue fallback pattern as every other alert action
  /// here. On total failure (not just a slow retry-able blip, but no path
  /// reached the laptop at all), the id is queued for automatic retry via
  /// flushPendingDeletes() instead of the delete just silently never
  /// happening - the caller still gets `false` back for this specific
  /// call (so it can show "queued, will retry" rather than claiming
  /// success it can't back up), but the alert WILL disappear once
  /// connectivity allows, with no further action needed from the user.
  static Future<bool> deleteAlert(int id) async {
    final ok = await _deleteAlertAttempt(id);
    if (ok) {
      await _unqueuePendingDelete(id);
    } else {
      await _queuePendingDelete(id);
    }
    return ok;
  }

  static Future<bool> _deleteAlertAttempt(int id) async {
    if (lastLanOk) {
      try {
        final r = await http.post(_u('/api/alert/$id/delete'), headers: _h)
            .timeout(const Duration(seconds: 8));
        _reachable();
        if (r.statusCode == 200) return true;
        // 404 (alert_not_found) or 409 (alert_already_deleted) both mean
        // this id is already gone server-side - not a failure from the
        // caller's point of view, since the end state (alert doesn't
        // exist) is exactly what was asked for.
        if (r.statusCode == 404 || r.statusCode == 409) return true;
        lastError = '/api/alert/$id/delete returned HTTP ${r.statusCode}';
      } catch (e) {
        lastError = '/api/alert/$id/delete LAN request failed: $e';
        _unreachable(e);
      }
    }
    if (Store.lastDeviceId == null) {
      lastError = 'delete_alert: no paired device to route the cross-network command to';
      return false;
    }
    // Previously any failure here (timeout, Firestore write rejected,
    // command claimed but never completed by the laptop, etc) was
    // completely silent - lastError was never touched on this path, so
    // the only feedback was a generic "Could not reach CAPHY" toast with
    // no way to tell LAN-timeout apart from "the remote command genuinely
    // failed" or "it timed out waiting for the laptop to pick it up".
    final result = await sendRemoteCommand(
        Store.lastDeviceId!, 'delete_alert', args: {'id': id});
    if (result != null && result['status'] == 'done') return true;
    if (result == null) {
      lastError = 'delete_alert: remote command timed out or Firestore write failed';
    } else {
      lastError = 'delete_alert: remote command finished with status '
          '"${result['status']}" - result: ${result['result']}';
    }
    return false;
  }

  static Future<bool> restoreAlert(int id) async {
    try {
      final r = await http.post(_u('/api/alert/$id/restore'), headers: _h)
          .timeout(const Duration(seconds: 5));
      _reachable();
      return r.statusCode == 200;
    } catch (e) {
      _unreachable(e);
      return false;
    }
  }

  static Future<bool> dismissAllAlerts() async {
    if (lastLanOk) {
      try {
        final r = await http.post(_u('/api/alerts/dismiss_all'), headers: _h)
            .timeout(const Duration(seconds: 5));
        _reachable();
        if (r.statusCode == 200) return true;
      } catch (e) {
        _unreachable(e);
        // Same cross-network fallback as dismissAlert() above.
      }
    }
    if (Store.lastDeviceId != null) {
      final result = await sendRemoteCommand(Store.lastDeviceId!, 'dismiss_all_alerts');
      if (result != null && result['status'] == 'done') return true;
    }
    return false;
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
