import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'api.dart';
import 'theme.dart';
import 'widgets.dart';
import 'webrtc_view.dart';
import 'ask_caphy_screen.dart';

class LiveTab extends StatefulWidget {
  const LiveTab({super.key});
  @override
  State<LiveTab> createState() => _LiveTabState();
}

class _LiveTabState extends State<LiveTab> {
  List<dynamic> _cams = [];
  int _sel = 0;
  Map<String, dynamic> _stat = {};
  Map<String, dynamic> _state = {};   // armed / camera_on / emergency / ...
  bool _recording = false;
  bool _recordPending = false;
  bool _nv = false;
  Timer? _statTimer;

  // Whether the phone can reach the laptop directly on this WiFi right
  // now. true -> use the existing MJPEG stream (simple, instant). false
  // -> fall back to a real WebRTC call (webrtc_view.dart) so "live view"
  // still means LIVE video even when away from home, not just snapshots.
  //
  // IMPORTANT: this used to be decided ONCE at tab-open time via its own
  // separate call to Api.isLanReachable(), completely disconnected from
  // the connectivity banner (Api.connectivityMode), which is refreshed
  // continuously and already does the real work of LAN self-healing (UDP
  // broadcast discovery, Firestore last_lan_ip refresh, etc). That
  // one-shot design meant: if the very first check happened to fail (a
  // single UDP broadcast round-trip has no reliability guarantee), this
  // tab would permanently lock onto WebRTC for the rest of the session -
  // even after the banner correctly self-healed back to amber/local a few
  // seconds later - because nothing here ever re-checked or reacted to
  // that. That's exactly the reported "keeps on connecting forever" bug:
  // stuck retrying a cloud signaling path that structurally cannot work
  // with zero internet, while a working LAN path sat unused right next to
  // it. Now this listens live to Api.connectivityMode instead, so it
  // always reflects the SAME up-to-date reachability picture the banner
  // shows, and recovers automatically the moment the banner does.
  bool get _lanChecked => _lanCheckedOnce;
  bool _lanCheckedOnce = false;
  bool get _lanReachable {
    final mode = Api.connectivityMode.value;
    // 'lan' = local-only (no internet) but laptop reachable on this
    // network - the amber case. 'online' also counts as LAN-reachable
    // whenever the last isLanReachable() probe succeeded, since 'online'
    // just means "internet AND laptop reachable" and a same-network setup
    // satisfies both at once (see refreshConnectivity()'s `lan` branch,
    // which lands on 'online' precisely when LAN + internet both work).
    if (mode == 'lan') return true;
    if (mode == 'offline') return false;
    return _lastLanProbe;         // 'online': fall back to the live probe result
  }
  bool _lastLanProbe = true;
  Timer? _lanProbeTimer;

  bool get _armed => _state['armed'] == true;
  bool get _cameraOn => _state['camera_on'] != false;
  bool get _emergency => _state['emergency'] == true;
  bool get _sirenOn => _state['siren'] == true;

  @override
  void initState() {
    super.initState();
    _loadCams();
    Api.connectivityMode.addListener(_onConnectivityChanged);
    _refreshLanProbe();
    // No more per-frame image polling (that was the lag). The MJPEG stream
    // widget renders frames continuously. We only poll lightweight status.
    _statTimer =
        Timer.periodic(const Duration(seconds: 2), (_) => _loadStat());
    // Independent short-interval LAN re-probe specifically for the
    // 'online' case (banner doesn't distinguish "online same-LAN" from
    // "online but laptop only reachable via cloud" - both just say
    // 'online' since either is a fully-working state for controls). Live
    // view needs to know which one it actually is to pick MJPEG vs
    // WebRTC, so it keeps its own lightweight recheck here rather than
    // reintroducing a one-shot decision.
    _lanProbeTimer =
        Timer.periodic(const Duration(seconds: 5), (_) => _refreshLanProbe());
  }

  void _onConnectivityChanged() {
    if (mounted) setState(() {});
  }

  bool _lanProbeInFlight = false;

  Future<void> _refreshLanProbe() async {
    // Same overlapping-calls guard as Api.refreshConnectivity() - this runs
    // on a Timer.periodic(seconds: 5), but Api.isLanReachable() alone can
    // take up to ~18s worst-case while fully offline (chained LAN probe +
    // Firestore fetch + UDP broadcast discovery timeouts). Without this
    // guard, a slow tick doesn't get cancelled by the next one - it keeps
    // running underneath it, so being offline for a while stacks up more
    // and more concurrent probes with no end in sight, which is what
    // presented as "the app hangs" when testing fully offline.
    if (_lanProbeInFlight) return;
    _lanProbeInFlight = true;
    try {
      final reachable = await Api.isLanReachable();
      if (mounted) {
        setState(() {
          _lastLanProbe = reachable;
          _lanCheckedOnce = true;
        });
      }
    } finally {
      _lanProbeInFlight = false;
    }
  }

  @override
  void dispose() {
    _statTimer?.cancel();
    _lanProbeTimer?.cancel();
    Api.connectivityMode.removeListener(_onConnectivityChanged);
    super.dispose();
  }

  Future<void> _loadCams() async {
    final c = await Api.cameras();
    if (!mounted) return;
    setState(() {
      _cams = c;
      // If the currently-selected camera isn't in the list at all, or IS
      // in the list but reports offline, and there's a different camera
      // that IS online, switch to that one automatically instead of
      // staying pointed at (and silently trying to stream from) a camera
      // that isn't actually connected right now.
      final selEntry = c.cast<Map>().where((m) => m['cam'] == _sel);
      final selOnline = selEntry.isNotEmpty && selEntry.first['online'] != false;
      if (!selOnline) {
        final firstOnline = c.cast<Map>().where((m) => m['online'] != false);
        if (firstOnline.isNotEmpty) {
          _sel = firstOnline.first['cam'] as int;
        }
      }
    });
  }

