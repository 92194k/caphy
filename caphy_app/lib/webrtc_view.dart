import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import 'theme.dart';
import 'webrtc_call.dart';

/// True live video widget for when the phone is NOT on the laptop's LAN.
/// Starts a WebRTC call (see webrtc_call.dart) on mount, renders the
/// remote video track once connected, and cleans up the call on dispose -
/// this is the direct counterpart to live_tab.dart's MjpegView, used
/// specifically for the "off WiFi" path.
class WebRtcView extends StatefulWidget {
  final String deviceId;
  final int cam;
  final BoxFit fit;
  const WebRtcView(
      {super.key, required this.deviceId, required this.cam, this.fit = BoxFit.cover});

  @override
  State<WebRtcView> createState() => _WebRtcViewState();
}

class _WebRtcViewState extends State<WebRtcView> {
  final RTCVideoRenderer _renderer = RTCVideoRenderer();
  WebrtcCall? _call;
  String _status = 'Connecting…';
  bool _errored = false;
  bool _turnUnavailable = false;
  bool _reconnecting = false;

  // Auto-reconnect: commercial cameras (Ring, Tapo, etc) never show the
  // user a dead "connection failed, tap to retry" screen - they silently
  // retry in the background. A terminal failure here (the laptop's own
  // WebRTC layer already gives a "disconnected" state 8s to self-heal
  // before it ever reaches this point - see web/webrtc_stream.py) means
  // starting a brand-new call from scratch, since aiortc has no
  // restartIce()-equivalent to fall back to. Capped + backed off so a
  // genuinely broken network (laptop off, no internet at all) doesn't spin
  // forever chewing battery/data.
  int _reconnectAttempts = 0;
  static const int _maxReconnectAttempts = 5;
  Timer? _reconnectTimer;
  bool _disposed = false;
  // Guards against two overlapping _connect() calls creating two
  // simultaneous WebrtcCall/RTCPeerConnection instances - e.g. the
  // laptop's proactive "closed" write and the phone's own answer-timeout
  // both firing onError close together would otherwise each schedule
  // their own reconnect independently of each other.
  bool _connecting = false;

  @override
  void initState() {
    super.initState();
    _connect();
  }

  Future<void> _connect() async {
    if (_connecting) return;
    _connecting = true;
    await _renderer.initialize();
    _call = WebrtcCall(
      deviceId: widget.deviceId,
      cam: widget.cam,
      onRemoteStream: (stream) {
        if (!mounted) return;
        setState(() {
          _renderer.srcObject = stream;
          _status = 'Live';
          _reconnecting = false;
        });
        // A frame actually arrived - this attempt succeeded, so future
        // failures start counting from zero again rather than inheriting
        // whatever attempt count got us here.
        _reconnectAttempts = 0;
      },
      onConnectionState: (state) {
        if (!mounted) return;
        setState(() => _status = _friendlyState(state));
      },
      onError: (err) {
        if (!mounted) return;
        setState(() {
          _errored = true;
          _status = err;
        });
        _scheduleReconnect();
      },
      onTurnUnavailable: () {
        if (!mounted) return;
        setState(() => _turnUnavailable = true);
      },
    );
    try {
      await _call!.start();
    } finally {
      // Setup (offer created, doc written, listener attached) is done -
      // from here on the call's own callbacks (onError/onRemoteStream)
      // drive what happens next, so it's safe to allow another _connect()
      // call again (e.g. from a reconnect timer that fires later).
      _connecting = false;
    }
  }

