import 'dart:async';
import 'dart:io';
import 'dart:typed_data';
import 'package:flutter/material.dart';
import 'package:gal/gal.dart';
import 'package:http/http.dart' as http;
import 'package:path_provider/path_provider.dart';
import 'api.dart';
import 'theme.dart';

/// Smooth MJPEG live view without any extra package.
///
/// Opens the multipart MJPEG stream with the `http` client and splits it into
/// JPEG frames (each starts with FF D8 and ends with FF D9). Each frame is
/// shown with Image.memory(gaplessPlayback), so there is no flicker and no
/// per-frame HTTP request - which is what caused the lag before.
class MjpegView extends StatefulWidget {
  final String url;
  final BoxFit fit;
  final bool active;
  // Called when the local stream can't be shown (bad status, dropped
  // connection, OR no frame within a few seconds). live_tab uses this to
  // auto-switch to the WebRTC path so Live view still works.
  final VoidCallback? onFailed;
  const MjpegView(
      {super.key, required this.url, this.fit = BoxFit.cover,
       this.active = true, this.onFailed});
  @override
  State<MjpegView> createState() => _MjpegViewState();
}

class _MjpegViewState extends State<MjpegView> {
  http.Client? _client;
  StreamSubscription? _sub;
  Uint8List? _frame;
  bool _error = false;
  bool _failedReported = false;
  Timer? _firstFrameTimer;
  final List<int> _buf = [];

  @override
  void initState() {
    super.initState();
    if (widget.active) _connect();
  }

  @override
  void didUpdateWidget(covariant MjpegView old) {
    super.didUpdateWidget(old);
    if (old.url != widget.url || old.active != widget.active) {
      _stop();
      _buf.clear();
      _frame = null;
      _error = false;
      if (widget.active) _connect();
    }
  }

  void _reportFailed() {
    if (_failedReported) return;
    _failedReported = true;
    widget.onFailed?.call();
  }

  /// Manual retry (from the "Camera Unavailable" button) - resets every
  /// piece of state a fresh _connect() expects, exactly like
  /// didUpdateWidget already does when the url/active flag changes, so
  /// retrying looks identical to a normal reconnect rather than a special
  /// case that could leave stale state behind.
  void _retry() {
    _stop();
    _buf.clear();
    _frame = null;
    setState(() => _error = false);
    _failedReported = false;
    _connect();
  }

  Future<void> _connect() async {
    // If no first frame arrives quickly, treat the local path as unusable
    // and let live_tab fall back to WebRTC (this is the Wi-Fi "spinner
    // forever" fix).
    _firstFrameTimer?.cancel();
    _firstFrameTimer = Timer(const Duration(seconds: 5), () {
      if (_frame == null) {
        if (mounted) setState(() => _error = true);
        _reportFailed();
      }
    });
    try {
      _client = http.Client();
      final req = http.Request('GET', Uri.parse(widget.url));
      final resp = await _client!.send(req);
      if (resp.statusCode != 200) {
        if (mounted) setState(() => _error = true);
        _reportFailed();
        return;
      }
      _error = false;
      _sub = resp.stream.listen(_onBytes,
          onError: (_) => _fail(), onDone: _fail, cancelOnError: true);
    } catch (_) {
      _fail();
    }
  }

  void _onBytes(List<int> chunk) {
    _buf.addAll(chunk);
    // find a complete JPEG (FFD8 ... FFD9) in the buffer
    int start = -1, end = -1;
    for (int i = 0; i < _buf.length - 1; i++) {
      if (_buf[i] == 0xFF && _buf[i + 1] == 0xD8) {
        start = i;
        break;
      }
    }
    if (start < 0) {
      if (_buf.length > 1 << 20) _buf.clear(); // guard runaway buffer
      return;
    }
    for (int i = start + 2; i < _buf.length - 1; i++) {
      if (_buf[i] == 0xFF && _buf[i + 1] == 0xD9) {
        end = i + 2;
        break;
      }
    }
    if (end < 0) return;
    final frame = Uint8List.fromList(_buf.sublist(start, end));
    _buf.removeRange(0, end);
    _firstFrameTimer?.cancel();   // got a frame -> local path is good
    if (mounted) setState(() => _frame = frame);
  }