  Future<void> _loadStat() async {
    final s = await Api.stats();
    for (final x in s) {
      if (x['cam'] == _sel && mounted) setState(() => _stat = x);
    }
    final st = await Api.state();
    if (st != null && mounted) {
      setState(() {
        // While a toggle is in flight (or was JUST confirmed - see the
        // toggle handlers' own setState calls), don't let this routine
        // poll overwrite that field with a value that might still be
        // stale. This is what caused "status wrong after toggling"
        // cross-network: the phone polls Api.state() every 2s regardless
        // of network mode, but off-LAN that read comes from the laptop's
        // Firestore heartbeat snapshot, which used to only refresh every
        // ~20s - so a toggle made in between kept getting visually
        // reverted by the next poll reading the not-yet-updated snapshot.
        // The laptop now pushes an updated snapshot immediately after any
        // change (_push_state_async in web/server.py), which fixes this
        // at the source, but keeping this guard too means a toggle's own
        // optimistic/confirmed UI state can never be clobbered by a poll
        // that happens to land in the small window before that push
        // reaches Firestore.
        final newState = Map<String, dynamic>.from(_state);
        if (!_armPending) newState['armed'] = st['armed'];
        if (!_cameraPending) newState['camera_on'] = st['camera_on'];
        if (!_emergencyPending) newState['emergency'] = st['emergency'];
        if (st.containsKey('siren')) newState['siren'] = st['siren'];
        for (final key in st.keys) {
          if (key != 'armed' && key != 'camera_on' && key != 'emergency') {
            newState[key] = st[key];
          }
        }
        _state = newState;
        _nv = st['night_vision'] == true;
      });
    }
  }

  bool _armPending = false;
  bool _sirenPending = false;
  int _sirenGen = 0;

  // Bumped every time a new arm/disarm tap starts. A slow/stale request's
  // result is checked against the generation it was FIRED under before
  // touching state - if a newer tap has since started (or the button was
  // manually unlocked), the old one's eventual result is just discarded
  // instead of clobbering whatever's now on screen. This is what makes
  // "tap again to cancel/retry immediately" safe: the old call keeps
  // running in the background (Dart Futures can't actually be cancelled),
  // but it can no longer touch the UI once it's stale.
  int _armGen = 0;

  Future<void> _toggleArm() async {
    // Was: if (_armPending) return - a second tap while one was still in
    // flight did NOTHING, so a slow network call left the button looking
    // and feeling completely dead/frozen for however long the timeout
    // took. Now a tap ALWAYS responds immediately: if nothing is pending
    // it starts a new request as before; if something IS pending, this tap
    // just unlocks the button right away (clears the spinner, lets you
    // tap again or navigate freely) rather than making you wait out
    // whatever's still in flight.
    if (_armPending) {
      setState(() => _armPending = false);
      _armGen++; // any in-flight call from before is now stale, see above
      _toast('Cancelled - tap again to retry');
      return;
    }
    final myGen = ++_armGen;
    final goingArmed = !_armed;
    setState(() => _armPending = true);
    // Optimistic feedback the instant the tap lands, before the network
    // round trip even starts - the button itself flips to a "…ing" label
    // below (see _primaryBtn call site) while _armPending is true, so
    // there's never a dead-looking tap even on a slow cross-network
    // command.
    _toast(goingArmed ? 'Arming…' : 'Disarming…');
    try {
      // Hard, independent ceiling on the WHOLE call - not just whatever
      // timeout sendRemoteCommand tries to apply internally. If anything
      // downstream (a Firestore SDK stream that stalls without ever
      // calling back, a plugin channel call that never returns, etc) hangs
      // past this, the button unlocks anyway instead of staying stuck on
      // "Arming…" forever - that dead-button state is worse than an
      // honest failure message the user can retry from.
      final r = await Api.setArmed(goingArmed)
          .timeout(const Duration(seconds: 18), onTimeout: () => null);
      if (myGen != _armGen) return; // superseded by a cancel/newer tap - drop silently
      if (r == null) {
        _toast('Could not reach CAPHY - try again', error: true);
        return;
      }
      _toast(r ? 'Armed ✓' : 'Disarmed ✓');
      await _loadStat();
    } catch (e) {
      if (myGen != _armGen) return;
      _toast('Could not reach CAPHY - try again', error: true);
    } finally {
      if (mounted && myGen == _armGen) setState(() => _armPending = false);
    }
  }

  bool _cameraPending = false;
  int _cameraGen = 0;

  Future<void> _toggleCamera() async {
    // Same cancel-on-retap pattern as _toggleArm above - a tap while
    // pending unlocks immediately instead of being swallowed/ignored for
    // the rest of the timeout.
    if (_cameraPending) {
      setState(() => _cameraPending = false);
      _cameraGen++;
      _toast('Cancelled - tap again to retry');
      return;
    }
    final myGen = ++_cameraGen;
    final turningOff = _cameraOn;
    setState(() => _cameraPending = true);
    _toast(turningOff ? 'Turning camera off…' : 'Turning camera on…');
    try {
      final r = await Api.setCamera(!_cameraOn)
          .timeout(const Duration(seconds: 18), onTimeout: () => null);
      if (myGen != _cameraGen) return;
      if (r == null) {
        _toast('Could not reach CAPHY - try again', error: true);
        return;
      }
      _toast(turningOff ? 'Camera off - detection paused ✓' : 'Camera on ✓');
      await _loadStat();
    } catch (e) {
      if (myGen != _cameraGen) return;
      _toast('Could not reach CAPHY - try again', error: true);
    } finally {
      if (mounted && myGen == _cameraGen) setState(() => _cameraPending = false);
    }
  }

  bool _emergencyPending = false;
  int _emergencyGen = 0;

