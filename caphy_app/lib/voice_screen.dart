import 'package:flutter/material.dart';
import 'package:speech_to_text/speech_to_text.dart';
import 'package:flutter_tts/flutter_tts.dart';
import 'api.dart';
import 'theme.dart';

/// Voice control for CAPHY.
///
/// COMMANDS ONLY - CAPHY does not chat. Every utterance is matched against the
/// fixed command list; anything else gets "I did not understand that command".
/// That keeps the whole system offline: no cloud LLM, no internet needed to
/// control the house.
///
/// Speak-first: the big mic is the main control and what you say appears live
/// on screen as you say it. Typing is available but hidden behind the keyboard
/// button - it is a fallback, not the main way in.
///
/// The command list is fetched from the system (/api/intents, which reads
/// voice/intents.json), so the phone can never show commands the system does
/// not actually understand.
class VoiceScreen extends StatefulWidget {
  const VoiceScreen({super.key});
  @override
  State<VoiceScreen> createState() => _VoiceScreenState();
}

class _Msg {
  final String text;
  final bool me;
  _Msg(this.text, this.me);
}

class _VoiceScreenState extends State<VoiceScreen> {
  final SpeechToText _stt = SpeechToText();
  final FlutterTts _tts = FlutterTts();
  final TextEditingController _input = TextEditingController();
  final ScrollController _scroll = ScrollController();

  bool _sttReady = false;
  bool _listening = false;
  bool _showKeyboard = false;
  String _lang = 'en';
  bool _busy = false;
  String _heard = ''; // live transcript while speaking
  final List<_Msg> _log = [];

  List<dynamic> _commands = [];
  bool _loadingCommands = true;

  @override
  void initState() {
    super.initState();
    _initStt();
    _loadCommands();
  }

  @override
  void dispose() {
    _input.dispose();
    _scroll.dispose();
    super.dispose();
  }

  Future<void> _loadCommands() async {
    final c = await Api.intents();
    if (!mounted) return;
    setState(() {
      _commands = c;
      _loadingCommands = false;
    });
  }

  Future<void> _initStt() async {
    try {
      _sttReady = await _stt.initialize(
        onError: (_) {
          if (mounted) setState(() => _listening = false);
        },
        onStatus: (s) {
          if (s == 'done' || s == 'notListening') {
            if (mounted) setState(() => _listening = false);
          }
        },
      );
    } catch (_) {
      _sttReady = false;
    }
    if (mounted) setState(() {});
  }

  // Tap the mic to start; it stops automatically after a pause.
  Future<void> _toggleMic() async {
    if (_listening) {
      await _stt.stop();
      setState(() => _listening = false);
      return;
    }
    if (!_sttReady) {
      _sysMsg('Microphone not available. Check app permissions in Settings.');
      return;
    }
    setState(() {
      _listening = true;
      _heard = '';
    });
    await _stt.listen(
      onResult: (r) {
        // Live transcript - shows the words as they are recognized.
        setState(() => _heard = r.recognizedWords);
        if (r.finalResult && r.recognizedWords.trim().isNotEmpty) {
          _send(r.recognizedWords);
        }
      },
      localeId: _lang == 'tl' ? 'fil_PH' : 'en_US',
      listenFor: const Duration(seconds: 20),
      pauseFor: const Duration(seconds: 3),
    );
  }

  Future<void> _send(String raw) async {
    final said = raw.trim();
    if (said.isEmpty || _busy) return;
    _input.clear();
    if (_listening) await _stt.stop();

    setState(() {
      _listening = false;
      _heard = '';
      _busy = true;
      _log.add(_Msg(said, true));
    });
    _scrollToEnd();

    final r = await Api.voice(said, lang: _lang);
    final reply = (r['reply'] ?? r['message'] ?? '').toString();

    if (!mounted) return;
    setState(() {
      if (reply.isNotEmpty) _log.add(_Msg(reply, false));
      _busy = false;
    });
    _scrollToEnd();
    if (reply.isNotEmpty) _speak(reply, (r['lang'] ?? _lang).toString());
  }

