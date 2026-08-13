import 'package:flutter/material.dart';
import 'package:mobile_scanner/mobile_scanner.dart';
import 'api.dart';
import 'theme.dart';
import 'widgets.dart';

class MeTab extends StatefulWidget {
  final VoidCallback onLogout;
  const MeTab({super.key, required this.onLogout});
  @override
  State<MeTab> createState() => _MeTabState();
}

class _MeTabState extends State<MeTab> {
  List<dynamic> _cams = [];
  List<Map<String, dynamic>> _devices = [];
  final Map<int, TextEditingController> _ctl = {};
  bool _connectingLocally = false;

  @override
  void initState() {
    super.initState();
    _load();
    _loadDevices();
  }

  Future<void> _load() async {
    final c = await Api.cameras();
    if (mounted) {
      setState(() {
        _cams = c;
        for (final cam in c) {
          _ctl[cam['cam'] as int] =
              TextEditingController(text: cam['name']?.toString() ?? '');
        }
      });
    }
  }

  Future<void> _loadDevices() async {
    final d = await Api.myDevices();
    if (mounted) setState(() => _devices = d);
  }

  @override
  void dispose() {
    for (final c in _ctl.values) {
      c.dispose();
    }
    super.dispose();
  }

  // Was a plain default SnackBar (flat gray bar, bottom of screen) - now
  // uses the same animated top-toast every other tab's action feedback
  // uses (live_tab.dart), so "Renamed to...", "Connected locally", etc.
  // all look and feel consistent instead of Me being the one screen with
  // Flutter's stock unstyled snackbar.
  void _toast(String m, {bool error = false}) =>
      showTopToast(context, m, error: error);

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(backgroundColor: cPanel, title: const Text('Me')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          panel(
            child: Row(children: [
              Container(
                width: 48,
                height: 48,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  color: cTeal2.withValues(alpha: 0.15),
                  border: Border.all(color: cTeal2.withValues(alpha: 0.4)),
                ),
                child: const Icon(Icons.person, color: cTeal2, size: 24),
              ),
              const SizedBox(width: 14),
              // Expanded so a long email/username can't push past the row -
              // it wraps to a second line (up to 2) and then ellipsises,
              // instead of overflowing (the "RenderFlex overflowed" error).
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    // Auto-shrink so a long email stays on ONE line: the text
                    // scales itself down to fit the available width instead of
                    // wrapping or overflowing.
                    SizedBox(
                      width: double.infinity,
                      child: FittedBox(
                        fit: BoxFit.scaleDown,
                        alignment: Alignment.centerLeft,
                        child: Text(Store.user,
                            maxLines: 1,
                            style: const TextStyle(
                                color: cText,
                                fontSize: 16,
                                fontWeight: FontWeight.bold)),
                      ),
                    ),
                    const Text('Homeowner',
                        style: TextStyle(color: cMuted, fontSize: 12)),
                  ],
                ),
              ),
            ]),
          ),
          const SizedBox(height: 20),
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              const Text('MY LAPTOPS',
                  style: TextStyle(
                      color: cDim, fontSize: 11, letterSpacing: 1.5)),
              GestureDetector(
                onTap: _loadDevices,
                child: const Icon(Icons.refresh, color: cMuted, size: 18),
              ),
            ],
          ),
          const SizedBox(height: 10),
          if (_devices.isEmpty)
            panel(
                child: const Text('No laptops connected yet',
                    style: TextStyle(color: cDim)))
          else
            ..._devices.map((d) {
              final isActive = d['device_id']?.toString() == Store.lastDeviceId;
              final online = d['online'] == true;
              final name = (d['hostname']?.toString().isNotEmpty ?? false)
                  ? d['hostname'].toString()
                  : 'CAPHY Laptop';
              return Padding(
                padding: const EdgeInsets.only(bottom: 10),
                child: panel(
                  child: Row(children: [
                    Icon(Icons.laptop,
                        color: isActive ? cTeal : cMuted, size: 22),
                    const SizedBox(width: 12),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(name,
                              style: TextStyle(
                                  color: cText,
                                  fontSize: 14,
                                  fontWeight: isActive
                                      ? FontWeight.bold
                                      : FontWeight.w500)),
                          const SizedBox(height: 2),
                          Row(children: [
                            Container(
                              width: 7,
                              height: 7,
                              decoration: BoxDecoration(
                                  color: online ? cTeal : cDim,
                                  shape: BoxShape.circle),
                            ),
                            const SizedBox(width: 6),
                            Text(online ? 'Online' : 'Offline',
                                style: TextStyle(
                                    color: online ? cTeal2 : cDim,
                                    fontSize: 11.5)),
                          ]),
                        ],
                      ),
                    ),
                    if (isActive)
                      const Padding(
                        padding: EdgeInsets.only(left: 8),
                        child: Text('Active',
                            style: TextStyle(color: cTeal, fontSize: 12)),
                      )
                    else
                      FilledButton(
                        style: FilledButton.styleFrom(
                            backgroundColor: cPanel2,
                            padding: const EdgeInsets.symmetric(
                                horizontal: 14, vertical: 8)),
                        onPressed: () {
                          Api.selectDevice(d);
                          setState(() {});
                          _load();
                          _toast('Switched to $name');
                        },
                        child: const Text('Use',
                            style: TextStyle(color: cTeal, fontSize: 12.5)),
                      ),
                  ]),
                ),
              );
            }),
          const SizedBox(height: 20),
          const Text('CAMERA NAMES',
              style: TextStyle(
                  color: cDim, fontSize: 11, letterSpacing: 1.5)),
          const SizedBox(height: 10),
          if (_cams.isEmpty)
            panel(
                child: const Text('No cameras found',
                    style: TextStyle(color: cDim))),
          ..._cams.map((c) {
            final id = c['cam'] as int;
            return Padding(
              padding: const EdgeInsets.only(bottom: 12),
              child: panel(
                child: Row(children: [
                  const Icon(Icons.videocam, color: cMuted, size: 20),
                  const SizedBox(width: 10),
                  Expanded(
                    child: TextField(
                      controller: _ctl[id],
                      style: const TextStyle(color: cText),
                      decoration: InputDecoration(
                        isDense: true,
                        hintText: 'Camera $id name',
                      ),
                    ),
                  ),
                  const SizedBox(width: 8),
                  FilledButton(
                    style: FilledButton.styleFrom(backgroundColor: cTeal),
                    onPressed: () async {
                      final name = _ctl[id]!.text.trim();
                      if (name.isEmpty) return;
                      final ok = await Api.renameCamera(id, name);
                      _toast(ok ? 'Renamed to "$name"' : 'Rename failed',
                          error: !ok);
                    },
                    child: const Text('Save',
                        style: TextStyle(color: Colors.black)),
                  ),
                ]),
              ),
            );
          }),
          const SizedBox(height: 12),
          const Text('SERVER',
              style: TextStyle(
                  color: cDim, fontSize: 11, letterSpacing: 1.5)),
          const SizedBox(height: 10),
          panel(
            child: Row(children: [
              const Icon(Icons.dns_outlined, color: cMuted, size: 20),
              const SizedBox(width: 10),
              Expanded(
                child: Text(
                    Store.hasServerAddress
                        ? Store.baseUrl
                        : 'No laptop connected yet',
                    style: TextStyle(
                        color: Store.hasServerAddress ? cText : cDim)),
              ),
            ]),
          ),
          const SizedBox(height: 12),
          SizedBox(
            width: double.infinity,
            child: FilledButton.icon(
              style: FilledButton.styleFrom(
                  backgroundColor: cTeal,
                  padding: const EdgeInsets.symmetric(vertical: 14)),
              onPressed: () async {
                final ok = await Navigator.of(context).push<bool>(
                    MaterialPageRoute(builder: (_) => const ConnectDeviceScreen()));
                if (ok == true && mounted) {
                  setState(() {});
                  _toast('Connected to laptop');
                }
              },
              icon: const Icon(Icons.qr_code_scanner, color: Colors.black),
              label: Text(
                  Store.hasServerAddress ? 'Reconnect Device' : 'Connect Device',
                  style: const TextStyle(
                      color: Colors.black, fontWeight: FontWeight.bold)),
            ),
          ),
          const SizedBox(height: 10),
          // "Connect Locally": for when you're already paired but the
          // laptop's local IP has changed (new network, DHCP lease
          // renewal) - re-pulls the laptop's latest LAN address from its
          // Firestore heartbeat and retries, without going through the
          // whole QR re-pairing flow. Same self-heal isLanReachable()
          // already runs automatically in the background; this is just an
          // explicit, immediate "try it now" for the user.
          SizedBox(
            width: double.infinity,
            child: OutlinedButton.icon(
              style: OutlinedButton.styleFrom(
                  foregroundColor: cTeal,
                  side: const BorderSide(color: cTeal),
                  padding: const EdgeInsets.symmetric(vertical: 12)),
              onPressed: _connectingLocally
                  ? null
                  : () async {
                      setState(() => _connectingLocally = true);
                      final found = await Api.reconnectToPairedDevice();
                      await Api.refreshConnectivity();
                      if (!mounted) return;
                      setState(() => _connectingLocally = false);
                      final ok = found && Store.hasServerAddress;
                      _toast(
                          ok
                              ? 'Connected locally'
                              : 'Could not find your laptop on this network',
                          error: !ok);
                    },
              icon: _connectingLocally
                  ? const SizedBox(
                      width: 16,
                      height: 16,
                      child: CircularProgressIndicator(strokeWidth: 2))
                  : const Icon(Icons.wifi_tethering),
              label: const Text('Connect Locally',
                  style: TextStyle(fontWeight: FontWeight.bold)),
            ),
          ),
          const SizedBox(height: 26),
          FilledButton.tonal(
            style: FilledButton.styleFrom(
                backgroundColor: cPanel2,
                padding: const EdgeInsets.symmetric(vertical: 14)),
            onPressed: widget.onLogout,
            child: const Text('Log out',
                style: TextStyle(color: cRed, fontWeight: FontWeight.bold)),
          ),
        ],
      ),
    );
  }
}

