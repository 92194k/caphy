import 'dart:async';
import 'package:cloud_firestore/cloud_firestore.dart' as fs;
import 'package:firebase_auth/firebase_auth.dart' as fb;
import 'package:flutter_webrtc/flutter_webrtc.dart';

/// True live video from a CAPHY desktop that isn't on the phone's current
/// WiFi. Video itself flows phone <-> laptop directly (or through a free
/// TURN relay when direct peer-to-peer can't form) - this class only
/// handles the "handshake" (SDP offer/answer + ICE candidates), carried
/// over Firestore's webrtc_calls/{callId} the same way arm/disarm rides
/// the commands/ queue in api.dart's sendRemoteCommand().
///
/// On the SAME WiFi as the laptop, prefer the existing MjpegView(url:
/// Api.streamUrl(...)) instead - simpler, zero connection setup delay.
/// This class is specifically the "I'm not on that network" path.
class WebrtcCall {
  final String deviceId;
  final int cam;

  RTCPeerConnection? _pc;
  MediaStream? remoteStream;
  String? _callId;
  StreamSubscription<fs.DocumentSnapshot>? _callSub;
  final Set<String> _appliedRemoteIce = {};
  Timer? _answerTimeout;
  Timer? _connectTimeout;
  Timer? _heartbeatTimer;
  bool _gotAnswer = false;
  bool _gotVideo = false;

  final _fs = fs.FirebaseFirestore.instance;
  final _auth = fb.FirebaseAuth.instance;

  final void Function(MediaStream stream)? onRemoteStream;
  final void Function(String state)? onConnectionState;
  final void Function(String error)? onError;
  final void Function()? onTurnUnavailable;

  bool _configuredIceFromAnswer = false;
  List? _lastAppliedIceServers;

  // Shared across every WebrtcCall instance in the app (not per-instance),
  // keyed by deviceId. Before this, every tile that opens a call - e.g.
  // the live-view grid's two tiles, which both call start() within the
  // same frame on tab-open - independently ran its own
  // devices/{deviceId}.get() just to read the laptop's published TURN
  // servers. Same document, same near-identical timestamp, fetched twice
  // (or more, with more camera slots) for no reason: the answer is
  // identical every time within a call session, and the answer-time
  // upgrade (see _onCallDocUpdate) already re-fetches fresher TURN
  // credentials anyway once the laptop answers. Caching the in-flight
  // Future (not just the result) means two calls starting in the same
  // frame share the SAME pending request instead of both firing their
  // own - cutting concurrent reads against this document from N to 1
  // regardless of how many tiles/cameras are opening calls at once.
  static final Map<String, Future<List<Map<String, dynamic>>>> _turnServersCache = {};

  static Future<List<Map<String, dynamic>>> _fetchTurnServers(
      fs.FirebaseFirestore db, String deviceId) {
    return _turnServersCache.putIfAbsent(deviceId, () async {
      final result = <Map<String, dynamic>>[];
      try {
        final snap = await db.collection('devices').doc(deviceId).get();
        final published = snap.data()?['turn_servers'];
        if (published is List) {
          for (final e in published) {
            if (e is Map && e['urls'] != null) {
              result.add({
                'urls': e['urls'],
                if (e['username'] != null) 'username': e['username'],
                if (e['credential'] != null) 'credential': e['credential'],
              });
            }
          }
        }
      } catch (_) {
        // Swallow - callers fall back to STUN-only defaults on empty list.
      } finally {
        // Don't keep a stale/failed result cached forever - a fresh call a
        // few seconds later (e.g. after reconnecting) should retry rather
        // than being stuck replaying an empty list from a transient
        // failure. Short-lived cache: just long enough to de-duplicate
        // calls starting within the same burst.
        Future.delayed(const Duration(seconds: 5), () {
          _turnServersCache.remove(deviceId);
        });
      }
      return result;
    });
  }