  void _fail() {
    if (mounted) setState(() => _error = true);
    _reportFailed();
  }

  void _stop() {
    _firstFrameTimer?.cancel();
    _sub?.cancel();
    _sub = null;
    _client?.close();
    _client = null;
  }

  @override
  void dispose() {
    _stop();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    if (!widget.active) {
      return const Center(
          child: Text('Camera is off', style: TextStyle(color: cDim)));
    }
    // Three distinct, deliberate states - never a bare frozen frame while
    // the stream is still starting up. This is what stops the first-open
    // "camera looks frozen/broken" impression: until a real decoded frame
    // has actually arrived, the video surface is fully covered by one of
    // the two states below, never a stale/blank image surface.
    if (_error) {
      return _CameraUnavailable(onRetry: _retry);
    }
    if (_frame == null) {
      return const _CameraLoading();
    }
    // Live frame is ready - crossfade in rather than a hard cut, so the
    // switch from "Camera Loading..." to real video reads as intentional
    // rather than a flicker/glitch.
    return AnimatedSwitcher(
      duration: const Duration(milliseconds: 280),
      child: Image.memory(_frame!,
          key: const ValueKey('liveframe'),
          fit: widget.fit,
          gaplessPlayback: true),
    );
  }
}

/// "Camera Loading..." placeholder shown from the moment the stream widget
/// mounts until the first real decoded frame arrives - covers the entire
/// preview area so nothing that could look like a frozen/broken feed is
/// ever visible during startup, exactly what panelists would otherwise
/// mistake for an error on first opening the Live tab.
class _CameraLoading extends StatefulWidget {
  const _CameraLoading();
  @override
  State<_CameraLoading> createState() => _CameraLoadingState();
}

class _CameraLoadingState extends State<_CameraLoading>
    with SingleTickerProviderStateMixin {
  late final AnimationController _pulse;

  @override
  void initState() {
    super.initState();
    _pulse = AnimationController(
        vsync: this, duration: const Duration(milliseconds: 1400))
      ..repeat(reverse: true);
  }

  @override
  void dispose() {
    _pulse.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Container(
      color: cBg,
      alignment: Alignment.center,
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          FadeTransition(
            opacity: Tween(begin: 0.35, end: 0.9).animate(
                CurvedAnimation(parent: _pulse, curve: Curves.easeInOut)),
            child: Container(
              width: 56,
              height: 56,
              decoration: BoxDecoration(
                color: cTeal2.withValues(alpha: 0.12),
                borderRadius: BorderRadius.circular(16),
                border: Border.all(color: cTeal2.withValues(alpha: 0.35)),
              ),
              child: const Icon(Icons.videocam_outlined,
                  color: cTeal2, size: 28),
            ),
          ),
          const SizedBox(height: 14),
          const SizedBox(
            width: 18,
            height: 18,
            child: CircularProgressIndicator(strokeWidth: 2, color: cTeal2),
          ),
          const SizedBox(height: 10),
          const Text('Camera Loading...',
              style: TextStyle(
                  color: cMuted, fontSize: 13, fontWeight: FontWeight.w600)),
        ],
      ),
    );
  }
}

/// Shown only once the connection has genuinely failed (bad status, dropped
/// stream, or no first frame within the timeout) - distinct wording and
/// look from the loading state above, so it never reads as "still trying"
/// when it has actually given up, and gives a clear way to try again
/// without leaving the Live tab or restarting the app.
class _CameraUnavailable extends StatelessWidget {
  final VoidCallback onRetry;
  const _CameraUnavailable({required this.onRetry});