/// "Connect Device": scans the QR code shown on the laptop's web dashboard
/// (Settings > Connect Device) and pairs this phone to it. This is the
/// answer to "we don't have dedicated camera hardware" - the laptop's own
/// webcam is the camera, so the phone needs a guided way to find *which*
/// laptop is its household's system instead of typing an IP by hand.
class ConnectDeviceScreen extends StatefulWidget {
  const ConnectDeviceScreen({super.key});
  @override
  State<ConnectDeviceScreen> createState() => _ConnectDeviceScreenState();
}

class _ConnectDeviceScreenState extends State<ConnectDeviceScreen> {
  final MobileScannerController _ctl = MobileScannerController();
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _ctl.dispose();
    super.dispose();
  }

  Future<void> _onDetect(BarcodeCapture capture) async {
    if (_busy) return;
    final raw = capture.barcodes.isNotEmpty ? capture.barcodes.first.rawValue : null;
    if (raw == null) return;

    // New scan-to-connect QR (v2) carries a one-time sign-in token. Older
    // v1 pairing QRs (code/ip/port) are still accepted as a fallback.
    final linkPayload = Api.parseLinkQr(raw);
    final payload = linkPayload ?? Api.parsePairingQr(raw);
    if (payload == null) {
      setState(() => _error = 'That QR code isn\'t a CAPHY connect code.');
      return;
    }

    setState(() {
      _busy = true;
      _error = null;
    });
    await _ctl.stop();

    final err = linkPayload != null
        ? await Api.signInWithScannedToken(linkPayload)
        : await Api.confirmPairing(payload);
    if (!mounted) return;

    if (err != null) {
      setState(() {
        _busy = false;
        _error = err;
      });
      await _ctl.start();
      return;
    }

    Navigator.of(context).pop(true);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(backgroundColor: cPanel, title: const Text('Connect Device')),
      backgroundColor: cBg,
      body: Column(
        children: [
          Padding(
            padding: const EdgeInsets.all(16),
            child: Text(
              'Open CAPHY on your laptop → Settings → Connect Device, '
              'then point your camera at the QR code shown there.',
              style: const TextStyle(color: cMuted, fontSize: 13, height: 1.5),
            ),
          ),
          Expanded(
            child: Stack(
              fit: StackFit.expand,
              children: [
                MobileScanner(controller: _ctl, onDetect: _onDetect),
                Center(
                  child: Container(
                    width: 240,
                    height: 240,
                    decoration: BoxDecoration(
                      border: Border.all(
                          color: _error != null ? cRed : cTeal, width: 3),
                      borderRadius: BorderRadius.circular(16),
                    ),
                  ),
                ),
                if (_busy)
                  Container(
                    color: Colors.black54,
                    child: const Center(
                      child: CircularProgressIndicator(color: cTeal),
                    ),
                  ),
              ],
            ),
          ),
          if (_error != null)
            Padding(
              padding: const EdgeInsets.all(16),
              child: Text(_error!,
                  textAlign: TextAlign.center,
                  style: const TextStyle(color: cRed, fontSize: 13)),
            ),
        ],
      ),
    );
  }
}
