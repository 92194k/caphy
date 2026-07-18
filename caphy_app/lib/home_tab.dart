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
    if (mounted) setState(() {
          _alerts = a;
          _loading = false;
        });
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
        const OfflineBanner(),
        Expanded(
          child: RefreshIndicator(
        onRefresh: _loadAll,
        color: cTeal,
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            Row(children: [
              Expanded(
                  child: _statCard('Cameras', '$online / $total',
                      online > 0 ? cGreen : cDim)),
              const SizedBox(width: 12),
              Expanded(
                  child: _statCard(
                      'Current Threat',
                      maxTier > 0 ? 'Tier $maxTier' : 'Clear',
                      maxTier > 0 ? tierColor(maxTier) : cTeal)),
            ]),
            const SizedBox(height: 18),
            const Text('Cameras',
                style: TextStyle(
                    color: cText, fontSize: 15, fontWeight: FontWeight.bold)),
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
                Text('${_alerts.length} shown',
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
            ..._alerts.map((a) => _alertRow(context, a)),
          ],
        ),
          ),
        ),
      ]),
    );
  }

  Widget _statCard(String label, String value, Color accent) => Container(
        padding: const EdgeInsets.all(16),
        decoration: BoxDecoration(
          color: cPanel2,
          borderRadius: BorderRadius.circular(14),
          border: Border.all(color: cLine),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(label.toUpperCase(),
                style: const TextStyle(
                    color: cMuted, fontSize: 10, letterSpacing: 1)),
            const SizedBox(height: 8),
            Text(value,
                style: TextStyle(
                    color: accent, fontSize: 24, fontWeight: FontWeight.bold)),
          ],
        ),
      );

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
    final sub = '${a['distance_m'] ?? '-'} m'
        '${a['camera'] != null ? ' · ${a['camera']}' : ''}'
        ' · ${_time(a['timestamp'])}';
    return Card(
      color: cPanel,
      margin: const EdgeInsets.only(bottom: 9),
      shape: RoundedRectangleBorder(
        side: BorderSide(color: cLine),
        borderRadius: BorderRadius.circular(12),
      ),
      child: ListTile(
        onTap: () => Navigator.of(context).push(
            MaterialPageRoute(builder: (_) => AlertDetailScreen(id: id))),
        leading: CircleAvatar(
          backgroundColor: tierColor(tier).withValues(alpha: 0.2),
          child: Text('$tier',
              style: TextStyle(
                  color: tierColor(tier), fontWeight: FontWeight.bold)),
        ),
        title: Text(event,
            style: const TextStyle(color: cText, fontWeight: FontWeight.w600)),
        subtitle: Text(sub, style: const TextStyle(color: cMuted)),
        trailing: const Icon(Icons.chevron_right, color: cDim),
      ),
    );
  }

  String _time(dynamic ts) {
    if (ts == null) return '';
    final s = ts.toString();
    return s.length >= 16 ? s.substring(11, 16) : s;
  }
}