  bool _iceServersEqual(List? a, List? b) {
    if (a == null || b == null) return false;
    if (a.length != b.length) return false;
    // Cheap structural comparison - good enough here since this only
    // gates whether to bother calling setConfiguration() again, not a
    // security check. A false negative just means one harmless extra
    // setConfiguration() call.
    for (int i = 0; i < a.length; i++) {
      if (a[i].toString() != b[i].toString()) return false;
    }
    return true;
  }

  WebrtcCall({
    required this.deviceId,
    required this.cam,
    this.onRemoteStream,
    this.onConnectionState,
    this.onError,
    this.onTurnUnavailable,
  });

  // Public STUN servers only - safe to embed in the app (no secret, no
  // cost, no account tied to them). Real TURN credentials are NEVER put
  // in the app itself (an APK can be decompiled, which would leak the
  // Metered API key straight into anyone's hands). Instead the LAPTOP
  // fetches fresh TURN credentials server-side (web/webrtc_stream.py,
  // using secrets_config.py) and hands them back inside its answer -
  // see _onCallDocUpdate below. The connection starts on STUN-only so
  // offer/answer negotiation isn't blocked waiting on that fetch, then
  // upgrades to the real TURN servers the moment the answer arrives,
  // before the harder NAT-traversal candidates are actually needed.
  static const _stunOnlyDefaults = [
    {'urls': 'stun:stun.l.google.com:19302'},
    {'urls': 'stun:global.stun.twilio.com:3478'},
  ];