  void _scrollToEnd() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scroll.hasClients) {
        _scroll.animateTo(_scroll.position.maxScrollExtent,
            duration: const Duration(milliseconds: 250), curve: Curves.easeOut);
      }
    });
  }

  void _sysMsg(String m) => setState(() => _log.add(_Msg(m, false)));

  Future<void> _speak(String text, String lang) async {
    try {
      await _tts.setLanguage(lang == 'tl' ? 'fil-PH' : 'en-US');
      await _tts.speak(text);
    } catch (_) {}
  }

  // ---------------------------------------------------------------- command list

  void _openCommandSheet() {
    showModalBottomSheet(
      context: context,
      backgroundColor: cPanel,
      isScrollControlled: true,
      shape: const RoundedRectangleBorder(
          borderRadius: BorderRadius.vertical(top: Radius.circular(18))),
      builder: (_) => _CommandSheet(
        commands: _commands,
        lang: _lang,
        loading: _loadingCommands,
        onTap: (phrase) {
          Navigator.pop(context);
          _send(phrase);
        },
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        backgroundColor: cPanel,
        title: const Text('Voice'),
        actions: [
          IconButton(
            tooltip: 'What can I say?',
            onPressed: _openCommandSheet,
            icon: const Icon(Icons.help_outline, color: cTeal2),
          ),
        ],
      ),
      body: Column(children: [
        // ---- language ----
        Padding(
          padding: const EdgeInsets.fromLTRB(12, 12, 12, 6),
          child:
              Row(mainAxisAlignment: MainAxisAlignment.spaceBetween, children: [
            const Text('Say a command',
                style: TextStyle(
                    color: cMuted, fontSize: 14, fontWeight: FontWeight.w600)),
            ToggleButtons(
              isSelected: [_lang == 'en', _lang == 'tl'],
              onPressed: (i) => setState(() => _lang = i == 0 ? 'en' : 'tl'),
              borderRadius: BorderRadius.circular(8),
              selectedColor: Colors.black,
              fillColor: cTeal2,
              color: cMuted,
              children: const [
                Padding(
                    padding: EdgeInsets.symmetric(horizontal: 12),
                    child: Text('EN')),
                Padding(
                    padding: EdgeInsets.symmetric(horizontal: 12),
                    child: Text('TL')),
              ],
            ),
          ]),
        ),

        // ---- quick command chips, straight from the system ----
        SizedBox(
          height: 44,
          child: _loadingCommands
              ? const Center(
                  child: SizedBox(
                      height: 18,
                      width: 18,
                      child: CircularProgressIndicator(
                          strokeWidth: 2, color: cTeal)))
              : ListView(
                  scrollDirection: Axis.horizontal,
                  padding: const EdgeInsets.symmetric(horizontal: 12),
                  children: [
                    for (final c in _commands)
                      Padding(
                        padding: const EdgeInsets.only(right: 8),
                        child: ActionChip(
                          backgroundColor: cPanel,
                          side: const BorderSide(color: cLine),
                          label: Text(_label(c, _lang),
                              style: const TextStyle(color: cTeal2)),
                          onPressed: () {
                            final says = _says(c, _lang);
                            if (says.isNotEmpty) _send(says.first);
                          },
                        ),
                      ),
                  ],
                ),
        ),

        // ---- conversation ----
        Expanded(
          child: _log.isEmpty ? _emptyState() : _conversation(),
        ),

        if (_busy)
          const LinearProgressIndicator(color: cTeal, backgroundColor: cPanel),

        // ---- live transcript while speaking ----
        if (_listening)
          Container(
            width: double.infinity,
            color: cPanel,
            padding: const EdgeInsets.fromLTRB(18, 12, 18, 4),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Row(children: const [
                Icon(Icons.graphic_eq, size: 16, color: cRed),
                SizedBox(width: 6),
                Text('Listening…',
                    style: TextStyle(
                        color: cRed, fontSize: 12, fontWeight: FontWeight.w600)),
              ]),
              const SizedBox(height: 6),
              Text(
                _heard.isEmpty ? 'Say a command…' : _heard,
                style: TextStyle(
                    color: _heard.isEmpty ? cDim : cText,
                    fontSize: 18,
                    height: 1.3),
              ),
            ]),
          ),

        // ---- mic-first control bar ----
        Container(
          padding: const EdgeInsets.fromLTRB(12, 10, 12, 18),
          color: cPanel,
          child: Column(children: [
            if (_showKeyboard)
              Padding(
                padding: const EdgeInsets.only(bottom: 10),
                child: Row(children: [
                  Expanded(
                    child: TextField(
                      controller: _input,
                      style: const TextStyle(color: cText),
                      autofocus: true,
                      onSubmitted: _send,
                      decoration: const InputDecoration(
                        isDense: true,
                        hintText: 'Type a command',
                      ),
                    ),
                  ),
                  IconButton(
                    onPressed: () => _send(_input.text),
                    icon: const Icon(Icons.send, color: cTeal2),
                  ),
                ]),
              ),
            Row(mainAxisAlignment: MainAxisAlignment.center, children: [
              IconButton(
                tooltip: 'Type instead',
                onPressed: () =>
                    setState(() => _showKeyboard = !_showKeyboard),
                icon: Icon(
                    _showKeyboard ? Icons.keyboard_hide : Icons.keyboard,
                    color: cMuted),
              ),
              const SizedBox(width: 24),
              GestureDetector(
                onTap: _toggleMic,
                child: AnimatedContainer(
                  duration: const Duration(milliseconds: 180),
                  height: _listening ? 78 : 70,
                  width: _listening ? 78 : 70,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    color: _listening ? cRed : cTeal,
                    boxShadow: [
                      BoxShadow(
                        color: (_listening ? cRed : cTeal).withOpacity(0.35),
                        blurRadius: _listening ? 22 : 10,
                        spreadRadius: _listening ? 4 : 0,
                      )
                    ],
                  ),
                  child: Icon(_listening ? Icons.stop : Icons.mic,
                      size: 34, color: Colors.black),
                ),
              ),
              const SizedBox(width: 24),
              IconButton(
                tooltip: 'What can I say?',
                onPressed: _openCommandSheet,
                icon: const Icon(Icons.list_alt, color: cMuted),
              ),
            ]),
            const SizedBox(height: 6),
            Text(
              _listening ? 'Tap to stop' : 'Tap and speak',
              style: const TextStyle(color: cDim, fontSize: 12),
            ),
          ]),
        ),
      ]),
    );
  }

  Widget _emptyState() => Center(
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 32),
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            const Icon(Icons.mic_none, size: 54, color: cDim),
            const SizedBox(height: 12),
            const Text(
              'Tap the mic and say a command.',
              textAlign: TextAlign.center,
              style: TextStyle(color: cMuted, fontSize: 15),
            ),
            const SizedBox(height: 16),
            OutlinedButton.icon(
              onPressed: _openCommandSheet,
              icon: const Icon(Icons.list_alt, size: 18),
              label: const Text('See all commands'),
              style: OutlinedButton.styleFrom(
                  foregroundColor: cTeal2,
                  side: const BorderSide(color: cLine)),
            ),
          ]),
        ),
      );

  Widget _conversation() => ListView.builder(
        controller: _scroll,
        padding: const EdgeInsets.all(14),
        itemCount: _log.length,
        itemBuilder: (_, i) => _bubble(_log[i]),
      );

  Widget _bubble(_Msg m) => Align(
        alignment: m.me ? Alignment.centerRight : Alignment.centerLeft,
        child: Container(
          margin: const EdgeInsets.only(bottom: 10),
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
          constraints:
              BoxConstraints(maxWidth: MediaQuery.of(context).size.width * 0.75),
          decoration: BoxDecoration(
            color: m.me ? cTeal : cPanel,
            borderRadius: BorderRadius.circular(14),
            border: Border.all(color: m.me ? cTeal : cLine),
          ),
          child: Text(m.text,
              style: TextStyle(color: m.me ? Colors.black : cText)),
        ),
      );
}

