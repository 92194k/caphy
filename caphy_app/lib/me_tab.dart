import 'package:flutter/material.dart';
import 'api.dart';
import 'theme.dart';

class MeTab extends StatefulWidget {
  final VoidCallback onLogout;
  const MeTab({super.key, required this.onLogout});
  @override
  State<MeTab> createState() => _MeTabState();
}

class _MeTabState extends State<MeTab> {
  List<dynamic> _cams = [];
  final Map<int, TextEditingController> _ctl = {};

  @override
  void initState() {
    super.initState();
    _load();
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

  @override
  void dispose() {
    for (final c in _ctl.values) {
      c.dispose();
    }
    super.dispose();
  }

  void _toast(String m) => ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(backgroundColor: cPanel, content: Text(m, style: const TextStyle(color: cText))));

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(backgroundColor: cPanel, title: const Text('Me')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          panel(
            child: Row(children: [
              const CircleAvatar(
                  backgroundColor: cBg,
                  child: Icon(Icons.person, color: cTeal2)),
              const SizedBox(width: 14),
              Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(Store.user,
                      style: const TextStyle(
                          color: cText,
                          fontSize: 16,
                          fontWeight: FontWeight.bold)),
                  const Text('Homeowner',
                      style: TextStyle(color: cMuted, fontSize: 12)),
                ],
              ),
            ]),
          ),
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
                      _toast(ok ? 'Renamed to "$name"' : 'Rename failed');
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
                child: Text(Store.baseUrl,
                    style: const TextStyle(color: cText)),
              ),
            ]),
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
