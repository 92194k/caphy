import 'package:flutter/material.dart';
import 'api.dart';
import 'theme.dart';

class AlertsTab extends StatefulWidget {
  const AlertsTab({super.key});
  @override
  State<AlertsTab> createState() => _AlertsTabState();
}

class _AlertsTabState extends State<AlertsTab> {
  List<dynamic> _alerts = [];
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    final a = await Api.alerts(limit: 100);
    if (mounted) {
      setState(() {
        _alerts = a;
        _loading = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        backgroundColor: cPanel,
        title: const Text('Alerts'),
        actions: [
          IconButton(
              onPressed: _load,
              icon: const Icon(Icons.refresh, color: cMuted)),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: _load,
        color: cTeal,
        child: _loading
            ? const Center(child: CircularProgressIndicator(color: cTeal))
            : _alerts.isEmpty
                ? ListView(children: const [
                    SizedBox(height: 120),
                    Center(
                        child: Text('No alerts yet',
                            style: TextStyle(color: cDim)))
                  ])
                : ListView.builder(
                    padding: const EdgeInsets.all(14),
                    itemCount: _alerts.length,
                    itemBuilder: (_, i) => _row(context, _alerts[i]),
                  ),
      ),
    );
  }

  Widget _row(BuildContext context, dynamic a) {
    final tier = (a['tier'] ?? 1) as int;
    final snap = a['snapshot'];
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: InkWell(
        borderRadius: BorderRadius.circular(12),
        onTap: () => Navigator.of(context).push(MaterialPageRoute(
            builder: (_) => AlertDetailScreen(id: a['id'] as int))),
        child: Container(
          padding: const EdgeInsets.all(10),
          decoration: BoxDecoration(
            color: cPanel,
            borderRadius: BorderRadius.circular(12),
            border: Border(
                left: BorderSide(color: tierColor(tier), width: 4),
                top: BorderSide(color: cLine),
                right: BorderSide(color: cLine),
                bottom: BorderSide(color: cLine)),
          ),
          child: Row(children: [
            ClipRRect(
              borderRadius: BorderRadius.circular(8),
              child: snap != null
                  ? Image.network(Api.mediaUrl(snap),
                      width: 58,
                      height: 44,
                      fit: BoxFit.cover,
                      errorBuilder: (_, __, ___) => _thumbFallback())
                  : _thumbFallback(),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(children: [
                    tierPill(tier),
                    const SizedBox(width: 8),
                    Flexible(
                      child: Text(a['event'] ?? '',
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                              color: cText, fontWeight: FontWeight.w600)),
                    ),
                  ]),
                  const SizedBox(height: 4),
                  Text(
                      '${a['distance_m'] ?? '-'} m'
                      '${a['camera'] != null ? ' · ${a['camera']}' : ''}'
                      ' · ${_time(a['timestamp'])}',
                      style: const TextStyle(color: cDim, fontSize: 12)),
                ],
              ),
            ),
            const Icon(Icons.chevron_right, color: cDim),
          ]),
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