  /// Starts the call: creates a local RTCPeerConnection (STUN-only to
  /// start), generates an SDP offer, writes it (+ this account's uid, so
  /// Firestore rules can check ownership) to a new webrtc_calls doc, then
  /// watches that doc for the laptop's answer, its real TURN credentials,
  /// and trickled ICE candidates.
  Future<void> start() async {
    final uid = _auth.currentUser?.uid;
    if (uid == null) {
      onError?.call('Not signed in');
      return;
    }

    // Pull the laptop's published TURN servers BEFORE building the peer
    // connection, so relay candidates are gathered from the very start.
    // Without this, mobile-data calls (carrier-grade NAT) can't connect -
    // the relay is the only possible path and it's too late to add it once
    // ICE gathering has begun. Falls back to STUN-only if none are
    // published yet (e.g. laptop just started).
    List<Map<String, dynamic>> iceServers =
        List<Map<String, dynamic>>.from(_stunOnlyDefaults);
    // Shared/de-duplicated across concurrent calls to the same device -
    // see _fetchTurnServers above. Falls back to STUN-only (the list
    // above) if this returns empty for any reason; the answer-time
    // upgrade in _onCallDocUpdate still applies on top regardless.
    iceServers.addAll(await _fetchTurnServers(_fs, deviceId));

    final config = <String, dynamic>{
      'iceServers': iceServers,
      'sdpSemantics': 'unified-plan',
    };

    _pc = await createPeerConnection(config);

    _pc!.onTrack = (RTCTrackEvent event) {
      if (event.track.kind == 'video' && event.streams.isNotEmpty) {
        _gotVideo = true;
        _connectTimeout?.cancel();
        remoteStream = event.streams.first;
        onRemoteStream?.call(remoteStream!);
      }
    };
    _pc!.onConnectionState = (RTCPeerConnectionState state) {
      onConnectionState?.call(state.toString());
    };

    // Receive-only: the phone watches the laptop's camera, it doesn't
    // send its own video/audio back.
    await _pc!.addTransceiver(
      kind: RTCRtpMediaType.RTCRtpMediaTypeVideo,
      init: RTCRtpTransceiverInit(direction: TransceiverDirection.RecvOnly),
    );

    final offer = await _pc!.createOffer();
    await _pc!.setLocalDescription(offer);

    // Retry-with-backoff around the create: this write's security rule
    // (firestore.rules, match /webrtc_calls/{callId}) does its own
    // server-side get(/devices/{deviceId}) to check ownership before
    // allowing it, separate from the TURN-server read above. That
    // get() can transiently fail - a just-refreshed/not-yet-propagated
    // ID token, a brief rules-evaluation hiccup - and previously any such
    // failure surfaced as a bare PERMISSION_DENIED with no retry, which
    // looked identical to "you don't own this device" even when the
    // token was simply stale for a moment. Two grid tiles opening calls
    // in the same frame made this more visible (more simultaneous
    // create attempts = more chances to catch a transient hiccup), but
    // it's not fundamentally a concurrency bug - it's a single missing
    // retry. A genuine ownership mismatch still fails all 3 attempts and
    // surfaces the same error as before.
    fs.DocumentReference<Map<String, dynamic>>? callRef;
    Object? lastError;
    for (var attempt = 0; attempt < 3; attempt++) {
      if (attempt > 0) {
        // Force a fresh ID token before retrying - if the previous
        // attempt failed because the token was stale, this is the actual
        // fix; if it wasn't, this is a harmless no-op.
        try {
          await _auth.currentUser?.getIdToken(true);
        } catch (_) {}
        await Future.delayed(Duration(milliseconds: 400 * attempt));
      }
      try {
        callRef = await _fs.collection('webrtc_calls').add({
          'device_id': deviceId,
          'caller_uid': uid,
          'cam': cam,
          'offer': {'sdp': offer.sdp, 'type': offer.type},
          'answer': null,
          'caller_ice': <Map<String, dynamic>>[],
          'status': 'pending',
          'created_at': fs.FieldValue.serverTimestamp(),
        });
        break;
      } catch (e) {
        lastError = e;
      }
    }
    if (callRef == null) {
      onError?.call('Could not start call: $lastError');
      return;
    }
    // Dart's null-safety promotion doesn't carry a nullable local across
    // closure boundaries (the Timer.periodic / onIceCandidate callbacks
    // below are separate function bodies) - even with the null check
    // right above, callRef itself still reads as nullable inside those
    // closures. Rebinding to a new non-nullable local here (instead of
    // sprinkling ! or ?. everywhere below) makes the "this is definitely
    // non-null past this point" guarantee explicit and keeps every use
    // below unchanged/readable.
    final fs.DocumentReference<Map<String, dynamic>> confirmedCallRef = callRef;
    _callId = confirmedCallRef.id;

    // Heartbeat: lets the laptop's call monitor (_monitor_call in
    // web/webrtc_stream.py) tell "phone is still actively watching" apart
    // from "phone backgrounded/crashed/left without hanging up cleanly" -
    // without this the laptop would keep encoding and streaming to a
    // viewer that's already gone. Firestore rules only allow the phone to
    // touch its own caller_ice / viewer_heartbeat fields, never
    // answer/status, so this can't be abused to interfere with the call.
    _heartbeatTimer = Timer.periodic(const Duration(seconds: 12), (_) {
      confirmedCallRef.update({
        'viewer_heartbeat': DateTime.now().millisecondsSinceEpoch / 1000.0,
      }).catchError((_) {});
    });

    _pc!.onIceCandidate = (RTCIceCandidate candidate) {
      if (candidate.candidate == null) return;
      confirmedCallRef.update({
        'caller_ice': fs.FieldValue.arrayUnion([
          {
            'candidate': candidate.candidate,
            'sdpMid': candidate.sdpMid,
            'sdpMLineIndex': candidate.sdpMLineIndex,
          }
        ])
      }).catchError((_) {});
    };

    _callSub = confirmedCallRef.snapshots().listen(_onCallDocUpdate, onError: (e) {
      onError?.call('Call signaling error: $e');
    });

    // If the laptop never answers, don't spin "Connecting…" forever. The
    // usual cause is that the laptop's WebRTC support isn't running - most
    // often aiortc isn't installed there (pip install aiortc av), or the
    // laptop is powered off / not signed in.
    _answerTimeout = Timer(const Duration(seconds: 20), () {
      if (_gotAnswer) return;
      onError?.call(
          'The laptop didn\'t answer the live video request.\n\n'
          'Make sure it\'s powered on and running CAPHY. If it is, its live-'
          'streaming support may be missing - on the laptop run:\n'
          'pip install aiortc av');
    });
    // Answer arrived but media never flowed - typically a restrictive
    // network with no working relay path.
    _connectTimeout = Timer(const Duration(seconds: 40), () {
      if (_gotVideo) return;
      onError?.call(
          'Connected to the laptop but live video couldn\'t start on this '
          'network. Try again, or switch networks if it keeps failing.');
    });
  }

