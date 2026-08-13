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
  // Which tier filter is active - 'all' or '1'/'2'/'3'. Replaces the old
  // High/Low-priority collapsible grouping: that hid Tier 1 & 2 behind an
  // expand tap by default, which made it slower to check a specific tier.
  // Explicit All/Tier 3/Tier 2/Tier 1 buttons let you jump straight to
  // exactly what you want to check, one tap, nothing hidden by default.
  String _filter = 'all';
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
                onPressed: _ackAllPending ? null : _ackAll,
                icon: _ackAllPending
                    ? const SizedBox(
                        width: 18,
                        height: 18,
                        child: CircularProgressIndicator(strokeWidth: 2, color: cMuted))
                    : const Icon(Icons.done_all, color: cMuted)),
          IconButton(
              onPressed: _load,
              icon: const Icon(Icons.refresh, color: cMuted)),
        ],
      ),
      body: Column(children: [
        _filterBar(),
        Expanded(
          child: RefreshIndicator(
            onRefresh: _load,
            color: cTeal,
            // Was Center(CircularProgressIndicator) swapped in for the
            // ENTIRE body while _loading - that meant the whole screen was
            // unscrollable/untouchable (no pull-to-refresh, nothing) for
            // the whole first load, which is a genuine freeze, not just a
            // feeling. A ListView must always be the direct child for
            // RefreshIndicator to even work, so now the loading state is
            // just a small inline row INSIDE that same always-present
            // scrollable - the screen never stops being interactive.
            child: _loading
                ? ListView(children: const [
                    Padding(
                      padding: EdgeInsets.only(top: 120),
                      child: Center(
                          child: CircularProgressIndicator(color: cTeal)),
                    ),
                  ])
                : _alerts.isEmpty
                    ? ListView(children: [
                        const SizedBox(height: 100),
                        Center(
                          child: Column(children: [
                            Container(
                              width: 84,
                              height: 84,
                              decoration: BoxDecoration(
                                shape: BoxShape.circle,
                                color: (Api.online.value ? cTeal2 : cRed)
                                    .withValues(alpha: 0.12),
                              ),
                              child: Icon(
                                  Api.online.value
                                      ? Icons.shield_outlined
                                      : Icons.cloud_off,
                                  size: 40,
                                  color: Api.online.value ? cTeal2 : cRed),
                            ),
                            const SizedBox(height: 16),
                            Text(
                                Api.online.value
                                    ? 'All clear'
                                    : 'Not connected to CAPHY',
                                style: TextStyle(
                                    color: Api.online.value ? cText : cRed,
                                    fontSize: 15,
                                    fontWeight: FontWeight.w700)),
                            const SizedBox(height: 6),
                            Text(
                                Api.online.value
                                    ? 'No alerts right now - CAPHY is watching'
                                    : 'Check your connection to see alerts',
                                style:
                                    const TextStyle(color: cDim, fontSize: 12.5)),
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
                    : _buildFilteredList(context),
          ),
        ),
      ]),
    );
  }

  /// Filter buttons: All / Tier 3 / Tier 2 / Tier 1 - replaces the old
  /// High/Low-priority collapsible grouping. Everything is visible by
  /// default (All); tapping a tier shows only that tier, one tap, nothing
  /// hidden behind an expand toggle, so checking a specific tier is fast.
  Widget _filterBar() {
    final counts = {
      'all': _alerts.length,
      '3': _alerts.where((a) => asInt(a['tier'], 1) == 3).length,
      '2': _alerts.where((a) => asInt(a['tier'], 1) == 2).length,
      '1': _alerts.where((a) => asInt(a['tier'], 1) == 1).length,
    };
    Widget chip(String value, String label, Color color) {
      final active = _filter == value;
      return Padding(
        padding: const EdgeInsets.only(right: 8),
        child: ChoiceChip(
          selected: active,
          onSelected: (_) => setState(() => _filter = value),
          label: Text('$label (${counts[value]})',
              style: TextStyle(
                  color: active ? Colors.black : color,
                  fontWeight: FontWeight.w700,
                  fontSize: 12.5)),
          backgroundColor: cPanel,
          selectedColor: color,
          side: BorderSide(color: active ? color : cLine),
          shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(20)),
        ),
      );
    }

    return Padding(
      padding: const EdgeInsets.fromLTRB(14, 10, 14, 4),
      child: SingleChildScrollView(
        scrollDirection: Axis.horizontal,
        child: Row(children: [
          chip('all', 'All', cTeal2),
          chip('3', 'Tier 3', cRed),
          chip('2', 'Tier 2', cOrange),
          chip('1', 'Tier 1', cTeal2),
        ]),
      ),
    );
  }

  /// Alerts matching the active tier filter, newest first - a flat list,
  /// nothing grouped or hidden by default.
  Widget _buildFilteredList(BuildContext context) {
    final shown = _filter == 'all'
        ? _alerts
        : _alerts.where((a) => asInt(a['tier'], 1).toString() == _filter).toList();

    if (shown.isEmpty) {
      return ListView(children: [
        const SizedBox(height: 90),
        Center(
          child: Column(children: [
            Container(
              width: 72,
              height: 72,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                color: cTeal2.withValues(alpha: 0.12),
              ),
              child: const Icon(Icons.security, size: 34, color: cTeal2),
            ),
            const SizedBox(height: 14),
            Text(
                _filter == 'all'
                    ? 'No alerts'
                    : 'No Tier $_filter alerts',
                style: const TextStyle(
                    color: cText, fontSize: 14, fontWeight: FontWeight.w600)),
            const SizedBox(height: 4),
            const Text('Nothing to review here right now',
                style: TextStyle(color: cDim, fontSize: 12)),
          ]),
        ),
      ]);
    }

    return ListView(
      padding: const EdgeInsets.all(14),
      children: shown.map((a) => _row(context, a)).toList(),
    );
  }

  // In-flight acknowledge ids - guards against a double-tap (or a fast
  // swipe + button tap) firing dismissAlert() twice for the same alert,
  // which previously could race two remote commands for one id.
  final Set<int> _ackInFlight = {};

  /// Acknowledge one alert. Removes the row from view IMMEDIATELY on tap
  /// (optimistic) rather than waiting for the network round trip, then
  /// confirms in the background; if it turns out CAPHY couldn't be reached
  /// at all, the row is restored and the user is told, instead of the row
  /// silently vanishing only to reappear on next refresh.
  Future<void> _ack(int id) async {
    if (_ackInFlight.contains(id)) return;
    _ackInFlight.add(id);

    dynamic removed;
    int removedIndex = -1;
    for (int i = 0; i < _alerts.length; i++) {
      if (asInt(_alerts[i]['id']) == id) {
        removedIndex = i;
        removed = _alerts[i];
        break;
      }
    }
    if (removedIndex >= 0 && mounted) {
      setState(() => _alerts.removeAt(removedIndex));
    }

    try {
      // Hard ceiling independent of whatever dismissAlert/sendRemoteCommand
      // do internally - guarantees _ackInFlight always clears so this id
      // can be retried, instead of getting permanently stuck if the
      // underlying call never resolves.
      final ok = await Api.dismissAlert(id)
          .timeout(const Duration(seconds: 18), onTimeout: () => false);
      if (!mounted) return;
      if (!ok) {
        // Roll back the optimistic removal - don't leave the user thinking
        // it worked when it didn't.
        if (removed != null) {
          setState(() {
            final insertAt = removedIndex.clamp(0, _alerts.length);
            _alerts.insert(insertAt, removed);
          });
        }
        showTopToast(context, 'Could not reach CAPHY - try again', error: true);
        return;
      }
      showTopToast(context, 'Alert acknowledged ✓');
    } finally {
      _ackInFlight.remove(id);
    }
  }

  // Separate in-flight guard from _ackInFlight - a delete and an
  // acknowledge on the same id are different operations (confirmDismiss
  // already stops both firing on the SAME swipe, but this still stops a
  // double-tap of a delete button from firing twice).
  final Set<int> _deleteInFlight = {};

  /// Permanently delete one alert. Same optimistic-remove-then-confirm
  /// pattern as _ack() - the row disappears immediately, and is restored
  /// with an error toast if the delete didn't actually reach CAPHY.
  Future<void> _delete(int id) async {
    if (_deleteInFlight.contains(id)) return;
    _deleteInFlight.add(id);

    dynamic removed;
    int removedIndex = -1;
    for (int i = 0; i < _alerts.length; i++) {
      if (asInt(_alerts[i]['id']) == id) {
        removedIndex = i;
        removed = _alerts[i];
        break;
      }
    }
    if (removedIndex >= 0 && mounted) {
      setState(() => _alerts.removeAt(removedIndex));
    }

    try {
      final ok = await Api.deleteAlert(id)
          .timeout(const Duration(seconds: 18), onTimeout: () => false);
      if (!mounted) return;
      if (!ok) {
        if (removed != null) {
          setState(() {
            final insertAt = removedIndex.clamp(0, _alerts.length);
            _alerts.insert(insertAt, removed);
          });
        }
        // Was a generic "Could not reach CAPHY" no matter what actually
        // went wrong - Api.deleteAlert() now records the real reason in
        // lastError (LAN failure, no paired device, remote command timeout,
        // or the remote command completing with a non-"done" status), so
        // this is now visible directly on the phone instead of needing
        // laptop console access to diagnose a delete that silently didn't
        // take effect.
        showTopToast(
            context,
            Api.lastError.isNotEmpty
                ? 'Delete failed: ${Api.lastError}'
                : 'Could not reach CAPHY - try again',
            error: true);
        return;
      }
      showTopToast(context, 'Alert deleted');
    } finally {
      _deleteInFlight.remove(id);
    }
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
    if (_ackAllPending) return;
    setState(() => _ackAllPending = true);
    showTopToast(context, 'Acknowledging all…');
    try {
      final ok2 = await Api.dismissAllAlerts()
          .timeout(const Duration(seconds: 18), onTimeout: () => false);
      if (!mounted) return;
      if (!ok2) {
        showTopToast(context, 'Could not reach CAPHY - try again', error: true);
        return;
      }
      showTopToast(context, 'All alerts acknowledged ✓');
      await _load();
    } catch (e) {
      if (mounted) {
        showTopToast(context, 'Could not reach CAPHY - try again', error: true);
      }
    } finally {
      if (mounted) setState(() => _ackAllPending = false);
    }
  }

  bool _ackAllPending = false;

  Widget _row(BuildContext context, dynamic a) {
    final tier = asInt(a['tier'], 1);
    final id = asInt(a['id']);
    final event = (a['event'] ?? 'Alert #$id').toString();
    final sub = '${a['distance_m'] ?? '-'} m away'
        '${a['camera'] != null ? ' · ${a['camera']}' : ''}'
        ' · ${_time(a['timestamp'])}';
    // Two-way swipe: right-to-left (endToStart) acknowledges - the row is
    // only HIDDEN, it stays in the database, same as before. Left-to-right
    // (startToEnd) is NEW - permanently deletes the alert (and its
    // snapshot/video on the laptop), so it confirms first via
    // confirmDismiss rather than firing immediately like acknowledge does,
    // since this one can't be undone.
    return Dismissible(
      key: ValueKey('alert$id'),
      direction: DismissDirection.horizontal,
      confirmDismiss: (direction) async {
        if (direction == DismissDirection.endToStart) return true;
        final confirmed = await showDialog<bool>(
          context: context,
          builder: (_) => AlertDialog(
            backgroundColor: cPanel,
            title: const Text('Delete this alert?',
                style: TextStyle(color: cText)),
            content: const Text(
                'This permanently removes the alert and its snapshot/video. '
                'This can\'t be undone.',
                style: TextStyle(color: cMuted)),
            actions: [
              TextButton(
                  onPressed: () => Navigator.pop(context, false),
                  child: const Text('Cancel', style: TextStyle(color: cMuted))),
              TextButton(
                  onPressed: () => Navigator.pop(context, true),
                  child: const Text('Delete', style: TextStyle(color: cRed))),
            ],
          ),
        );
        return confirmed ?? false;
      },
      background: Container(
        alignment: Alignment.centerLeft,
        padding: const EdgeInsets.only(left: 20, bottom: 10),
        decoration: BoxDecoration(
          color: cRed.withValues(alpha: 0.25),
          borderRadius: BorderRadius.circular(12),
        ),
        child: const Row(
            mainAxisAlignment: MainAxisAlignment.start,
            children: [
              Icon(Icons.delete_outline, color: cRed),
              SizedBox(width: 8),
              Text('Delete',
                  style: TextStyle(color: cRed, fontWeight: FontWeight.w600)),
            ]),
      ),
      secondaryBackground: Container(
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
      onDismissed: (direction) {
        if (direction == DismissDirection.endToStart) {
          _ack(id);
        } else {
          _delete(id);
        }
      },
      child: Container(
        margin: const EdgeInsets.only(bottom: 12),
        decoration: BoxDecoration(
          color: cPanel2.withValues(alpha: 0.72),
          borderRadius: BorderRadius.circular(16),
          // A Border with per-side colors (accent left edge + faint rest)
          // plus borderRadius throws "A borderRadius can only be given on
          // borders with uniform colors" at paint time - Flutter drops the
          // whole card's content silently (no red error, just an empty
          // box), which is why every alert here rendered blank despite
          // real data ("All (100)", "5 shown" etc. still showing fine,
          // since those live outside this widget). Fixed by using one
          // uniform border and drawing the tier accent as a separate
          // rectangle instead of a border side.
          border: Border.all(color: const Color(0x22789AD2)),
        ),
        child: Material(
          color: Colors.transparent,
          child: InkWell(
            borderRadius: BorderRadius.circular(16),
            onTap: () => Navigator.of(context).push(
                MaterialPageRoute(builder: (_) => AlertDetailScreen(id: id))),
            child: Row(children: [
              ClipRRect(
                borderRadius: const BorderRadius.horizontal(left: Radius.circular(16)),
                child: Container(width: 3, height: 76, color: tierColor(tier)),
              ),
              Expanded(
                child: Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
                  child: Row(children: [
                    AlertThumbnail(
                        snapshot: a['snapshot']?.toString(), tier: tier, size: 56),
                    const SizedBox(width: 12),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Row(children: [
                            Flexible(
                              child: Text(event,
                                  overflow: TextOverflow.ellipsis,
                                  style: const TextStyle(
                                      color: cText,
                                      fontWeight: FontWeight.w600,
                                      fontSize: 14.5)),
                            ),
                            const SizedBox(width: 8),
                            _tierPill(tier),
                          ]),
                          const SizedBox(height: 4),
                          Text(sub,
                              style: const TextStyle(color: cMuted, fontSize: 12.5)),
                        ],
                      ),
                    ),
                    const SizedBox(width: 8),
                    OutlinedButton(
                      onPressed: () => _ack(id),
                      style: OutlinedButton.styleFrom(
                        foregroundColor: cTeal2,
                        backgroundColor: cPanel,
                        side: const BorderSide(color: cLine),
                        padding: const EdgeInsets.symmetric(
                            horizontal: 12, vertical: 8),
                        shape: RoundedRectangleBorder(
                            borderRadius: BorderRadius.circular(8)),
                        minimumSize: const Size(0, 34),
                      ),
                      child: const Text('Acknowledge',
                          style: TextStyle(fontSize: 12)),
                    ),
                  ]),
                ),
              ),
            ]),
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
  bool _acking = false;
  bool _deleting = false;

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

  /// Same Acknowledge behavior as AlertsTab._ack(): dismisses the alert on
  /// the server, shows a toast, then pops back to the list.
  Future<void> _acknowledge() async {
    if (_acking) return;
    setState(() => _acking = true);
    bool ok = false;
    try {
      ok = await Api.dismissAlert(widget.id)
          .timeout(const Duration(seconds: 18), onTimeout: () => false);
    } catch (e) {
      ok = false;
    }
    if (!mounted) return;
    setState(() => _acking = false);
    if (!ok) {
      showTopToast(context, 'Could not reach CAPHY - try again', error: true);
      return;
    }
    showTopToast(context, 'Alert acknowledged ✓');
    Navigator.of(context).pop();
  }

  Future<void> _delete() async {
    if (_deleting) return;
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (_) => AlertDialog(
        backgroundColor: cPanel,
        title: const Text('Delete this alert?', style: TextStyle(color: cText)),
        content: const Text(
            'This permanently removes the alert and its snapshot/video. '
            'This can\'t be undone.',
            style: TextStyle(color: cMuted)),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('Cancel', style: TextStyle(color: cMuted))),
          TextButton(
              onPressed: () => Navigator.pop(context, true),
              child: const Text('Delete', style: TextStyle(color: cRed))),
        ],
      ),
    );
    if (confirmed != true) return;
    setState(() => _deleting = true);
    bool ok = false;
    try {
      ok = await Api.deleteAlert(widget.id)
          .timeout(const Duration(seconds: 18), onTimeout: () => false);
    } catch (e) {
      ok = false;
    }
    if (!mounted) return;
    setState(() => _deleting = false);
    if (!ok) {
      // Same visible-real-reason improvement as AlertsTab._delete().
      showTopToast(
          context,
          Api.lastError.isNotEmpty
              ? 'Delete failed: ${Api.lastError}'
              : 'Could not reach CAPHY - try again',
          error: true);
      return;
    }
    showTopToast(context, 'Alert deleted');
    Navigator.of(context).pop();
  }

  @override
  Widget build(BuildContext context) {
    final a = _a;
    return Scaffold(
      appBar: AppBar(
        backgroundColor: cPanel,
        title: Text('Alert #${widget.id}'),
        actions: [
          IconButton(
            tooltip: 'Delete',
            onPressed: _deleting ? null : _delete,
            icon: _deleting
                ? const SizedBox(
                    width: 18,
                    height: 18,
                    child: CircularProgressIndicator(strokeWidth: 2, color: cRed))
                : const Icon(Icons.delete_outline, color: cRed),
          ),
        ],
      ),
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
                    _detail('Distance', '${a['distance_m'] ?? '-'} m away'),
                    _detail(
                        'Confidence',
                        a['confidence'] != null
                            ? a['confidence'].toString()
                            : '—'),
                    _detail('Time', a['timestamp']?.toString() ?? '—'),
                    _detail('Video', a['has_video'] == true ? 'recorded' : 'none'),
                    const SizedBox(height: 24),
                    SizedBox(
                      width: double.infinity,
                      child: FilledButton.icon(
                        onPressed: _acking ? null : _acknowledge,
                        style: FilledButton.styleFrom(
                          backgroundColor: cTeal,
                          padding: const EdgeInsets.symmetric(vertical: 14),
                          shape: RoundedRectangleBorder(
                              borderRadius: BorderRadius.circular(10)),
                        ),
                        icon: _acking
                            ? const SizedBox(
                                width: 16,
                                height: 16,
                                child: CircularProgressIndicator(
                                    strokeWidth: 2, color: Colors.black))
                            : const Icon(Icons.done, color: Colors.black),
                        label: const Text('Acknowledge',
                            style: TextStyle(
                                color: Colors.black, fontWeight: FontWeight.bold)),
                      ),
                    ),
                  ],
                ),
    );
  }

  Widget _detail(String k, String v) => Container(
        padding: const EdgeInsets.symmetric(vertical: 13),
        decoration: const BoxDecoration(
            border: Border(top: BorderSide(color: cLine))),
        child: Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Text(k, style: const TextStyle(color: cMuted, fontSize: 13.5)),
            Text(v,
                style: const TextStyle(
                    color: cText, fontWeight: FontWeight.w600, fontSize: 14)),
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