  @override
  Widget build(BuildContext context) {
    return Container(
      color: cBg,
      alignment: Alignment.center,
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Container(
            width: 56,
            height: 56,
            decoration: BoxDecoration(
              color: cRed.withValues(alpha: 0.12),
              borderRadius: BorderRadius.circular(16),
              border: Border.all(color: cRed.withValues(alpha: 0.35)),
            ),
            child: const Icon(Icons.videocam_off_outlined,
                color: cRed, size: 28),
          ),
          const SizedBox(height: 14),
          const Text('Camera Unavailable',
              style: TextStyle(
                  color: cText, fontSize: 14, fontWeight: FontWeight.w700)),
          const SizedBox(height: 4),
          const Text('Could not connect to this camera.',
              style: TextStyle(color: cMuted, fontSize: 12.5)),
          const SizedBox(height: 14),
          OutlinedButton.icon(
            onPressed: onRetry,
            icon: const Icon(Icons.refresh, size: 16, color: cTeal2),
            label: const Text('Retry', style: TextStyle(color: cTeal2)),
            style: OutlinedButton.styleFrom(
              side: const BorderSide(color: cLine),
              backgroundColor: cPanel2,
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
              shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(8)),
            ),
          ),
        ],
      ),
    );
  }
}

/// Album name CAPHY creates in the phone gallery.
const kCaphyAlbum = 'CAPHY';

/// Why a gallery save failed, so the caller can show something more useful
/// than a generic "could not save" (and so it's diagnosable at all - the
/// old version swallowed every error silently, which is why this needed a
/// second look when it actually failed on-device).
enum SaveOutcome { ok, permissionDenied, downloadFailed, galleryWriteFailed }

/// Save an image the server just produced into the phone gallery album.
/// Downloads [url] bytes, writes a temp file, then hands it to Gal.
Future<SaveOutcome> saveImageToGalleryEx(String url,
    {String prefix = 'caphy'}) async {
  try {
    if (!await Gal.hasAccess()) {
      if (!await Gal.requestAccess()) {
        debugPrint('[CAPHY] gallery: photo permission denied by the user/OS');
        return SaveOutcome.permissionDenied;
      }
    }
  } catch (e) {
    debugPrint('[CAPHY] gallery: permission check failed: $e');
    return SaveOutcome.permissionDenied;
  }
  Uint8List bytes;
  try {
    final r = await http.get(Uri.parse(url)).timeout(const Duration(seconds: 20));
    if (r.statusCode != 200) {
      debugPrint('[CAPHY] gallery: download failed, HTTP ${r.statusCode}');
      return SaveOutcome.downloadFailed;
    }
    bytes = r.bodyBytes;
  } catch (e) {
    debugPrint('[CAPHY] gallery: download failed: $e');
    return SaveOutcome.downloadFailed;
  }
  try {
    final dir = await getTemporaryDirectory();
    final f = File(
        '${dir.path}/${prefix}_${DateTime.now().millisecondsSinceEpoch}.jpg');
    await f.writeAsBytes(bytes);
    await Gal.putImage(f.path, album: kCaphyAlbum);
    return SaveOutcome.ok;
  } catch (e) {
    debugPrint('[CAPHY] gallery: writing image to gallery failed: $e');
    return SaveOutcome.galleryWriteFailed;
  }
}

/// Back-compat bool wrapper for call sites that only need yes/no.
Future<bool> saveImageToGallery(String url, {String prefix = 'caphy'}) async =>
    (await saveImageToGalleryEx(url, prefix: prefix)) == SaveOutcome.ok;

/// Download a recording from the server and save it into the CAPHY gallery
/// album. [url] is the full http URL to the video file.
Future<SaveOutcome> saveVideoUrlToGalleryEx(String url,
    {String prefix = 'caphy'}) async {
  try {
    if (!await Gal.hasAccess()) {
      if (!await Gal.requestAccess()) {
        debugPrint('[CAPHY] gallery: video permission denied by the user/OS');
        return SaveOutcome.permissionDenied;
      }
    }
  } catch (e) {
    debugPrint('[CAPHY] gallery: permission check failed: $e');
    return SaveOutcome.permissionDenied;
  }
  // keep the real extension so the gallery recognises the container.
  var ext = 'mp4';
  final dot = url.lastIndexOf('.');
  if (dot != -1 && url.length - dot <= 5) {
    ext = url.substring(dot + 1).split('?').first.toLowerCase();
  }
  if (ext != 'mp4' && ext != 'mov') {
    // The PC only falls back to this (.avi/MJPG) when its H.264 encoder
    // isn't available - Android's gallery/MediaStore does not reliably
    // accept .avi, so this is the #1 cause of "could not save to gallery".
    debugPrint('[CAPHY] gallery: server sent a .$ext recording, not mp4 - '
        'the PC likely fell back to a codec Android cannot import. '
        'This save will probably fail.');
  }
  Uint8List bytes;
  try {
    final r = await http.get(Uri.parse(url)).timeout(const Duration(seconds: 60));
    if (r.statusCode != 200) {
      debugPrint('[CAPHY] gallery: download failed, HTTP ${r.statusCode}');
      return SaveOutcome.downloadFailed;
    }
    bytes = r.bodyBytes;
  } catch (e) {
    debugPrint('[CAPHY] gallery: download failed: $e');
    return SaveOutcome.downloadFailed;
  }
  try {
    final dir = await getTemporaryDirectory();
    final f = File(
        '${dir.path}/${prefix}_${DateTime.now().millisecondsSinceEpoch}.$ext');
    await f.writeAsBytes(bytes);
    await Gal.putVideo(f.path, album: kCaphyAlbum);
    return SaveOutcome.ok;
  } catch (e) {
    debugPrint('[CAPHY] gallery: writing video to gallery failed ($ext): $e');
    return SaveOutcome.galleryWriteFailed;
  }
}

