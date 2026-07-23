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

  @override
  void initState() {
    super.initState();
    _connect();
  }

  Future<void> _connect() async {
    await _renderer.initialize();
    _call = WebrtcCall(
      deviceId: widget.deviceId,
      cam: widget.cam,
      onRemoteStream: (stream) {
        if (!mounted) return;
        setState(() {
          _renderer.srcObject = stream;
          _status = 'Live';
        });
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
      },
      onTurnUnavailable: () {
        if (!mounted) return;
        setState(() => _turnUnavailable = true);
      },
    );
    await _call!.start();
  }

  String _friendlyState(String raw) {
    if (raw.contains('connected') && !raw.contains('Dis') && !raw.contains('Fail')) {
      return 'Live';
    }
    if (raw.contains('connecting')) return 'Connecting…';
    if (raw.contains('Fail')) return 'Connection failed — check your internet';
    if (raw.contains('Dis') || raw.contains('closed')) return 'Disconnected';
    return 'Connecting…';
  }

  @override
  void didUpdateWidget(covariant WebRtcView old) {
    super.didUpdateWidget(old);
    if (old.deviceId != widget.deviceId || old.cam != widget.cam) {
      _call?.hangUp();
      _renderer.srcObject = null;
      setState(() {
        _status = 'Connecting…';
        _errored = false;
      });
      _connect();
    }
  }

  @override
  void dispose() {
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
                if (!_errored)
                  const CircularProgressIndicator(color: cTeal)
                else
                  const Icon(Icons.wifi_off, color: cRed, size: 32),
                const SizedBox(height: 12),
                Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 24),
                  child: Text(_status,
                      textAlign: TextAlign.center,
                      style: TextStyle(color: _errored ? cRed : cMuted, fontSize: 12)),
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
