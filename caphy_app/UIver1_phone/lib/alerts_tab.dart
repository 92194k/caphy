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
  bool _showLower = false;   // is the Tier 1/2 "lower priority" group expanded
  Timer? _fallbackPoll;
  StreamSubscription<void>? _liveSub;

  @override
  void initState() {
    super.initState();
    _load();
    // Real-time: the moment a tier alert is confirmed on the system, it is
    // pushed here instantly (SSE) - no polling delay, no manual refresh.
    _liveSub = Api.watchAlerts().listen((_) => _load(quiet: true));
    // Slow fallback poll in case the live connection ever silently drops
    // (backgrounded app, flaky Wi-Fi) - a safety net, not the main path.
    _fallbackPoll =
        Timer.periodic(const Duration(seconds: 15), (_) => _load(quiet: true));
  }

  @override
  void dispose() {
    _liveSub?.cancel();
    _fallbackPoll?.cancel();
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
                    : ListView(
                        padding: const EdgeInsets.all(14),
                        children: _buildGroupedAlerts(context),
                      ),
          ),
        ),
      ]),
    );
  }

  /// Builds the alert list grouped by priority: Tier 3 (high priority) shown
  /// first and always, then a collapsible "Lower priority" group holding
  /// Tier 1 & 2. This is the alert-fatigue layout - the alerts that matter
  /// most are up top and never buried, while the routine ones are tucked
  /// away (still saved, still with snapshots, just one tap to reveal).
  List<Widget> _buildGroupedAlerts(BuildContext context) {
    final high = _alerts.where((a) => asInt(a['tier'], 1) >= 3).toList();
    final lower = _alerts.where((a) => asInt(a['tier'], 1) < 3).toList();

    final children = <Widget>[];

    if (high.isNotEmpty) {
      children.add(_groupLabel('HIGH PRIORITY · TIER 3', cRed));
      children.addAll(high.map((a) => _row(context, a)));
    } else {
      children.add(Padding(
        padding: const EdgeInsets.symmetric(vertical: 18),
        child: Center(
          child: Text('No high-priority (Tier 3) alerts',
              style: TextStyle(color: cDim, fontSize: 13)),
        ),
      ));
    }

    if (lower.isNotEmpty) {
      children.add(const SizedBox(height: 8));
      children.add(
        InkWell(
          onTap: () => setState(() => _showLower = !_showLower),
          borderRadius: BorderRadius.circular(10),
          child: Container(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 12),
            decoration: BoxDecoration(
              color: cPanel,
              borderRadius: BorderRadius.circular(10),
              border: Border.all(color: cLine),
            ),
            child: Row(children: [
              Icon(_showLower ? Icons.expand_less : Icons.expand_more,
                  color: cMuted, size: 20),
              const SizedBox(width: 8),
              Expanded(
                child: Text('Lower priority · Tier 1 & 2  (${lower.length})',
                    style: const TextStyle(
                        color: cText, fontSize: 13, fontWeight: FontWeight.w600)),
              ),
              Text(_showLower ? 'Hide' : 'Show',
                  style: const TextStyle(color: cTeal2, fontSize: 12)),
            ]),
          ),
        ),
      );
      if (_showLower) {
        children.add(const SizedBox(height: 10));
        children.addAll(lower.map((a) => _row(context, a)));
      }
    }

    return children;
  }

  Widget _groupLabel(String text, Color color) => Padding(
        padding: const EdgeInsets.only(bottom: 8, top: 2),
        child: Text(text,
            style: TextStyle(
                color: color, fontSize: 11, letterSpacing: 1.2,
                fontWeight: FontWeight.w700)),
      );

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
          color: cTeal.withValues(alpha: 0.25),
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
          leading: Container(
            width: 44,
            height: 44,
            decoration: BoxDecoration(
              color: cTeal2.withValues(alpha: 0.15),
              borderRadius: BorderRadius.circular(10),
            ),
            child: Icon(Icons.person, color: cTeal2, size: 22),
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
                      GestureDetector(
                        onTap: () => Navigator.of(context).push(
                          MaterialPageRoute(
                            builder: (_) => PhotoViewerScreen(
                              url: Api.mediaUrl(a['snapshot']),
                              title: [
                                (a['event'] ?? '').toString(),
                                if (a['camera'] != null) a['camera'].toString(),
                                (a['timestamp'] ?? '').toString(),
                              ].where((s) => s.isNotEmpty).join(' · '),
                            ),
                          ),
                        ),
                        child: Stack(children: [
                          ClipRRect(
                            borderRadius: BorderRadius.circular(12),
                            child: Image.network(Api.mediaUrl(a['snapshot']),
                                fit: BoxFit.cover,
                                width: double.infinity,
                                errorBuilder: (_, _, _) => Container(
                                    height: 180,
                                    color: cPanel,
                                    child: const Center(
                                        child: Text('snapshot unavailable',
                                            style: TextStyle(color: cDim))))),
                          ),
                          Positioned(
                            right: 10,
                            bottom: 10,
                            child: Container(
                              padding: const EdgeInsets.symmetric(
                                  horizontal: 10, vertical: 6),
                              decoration: BoxDecoration(
                                color: Colors.black.withValues(alpha: 0.55),
                                borderRadius: BorderRadius.circular(20),
                                border: Border.all(color: cLine),
                              ),
                              child: const Row(
                                mainAxisSize: MainAxisSize.min,
                                children: [
                                  Icon(Icons.zoom_in, size: 14, color: Colors.white),
                                  SizedBox(width: 5),
                                  Text('View',
                                      style: TextStyle(
                                          color: Colors.white, fontSize: 11)),
                                ],
                              ),
                            ),
                          ),
                        ]),
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

// ==================== Snapshot photo viewer ====================
// Fullscreen pinch-to-zoom / drag-to-pan viewer, matching the web console's
// snapshot lightbox: dark background, title bar (event · camera · time),
// zoom controls, Exit, and a hint line at the bottom.
class PhotoViewerScreen extends StatefulWidget {
  final String url;
  final String title;
  const PhotoViewerScreen({super.key, required this.url, required this.title});

  @override
  State<PhotoViewerScreen> createState() => _PhotoViewerScreenState();
}

class _PhotoViewerScreenState extends State<PhotoViewerScreen> {
  final _controller = TransformationController();
  double _scale = 1.0;

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  void _setScale(double s) {
    s = s.clamp(1.0, 6.0);
    _controller.value = Matrix4.diagonal3Values(s, s, 1.0);
    setState(() => _scale = s);
  }

  void _reset() => _setScale(1.0);

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: const Color(0xF2070B0E),
      body: SafeArea(
        child: Column(children: [
          // ---- title bar: matches the web lightbox layout ----
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 10, 12, 10),
            child: Row(children: [
              Expanded(
                child: Text(widget.title,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(
                        color: cText,
                        fontSize: 13,
                        fontWeight: FontWeight.w600)),
              ),
              _ctrlBtn(Icons.remove, () => _setScale(_scale - 0.5)),
              const SizedBox(width: 6),
              GestureDetector(
                onTap: _reset,
                child: Container(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
                  decoration: BoxDecoration(
                    color: cPanel2,
                    borderRadius: BorderRadius.circular(8),
                    border: Border.all(color: cLine),
                  ),
                  child: Text('${(_scale * 100).round()}%',
                      style: const TextStyle(color: cText, fontSize: 12)),
                ),
              ),
              const SizedBox(width: 6),
              _ctrlBtn(Icons.add, () => _setScale(_scale + 0.5)),
              const SizedBox(width: 10),
              GestureDetector(
                onTap: () => Navigator.pop(context),
                child: Container(
                  padding: const EdgeInsets.symmetric(
                      horizontal: 14, vertical: 9),
                  decoration: BoxDecoration(
                    color: cRed,
                    borderRadius: BorderRadius.circular(8),
                  ),
                  child: const Row(mainAxisSize: MainAxisSize.min, children: [
                    Icon(Icons.close, size: 15, color: Colors.white),
                    SizedBox(width: 5),
                    Text('Exit',
                        style: TextStyle(color: Colors.white, fontSize: 13)),
                  ]),
                ),
              ),
            ]),
          ),
          // ---- image stage: pinch to zoom, drag to pan ----
          Expanded(
            child: InteractiveViewer(
              transformationController: _controller,
              minScale: 1.0,
              maxScale: 6.0,
              onInteractionUpdate: (d) {
                final s = _controller.value.getMaxScaleOnAxis();
                if ((s - _scale).abs() > 0.02) setState(() => _scale = s);
              },
              child: Center(
                child: Image.network(
                  widget.url,
                  fit: BoxFit.contain,
                  errorBuilder: (_, _, _) => const Text(
                      'snapshot unavailable',
                      style: TextStyle(color: cDim)),
                ),
              ),
            ),
          ),
          // ---- hint line: matches "scroll to zoom · drag to pan · Esc to exit" ----
          const Padding(
            padding: EdgeInsets.symmetric(vertical: 14),
            child: Text('pinch to zoom · drag to pan · tap Exit to close',
                style: TextStyle(color: cMuted, fontSize: 12)),
          ),
        ]),
      ),
    );
  }

  Widget _ctrlBtn(IconData icon, VoidCallback onTap) => GestureDetector(
        onTap: onTap,
        child: Container(
          width: 34,
          height: 34,
          decoration: BoxDecoration(
            color: cPanel2,
            borderRadius: BorderRadius.circular(8),
            border: Border.all(color: cLine),
          ),
          child: Icon(icon, size: 17, color: cText),
        ),
      );
}