/// Back-compat bool wrapper for call sites that only need yes/no.
Future<bool> saveVideoUrlToGallery(String url, {String prefix = 'caphy'}) async =>
    (await saveVideoUrlToGalleryEx(url, prefix: prefix)) == SaveOutcome.ok;

/// Brief message shown at the TOP of the screen (not the bottom), so it never
/// covers the mic or the controls on the voice screen.
// Kept as a thin wrapper so every existing call site (live_tab.dart,
// me_tab.dart, etc.) keeps working unchanged - the real widget is
// _TopToast below, a proper animated overlay instead of the old
// insta-appear/insta-disappear flat-colored box. That old version also
// treated "success" and "error" inconsistently (dark card vs. solid red
// fill), which is part of what read as "shitty" - now both states share
// one consistent glass-card look and only the accent color/icon changes.
//
// ONE toast at a time, globally. Rapid taps (e.g. tap a button, tap it
// again to cancel, tap again to retry - exactly what the arm/siren/
// emergency/camera "cancel on retap" buttons now do) used to call this
// every time, and EVERY call inserted a brand new OverlayEntry that only
// removed itself after its own 2.2s timer. Several of those stacking up
// at the same fixed top position, each independently animating in/out,
// is what made the top of the screen become an unresponsive stack of
// full-width Containers absorbing taps meant for whatever was underneath
// (the AppBar, the status row) - it read exactly like "click twice and
// the screen freezes." Now a new call immediately retires whatever toast
// is currently showing before inserting the new one, so there is never
// more than one on screen and never more than one pending removal timer.
OverlayEntry? _activeToastEntry;

void showTopToast(BuildContext context, String message, {bool error = false}) {
  // Retire whatever's currently showing FIRST, synchronously, so its
  // removal isn't racing the new one's insertion.
  _activeToastEntry?.remove();
  _activeToastEntry = null;

  final overlay = Overlay.of(context);
  late OverlayEntry entry;
  entry = OverlayEntry(
    builder: (ctx) => _TopToast(
      message: message,
      error: error,
      onDone: () {
        if (entry.mounted) entry.remove();
        if (identical(_activeToastEntry, entry)) _activeToastEntry = null;
      },
    ),
  );
  _activeToastEntry = entry;
  overlay.insert(entry);
}

class _TopToast extends StatefulWidget {
  final String message;
  final bool error;
  final VoidCallback onDone;
  const _TopToast(
      {required this.message, required this.error, required this.onDone});

  @override
  State<_TopToast> createState() => _TopToastState();
}