  void _scheduleReconnect() {
    if (_disposed) return;
    if (_connecting) return;   // a connect attempt is already in flight
    if (_reconnectAttempts >= _maxReconnectAttempts) {
      // Give up auto-retrying, but the error message + a manual path
      // (didUpdateWidget / re-opening the tab) is still there - this just
      // stops silently hammering a laptop that's genuinely offline.
      return;
    }
    _reconnectAttempts++;
    // Backoff: 2s, 4s, 8s, 16s, 30s(capped) - fast enough to recover from
    // a brief hiccup quickly, but backs off instead of hammering a truly
    // dead connection every couple seconds.
    final delaySec = (2 << (_reconnectAttempts - 1)).clamp(2, 30);
    setState(() {
      _reconnecting = true;
      _status = 'Reconnecting… (attempt $_reconnectAttempts/$_maxReconnectAttempts)';
    });
    _reconnectTimer?.cancel();
    _reconnectTimer = Timer(Duration(seconds: delaySec), () async {
      if (_disposed || !mounted) return;
      await _call?.hangUp();
      _renderer.srcObject = null;
      setState(() => _errored = false);
      await _connect();
    });
  }

  String _friendlyState(String raw) {
    if (raw.contains('connected') && !raw.contains('Dis') && !raw.contains('Fail')) {
      return 'Live';
    }
    if (raw.contains('connecting')) return 'Connecting…';
    if (raw.contains('Fail')) {
      _scheduleReconnect();
      return _reconnecting
          ? 'Reconnecting… (attempt $_reconnectAttempts/$_maxReconnectAttempts)'
          : 'Connection failed — check your internet';
    }
    if (raw.contains('Dis') || raw.contains('closed')) return 'Disconnected';
    return 'Connecting…';
  }

  @override
  void didUpdateWidget(covariant WebRtcView old) {
    super.didUpdateWidget(old);
    if (old.deviceId != widget.deviceId || old.cam != widget.cam) {
      _reconnectTimer?.cancel();
      _reconnectAttempts = 0;
      _connecting = false;   // force-allow the fresh connect below
      _call?.hangUp();
      _renderer.srcObject = null;
      setState(() {
        _status = 'Connecting…';
        _errored = false;
        _reconnecting = false;
      });
      _connect();
    }
  }

  @override
  void dispose() {
    _disposed = true;
    _reconnectTimer?.cancel();
    _call?.hangUp();
    _renderer.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final live = _renderer.srcObject != null && !_errored;
    return Stack(
      fit: StackFit.expand,
      children: [
        if (live)
          RTCVideoView(_renderer, objectFit: widget.fit == BoxFit.contain
              ? RTCVideoViewObjectFit.RTCVideoViewObjectFitContain
              : RTCVideoViewObjectFit.RTCVideoViewObjectFitCover)
        else
          Container(color: Colors.black),
        if (!live)
          Center(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                // Reconnecting is shown the same as normal connecting
                // (spinner, muted text) rather than the harsher red error
                // icon - this is the "quietly trying again in the
                // background" behavior Ring/Tapo use instead of putting a
                // scary error in front of the user for what's usually a
                // brief, self-resolving blip.
                if (!_errored || _reconnecting)
                  const CircularProgressIndicator(color: cTeal)
                else
                  const Icon(Icons.wifi_off, color: cRed, size: 32),
                const SizedBox(height: 12),
                Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 24),
                  child: Text(_status,
                      textAlign: TextAlign.center,
                      style: TextStyle(
                          color: (_errored && !_reconnecting) ? cRed : cMuted,
                          fontSize: 12)),
                ),
              ],
            ),
          ),
        // Relay (TURN) service was unreachable when this call was set up -
        // the connection may still work fine (most networks don't need a
        // relay at all), but on a restrictive network it might not
        // connect. This is an honest heads-up, not necessarily a failure.
        if (_turnUnavailable)
          Positioned(
            top: 10,
            left: 10,
            right: 10,
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
              decoration: BoxDecoration(
                color: cOrange.withValues(alpha: 0.85),
                borderRadius: BorderRadius.circular(8),
              ),
              child: const Text(
                'Relay service temporarily unavailable — live video may not '
                'connect on some networks',
                style: TextStyle(color: Colors.black, fontSize: 11),
                textAlign: TextAlign.center,
              ),
            ),
          ),
      ],
    );
  }
}