// ------------------------------------------------------------------ helpers

String _label(dynamic c, String lang) {
  final l = (c['label'] as Map?) ?? {};
  return (l[lang] ?? l['en'] ?? c['id']).toString();
}

List<String> _says(dynamic c, String lang) {
  final s = (c['say'] as Map?) ?? {};
  final list = (s[lang] as List?) ?? (s['en'] as List?) ?? [];
  return list.map((e) => e.toString()).toList();
}

/// Bottom sheet listing every command CAPHY understands, grouped, with the
/// exact phrases to say. Tapping a phrase runs it.
class _CommandSheet extends StatelessWidget {
  final List<dynamic> commands;
  final String lang;
  final bool loading;
  final void Function(String phrase) onTap;

  const _CommandSheet({
    required this.commands,
    required this.lang,
    required this.loading,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    if (loading) {
      return const SizedBox(
          height: 220,
          child: Center(child: CircularProgressIndicator(color: cTeal)));
    }
    if (commands.isEmpty) {
      return const SizedBox(
        height: 220,
        child: Center(
          child: Padding(
            padding: EdgeInsets.all(24),
            child: Text(
              'Could not load the command list.\n'
              'Check that CAPHY is running and the address in Settings is correct.',
              textAlign: TextAlign.center,
              style: TextStyle(color: cMuted),
            ),
          ),
        ),
      );
    }

    // group -> commands
    final groups = <String, List<dynamic>>{};
    for (final c in commands) {
      groups.putIfAbsent((c['group'] ?? 'Other').toString(), () => []).add(c);
    }

    return DraggableScrollableSheet(
      expand: false,
      initialChildSize: 0.75,
      maxChildSize: 0.92,
      builder: (_, controller) => Column(children: [
        const SizedBox(height: 10),
        Container(
            width: 40,
            height: 4,
            decoration: BoxDecoration(
                color: cLine, borderRadius: BorderRadius.circular(2))),
        const Padding(
          padding: EdgeInsets.fromLTRB(18, 14, 18, 4),
          child: Align(
            alignment: Alignment.centerLeft,
            child: Text('What you can say',
                style: TextStyle(
                    color: cText, fontSize: 18, fontWeight: FontWeight.w700)),
          ),
        ),
        Padding(
          padding: const EdgeInsets.fromLTRB(18, 0, 18, 10),
          child: Align(
            alignment: Alignment.centerLeft,
            child: Text(
              lang == 'tl'
                  ? 'Pindutin ang mic, pagkatapos sabihin ang isa sa mga ito.'
                  : 'Tap the mic, then say any one of these.',
              style: const TextStyle(color: cDim, fontSize: 13),
            ),
          ),
        ),
        Expanded(
          child: ListView(
            controller: controller,
            padding: const EdgeInsets.fromLTRB(14, 0, 14, 24),
            children: [
              for (final entry in groups.entries) ...[
                Padding(
                  padding: const EdgeInsets.fromLTRB(4, 14, 4, 8),
                  child: Text(entry.key.toUpperCase(),
                      style: const TextStyle(
                          color: cTeal2,
                          fontSize: 12,
                          fontWeight: FontWeight.w700,
                          letterSpacing: 1.1)),
                ),
                for (final c in entry.value)
                  Container(
                    margin: const EdgeInsets.only(bottom: 8),
                    decoration: BoxDecoration(
                      color: cBg,
                      borderRadius: BorderRadius.circular(12),
                      border: Border.all(color: cLine),
                    ),
                    padding: const EdgeInsets.fromLTRB(14, 12, 14, 12),
                    child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Row(children: [
                            Expanded(
                              child: Text(_label(c, lang),
                                  style: const TextStyle(
                                      color: cText,
                                      fontSize: 15,
                                      fontWeight: FontWeight.w600)),
                            ),
                            if (c['sensitive'] == true)
                              const Padding(
                                padding: EdgeInsets.only(left: 6),
                                child: Text('asks to confirm',
                                    style:
                                        TextStyle(color: cDim, fontSize: 11)),
                              ),
                          ]),
                          const SizedBox(height: 8),
                          Wrap(
                            spacing: 6,
                            runSpacing: 6,
                            children: [
                              for (final p in _says(c, lang))
                                GestureDetector(
                                  onTap: () => onTap(p),
                                  child: Container(
                                    padding: const EdgeInsets.symmetric(
                                        horizontal: 10, vertical: 6),
                                    decoration: BoxDecoration(
                                      color: cPanel,
                                      borderRadius: BorderRadius.circular(20),
                                      border: Border.all(color: cLine),
                                    ),
                                    child: Text('"$p"',
                                        style: const TextStyle(
                                            color: cMuted, fontSize: 13)),
                                  ),
                                ),
                            ],
                          ),
                        ]),
                  ),
              ],
            ],
          ),
        ),
      ]),
    );
  }
}
