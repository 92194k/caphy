import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'api.dart';
import 'theme.dart';
import 'widgets.dart';
import 'voice_screen.dart';

class LiveTab extends StatefulWidget {
  const LiveTab({super.key});
  @override
  State<LiveTab> createState() => _LiveTabState();
}

class _LiveTabState extends State<LiveTab> {
  List<dynamic> _cams = [];
  int _sel = 0;
  Map<String, dynamic> _stat = {};
  Map<String, dynamic> _state = {};   // armed / camera_on / emergency / ...
  bool _recording = false;
  bool _nv = false;
  Timer? _statTimer;

  bool get _armed => _state['armed'] == true;
  bool get _cameraOn => _state['camera_on'] != false;
  bool get _emergency => _state['emergency'] == true;

  @override
  void initState() {
    super.initState();
    _loadCams();
    // No more per-frame image polling (that was the lag). The MJPEG stream
    // widget renders frames continuously. We only poll lightweight status.
    _statTimer =
        Timer.periodic(const Duration(seconds: 2), (_) => _loadStat());
  }

  @override
  void dispose() {
    _statTimer?.cancel();
    super.dispose();
  }

  Future<void> _loadCams() async {
    final c = await Api.cameras();
    if (mounted) setState(() => _cams = c);
  }

  Future<void> _loadStat() async {
    final s = await Api.stats();
    for (final x in s) {
      if (x['cam'] == _sel && mounted) setState(() => _stat = x);
    }
    final st = await Api.state();
    if (st != null && mounted) {
      setState(() {
        _state = st;
        _nv = st['night_vision'] == true;
      });
    }
  }

  Future<void> _toggleArm() async {
    final r = await Api.setArmed(!_armed);
    if (r == null) return _toast('Could not reach CAPHY');
    _toast(r ? 'System armed' : 'System disarmed');
    _loadStat();
  }

  Future<void> _toggleCamera() async {
    final turningOff = _cameraOn;
    final r = await Api.setCamera(!_cameraOn);
    if (r == null) return _toast('Could not reach CAPHY');
    _toast(turningOff
        ? 'Camera off - detection paused'
        : 'Camera on');
    _loadStat();
  }

  Future<void> _toggleEmergency() async {
    if (!_emergency) {
      final ok = await showDialog<bool>(
        context: context,
        builder: (_) => AlertDialog(
          backgroundColor: cPanel,
          title: const Text('Activate emergency mode?',
              style: TextStyle(color: cText)),
          content: const Text(
              'This forces the camera on, arms the system, sounds the siren '
              'and sends a push alert.',
              style: TextStyle(color: cMuted)),
          actions: [
            TextButton(
                onPressed: () => Navigator.pop(context, false),
                child: const Text('Cancel', style: TextStyle(color: cMuted))),
            TextButton(
                onPressed: () => Navigator.pop(context, true),
                child: const Text('Activate', style: TextStyle(color: cRed))),
          ],
        ),
      );
      if (ok != true) return;
    }
    final r = await Api.setEmergency(!_emergency);
    if (r == null) return _toast('Could not reach CAPHY');
    _toast(r ? 'EMERGENCY MODE ACTIVE' : 'Emergency mode cancelled');
    _loadStat();
  }

  void _toast(String m, {bool error = false}) =>
      showTopToast(context, m, error: error);

