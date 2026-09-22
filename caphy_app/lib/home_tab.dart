import 'dart:async';
import 'package:flutter/material.dart';
import 'api.dart';
import 'theme.dart';
import 'widgets.dart';
import 'alerts_tab.dart';

class HomeTab extends StatefulWidget {
  const HomeTab({super.key});
  @override
  State<HomeTab> createState() => _HomeTabState();
}

class _HomeTabState extends State<HomeTab> {
  List<dynamic> _stats = [];
  List<dynamic> _alerts = [];
  Timer? _timer;
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _loadAll();
    _timer = Timer.periodic(const Duration(seconds: 2), (_) => _loadStats());
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  Future<void> _loadAll() async {
    await _loadStats();
    final a = await Api.alerts(limit: 5);
    if (mounted) {
      setState(() {
        _alerts = a;
        _loading = false;
      });
    }
  }

  Future<void> _loadStats() async {
    final s = await Api.stats();
    if (mounted) setState(() => _stats = s);
  }

  @override
  Widget build(BuildContext context) {
    final online = _stats.where((s) => s['online'] == true).length;
    final total = _stats.length;
    int maxTier = 0;
    for (final s in _stats) {
      final t = (s['tier'] ?? 0) as int;
      if (t > maxTier) maxTier = t;
    }
    return Scaffold(
      appBar: AppBar(
        backgroundColor: cPanel,
        title: const Text('CAPHY',
            style: TextStyle(fontWeight: FontWeight.bold, letterSpacing: 2)),
        actions: [
          Padding(
            padding: const EdgeInsets.only(right: 14),
            child: Row(children: [
              Icon(Icons.circle, size: 10, color: online > 0 ? cGreen : cDim),
              const SizedBox(width: 6),
              Text('$online/$total live',
                  style: const TextStyle(color: cMuted, fontSize: 12)),
            ]),
          ),
        ],
      ),
      body: Column(children: [
        Expanded(
          child: RefreshIndicator(
        onRefresh: _loadAll,
        color: cTeal,
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            // ---- big-picture status: current threat tier front and
            // center (it's the thing worth glancing at first), camera
            // online count right beside it for quick context.
            _statusHero(maxTier),
            const SizedBox(height: 14),
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                const Text('Cameras',
                    style: TextStyle(
                        color: cText, fontSize: 15, fontWeight: FontWeight.bold)),
                Text('$online of $total cameras online',
                    style: TextStyle(
                        color: online > 0 ? cGreen : cDim, fontSize: 12.5)),
              ],
            ),
            const SizedBox(height: 10),
            if (_stats.isEmpty)
              panel(
                  child: const Text('No cameras reporting',
                      style: TextStyle(color: cDim))),
            ..._stats.map(_cameraRow),
            const SizedBox(height: 18),
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                const Text('Recent Alerts',
                    style: TextStyle(
                        color: cText,
                        fontSize: 15,
                        fontWeight: FontWeight.bold)),
                // Home only ever previews a HANDFUL of alerts - the full
                // list with filters/search lives on the Alerts tab. Showing
                // all of them here (previously the full _alerts list, which
                // could be dozens) made the dashboard feel cluttered and
                // pushed the rest of the home screen below the fold for no
                // benefit, since anyone wanting more just taps the Alerts
                // tab. Capped to 3 - the label below reflects the cap, not
                // the total, so it doesn't look like a miscount.
                Text('${_alerts.take(4).length} of ${_alerts.length} shown',
                    style: const TextStyle(color: cDim, fontSize: 12)),
              ],
            ),
            const SizedBox(height: 10),
            if (_loading)
              const Padding(
                  padding: EdgeInsets.all(20),
                  child: Center(
                      child: CircularProgressIndicator(color: cTeal))),
            if (!_loading && _alerts.isEmpty)
              panel(
                  child: const Text('No alerts yet',
                      style: TextStyle(color: cDim))),
            ..._alerts.take(4).map((a) => _alertRow(context, a)),
          ],
        ),
          ),
        ),
      ]),
    );
  }

  /// Big status banner: current threat tier (or "All Clear") is the
  /// dominant element - a security app's single most important glance -
  /// with the camera online count as a smaller secondary line underneath,
  /// instead of two equal-weight stat cards competing for attention.
  Widget _statusHero(int maxTier) {
    final clear = maxTier == 0;
    final accent = clear ? cTeal : tierColor(maxTier);
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(18),
      decoration: BoxDecoration(
        color: cPanel2,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: accent.withValues(alpha: 0.4)),
      ),
      child: Row(children: [
        Container(
          width: 52,
          height: 52,
          decoration: BoxDecoration(
            color: accent.withValues(alpha: 0.15),
            shape: BoxShape.circle,
          ),
          child: Icon(clear ? Icons.shield_outlined : Icons.warning_amber,
              color: accent, size: 26),
        ),
        const SizedBox(width: 14),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text('CURRENT STATUS',
                  style: const TextStyle(
                      color: cMuted, fontSize: 10.5, letterSpacing: 1.2)),
              const SizedBox(height: 4),
              Text(clear ? 'All Clear' : 'Tier $maxTier Alert',
                  style: TextStyle(
                      color: accent, fontSize: 20, fontWeight: FontWeight.bold)),
            ],
          ),
        ),
      ]),
    );
  }

  Widget _cameraRow(dynamic s) {
    final online = s['online'] == true;
    final tier = (s['tier'] ?? 0) as int;
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: panel(
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
        child: Row(children: [
          Icon(Icons.videocam,
              color: online ? cTeal2 : cDim, size: 20),
          const SizedBox(width: 12),
          Expanded(
            child: Text(s['name'] ?? 'Cam ${s['cam']}',
                style: const TextStyle(
                    color: cText, fontWeight: FontWeight.w600)),
          ),
          if (tier > 0) tierPill(tier),
          if (tier == 0)
            Text(online ? 'online' : 'offline',
                style: TextStyle(
                    color: online ? cGreen : cDim, fontSize: 12)),
        ]),
      ),
    );
  }

  Widget _alertRow(BuildContext context, dynamic a) {
    final tier = asInt(a['tier'], 1);
    final id = asInt(a['id']);
    final event = (a['event'] ?? 'Alert #$id').toString();
    final sub = '${a['distance_m'] ?? '-'} m away'
        '${a['camera'] != null ? ' · ${a['camera']}' : ''}'
        ' · ${_time(a['timestamp'])}';
    final accent = tierColor(tier);
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: Material(
        color: Colors.transparent,
        child: InkWell(
          borderRadius: BorderRadius.circular(16),
          onTap: () => Navigator.of(context).push(
              MaterialPageRoute(builder: (_) => AlertDetailScreen(id: id))),
          child: Container(
            decoration: BoxDecoration(
              color: cPanel2.withValues(alpha: 0.72),
              borderRadius: BorderRadius.circular(16),
              // A Border with different colors per side (accent left edge,
              // faint the rest) combined with borderRadius throws "A
              // borderRadius can only be given on borders with uniform
              // colors" at paint time - Flutter silently drops the whole
              // card's content when that happens (no red error screen,
              // just an empty box), which is exactly why every alert row
              // was rendering blank despite the data being there. Fixed by
              // using one uniform, near-invisible border here and drawing
              // the tier-colored accent separately below as a plain
              // rectangle instead of a border side.
              border: Border.all(color: const Color(0x22789AD2)),
            ),
            child: Row(children: [
              ClipRRect(
                borderRadius: const BorderRadius.horizontal(left: Radius.circular(16)),
                child: Container(width: 3, height: 68, color: accent),
              ),
              Expanded(
                child: Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
                  child: Row(children: [
                    AlertThumbnail(
                        snapshot: a['snapshot']?.toString(), tier: tier, size: 52),
                    const SizedBox(width: 12),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(event,
                              style: const TextStyle(
                                  color: cText, fontWeight: FontWeight.w600, fontSize: 14.5)),
                          const SizedBox(height: 3),
                          Text(sub,
                              style: const TextStyle(color: cMuted, fontSize: 12.5)),
                        ],
                      ),
                    ),
                    const SizedBox(width: 6),
                    const Icon(Icons.chevron_right, color: cDim, size: 20),
                  ]),
                ),
              ),
            ]),
          ),
        ),
      ),
    );
  }

  String _time(dynamic ts) {
    if (ts == null) return '';
    final s = ts.toString();
    return s.length >= 16 ? s.substring(11, 16) : s;
  }
}
