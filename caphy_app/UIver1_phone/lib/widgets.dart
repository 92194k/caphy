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
    if (_frame != null) {
      return Image.memory(_frame!, fit: widget.fit, gaplessPlayback: true);
    }
    if (_error) {
      return const Center(
          child: Text('connecting to camera...',
              style: TextStyle(color: cDim)));
    }
    return const Center(child: CircularProgressIndicator(color: cTeal));
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
void showTopToast(BuildContext context, String message, {bool error = false}) {
  final overlay = Overlay.of(context);
  final entry = OverlayEntry(
    builder: (ctx) => Positioned(
      top: MediaQuery.of(ctx).padding.top + 12,
      left: 16,
      right: 16,
      child: Material(
        color: Colors.transparent,
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
          decoration: BoxDecoration(
            color: error ? cRed : cPanel,
            borderRadius: BorderRadius.circular(12),
            border: Border.all(color: error ? cRed : cLine),
            boxShadow: [
              BoxShadow(
                  color: Colors.black.withValues(alpha: 0.4),
                  blurRadius: 12,
                  offset: const Offset(0, 4))
            ],
          ),
          child: Row(children: [
            Icon(error ? Icons.error_outline : Icons.check_circle_outline,
                color: error ? Colors.white : cTeal2, size: 18),
            const SizedBox(width: 10),
            Expanded(
              child: Text(message,
                  style: TextStyle(
                      color: error ? Colors.white : cText, fontSize: 13.5)),
            ),
          ]),
        ),
      ),
    ),
  );
  overlay.insert(entry);
  Future.delayed(const Duration(seconds: 2), entry.remove);
}

/// Auto-switching connectivity banner. Watches Api.connectivityMode and:
///   - shows nothing when online (cloud features available),
///   - shows an amber "Offline · Local Wi-Fi" bar when there's no internet
///     but the laptop is reachable on the LAN (live view + controls still
///     work, just not the from-anywhere features),
///   - shows a red "No connection" bar when nothing is reachable.
/// This is what makes the app auto-detect a dropped internet connection and
/// fall back to the local network without the user doing anything.
class ModeBanner extends StatelessWidget {
  const ModeBanner({super.key});

  @override
  Widget build(BuildContext context) {
    return ValueListenableBuilder<String>(
      valueListenable: Api.connectivityMode,
      builder: (context, mode, _) {
        if (mode == 'online') return const SizedBox.shrink();
        final lan = mode == 'lan';
        final color = lan ? cOrange : cRed;
        return Container(
          width: double.infinity,
          color: color.withValues(alpha: 0.14),
          padding: const EdgeInsets.fromLTRB(14, 9, 14, 9),
          child: Row(children: [
            Icon(lan ? Icons.wifi_tethering : Icons.cloud_off,
                color: color, size: 18),
            const SizedBox(width: 10),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(lan ? 'Offline mode · Local Wi‑Fi' : 'No connection',
                      style: TextStyle(
                          color: color,
                          fontWeight: FontWeight.w700,
                          fontSize: 12.5)),
                  Text(
                    lan
                        ? 'No internet detected — connected directly to your laptop over local Wi‑Fi. Live view and controls work; from‑anywhere features are paused.'
                        : 'Can\'t reach CAPHY. Make sure the laptop is on, and that you have internet or are on the same Wi‑Fi as the laptop.',
                    style: const TextStyle(color: cMuted, fontSize: 11),
                  ),
                ],
              ),
            ),
          ]),
        );
      },
    );
  }
}

/// Red bar shown whenever the app cannot reach the CAPHY system.
///
/// Without this, an unreachable server and a genuinely empty system look
/// identical - both render an empty list - which is impossible to diagnose
/// during a live demo.
class OfflineBanner extends StatelessWidget {
  const OfflineBanner({super.key});

  @override
  Widget build(BuildContext context) {
    return ValueListenableBuilder<bool>(
      valueListenable: Api.online,
      builder: (context, online, _) {
        if (online) return const SizedBox.shrink();
        return Container(
          width: double.infinity,
          color: cRed.withValues(alpha: 0.15),
          padding: const EdgeInsets.fromLTRB(14, 10, 14, 10),
          child: Row(children: [
            const Icon(Icons.cloud_off, color: cRed, size: 18),
            const SizedBox(width: 10),
            Expanded(
              child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Text('Can\'t reach CAPHY',
                        style: TextStyle(
                            color: cRed,
                            fontWeight: FontWeight.w700,
                            fontSize: 13)),
                    Text(
                      Store.hasServerAddress
                          ? 'Not reachable on this Wi-Fi and no recent '
                              'check-in from the laptop over the internet - '
                              'make sure it\'s powered on and connected.'
                          : 'No recent check-in from the laptop over the '
                              'internet - make sure it\'s powered on and '
                              'connected.',
                      style: const TextStyle(color: cMuted, fontSize: 11.5),
                    ),
                  ]),
            ),
          ]),
        );
      },
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

  const StateChip({
    super.key,
    required this.label,
    required this.on,
    this.onText = 'ON',
    this.offText = 'OFF',
    this.onColor,
    this.icon,
  });

  @override
  Widget build(BuildContext context) {
    final c = on ? (onColor ?? cTeal2) : cDim;
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