  Future<void> _toggleEmergency() async {
    // Same cancel-on-retap pattern as arm/camera above - but only once the
    // network call is actually pending, not while the confirm dialog is up
    // (that dialog already has its own Cancel button and isn't a frozen-
    // feeling wait, so retapping the button behind it shouldn't dismiss it
    // unexpectedly).
    if (_emergencyPending) {
      setState(() => _emergencyPending = false);
      _emergencyGen++;
      _toast('Cancelled - tap again to retry');
      return;
    }
    if (!_emergency) {
      final ok = await showDialog<bool>(
        context: context,
        builder: (_) => AlertDialog(
          backgroundColor: cPanel,
          title: const Text('Activate emergency mode?',
              style: TextStyle(color: cText)),
          content: const Text(
              'This forces the camera on, arms the system, sounds the siren '
              'and sends a push alert.',
              style: TextStyle(color: cMuted)),
          actions: [
            TextButton(
                onPressed: () => Navigator.pop(context, false),
                child: const Text('Cancel', style: TextStyle(color: cMuted))),
            TextButton(
                onPressed: () => Navigator.pop(context, true),
                child: const Text('Activate', style: TextStyle(color: cRed))),
          ],
        ),
      );
      if (ok != true) return;
    }
    final myGen = ++_emergencyGen;
    setState(() => _emergencyPending = true);
    _toast(_emergency ? 'Cancelling emergency mode…' : 'Activating emergency mode…');
    try {
      final r = await Api.setEmergency(!_emergency)
          .timeout(const Duration(seconds: 18), onTimeout: () => null);
      if (myGen != _emergencyGen) return;
      if (r == null) {
        _toast('Could not reach CAPHY - try again', error: true);
        return;
      }
      _toast(r ? 'EMERGENCY MODE ACTIVE ✓' : 'Emergency mode cancelled ✓');
      await _loadStat();
    } catch (e) {
      if (myGen != _emergencyGen) return;
      _toast('Could not reach CAPHY - try again', error: true);
    } finally {
      if (mounted && myGen == _emergencyGen) setState(() => _emergencyPending = false);
    }
  }

  void _toast(String m, {bool error = false}) =>
      showTopToast(context, m, error: error);

