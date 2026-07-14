import 'dart:async';
import 'package:flutter/material.dart';
import 'api.dart';
import 'theme.dart';

class LiveTab extends StatefulWidget {
  const LiveTab({super.key});
  @override
  State<LiveTab> createState() => _LiveTabState();
}

class _LiveTabState extends State<LiveTab> {
  List<dynamic> _cams = [];
  int _sel = 0;
  int _frame = 0; // cache-buster for the live image
  Map<String, dynamic> _stat = {};
  bool _recording = false;
  bool _nv = false;
  Timer? _frameTimer;
  Timer? _statTimer;

  @override
  void initState() {
    super.initState();
    _loadCams();
    _frameTimer = Timer.periodic(
        const Duration(milliseconds: 250), (_) => setState(() => _frame++));
    _statTimer =
        Timer.periodic(const Duration(seconds: 1), (_) => _loadStat());
  }

  @override
  void dispose() {
    _frameTimer?.cancel();
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
  }

  void _toast(String m) => ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(backgroundColor: cPanel, content: Text(m, style: const TextStyle(color: cText))));

  @override
  Widget build(BuildContext context) {
    final tier = (_stat['tier'] ?? 0) as int;
    return Scaffold(
      appBar: AppBar(backgroundColor: cPanel, title: const Text('Live')),
      body: ListView(
        padding: const EdgeInsets.all(14),
        children: [
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
          // live view
          Stack(children: [
            AspectRatio(
              aspectRatio: 4 / 3,
              child: ClipRRect(
                borderRadius: BorderRadius.circular(12),
                child: Image.network(
                  '${Api.frameUrl(_sel)}&t=$_frame',
                  fit: BoxFit.cover,
                  gaplessPlayback: true,
                  errorBuilder: (_, __, ___) => Container(
                    color: Colors.black,
                    child: const Center(
                        child: Text('connecting to camera...',
                            style: TextStyle(color: cDim))),
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
          // controls
          Wrap(spacing: 10, runSpacing: 10, children: [
            _ctrl(Icons.camera_alt, 'Snapshot', false, () async {
              _toast(await Api.snapshot(_sel) ? 'Snapshot saved' : 'Failed');
            }),
            _ctrl(Icons.fiber_manual_record, 'Record', _recording, () async {
              final on = await Api.record(_sel);
              setState(() => _recording = on);
              _toast(on ? 'Recording started' : 'Recording stopped');
            }),
            _ctrl(Icons.nightlight_round, 'Night Vision', _nv, () async {
              final on = await Api.nightVision(_sel);
              setState(() => _nv = on);
              _toast('Night vision ${on ? "on" : "off"}');
            }),
            _ctrl(Icons.notifications_active, 'Alarm', false, () async {
              final on = await Api.siren();
              _toast(on ? 'Siren ON' : 'Siren off');
            }),
            _ctrl(Icons.mic, 'Speak', false, _openVoice),
            _ctrl(Icons.fullscreen, 'Fullscreen', false, _openFullscreen),
          ]),
        ],
      ),
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

  Widget _ctrl(IconData icon, String label, bool active, VoidCallback onTap) {
    return SizedBox(
      width: 104,
      child: OutlinedButton(
        style: OutlinedButton.styleFrom(
          backgroundColor: active ? cTeal : cPanel,
          side: BorderSide(color: active ? cTeal : cLine),
          padding: const EdgeInsets.symmetric(vertical: 12),
        ),
        onPressed: onTap,
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          Icon(icon, size: 20, color: active ? Colors.black : cText),
          const SizedBox(height: 4),
          Text(label,
              style: TextStyle(
                  fontSize: 11, color: active ? Colors.black : cText)),
        ]),
      ),
    );
  }

  void _openVoice() {
    showModalBottomSheet(
      context: context,
      backgroundColor: cPanel2,
      builder: (_) => Padding(
        padding: const EdgeInsets.all(18),
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          const Text('Voice Command',
              style: TextStyle(
                  color: cText, fontSize: 16, fontWeight: FontWeight.bold)),
          const SizedBox(height: 4),
          const Text('Tap a command to send it to the system',
              style: TextStyle(color: cMuted, fontSize: 12)),
          const SizedBox(height: 16),
          Wrap(spacing: 10, runSpacing: 10, children: [
            for (final c in ['Arm', 'Disarm', 'Snapshot', 'Siren', 'Stop', 'Night vision'])
              FilledButton.tonal(
                style: FilledButton.styleFrom(backgroundColor: cPanel),
                onPressed: () async {
                  Navigator.pop(context);
                  final r = await Api.voice(c);
                  _toast(r['message']?.toString() ?? 'Done');
                },
                child: Text(c, style: const TextStyle(color: cTeal2)),
              ),
          ]),
        ]),
      ),
    );
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
  int _f = 0;
  Timer? _t;
  @override
  void initState() {
    super.initState();
    _t = Timer.periodic(
        const Duration(milliseconds: 250), (_) => setState(() => _f++));
  }

  @override
  void dispose() {
    _t?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        backgroundColor: Colors.black,
        body: Stack(children: [
          Center(
            child: Image.network('${Api.frameUrl(widget.cam)}&t=$_f',
                fit: BoxFit.contain, gaplessPlayback: true),
          ),
          Positioned(
            top: 40,
            right: 16,
            child: IconButton(
                icon: const Icon(Icons.close, color: Colors.white, size: 30),
                onPressed: () => Navigator.pop(context)),
          ),
        ]),
      );
}
