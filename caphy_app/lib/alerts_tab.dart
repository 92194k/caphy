import 'dart:async';
import 'package:flutter/material.dart';
import 'api.dart';
import 'theme.dart';
import 'widgets.dart';

class AlertsTab extends StatefulWidget {
  const AlertsTab({super.key});
  @override
  State<AlertsTab> createState() => _AlertsTabState();
}

class _AlertsTabState extends State<AlertsTab> {
  List<dynamic> _alerts = [];
  bool _loading = true;
  Timer? _poll;

  @override
  void initState() {
    super.initState();
    _load();
    // Auto-refresh: new alerts appear without pulling to refresh. Pull-to-
    // refresh and the refresh button still work if you want an instant update.
    _poll = Timer.periodic(const Duration(seconds: 4), (_) => _load(quiet: true));
  }

  @override
  void dispose() {
    _poll?.cancel();
    super.dispose();
  }

  Future<void> _load({bool quiet = false}) async {
    final a = await Api.alerts(limit: 100);
    if (!mounted) return;
    // Don't fight the user mid-swipe: only replace the list if it changed.
    final changed = a.length != _alerts.length ||
        (a.isNotEmpty && _alerts.isNotEmpty && a.first['id'] != _alerts.first['id']);
    if (quiet && !changed) {
      if (_loading) setState(() => _loading = false);
      return;
    }
    setState(() {
      _alerts = a;
      _loading = false;
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        backgroundColor: cPanel,
        title: const Text('Alerts'),
        actions: [
          if (_alerts.isNotEmpty)
            IconButton(
                tooltip: 'Acknowledge all',
                onPressed: _ackAll,
                icon: const Icon(Icons.done_all, color: cMuted)),
          IconButton(
              onPressed: _load,
              icon: const Icon(Icons.refresh, color: cMuted)),
        ],
      ),
      body: Column(children: [
        const OfflineBanner(),
        Expanded(
          child: RefreshIndicator(
            onRefresh: _load,
            color: cTeal,
            child: _loading
                ? const Center(child: CircularProgressIndicator(color: cTeal))
                : _alerts.isEmpty
                    ? ListView(children: [
                        const SizedBox(height: 120),
                        Center(
                          child: Column(children: [
                            Icon(
                                Api.online.value
                                    ? Icons.check_circle_outline
                                    : Icons.cloud_off,
                                size: 44,
                                color: Api.online.value ? cDim : cRed),
                            const SizedBox(height: 10),
                            Text(
                                Api.online.value
                                    ? 'No new alerts'
                                    : 'Not connected to CAPHY',
                                style: TextStyle(
                                    color: Api.online.value ? cDim : cRed)),
                            if (Api.online.value)
                              const Padding(
                                padding: EdgeInsets.only(top: 6),
                                child: Text(
                                    'Acknowledged alerts stay in the database',
                                    style:
                                        TextStyle(color: cDim, fontSize: 11)),
                              ),
                          ]),
                        )
                      ])
                    : ListView.builder(
                        padding: const EdgeInsets.all(14),
                        itemCount: _alerts.length,
                        itemBuilder: (_, i) => _row(context, _alerts[i]),
                      ),
          ),
        ),
      ]),
    );
  }

  /// Acknowledge one alert. It disappears from the list but the row stays.
  /// Confirmation shows at the TOP so it matches the rest of the app.
  Future<void> _ack(int id) async {
    final ok = await Api.dismissAlert(id);
    if (!mounted) return;
    if (!ok) {
      showTopToast(context, 'Could not reach CAPHY', error: true);
      _load();
      return;
    }
    setState(() => _alerts.removeWhere((a) => asInt(a['id']) == id));
    showTopToast(context, 'Alert acknowledged');
  }