  @override
  Widget build(BuildContext context) {
    final tier = (_stat['tier'] ?? 0) as int;
    return Scaffold(
      appBar: AppBar(backgroundColor: cPanel, title: const Text('Live')),
      body: Column(children: [
        Expanded(
          child: ListView(
        padding: const EdgeInsets.all(14),
        children: [
          // ---- system state at a glance ----
          // Emergency mode OVERRIDES the normal 3-chip row entirely rather
          // than being squeezed in as a 4th chip - when it's active nothing
          // else about individual system/camera/night-vision state matters
          // as much as "emergency is on", so it gets one full-width,
          // impossible-to-miss banner instead of competing for space.
          //
          // The old 3-4-chip single row (each squeezed into Expanded,
          // compact mode) truncated "DISARMED" mid-word on a normal phone
          // width - now only System/Camera/Night vision share a row (still
          // Expanded so it stays tidy on any width), and System alone gets
          // enough breathing room via a slightly smaller compact font
          // ceiling + FittedBox safeguard in StateChip so the full word
          // always fits instead of clipping.
          Padding(
            padding: const EdgeInsets.only(bottom: 12),
            child: _emergency
                ? _emergencyOverrideBanner()
                : Row(
                    children: [
                      Expanded(
                        flex: 4,
                        child: StateChip(
                            label: 'System',
                            on: _armed,
                            onText: 'ARMED',
                            offText: 'DISARMED',
                            icon: Icons.shield,
                            compact: true),
                      ),
                      const SizedBox(width: 6),
                      Expanded(
                        flex: 3,
                        child: StateChip(
                            label: 'Camera',
                            on: _cameraOn,
                            onColor: cTeal2,
                            icon: Icons.videocam,
                            compact: true),
                      ),
                      const SizedBox(width: 6),
                      Expanded(
                        flex: 3,
                        child: StateChip(
                            label: 'Night vision',
                            on: _nv,
                            icon: Icons.nightlight_round,
                            compact: true),
                      ),
                    ],
                  ),
          ),
          if (!_cameraOn)
            Container(
              margin: const EdgeInsets.only(bottom: 12),
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 9),
              decoration: BoxDecoration(
                color: cOrange.withValues(alpha: 0.12),
                borderRadius: BorderRadius.circular(10),
                border: Border.all(color: cOrange),
              ),
              child: Row(children: const [
                Icon(Icons.videocam_off, color: cOrange, size: 16),
                SizedBox(width: 8),
                Expanded(
                  child: Text(
                    'Camera OFF • Detection paused',
                    overflow: TextOverflow.ellipsis,
                    maxLines: 1,
                    style: TextStyle(color: cOrange, fontSize: 12.5, fontWeight: FontWeight.w600),
                  ),
                ),
              ]),
            ),
          if (_cams.length > 1)
            Padding(
              padding: const EdgeInsets.only(bottom: 12),
              child: Row(
                children: _cams.map<Widget>((c) {
                  final id = c['cam'] as int;
                  final on = id == _sel;
                  final name = (c['name'] ?? 'Cam $id').toString();
                  // /api/cameras already reports each camera's real online
                  // state (see web/server.py's api_cameras -> w.get_stats()
                  // ['online']) but this UI previously never read it - every
                  // tile looked equally selectable regardless of whether
                  // that camera's worker actually had a live source behind
                  // it. Tapping an offline camera (e.g. a phone/IP camera
                  // that's disconnected) still switched _sel to it and
                  // silently tried to stream from it, which is why an
                  // offline camera's tile could end up showing whatever the
                  // laptop's stream endpoint fell back to for that slot -
                  // reported as "it opens the laptop cam instead". Now an
                  // offline camera is visibly dimmed/marked and tapping it
                  // warns instead of silently switching.
                  final camOnline = c['online'] != false;
                  return Expanded(
                    child: Padding(
                      padding: const EdgeInsets.only(right: 8),
                      child: Material(
                        color: Colors.transparent,
                        child: InkWell(
                          borderRadius: BorderRadius.circular(12),
                          onTap: on
                              ? null
                              : () {
                                  if (!camOnline) {
                                    _toast('$name is offline - not connected right now',
                                        error: true);
                                    return;
                                  }
                                  setState(() {
                                    _sel = id;
                                    _stat = {};
                                  });
                                },
                          child: Opacity(
                            opacity: camOnline ? 1.0 : 0.55,
                            child: AnimatedContainer(
                              duration: const Duration(milliseconds: 180),
                              padding: const EdgeInsets.symmetric(vertical: 11),
                              decoration: BoxDecoration(
                                gradient: on ? cGrad : null,
                                color: on ? null : cPanel2,
                                borderRadius: BorderRadius.circular(12),
                                border: Border.all(
                                    color: on
                                        ? Colors.transparent
                                        : (camOnline ? cLine : cRed)),
                              ),
                              child: Row(
                                mainAxisAlignment: MainAxisAlignment.center,
                                children: [
                                  // Was laptop_mac vs. smartphone depending on
                                  // which device the camera lives on - both
                                  // are just "a camera" to the user here, so
                                  // one consistent camera icon for every
                                  // source instead of two different pictograms.
                                  Icon(
                                      camOnline
                                          ? Icons.videocam
                                          : Icons.videocam_off,
                                      size: 16,
                                      color: on ? Colors.black : cMuted),
                                  const SizedBox(width: 6),
                                  Flexible(
                                    child: Text(
                                        camOnline ? name : '$name (offline)',
                                        overflow: TextOverflow.ellipsis,
                                        style: TextStyle(
                                            color: on ? Colors.black : cMuted,
                                            fontWeight: on
                                                ? FontWeight.w700
                                                : FontWeight.w600,
                                            fontSize: 13)),
                                  ),
                                ],
                              ),
                            ),
                          ),
                        ),
                      ),
                    ),
                  );
                }).toList(),
              ),
            ),
          // Live view, CCTV-style: every known camera shown at once in a
          // grid (1 camera = full width, 2+ = a 2-up grid) instead of
          // forcing a pick-one-to-view selector first. Tapping any tile
          // opens that camera fullscreen ("like a real CCTV"). The
          // separate camera-select row above still controls which camera
          // the Arm/Siren/Snapshot/Record/etc buttons act on - viewing and
          // controlling are deliberately independent now, since you may
          // want to watch both cameras while only one is "selected" for
          // control actions.
          _cams.length <= 1
              ? _buildVideoStack(_sel, showDetection: true, big: true)
              : GridView.count(
                  crossAxisCount: 2,
                  shrinkWrap: true,
                  physics: const NeverScrollableScrollPhysics(),
                  mainAxisSpacing: 10,
                  crossAxisSpacing: 10,
                  childAspectRatio: 16 / 12,
                  children: _cams
                      .map<Widget>((c) => _buildVideoStack(
                          c['cam'] as int,
                          showDetection: c['cam'] == _sel,
                          big: false))
                      .toList(),
                ),
          const SizedBox(height: 16),
          // ---- controls (matches the web console) ----
          Row(children: [
            Expanded(
              child: _primaryBtn(
                  _armed ? Icons.shield : Icons.shield_outlined,
                  _armPending
                      ? (_armed ? 'Disarming…' : 'Arming…')
                      : (_armed ? 'Disarm' : 'Arm'),
                  _toggleArm,
                  loading: _armPending),
            ),
            const SizedBox(width: 10),
            Expanded(
              // Was always "Trigger Siren" no matter what - once it turned
              // ON, tapping again looked identical and the button never
              // told you HOW to stop it. /api/siren is actually a toggle
              // (see Api.siren() and web/server.py's api_siren), so the
              // label/icon now track the real current state and flip to
              // "Stop Siren" the moment it's sounding, driven by _sirenOn
              // which _loadStat() keeps in sync from the live poll (so it
              // also updates correctly if the siren was toggled from the
              // web dashboard or another phone, not just from here).
              child: _dangerBtn(
                  _sirenOn ? Icons.notifications_off : Icons.notifications_active,
                  _sirenPending
                      ? (_sirenOn ? 'Stopping…' : 'Activating…')
                      : (_sirenOn ? 'Stop Siren' : 'Trigger Siren'), () async {
                // Same cancel-on-retap pattern as arm/camera/emergency -
                // tapping while pending unlocks immediately instead of the
                // button refusing input for up to 18s.
                if (_sirenPending) {
                  setState(() => _sirenPending = false);
                  _sirenGen++;
                  _toast('Cancelled - tap again to retry');
                  return;
                }
                final myGen = ++_sirenGen;
                setState(() => _sirenPending = true);
                try {
                  // Same hard ceiling as _toggleArm - never leave the
                  // button stuck on "Activating…" no matter what stalls
                  // underneath.
                  final on = await Api.siren()
                      .timeout(const Duration(seconds: 18), onTimeout: () => null);
                  if (myGen != _sirenGen) return;
                  if (on == null) {
                    _toast('Could not reach CAPHY - try again', error: true);
                    return;
                  }
                  _toast(on ? 'Siren ON ✓' : 'Siren stopped ✓');
                  await _loadStat();
                } catch (e) {
                  if (myGen != _sirenGen) return;
                  _toast('Could not reach CAPHY - try again', error: true);
                } finally {
                  if (mounted && myGen == _sirenGen) setState(() => _sirenPending = false);
                }
              }, loading: _sirenPending, active: _sirenOn),
            ),
          ]),
          const SizedBox(height: 10),
          // ---- utility icons: clean 3+3 grid, no scrolling ----
          // The Speak/mic button was removed along with the whole voice
          // assistant feature (being redesigned from scratch) - down to 6
          // controls now, laid out as two even rows of 3.
          Row(children: [
            Expanded(
              child: _iconBtn(
                  _cameraOn ? Icons.videocam : Icons.videocam_off,
                  _cameraOn, _toggleCamera, tip: 'Camera', loading: _cameraPending),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: _iconBtn(Icons.camera_alt, false, () async {
                // Same-WiFi: fetch the frame directly and save it to the
                // phone's own gallery (fast, no laptop-side file written).
                // Cross-network: Api.frameUrl() points at the laptop's LAN
                // address, which is unreachable on mobile data - this
                // button previously had NO fallback at all for that case,
                // so it silently did nothing. Now falls back to
                // Api.snapshot() (which already has its own cross-network
                // path via the Firestore command queue), which saves the
                // snapshot on the LAPTOP instead - not identical behavior,
                // but "saved somewhere and the user is told where" beats
                // "silently does nothing".
                if (_lanReachable) {
                  final r = await saveImageToGalleryEx(
                      '${Api.frameUrl(_sel)}&t=${DateTime.now().millisecondsSinceEpoch}',
                      prefix: _mediaLabel());
                  _toast(_saveMessage(r, 'Snapshot'), error: r != SaveOutcome.ok);
                  return;
                }
                final ok = await Api.snapshot(_sel)
                    .timeout(const Duration(seconds: 18), onTimeout: () => false);
                _toast(ok ? 'Snapshot saved on your CAPHY laptop'
                          : 'Could not reach CAPHY - try again',
                    error: !ok);
              }, tip: 'Snapshot'),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: _iconBtn(Icons.fiber_manual_record, _recording, () async {
                if (_recordPending) return;
                setState(() => _recordPending = true);
                try {
                  // Hard ceiling independent of whatever Api.record() does
                  // internally - same pattern as Arm/Siren/Camera/
                  // Emergency, added here because Record previously had NO
                  // timeout/pending-state handling at all and could look
                  // stuck indefinitely on a bad connection.
                  final res = await Api.record(_sel).timeout(
                      const Duration(seconds: 18),
                      onTimeout: () => {'recording': _recording});
                  final on = res['recording'] == true;
                  setState(() => _recording = on);
                  if (on) {
                    _toast('Recording started');
                  } else {
                    _toast('Saving recording...');
                    final url = res['video_url'];
                    final name = res['video'];
                    if (url != null) {
                      // must include the auth token, or the server's /video
                      // route 401s and the download always "fails" - Api.mediaUrl
                      // appends it the same way frameUrl/streamUrl already do.
                      final r = await saveVideoUrlToGalleryEx(
                          Api.mediaUrl(url),
                          prefix: _mediaLabel());
                      if (r == SaveOutcome.ok) {
                        // now on the phone - remove the PC's temporary copy so
                        // a phone-triggered recording lives only on the phone.
                        if (name != null) Api.deleteMedia(name);
                      }
                      _toast(_saveMessage(r, 'Recording'), error: r != SaveOutcome.ok);
                    } else {
                      _toast('Recording saved on PC');
                    }
                  }
                } catch (e) {
                  _toast('Could not reach CAPHY - try again', error: true);
                } finally {
                  if (mounted) setState(() => _recordPending = false);
                }
              }, tip: 'Record', loading: _recordPending),
            ),
          ]),
          const SizedBox(height: 8),
          Row(children: [
            Expanded(
              child: _iconBtn(Icons.nightlight_round, _nv, () async {
                final on = await Api.nightVision(_sel);
                setState(() => _nv = on);
                _toast('Night vision ${on ? "on" : "off"}');
              }, tip: 'Night vision'),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: _iconBtn(Icons.fullscreen, false, _openFullscreen,
                  tip: 'Fullscreen'),
            ),
          ]),
          const SizedBox(height: 10),
          // Emergency is a much bigger deal than the utility icons above
          // (arms the system, forces the camera on, sounds the siren AND
          // pushes an alert) - it was previously just another small square
          // in a 3-icon row, easy to miss and easy to mistake for a minor
          // toggle like night vision. Now it's its own full-width, clearly
          // labeled, distinctly styled control so tapping it is a
          // deliberate decision, not an accidental icon tap.
          SizedBox(
            width: double.infinity,
            child: Material(
              color: Colors.transparent,
              borderRadius: BorderRadius.circular(12),
              child: InkWell(
                borderRadius: BorderRadius.circular(12),
                // Was `_emergencyPending ? null : ...` - that blocked the
                // tap from ever reaching _toggleEmergency's own
                // cancel-on-retap handling while pending. Always call it;
                // the function itself now decides cancel vs. start.
                onTap: _toggleEmergency,
                child: Container(
                  padding: const EdgeInsets.symmetric(vertical: 13),
                  decoration: BoxDecoration(
                    color: _emergency
                        ? cRed.withValues(alpha: 0.85)
                        : cRed.withValues(alpha: 0.12),
                    borderRadius: BorderRadius.circular(12),
                    border: Border.all(
                        color: cRed.withValues(alpha: _emergency ? 1 : 0.55),
                        width: 1.3),
                    boxShadow: _emergency
                        ? [
                            BoxShadow(
                                color: cRed.withValues(alpha: 0.45),
                                blurRadius: 16,
                                offset: const Offset(0, 5)),
                          ]
                        : [],
                  ),
                  child: Row(
                    mainAxisAlignment: MainAxisAlignment.center,
                    children: [
                      _emergencyPending
                          ? SizedBox(
                              width: 17,
                              height: 17,
                              child: CircularProgressIndicator(
                                  strokeWidth: 2,
                                  color: _emergency ? Colors.white : cRed))
                          : Icon(Icons.warning_amber_rounded,
                              color: _emergency ? Colors.white : cRed,
                              size: 19),
                      const SizedBox(width: 9),
                      Text(
                          _emergencyPending
                              ? (_emergency
                                  ? 'Cancelling…'
                                  : 'Activating…')
                              : (_emergency
                                  ? 'Cancel Emergency Mode'
                                  : 'Activate Emergency Mode'),
                          style: TextStyle(
                              color: _emergency ? Colors.white : cRed,
                              fontWeight: FontWeight.w800,
                              fontSize: 13.5)),
                    ],
                  ),
                ),
              ),
            ),
          ),
          const SizedBox(height: 8),
          // Text-only voice-assistant v2 entry point (step 1 of the rebuild -
          // no mic/wake-word yet, see ask_caphy_screen.dart for why).
          SizedBox(
            width: double.infinity,
            child: _iconBtn(Icons.chat_bubble_outline, false, () {
              Navigator.of(context).push(MaterialPageRoute(
                builder: (_) => const AskCaphyScreen(),
              ));
            }, tip: 'Ask CAPHY'),
          ),
        ],
          ),
        ),
      ]),
    );
  }

  /// Filename label for a manual snapshot/recording so it's obvious which
  /// camera it came from just by the file name in the gallery, e.g.
  /// "CAPHY_CamPhone_1737384930000.jpg" - the save helpers append their own
  /// timestamp after this label. The camera name is also burned into the
  /// image/video itself by the system, so the label carries through either
  /// way even if the file gets renamed.
  String _mediaLabel() {
    String name = 'Cam$_sel';
    for (final c in _cams) {
      if (c['cam'] == _sel) {
        name = (c['name'] ?? name).toString();
        break;
      }
    }
    final safe = name.trim().replaceAll(RegExp(r'[^A-Za-z0-9]+'), '');
    return 'CAPHY_${safe.isEmpty ? 'Cam$_sel' : safe}';
  }

  /// Turn a gallery-save result into a message that actually tells the user
  /// what to do next, instead of a generic "failed".
  String _saveMessage(SaveOutcome r, String kind) {
    switch (r) {
      case SaveOutcome.ok:
        return '$kind saved to gallery (CAPHY album)';
      case SaveOutcome.permissionDenied:
        return '$kind not saved - allow Photos & videos access for CAPHY '
            'in your phone Settings > Apps > CAPHY > Permissions';
      case SaveOutcome.downloadFailed:
        return '$kind not saved - could not reach the system to download it';
      case SaveOutcome.galleryWriteFailed:
        return '$kind not saved - the phone gallery rejected the file';
    }
  }

  // Full-width takeover shown INSTEAD OF the normal System/Camera/Night
  // vision row whenever emergency mode is active - a pulsing red banner
  // rather than a 4th small chip squeezed into the row, since "emergency
  // is active" should visually dominate the screen, not compete with
  // routine status chips for attention.
  Widget _emergencyOverrideBanner() {
    return _EmergencyPulse(
      child: Container(
          width: double.infinity,
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
          decoration: BoxDecoration(
            gradient: LinearGradient(
              colors: [cRed.withValues(alpha: 0.22), cRed.withValues(alpha: 0.10)],
              begin: Alignment.centerLeft,
              end: Alignment.centerRight,
            ),
            borderRadius: BorderRadius.circular(14),
            border: Border.all(color: cRed.withValues(alpha: 0.7), width: 1.4),
            boxShadow: [
              BoxShadow(
                  color: cRed.withValues(alpha: 0.3),
                  blurRadius: 20,
                  spreadRadius: -2),
            ],
          ),
          child: Row(children: [
            Container(
              width: 38,
              height: 38,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                color: cRed.withValues(alpha: 0.22),
              ),
              child: const Icon(Icons.warning_amber_rounded,
                  color: cRed, size: 22),
            ),
            const SizedBox(width: 12),
            const Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text('EMERGENCY MODE ACTIVE',
                      style: TextStyle(
                          color: cRed,
                          fontWeight: FontWeight.w800,
                          fontSize: 13.5,
                          letterSpacing: 0.3)),
                  SizedBox(height: 2),
                  Text('System armed · camera on · siren sounding',
                      style: TextStyle(color: cMuted, fontSize: 11.5)),
                ],
              ),
            ),
          ]),
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

  // Filled-teal primary action (Arm / Disarm).
  Widget _primaryBtn(IconData icon, String label, VoidCallback onTap,
      {bool loading = false}) {
    // Gradient fill + soft glow instead of a flat teal FilledButton - same
    // cGrad used for selected chips/the send button elsewhere, so the
    // app's "main action" buttons all read as one consistent, more
    // deliberate style instead of Material's stock flat look.
    return Material(
      color: Colors.transparent,
      borderRadius: BorderRadius.circular(12),
      child: InkWell(
        borderRadius: BorderRadius.circular(12),
        // Was `loading ? null : onTap` - that made the button completely
        // dead/untappable for the whole time a command was in flight,
        // which is the actual freeze: on a slow or stuck connection the
        // button could refuse taps for the full 18s ceiling with no way
        // out. Now it ALWAYS accepts a tap; the handler itself (see
        // _toggleArm/_toggleEmergency/siren's onTap in build()) treats a
        // tap while pending as "cancel this and let me try again" instead
        // of the widget silently swallowing it.
        onTap: onTap,
        child: Container(
          padding: const EdgeInsets.symmetric(vertical: 14),
          decoration: BoxDecoration(
            gradient: loading ? null : cGrad,
            color: loading ? cPanel2 : null,
            borderRadius: BorderRadius.circular(12),
            boxShadow: loading
                ? []
                : [
                    BoxShadow(
                        color: cTeal.withValues(alpha: 0.35),
                        blurRadius: 18,
                        offset: const Offset(0, 6)),
                  ],
          ),
          child: Row(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              loading
                  ? const SizedBox(
                      width: 16,
                      height: 16,
                      child: CircularProgressIndicator(
                          strokeWidth: 2, color: cMuted))
                  : Icon(icon, size: 18, color: Colors.black),
              const SizedBox(width: 8),
              Text(label,
                  style: TextStyle(
                      color: loading ? cMuted : Colors.black,
                      fontWeight: FontWeight.w700)),
            ],
          ),
        ),
      ),
    );
  }

  // Red danger action (Trigger Siren).
  // `active` (e.g. siren currently sounding) fills the button solid red
  // instead of the usual translucent outline - same visual language as the
  // Emergency button's on/off states, so "this is currently ON, tap to
  // stop it" reads the same way everywhere in the app instead of every
  // danger action looking identical regardless of its current state.
  Widget _dangerBtn(IconData icon, String label, VoidCallback onTap,
      {bool loading = false, bool active = false}) {
    return Material(
      color: Colors.transparent,
      borderRadius: BorderRadius.circular(12),
      child: InkWell(
        borderRadius: BorderRadius.circular(12),
        // Same reasoning as _primaryBtn above - never hard-disable the tap
        // target itself, the handler decides cancel-vs-start.
        onTap: onTap,
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 200),
          padding: const EdgeInsets.symmetric(vertical: 14),
          decoration: BoxDecoration(
            color: active ? cRed.withValues(alpha: 0.85) : cRed.withValues(alpha: 0.14),
            borderRadius: BorderRadius.circular(12),
            border: Border.all(
                color: cRed.withValues(alpha: active ? 1 : 0.55),
                width: active ? 1.4 : 1.2),
            boxShadow: [
              BoxShadow(
                  color: cRed.withValues(alpha: active ? 0.4 : 0.18),
                  blurRadius: active ? 16 : 14,
                  spreadRadius: -4,
                  offset: active ? const Offset(0, 4) : Offset.zero),
            ],
          ),
          child: Row(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              loading
                  ? SizedBox(
                      width: 16,
                      height: 16,
                      child: CircularProgressIndicator(
                          strokeWidth: 2, color: active ? Colors.white : cRed))
                  : Icon(icon, size: 18, color: active ? Colors.white : cRed),
              const SizedBox(width: 8),
              Text(label,
                  style: TextStyle(
                      color: active ? Colors.white : cRed,
                      fontWeight: FontWeight.w700)),
            ],
          ),
        ),
      ),
    );
  }

  // Dark icon-only control (snapshot, record, night vision, etc.). Used
  // inside an Expanded slot in a fixed 4+3 grid (see build()) - no fixed
  // width, so each button evenly fills its share of the row instead of
  // sitting left-aligned in unused space.
  Widget _iconBtn(IconData icon, bool active, VoidCallback onTap,
      {bool danger = false, String? tip, bool loading = false}) {
    final iconColor = danger ? cRed : (active ? cTeal2 : cMuted);
    final borderColor = danger
        ? cRed.withValues(alpha: 0.5)
        : (active ? cTeal : cLine);
    return Tooltip(
      message: tip ?? '',
      child: InkWell(
        // Never hard-disable on loading - same reasoning as _primaryBtn/
        // _dangerBtn above, so no icon control in this row can ever feel
        // stuck/unresponsive either.
        onTap: onTap,
        borderRadius: BorderRadius.circular(10),
        child: Container(
          height: 46,
          alignment: Alignment.center,
          decoration: BoxDecoration(
            color: active ? cTeal.withValues(alpha: 0.15) : cPanel2,
            borderRadius: BorderRadius.circular(10),
            border: Border.all(color: borderColor),
          ),
          child: loading
              ? SizedBox(
                  width: 16,
                  height: 16,
                  child: CircularProgressIndicator(strokeWidth: 2, color: iconColor))
              : Icon(icon, size: 20, color: iconColor),
        ),
      ),
    );
  }

  void _openFullscreen([int? cam]) {
    final target = cam ?? _sel;
    Navigator.of(context).push(MaterialPageRoute(
        builder: (_) => _FullscreenView(cam: target, lanReachable: _lanReachable)));
  }

  /// One camera's live tile: video + DETECTION overlay + tier badge, same
  /// content that used to be hardcoded to just _sel. Now parameterized by
  /// camId so the grid can render one of these per known camera.
  /// showDetection is false for every non-selected tile in the grid - the
  /// DETECTION panel reads _stat, which _loadStat() only ever populates
  /// for the currently-selected/controlled camera, so showing it on every
  /// tile would just repeat the same numbers (or stale ones) under the
  /// wrong camera's feed.  big=true keeps the original single-camera
  /// 16:11 aspect ratio (used when there's only one camera at all);
  /// big=false is the smaller grid-tile version.
  Widget _buildVideoStack(int camId,
      {required bool showDetection, required bool big}) {
    final tier = (_stat['tier'] ?? 0) as int;
    final isSelected = camId == _sel;
    // Live view: MJPEG on the laptop's own LAN (instant, simple); true
    // WebRTC live video when off that network (see webrtc_view.dart) -
    // either way this is REAL live footage, never a static snapshot.
    // RepaintBoundary here is deliberate, not decorative: every tile lives
    // inside a ListView/GridView (see build() above), and every
    // _statTimer/_loadStat tick (every 2s) triggers a setState() that
    // relayouts the list. flutter_webrtc's RTCVideoView is backed by a
    // native PlatformView texture on Android, and repeated ancestor
    // relayout of a PlatformView inside a scrollable is a known trigger
    // for that texture going blank (black, no error, no spinner) even
    // though the underlying RTCPeerConnection is still healthy - this was
    // the "video only works in fullscreen" bug, since _FullscreenView
    // renders the same WebRtcView inside a plain Scaffold that never gets
    // relaid-out this way. Isolating each tile's video subtree in its own
    // repaint boundary stops the surrounding grid/list churn from
    // reaching the platform view.
    return GestureDetector(
      onTap: () => _openFullscreen(camId),
      child: RepaintBoundary(
        child: Stack(children: [
          AspectRatio(
            aspectRatio: big ? 16 / 11 : 16 / 12,
            child: ClipRRect(
              borderRadius: BorderRadius.circular(12),
              child: Container(
                color: Colors.black,
                child: !_lanChecked
                    ? const Center(
                        child: Column(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            CircularProgressIndicator(color: cTeal),
                            SizedBox(height: 12),
                            Text('Camera loading…',
                                style: TextStyle(color: cMuted, fontSize: 12.5)),
                          ],
                        ),
                      )
                    : _lanReachable
                        ? MjpegView(
                            key: ValueKey('stream$camId'),
                            url: Api.streamUrl(camId),
                            active: isSelected ? _cameraOn : true,
                            fit: BoxFit.cover,
                            // If the local stream stalls (the Wi-Fi
                            // "spinner forever" case), force an immediate
                            // re-probe rather than just giving up - if the
                            // probe still says LAN is reachable but the
                            // actual stream failed, treat this one probe
                            // result as stale and mark it false directly;
                            // if the probe already knows it's unreachable,
                            // this is a no-op since _lanReachable will
                            // already reflect that.
                            onFailed: () {
                              if (mounted) {
                                setState(() => _lastLanProbe = false);
                                _refreshLanProbe();
                              }
                            },
                          )
                        : (Store.lastDeviceId != null
                            ? WebRtcView(
                                key: ValueKey('webrtc$camId'),
                                deviceId: Store.lastDeviceId!,
                                cam: camId,
                                fit: BoxFit.cover,
                              )
                            : const Center(
                                child: Text('No paired device',
                                    style: TextStyle(color: cMuted)))),
              ),
            ),
          ),
          if (showDetection)
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
                    Text('Est. distance ${_stat['distance'] ?? '-'} m away',
                        style: const TextStyle(color: cOrange, fontSize: 12)),
                  ],
                ),
              ),
            ),
          if (showDetection && tier > 0)
            Positioned(top: 10, right: 10, child: tierPill(tier)),
          // Small fullscreen affordance on every tile, same icon language
          // as the old single-view fullscreen button - the whole tile is
          // already tappable to open fullscreen, this just makes that
          // discoverable at a glance instead of being an invisible
          // gesture.
          Positioned(
            bottom: 8,
            right: 8,
            child: Container(
              padding: const EdgeInsets.all(6),
              decoration: BoxDecoration(
                color: Colors.black.withValues(alpha: 0.55),
                borderRadius: BorderRadius.circular(8),
              ),
              child: const Icon(Icons.fullscreen, color: Colors.white, size: 18),
            ),
          ),
        ]),
      ),
    );
  }
}