  @override
  Widget build(BuildContext context) {
    final tier = (_stat['tier'] ?? 0) as int;
    return Scaffold(
      appBar: AppBar(backgroundColor: cPanel, title: const Text('Live')),
      body: Column(children: [
        const OfflineBanner(),
        Expanded(
          child: ListView(
        padding: const EdgeInsets.all(14),
        children: [
          // ---- system state at a glance ----
          Padding(
            padding: const EdgeInsets.only(bottom: 12),
            child: Wrap(spacing: 8, runSpacing: 8, children: [
              StateChip(
                  label: 'System',
                  on: _armed,
                  onText: 'ARMED',
                  offText: 'DISARMED',
                  icon: Icons.shield),
              StateChip(
                  label: 'Camera',
                  on: _cameraOn,
                  onColor: cTeal2,
                  icon: Icons.videocam),
              StateChip(
                  label: 'Night vision',
                  on: _nv,
                  icon: Icons.nightlight_round),
              if (_emergency)
                const StateChip(
                    label: 'EMERGENCY',
                    on: true,
                    onText: 'ACTIVE',
                    offText: '',
                    onColor: cRed,
                    icon: Icons.warning_amber),
            ]),
          ),
          if (!_cameraOn)
            Container(
              margin: const EdgeInsets.only(bottom: 12),
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(
                color: cOrange.withOpacity(0.12),
                borderRadius: BorderRadius.circular(10),
                border: Border.all(color: cOrange),
              ),
              child: Row(children: const [
                Icon(Icons.videocam_off, color: cOrange, size: 18),
                SizedBox(width: 10),
                Expanded(
                  child: Text(
                    'Camera is OFF. The device is released and detection is '
                    'paused - this is not a fault.',
                    style: TextStyle(color: cOrange, fontSize: 12),
                  ),
                ),
              ]),
            ),
          if (_cams.length > 1)
            Padding(
              padding: const EdgeInsets.only(bottom: 12),
              child: Row(
                children: _cams.map<Widget>((c) {
                  final id = c['cam'] as int;
                  final on = id == _sel;
                  return Padding(
                    padding: const EdgeInsets.only(right: 8),
                    child: ChoiceChip(
                      label: Text(c['name'] ?? 'Cam $id'),
                      selected: on,
                      selectedColor: cTeal,
                      backgroundColor: cPanel,
                      labelStyle: TextStyle(color: on ? Colors.black : cMuted),
                      onSelected: (_) => setState(() {
                        _sel = id;
                        _stat = {};
                      }),
                    ),
                  );
                }).toList(),
              ),
            ),
          // live view - continuous MJPEG stream (smooth, no polling)
          Stack(children: [
            AspectRatio(
              aspectRatio: 4 / 3,
              child: ClipRRect(
                borderRadius: BorderRadius.circular(12),
                child: Container(
                  color: Colors.black,
                  child: MjpegView(
                    key: ValueKey('stream$_sel'),
                    url: Api.streamUrl(_sel),
                    active: _cameraOn,
                    fit: BoxFit.cover,
                  ),
                ),
              ),
            ),
            Positioned(
              top: 10,
              left: 10,
              child: Container(
                padding: const EdgeInsets.all(10),
                decoration: BoxDecoration(
                    color: Colors.black.withValues(alpha: 0.55),
                    borderRadius: BorderRadius.circular(10),
                    border: Border.all(color: cLine)),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Text('DETECTION',
                        style: TextStyle(
                            color: cTeal2, fontSize: 10, letterSpacing: 2)),
                    const SizedBox(height: 6),
                    _dot('Factor 1 · Motion', _stat['motion'] == true),
                    _dot('Factor 2 · Person', _stat['person'] == true),
                    const SizedBox(height: 4),
                    Text('Est. distance ${_stat['distance'] ?? '-'} m',
                        style: const TextStyle(color: cOrange, fontSize: 12)),
                  ],
                ),
              ),
            ),
            if (tier > 0)
              Positioned(top: 10, right: 10, child: tierPill(tier)),
          ]),
          const SizedBox(height: 16),
          // ---- controls (matches the web console) ----
          Row(children: [
            Expanded(
              child: _primaryBtn(
                  _armed ? Icons.shield : Icons.shield_outlined,
                  _armed ? 'Disarm' : 'Arm', _toggleArm),
            ),
            const SizedBox(width: 10),
            Expanded(
              child: _dangerBtn(Icons.notifications_active, 'Trigger Siren',
                  () async {
                final on = await Api.siren();
                _toast(on ? 'Siren ON' : 'Siren off');
              }),
            ),
          ]),
          const SizedBox(height: 10),
          Wrap(spacing: 8, runSpacing: 8, children: [
            _iconBtn(_cameraOn ? Icons.videocam : Icons.videocam_off,
                _cameraOn, _toggleCamera, tip: 'Camera'),
            _iconBtn(Icons.camera_alt, false, () async {
              final ok = await Api.snapshot(_sel);
              if (!ok) {
                _toast('Snapshot failed', error: true);
                return;
              }
              final saved = await saveImageToGallery(
                  '${Api.frameUrl(_sel)}&t=${DateTime.now().millisecondsSinceEpoch}',
                  prefix: 'snapshot');
              _toast(saved
                  ? 'Snapshot saved to gallery (CAPHY album)'
                  : 'Snapshot saved on PC');
            }, tip: 'Snapshot'),
            _iconBtn(Icons.fiber_manual_record, _recording, () async {
              final res = await Api.record(_sel);
              final on = res['recording'] == true;
              setState(() => _recording = on);
              if (on) {
                _toast('Recording started');
              } else {
                _toast('Saving recording...');
                final url = res['video_url'];
                if (url != null) {
                  final saved =
                      await saveVideoUrlToGallery('${Store.baseUrl}$url');
                  _toast(saved
                      ? 'Recording saved to gallery (CAPHY album)'
                      : 'Recording saved on PC');
                } else {
                  _toast('Recording saved on PC');
                }
              }
            }, tip: 'Record'),
            _iconBtn(Icons.nightlight_round, _nv, () async {
              final on = await Api.nightVision(_sel);
              setState(() => _nv = on);
              _toast('Night vision ${on ? "on" : "off"}');
            }, tip: 'Night vision'),
            _iconBtn(Icons.mic, false, _openVoice, tip: 'Speak'),
            _iconBtn(Icons.fullscreen, false, _openFullscreen, tip: 'Fullscreen'),
            _iconBtn(Icons.warning_amber, _emergency, _toggleEmergency,
                danger: true, tip: 'Emergency'),
          ]),
        ],
          ),
        ),
      ]),
    );
  }

  Widget _dot(String label, bool on) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 3),
        child: Row(mainAxisSize: MainAxisSize.min, children: [
          Icon(Icons.circle, size: 9, color: on ? cGreen : cDim),
          const SizedBox(width: 8),
          Text(label, style: const TextStyle(color: cMuted, fontSize: 12)),
        ]),
      );

  // Filled-teal primary action (Arm / Disarm).
  Widget _primaryBtn(IconData icon, String label, VoidCallback onTap) {
    return FilledButton.icon(
      onPressed: onTap,
      icon: Icon(icon, size: 18, color: Colors.black),
      label: Text(label,
          style: const TextStyle(color: Colors.black, fontWeight: FontWeight.w700)),
      style: FilledButton.styleFrom(
        backgroundColor: cTeal,
        padding: const EdgeInsets.symmetric(vertical: 13),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
      ),
    );
  }

  // Red danger action (Trigger Siren).
  Widget _dangerBtn(IconData icon, String label, VoidCallback onTap) {
    return OutlinedButton.icon(
      onPressed: onTap,
      icon: Icon(icon, size: 18, color: cRed),
      label: Text(label,
          style: const TextStyle(color: cRed, fontWeight: FontWeight.w700)),
      style: OutlinedButton.styleFrom(
        backgroundColor: cRed.withValues(alpha: 0.12),
        side: BorderSide(color: cRed.withValues(alpha: 0.6)),
        padding: const EdgeInsets.symmetric(vertical: 13),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
      ),
    );
  }

  // Dark icon-only control (snapshot, record, night vision, etc.).
  Widget _iconBtn(IconData icon, bool active, VoidCallback onTap,
      {bool danger = false, String? tip}) {
    final iconColor = danger ? cRed : (active ? cTeal2 : cMuted);
    final borderColor = danger
        ? cRed.withValues(alpha: 0.5)
        : (active ? cTeal : cLine);
    return Tooltip(
      message: tip ?? '',
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(10),
        child: Container(
          width: 50,
          height: 46,
          decoration: BoxDecoration(
            color: active ? cTeal.withValues(alpha: 0.15) : cPanel2,
            borderRadius: BorderRadius.circular(10),
            border: Border.all(color: borderColor),
          ),
          child: Icon(icon, size: 20, color: iconColor),
        ),
      ),
    );
  }

  void _openVoice() {
    Navigator.of(context).push(
        MaterialPageRoute(builder: (_) => const VoiceScreen()));
  }

  void _openFullscreen() {
    Navigator.of(context).push(MaterialPageRoute(
        builder: (_) => _FullscreenView(cam: _sel)));
  }
}