class _TopToastState extends State<_TopToast>
    with SingleTickerProviderStateMixin {
  late final AnimationController _c;
  late final Animation<Offset> _slide;
  late final Animation<double> _fade;

  @override
  void initState() {
    super.initState();
    _c = AnimationController(
        vsync: this, duration: const Duration(milliseconds: 260));
    _slide = Tween(begin: const Offset(0, -0.35), end: Offset.zero)
        .animate(CurvedAnimation(parent: _c, curve: Curves.easeOutCubic));
    _fade = CurvedAnimation(parent: _c, curve: Curves.easeOut);
    _c.forward();
    Future.delayed(const Duration(milliseconds: 2200), () async {
      if (!mounted) return;
      await _c.reverse();
      widget.onDone();
    });
  }

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final accent = widget.error ? cRed : cTeal2;
    return Positioned(
      top: MediaQuery.of(context).padding.top + 12,
      left: 16,
      right: 16,
      child: FadeTransition(
        opacity: _fade,
        child: SlideTransition(
          position: _slide,
          child: Material(
            color: Colors.transparent,
            child: Container(
              decoration: BoxDecoration(
                color: cPanel,
                borderRadius: BorderRadius.circular(14),
                border: Border.all(color: cLine),
                boxShadow: [
                  BoxShadow(
                      color: Colors.black.withValues(alpha: 0.45),
                      blurRadius: 18,
                      offset: const Offset(0, 6)),
                  BoxShadow(
                      color: accent.withValues(alpha: 0.18),
                      blurRadius: 22,
                      spreadRadius: -4),
                ],
              ),
              child: ClipRRect(
                borderRadius: BorderRadius.circular(14),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    // Solid accent spine instead of a per-side Border (which
                    // throws with a borderRadius - see the alert-row fix
                    // elsewhere in this app) - also just reads as more
                    // deliberate/"advanced" than a plain outline.
                    Container(width: 4, color: accent),
                    Expanded(
                      child: Padding(
                        padding: const EdgeInsets.fromLTRB(12, 12, 14, 12),
                        child: Row(children: [
                          Container(
                            width: 30,
                            height: 30,
                            decoration: BoxDecoration(
                              shape: BoxShape.circle,
                              color: accent.withValues(alpha: 0.16),
                            ),
                            child: Icon(
                                widget.error
                                    ? Icons.error_outline
                                    : Icons.check_circle_outline,
                                color: accent,
                                size: 17),
                          ),
                          const SizedBox(width: 12),
                          Expanded(
                            child: Text(widget.message,
                                style: const TextStyle(
                                    color: cText,
                                    fontSize: 13.5,
                                    fontWeight: FontWeight.w600)),
                          ),
                        ]),
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

/// Auto-switching connectivity banner. Watches Api.connectivityMode and:
///   - shows nothing when online (cloud features available),
///   - shows an amber "Offline mode · Local Wi‑Fi" bar when there's no
///     internet but the laptop IS reachable on the LAN (live view +
///     controls still work, just not the from-anywhere features) - this is
///     "Offline Mode",
///   - shows a red "Can't reach your laptop" bar when nothing is reachable
///     at all (different Wi‑Fi, laptop off, etc.) - this is "Laptop
///     Unreachable", a distinctly worse situation than Offline Mode since
///     NOTHING works until it's fixed,
///   - shows a teal "Back online" bar with a button when internet returns
///     after either of the above - this does NOT auto-clear; the user taps
///     it to confirm they've seen it (see Api.acknowledgeReconnect()).
/// This is what makes the app auto-detect a dropped internet connection and
/// fall back to the local network without the user doing anything, while
/// still giving them a deliberate "you're back" moment instead of a banner
/// that silently vanishes and might go unnoticed.
class ModeBanner extends StatefulWidget {
  const ModeBanner({super.key});

  @override
  State<ModeBanner> createState() => _ModeBannerState();
}

class _ModeBannerState extends State<ModeBanner> {
  bool _reconnecting = false;
  bool _connectingLocally = false;

  Future<void> _tapReconnect() async {
    if (_reconnecting) return;
    setState(() => _reconnecting = true);
    try {
      await Api.acknowledgeReconnect();
    } finally {
      if (mounted) setState(() => _reconnecting = false);
    }
  }

  /// "Connect Locally" - re-runs the same LAN self-heal that
  /// isLanReachable() does (pull the laptop's latest last_lan_ip from
  /// Firestore, try it, adopt it if it works), but as an explicit,
  /// user-triggered action from the red "can't reach" state rather than
  /// waiting for the next background poll. Also available from the Me tab.
  Future<void> _tapConnectLocally() async {
    if (_connectingLocally) return;
    setState(() => _connectingLocally = true);
    try {
      await Api.reconnectToPairedDevice();
      await Api.refreshConnectivity();
    } finally {
      if (mounted) setState(() => _connectingLocally = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return ValueListenableBuilder<String>(
      valueListenable: Api.connectivityMode,
      builder: (context, mode, _) {
        if (mode == 'online') return const SizedBox.shrink();

        // One compact row for every state: icon, one short label, and (if
        // relevant) a small trailing action. No subtitle line, no repeated
        // text in the button itself - the label already says what's
        // happening, the button just says what tapping it does.
        if (mode == 'reconnected') {
          return _bar(
            color: cTeal,
            icon: Icons.wifi,
            label: 'Back online',
            action: _reconnecting ? null : _tapReconnect,
            actionLabel: 'Reconnect',
            actionBusy: _reconnecting,
          );
        }

        final lan = mode == 'lan';
        return _bar(
          color: lan ? cOrange : cRed,
          icon: lan ? Icons.wifi_tethering : Icons.cloud_off,
          label: lan ? 'Offline mode · Local Wi‑Fi' : 'Can\'t reach your laptop',
          action: lan ? null : (_connectingLocally ? null : _tapConnectLocally),
          actionLabel: 'Connect locally',
          actionBusy: _connectingLocally,
        );
      },
    );
  }

  Widget _bar({
    required Color color,
    required IconData icon,
    required String label,
    required VoidCallback? action,
    required String actionLabel,
    required bool actionBusy,
  }) {
    // Was a flat full-width color-wash strip with a bare TextButton -
    // reads as a raised card now (icon in its own colored chip, a pill
    // action button instead of a plain text link) so it matches the same
    // glass-card language as the toast/alert popup instead of looking like
    // a separate, cheaper component.
    return AnimatedContainer(
      duration: const Duration(milliseconds: 220),
      curve: Curves.easeOut,
      width: double.infinity,
      margin: const EdgeInsets.fromLTRB(12, 10, 12, 2),
      padding: const EdgeInsets.fromLTRB(10, 9, 10, 9),
      decoration: BoxDecoration(
        color: cPanel2,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: color.withValues(alpha: 0.4)),
        boxShadow: [
          BoxShadow(
              color: color.withValues(alpha: 0.12),
              blurRadius: 14,
              spreadRadius: -4),
        ],
      ),
      child: Row(children: [
        Container(
          width: 30,
          height: 30,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            color: color.withValues(alpha: 0.16),
          ),
          child: Icon(icon, color: color, size: 16),
        ),
        const SizedBox(width: 10),
        Expanded(
          child: Text(label,
              style: TextStyle(
                  color: cText, fontWeight: FontWeight.w600, fontSize: 12.8)),
        ),
        if (action != null || actionBusy) ...[
          const SizedBox(width: 8),
          Material(
            color: color.withValues(alpha: 0.16),
            borderRadius: BorderRadius.circular(20),
            child: InkWell(
              borderRadius: BorderRadius.circular(20),
              onTap: action,
              child: Padding(
                padding:
                    const EdgeInsets.symmetric(horizontal: 12, vertical: 7),
                child: actionBusy
                    ? SizedBox(
                        width: 13,
                        height: 13,
                        child: CircularProgressIndicator(
                            strokeWidth: 2, color: color))
                    : Text(actionLabel,
                        style: TextStyle(
                            color: color,
                            fontWeight: FontWeight.w700,
                            fontSize: 11.5)),
              ),
            ),
          ),
        ],
      ]),
    );
  }
}

// NOTE: OfflineBanner (a second, separately-driven red "can't reach" bar)
// used to live here and was rendered inside the Home/Live/Alerts tab
// bodies, on top of ModeBanner which already sits above all tabs in
// HomeShell and covers the exact same "can't reach" state (plus the
// amber/teal states OfflineBanner didn't have). The two were driven by
// two different signals (Api.online vs Api.connectivityMode) that don't
// always transition together, so both could show red at once - the
// reported "two red bars" bug. Removed rather than kept as a second
// signal to reconcile, since ModeBanner is the more complete, more
// accurate widget and nothing is lost by relying on it alone.

/// Compact alert thumbnail used in the alert list rows and the home
/// dashboard's Recent Alerts section: shows the server-annotated snapshot
/// (bounding box already burned in server-side) with a small tier badge in
/// the corner. Falls back to a generic person icon if there's no snapshot
/// path or the image fails to load - never leaves a blank box.
class AlertThumbnail extends StatelessWidget {
  final String? snapshot;
  final int tier;
  final double size;
  const AlertThumbnail(
      {super.key, required this.snapshot, required this.tier, this.size = 56});

  @override
  Widget build(BuildContext context) {
    final c = tierColor(tier);
    Widget fallback() => Container(
          color: cTeal2.withValues(alpha: 0.15),
          alignment: Alignment.center,
          child: Icon(Icons.person, color: cTeal2, size: size * 0.42),
        );
    return SizedBox(
      width: size,
      height: size,
      child: Stack(
        children: [
          ClipRRect(
            borderRadius: BorderRadius.circular(10),
            child: (snapshot == null || snapshot!.isEmpty)
                ? fallback()
                : Image.network(
                    Api.mediaUrl(snapshot!),
                    width: size,
                    height: size,
                    fit: BoxFit.cover,
                    errorBuilder: (_, _, _) => fallback(),
                    loadingBuilder: (ctx, child, progress) {
                      if (progress == null) return child;
                      return fallback();
                    },
                  ),
          ),
          Positioned(
            right: 2,
            bottom: 2,
            child: Container(
              width: 14,
              height: 14,
              decoration: BoxDecoration(
                color: c,
                shape: BoxShape.circle,
                border: Border.all(color: cPanel, width: 1.5),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// Small ON/OFF chip used to show system state at a glance.
class StateChip extends StatelessWidget {
  final String label;
  final bool on;
  final String onText;
  final String offText;
  final Color? onColor;
  final IconData? icon;
  // Compact mode: smaller padding/font and no icon, meant for use inside
  // an Expanded slot in a fixed-width row (see live_tab.dart's status
  // row) where several chips need to share one line on any phone width
  // without wrapping or truncating.
  final bool compact;

  const StateChip({
    super.key,
    required this.label,
    required this.on,
    this.onText = 'ON',
    this.offText = 'OFF',
    this.onColor,
    this.icon,
    this.compact = false,
  });

  @override
  Widget build(BuildContext context) {
    final c = on ? (onColor ?? cTeal2) : cDim;
    if (compact) {
      // Was one line ("System DISARMED") with overflow:ellipsis, which cut
      // the state word off mid-word ("DISARM…") on a normal phone width
      // once 3-4 chips shared a row. Two lines - a small label on top, the
      // actual ON/OFF word on its own line below in FittedBox - means the
      // state word always renders in FULL (shrinking its font slightly if
      // it must) instead of ever being truncated, which is the one piece
      // of information this chip exists to show.
      return Container(
        alignment: Alignment.center,
        padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 8),
        decoration: BoxDecoration(
          color: on ? c.withValues(alpha: 0.14) : cPanel,
          borderRadius: BorderRadius.circular(10),
          border: Border.all(color: on ? c : cLine, width: on ? 1.4 : 1),
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(label,
                textAlign: TextAlign.center,
                overflow: TextOverflow.ellipsis,
                maxLines: 1,
                style: TextStyle(
                    color: c.withValues(alpha: 0.75),
                    fontSize: 9.5,
                    fontWeight: FontWeight.w700,
                    letterSpacing: 0.2)),
            const SizedBox(height: 2),
            FittedBox(
              fit: BoxFit.scaleDown,
              child: Text(on ? onText : offText,
                  maxLines: 1,
                  style: TextStyle(
                      color: c, fontSize: 12, fontWeight: FontWeight.w800)),
            ),
          ],
        ),
      );
    }
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
      decoration: BoxDecoration(
        color: cPanel,
        borderRadius: BorderRadius.circular(20),
        border: Border.all(color: on ? c : cLine),
      ),
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        if (icon != null) ...[
          Icon(icon, size: 13, color: c),
          const SizedBox(width: 5),
        ],
        Text('$label ${on ? onText : offText}',
            style: TextStyle(
                color: c, fontSize: 11.5, fontWeight: FontWeight.w600)),
      ]),
    );
  }
}