class _FullscreenView extends StatefulWidget {
  final int cam;
  final bool lanReachable;
  const _FullscreenView({required this.cam, required this.lanReachable});
  @override
  State<_FullscreenView> createState() => _FullscreenViewState();
}

class _FullscreenViewState extends State<_FullscreenView> {
  bool _showExit = true;
  late bool _lan = widget.lanReachable;   // may flip to false on stream fail
  Timer? _hideTimer;

  @override
  void initState() {
    super.initState();

    // True fullscreen: hide the status bar AND the navigation bar.
    // immersiveSticky means a swipe from the edge shows them briefly and then
    // they slide away again - right for a CCTV view you watch for a long time.
    SystemChrome.setEnabledSystemUIMode(SystemUiMode.immersiveSticky);

    // Camera footage is landscape, so rotate to match.
    SystemChrome.setPreferredOrientations([
      DeviceOrientation.landscapeLeft,
      DeviceOrientation.landscapeRight,
    ]);

    _startHideTimer();
  }

  @override
  void dispose() {
    _hideTimer?.cancel();

    // Put the phone back the way we found it, or the rest of the app stays
    // stuck fullscreen and locked in landscape.
    SystemChrome.setEnabledSystemUIMode(SystemUiMode.edgeToEdge);
    SystemChrome.setPreferredOrientations(DeviceOrientation.values);
    super.dispose();
  }