  Future<void> _onCallDocUpdate(fs.DocumentSnapshot snap) async {
    if (_pc == null) return;
    final data = snap.data() as Map<String, dynamic>?;
    if (data == null) return;

    if (data['status'] == 'error') {
      onError?.call(data['error']?.toString() ?? 'The laptop could not answer the call.');
      return;
    }

    // The laptop proactively closes a call (without a graceful WebRTC
    // teardown handshake) when its own monitor detects the stream went
    // stale or the phone's heartbeat stopped (see _monitor_call in
    // web/webrtc_stream.py) - waiting for the phone's own peer connection
    // to notice the remote side vanished can take a while (WebRTC has no
    // fast "the other end hung up" signal by design), so react to this
    // status write directly instead: treat it the same as a connection
    // error, which is what drives WebRtcView's auto-reconnect.
    if (data['status'] == 'closed' && _gotAnswer) {
      onError?.call('Live view connection was reset - reconnecting…');
      return;
    }

    // Upgrade from the STUN-only defaults to the laptop's real, freshly-
    // fetched TURN credentials (see web/webrtc_stream.py) - happens on the
    // initial answer, AND again any time the laptop pushes a refreshed set
    // for a long-lived call (_monitor_call's periodic TURN refresh) - a
    // stale credential set would otherwise sit unused until the whole call
    // reconnects from scratch, defeating the point of refreshing early.
    final rawServers = data['ice_servers'] as List?;
    if (rawServers != null &&
        rawServers.isNotEmpty &&
        (!_configuredIceFromAnswer || !_iceServersEqual(rawServers, _lastAppliedIceServers))) {
      final iceServers = rawServers.map((e) {
        final m = e as Map<String, dynamic>;
        return {
          'urls': m['urls'],
          if (m['username'] != null) 'username': m['username'],
          if (m['credential'] != null) 'credential': m['credential'],
        };
      }).toList();
      try {
        await _pc!.setConfiguration({'iceServers': iceServers});
        _lastAppliedIceServers = rawServers;
      } catch (_) {
        // Non-fatal - the connection keeps trying with whatever
        // configuration it already has.
      }
    }
    if (!_configuredIceFromAnswer) {
      if (data['turn_available'] == false) {
        onTurnUnavailable?.call();
      }
      _configuredIceFromAnswer = true;
    }

    final answer = data['answer'] as Map<String, dynamic>?;
    if (answer != null && (await _pc!.getRemoteDescription()) == null) {
      _gotAnswer = true;
      _answerTimeout?.cancel();
      await _pc!.setRemoteDescription(
          RTCSessionDescription(answer['sdp'], answer['type']));
    }

    final calleeIce = (data['callee_ice'] as List?) ?? [];
    for (int i = 0; i < calleeIce.length; i++) {
      final key = 'callee_$i';
      if (_appliedRemoteIce.contains(key)) continue;
      final c = calleeIce[i] as Map<String, dynamic>;
      if (c['candidate'] == null) continue;
      _appliedRemoteIce.add(key);
      try {
        await _pc!.addCandidate(RTCIceCandidate(
            c['candidate'], c['sdpMid'], c['sdpMLineIndex']));
      } catch (_) {}
    }
  }

  /// Cleans up: closes the peer connection and marks the call closed in
  /// Firestore so the laptop's listener stops streaming to it.
  Future<void> hangUp() async {
    _answerTimeout?.cancel();
    _connectTimeout?.cancel();
    _heartbeatTimer?.cancel();
    await _callSub?.cancel();
    _callSub = null;
    try {
      await _pc?.close();
    } catch (_) {}
    _pc = null;
    if (_callId != null) {
      try {
        await _fs.collection('webrtc_calls').doc(_callId).update({'status': 'closed'});
      } catch (_) {}
    }
  }
}