class _FullscreenView extends StatefulWidget {
  final int cam;
  const _FullscreenView({required this.cam});
  @override
  State<_FullscreenView> createState() => _FullscreenViewState();
}

class _FullscreenViewState extends State<_FullscreenView> {
  bool _showExit = true;
  Timer? _hideTimer;

  @override
  void initState() {
    super.initState();

    // True fullscreen: hide the status bar AND the navigation bar.
    // immersiveSticky means a swipe from the edge shows them briefly and then
    // they slide away again - right for a CCTV view you watch for a long time.
    SystemChrome.setEnabledSystemUIMode(SystemUiMode.immersiveSticky);

    // Camera footage is landscape, so rotate to match.
    SystemChrome.setPreferredOrientations([
      DeviceOrientation.landscapeLeft,
      DeviceOrientation.landscapeRight,
    ]);

    _startHideTimer();
  }

  @override
  void dispose() {
    _hideTimer?.cancel();

    // Put the phone back the way we found it, or the rest of the app stays
    // stuck fullscreen and locked in landscape.
    SystemChrome.setEnabledSystemUIMode(SystemUiMode.edgeToEdge);
    SystemChrome.setPreferredOrientations(DeviceOrientation.values);
    super.dispose();
  }

  void _startHideTimer() {
    _hideTimer?.cancel();
    _hideTimer = Timer(const Duration(seconds: 3), () {
      if (mounted) setState(() => _showExit = false);
    });
  }

  void _tap() {
    setState(() => _showExit = !_showExit);
    if (_showExit) _startHideTimer();
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        backgroundColor: Colors.black,
        // Fullscreen is just the live video - no controls. Tap to reveal Exit.
        body: Stack(children: [
          Positioned.fill(
            child: GestureDetector(
              onTap: _tap,
              child: MjpegView(
                key: ValueKey('fs${widget.cam}'),
                url: Api.streamUrl(widget.cam),
                fit: BoxFit.contain,
              ),
            ),
          ),
          Positioned(
            top: 14,
            right: 14,
            child: AnimatedOpacity(
              opacity: _showExit ? 1 : 0,
              duration: const Duration(milliseconds: 200),
              child: GestureDetector(
                onTap: () => Navigator.pop(context),
                child: Container(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
                  decoration: BoxDecoration(
                    color: Colors.black.withOpacity(0.6),
                    borderRadius: BorderRadius.circular(24),
                    border: Border.all(color: cLine),
                  ),
                  child: const Row(mainAxisSize: MainAxisSize.min, children: [
                    Icon(Icons.close, color: Colors.white, size: 18),
                    SizedBox(width: 6),
                    Text('Exit',
                        style: TextStyle(color: Colors.white, fontSize: 13)),
                  ]),
                ),
              ),
            ),
          ),
        ]),
      );
}