  Future<void> _ackAll() async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (_) => AlertDialog(
        backgroundColor: cPanel,
        title: const Text('Acknowledge all alerts?',
            style: TextStyle(color: cText)),
        content: const Text(
            'They are hidden from this list but kept in the database.',
            style: TextStyle(color: cMuted)),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('Cancel', style: TextStyle(color: cMuted))),
          TextButton(
              onPressed: () => Navigator.pop(context, true),
              child:
                  const Text('Acknowledge', style: TextStyle(color: cTeal2))),
        ],
      ),
    );
    if (ok != true) return;
    await Api.dismissAllAlerts();
    _load();
  }

  Widget _row(BuildContext context, dynamic a) {
    final tier = asInt(a['tier'], 1);
    final id = asInt(a['id']);
    final event = (a['event'] ?? 'Alert #$id').toString();
    final sub = '${a['distance_m'] ?? '-'} m'
        '${a['camera'] != null ? ' · ${a['camera']}' : ''}'
        ' · ${_time(a['timestamp'])}';
    // Swipe left to acknowledge, or use the check button. Either way the row
    // is only hidden - it stays in the database.
    return Dismissible(
      key: ValueKey('alert$id'),
      direction: DismissDirection.endToStart,
      background: Container(
        alignment: Alignment.centerRight,
        padding: const EdgeInsets.only(right: 20, bottom: 10),
        decoration: BoxDecoration(
          color: cTeal.withOpacity(0.25),
          borderRadius: BorderRadius.circular(12),
        ),
        child: const Row(
            mainAxisAlignment: MainAxisAlignment.end,
            children: [
              Text('Acknowledge',
                  style: TextStyle(color: cTeal2, fontWeight: FontWeight.w600)),
              SizedBox(width: 8),
              Icon(Icons.done, color: cTeal2),
            ]),
      ),
      onDismissed: (_) => _ack(id),
      child: Card(
        color: cPanel,
        margin: const EdgeInsets.only(bottom: 10),
        shape: RoundedRectangleBorder(
          side: BorderSide(color: cLine),
          borderRadius: BorderRadius.circular(12),
        ),
        child: ListTile(
          onTap: () => Navigator.of(context).push(
              MaterialPageRoute(builder: (_) => AlertDetailScreen(id: id))),
          leading: CircleAvatar(
            backgroundColor: cBg,
            child: Icon(Icons.person, color: tierColor(tier)),
          ),
          title: Row(children: [
            Flexible(
              child: Text(event,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                      color: cText, fontWeight: FontWeight.w600)),
            ),
            const SizedBox(width: 8),
            _tierPill(tier),
          ]),
          subtitle: Text(sub, style: const TextStyle(color: cMuted)),
          trailing: OutlinedButton(
            onPressed: () => _ack(id),
            style: OutlinedButton.styleFrom(
              foregroundColor: cTeal2,
              backgroundColor: cPanel2,
              side: const BorderSide(color: cLine),
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
              shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(8)),
              minimumSize: const Size(0, 34),
            ),
            child: const Text('Acknowledge', style: TextStyle(fontSize: 12)),
          ),
        ),
      ),
    );
  }

  Widget _thumbFallback() => Container(
        width: 58,
        height: 44,
        color: cBg,
        child: const Icon(Icons.person, color: cOrange, size: 20),
      );

  // Small "TIER N" pill, outlined in the tier colour (matches the web console).
  Widget _tierPill(int tier) {
    final c = tierColor(tier);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      decoration: BoxDecoration(
        color: c.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(6),
        border: Border.all(color: c.withValues(alpha: 0.55)),
      ),
      child: Text('TIER $tier',
          style: TextStyle(
              color: c,
              fontSize: 10,
              fontWeight: FontWeight.bold,
              letterSpacing: 0.5)),
    );
  }

  String _time(dynamic ts) {
    if (ts == null) return '';
    final s = ts.toString();
    return s.length >= 16 ? s.substring(0, 16).replaceFirst('T', ' ') : s;
  }
}

// ==================== Alert detail ====================
class AlertDetailScreen extends StatefulWidget {
  final int id;
  const AlertDetailScreen({super.key, required this.id});
  @override
  State<AlertDetailScreen> createState() => _AlertDetailScreenState();
}

class _AlertDetailScreenState extends State<AlertDetailScreen> {
  Map<String, dynamic>? _a;
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    final a = await Api.alert(widget.id);
    if (mounted) {
      setState(() {
        _a = a;
        _loading = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final a = _a;
    return Scaffold(
      appBar: AppBar(backgroundColor: cPanel, title: Text('Alert #${widget.id}')),
      body: _loading
          ? const Center(child: CircularProgressIndicator(color: cTeal))
          : a == null
              ? const Center(
                  child: Text('Could not load alert',
                      style: TextStyle(color: cDim)))
              : ListView(
                  padding: const EdgeInsets.all(16),
                  children: [
                    if (a['snapshot'] != null)
                      ClipRRect(
                        borderRadius: BorderRadius.circular(12),
                        child: Image.network(Api.mediaUrl(a['snapshot']),
                            fit: BoxFit.cover,
                            errorBuilder: (_, __, ___) => Container(
                                height: 180,
                                color: cPanel,
                                child: const Center(
                                    child: Text('snapshot unavailable',
                                        style: TextStyle(color: cDim))))),
                      ),
                    const SizedBox(height: 16),
                    Row(children: [
                      tierPill((a['tier'] ?? 1) as int),
                      const SizedBox(width: 10),
                      Text(a['event'] ?? '',
                          style: const TextStyle(
                              color: cText,
                              fontSize: 16,
                              fontWeight: FontWeight.bold)),
                    ]),
                    const SizedBox(height: 16),
                    _detail('Camera', a['camera']?.toString() ?? '—'),
                    _detail('Distance', '${a['distance_m'] ?? '-'} m'),
                    _detail(
                        'Confidence',
                        a['confidence'] != null
                            ? a['confidence'].toString()
                            : '—'),
                    _detail('Time', a['timestamp']?.toString() ?? '—'),
                    _detail('Video', a['has_video'] == true ? 'recorded' : 'none'),
                  ],
                ),
    );
  }

  Widget _detail(String k, String v) => Container(
        padding: const EdgeInsets.symmetric(vertical: 12),
        decoration: const BoxDecoration(
            border: Border(top: BorderSide(color: cLine))),
        child: Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Text(k, style: const TextStyle(color: cMuted, fontSize: 13)),
            Text(v,
                style: const TextStyle(
                    color: cText, fontWeight: FontWeight.w600)),
          ],
        ),
      );
}