  void _startHideTimer() {
    _hideTimer?.cancel();
    _hideTimer = Timer(const Duration(seconds: 3), () {
      if (mounted) setState(() => _showExit = false);
    });
  }

  void _tap() {
    setState(() => _showExit = !_showExit);
    if (_showExit) _startHideTimer();
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        backgroundColor: Colors.black,
        // Fullscreen is just the live video - no controls. Tap to reveal Exit.
        body: Stack(children: [
          Positioned.fill(
            child: GestureDetector(
              onTap: _tap,
              child: _lan
                  ? MjpegView(
                      key: ValueKey('fs${widget.cam}'),
                      url: Api.streamUrl(widget.cam),
                      fit: BoxFit.contain,
                      onFailed: () {
                        if (mounted && _lan) setState(() => _lan = false);
                      },
                    )
                  : (Store.lastDeviceId != null
                      ? WebRtcView(
                          key: ValueKey('fswebrtc${widget.cam}'),
                          deviceId: Store.lastDeviceId!,
                          cam: widget.cam,
                          fit: BoxFit.contain,
                        )
                      : const Center(
                          child: Text('No paired device',
                              style: TextStyle(color: cMuted)))),
            ),
          ),
          Positioned(
            top: 14,
            right: 14,
            child: AnimatedOpacity(
              opacity: _showExit ? 1 : 0,
              duration: const Duration(milliseconds: 200),
              child: GestureDetector(
                onTap: () => Navigator.pop(context),
                child: Container(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
                  decoration: BoxDecoration(
                    color: Colors.black.withValues(alpha: 0.6),
                    borderRadius: BorderRadius.circular(24),
                    border: Border.all(color: cLine),
                  ),
                  child: const Row(mainAxisSize: MainAxisSize.min, children: [
                    Icon(Icons.close, color: Colors.white, size: 18),
                    SizedBox(width: 6),
                    Text('Exit',
                        style: TextStyle(color: Colors.white, fontSize: 13)),
                  ]),
                ),
              ),
            ),
          ),
        ]),
      );
}

/// Slow opacity breathing loop (not a hard blink) for the emergency
/// override banner - reads as "actively urgent" without being genuinely
/// flashing/strobing, which would be a real accessibility problem on a
/// safety-critical control.
class _EmergencyPulse extends StatefulWidget {
  final Widget child;
  const _EmergencyPulse({required this.child});

  @override
  State<_EmergencyPulse> createState() => _EmergencyPulseState();
}

class _EmergencyPulseState extends State<_EmergencyPulse>
    with SingleTickerProviderStateMixin {
  late final AnimationController _c;
  late final Animation<double> _opacity;

  @override
  void initState() {
    super.initState();
    _c = AnimationController(
        vsync: this, duration: const Duration(milliseconds: 1100))
      ..repeat(reverse: true);
    _opacity = Tween(begin: 0.72, end: 1.0)
        .animate(CurvedAnimation(parent: _c, curve: Curves.easeInOut));
  }

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return FadeTransition(opacity: _opacity, child: widget.child);
  }
}
