import 'package:flutter/material.dart';
import 'package:speech_to_text/speech_to_text.dart' as stt;
import 'package:speech_to_text/speech_recognition_result.dart';
import 'package:speech_to_text/speech_recognition_error.dart';
import 'package:flutter_tts/flutter_tts.dart';
import 'api.dart';
import 'theme.dart';

/// Voice-first chat with CAPHY's voice assistant (v2 rebuild).
///
/// The mic is the primary control - a big tap-to-speak button, CAPHY
/// speaks its replies back out loud, and typing is a secondary option
/// tucked behind a small toggle for whenever voice isn't practical.
///
/// Deliberately PUSH-TO-TALK, not always-listening: the speech_to_text
/// package's own docs say plainly it's "designed for short intermittent
/// use... there is not yet a way to achieve [continuous listening] using
/// the Android or iOS speech recognition capabilities." That matches
/// exactly what broke last time (error_no_match killing the session, the
/// mic "freezing" mid-sentence) - it wasn't a bug in our code, it's the
/// platform recognizer not supporting an always-on session at all. One
/// bounded listen() per tap is the case this package is actually built
/// for, so that's what this screen does.
///
/// Wake-word ("Hey CAPHY" without touching the phone) is a different,
/// separate capability - not covered by speech_to_text - and would need
/// its own dedicated always-on wake-word package if pursued later.
class AskCaphyScreen extends StatefulWidget {
  const AskCaphyScreen({super.key});

  @override
  State<AskCaphyScreen> createState() => _AskCaphyScreenState();
}

// Speech recognizers routinely mishear "CAPHY" (an invented word) as a real
// English word that sounds close to it - "coffee" being the single most
// common one, since "Ka-Fee" and "coffee" are nearly homophones. This runs
// on every transcript (partial and final) before it reaches the chat/AI, so
// the mishearing never actually leaves the phone. Whole-word match only
// (word boundaries), case-insensitive, so it doesn't corrupt an unrelated
// sentence that happens to contain "coffee" as part of another word.
final RegExp _caphyMishearings = RegExp(
  r'\b(coffee|copy|kathy|cathy|cafe|caffe|kafi|kaffe|kopi)\b',
  caseSensitive: false,
);

String normalizeCaphyMishearings(String text) {
  return text.replaceAll(_caphyMishearings, 'CAPHY');
}

// flutter_tts reads text literally, so it would say "CAPHY" as letters or as
// a garbled attempt rather than "Ka-Fee". Respelling only for what's SPOKEN
// (never for what's shown on screen - the chat bubble still reads "CAPHY").
String _forSpeech(String text) {
  return text.replaceAll(RegExp(r'\bCAPHY\b', caseSensitive: false), 'Kah-Fee');
}

// Very small heuristic to tell Tagalog from English text so the TTS voice
// can switch to match whatever CAPHY actually replied in (which itself
// follows whatever language the user spoke/typed). This is NOT meant to be
// a real language detector - it only needs to catch the common function
// words that show up in almost every Tagalog sentence, which is enough to
// pick the right voice for a short spoken reply.
final RegExp _tagalogMarkers = RegExp(
  r'\b(ang|ng|mga|na|po|opo|hindi|oo|paano|puwede|pwede|bakit|saan|kailan|'
  r'ito|iyan|iyon|kayo|ikaw|namin|natin|sila|siya|meron|wala|gusto|salamat)\b',
  caseSensitive: false,
);

bool _looksTagalog(String text) {
  final matches = _tagalogMarkers.allMatches(text).length;
  return matches >= 2; // a couple of marker words is enough signal for a short reply
}

class _ChatMessage {
  final String text;
  final bool fromUser;
  final String? type; // "talk" | "know" | "do" | "clarify" | "error" | null (user msg)
  _ChatMessage(this.text, this.fromUser, {this.type});
}

class _AskCaphyScreenState extends State<AskCaphyScreen> {
  final _controller = TextEditingController();
  final _scrollController = ScrollController();
  final List<_ChatMessage> _messages = [
    _ChatMessage(
        "Hi! I'm CAPHY. Tap the mic and tell me what you need.",
        false,
        type: 'talk'),
  ];
  bool _sending = false;

  // Message queue: previously the mic/send controls were disabled outright
  // while a request was in flight (onTap: !_sending ? ... : null), so
  // tapping again mid-reply did nothing at all - which read as "can't send
  // the next thing" / felt broken, even though nothing technically
  // errored. Now a new message just joins a queue and is spoken/sent the
  // moment the current one finishes, back to back, the way a real
  // assistant handles rapid follow-ups instead of forcing the user to
  // watch and wait for the mic to unlock again.
  final List<String> _pendingQueue = [];
  bool _processingQueue = false;

  final stt.SpeechToText _speech = stt.SpeechToText();
  bool _speechAvailable = false;
  bool _listening = false;
  String _partialText = '';

  final FlutterTts _tts = FlutterTts();
  bool _ttsReady = false;

  @override
  void initState() {
    super.initState();
    _initSpeech();
    _initTts();
  }

  Future<void> _initTts() async {
    try {
      await _tts.setLanguage('en-US');
      await _tts.setSpeechRate(0.48);
      await _tts.setPitch(1.0);
      await _tts.setVolume(1.0);
      // Without this, tts.speak()'s future resolves as soon as playback
      // STARTS, not when it finishes - which would make the queue drainer
      // (_drainQueue) move on to the next reply's speech immediately,
      // overlapping audio instead of speaking replies one at a time. This
      // makes await _tts.speak(...) actually wait for the audio to finish.
      try {
        await _tts.awaitSpeakCompletion(true);
      } catch (_) {
        // Unsupported on some platforms/plugin versions - non-fatal; the
        // queue still functions, replies might just overlap in speech.
      }
      // Default flutter_tts audio routing on Android can end up sounding
      // muffled/quiet, like a phone-call earpiece, instead of playing over
      // the normal speaker at full quality. This explicitly sets a proper
      // playback audio attribute so replies sound like a normal spoken
      // notification, not a call.
      try {
        await _tts.setAudioAttributesForNavigation();
      } catch (_) {
        // Android-only call - safe to ignore if unsupported on this platform.
      }
      try {
        await _tts.setIosAudioCategory(
          IosTextToSpeechAudioCategory.playback,
          [IosTextToSpeechAudioCategoryOptions.mixWithOthers],
        );
      } catch (_) {
        // iOS-only call - safe to ignore on Android.
      }
      _ttsReady = true;
    } catch (_) {
      _ttsReady = false;
    }
  }

  /// Picks the TTS voice locale to match what was actually said - Tagalog
  /// reply gets a Tagalog (or closest available) voice, otherwise English.
  /// Falls back silently to whatever the device already has if the exact
  /// locale isn't installed - never blocks speaking over a missing voice.
  Future<void> _setTtsLanguageFor(String text) async {
    try {
      final tagalog = _looksTagalog(text);
      await _tts.setLanguage(tagalog ? 'fil-PH' : 'en-US');
    } catch (_) {
      // If fil-PH isn't installed on this device, flutter_tts throws and we
      // just keep whatever language was already set - still speaks, just in
      // the fallback voice, better than not speaking at all.
    }
  }

  Future<void> _speak(String text) async {
    if (!_ttsReady || text.trim().isEmpty) return;
    try {
      await _tts.stop();
      await _setTtsLanguageFor(text);
      await _tts.speak(_forSpeech(text));
    } catch (_) {
      // TTS failing is never fatal - the reply is still shown as text.
    }
  }

  // Runs once per screen open. If the device has no mic permission or no
  // recognizer available, _speechAvailable just stays false and the mic
  // button quietly disables itself - the text box always still works.
  Future<void> _initSpeech() async {
    bool available = false;
    try {
      available = await _speech.initialize(
        onStatus: _onSpeechStatus,
        onError: _onSpeechError,
      );
    } catch (_) {
      available = false;
    }
    if (!mounted) return;
    setState(() => _speechAvailable = available);
  }

  void _onSpeechStatus(String status) {
    // 'listening' / 'notListening' / 'done' are all normal, expected status
    // values during one bounded session - only treat the session as truly
    // over when the plugin says so via isListening, not by guessing from a
    // specific status string (that guesswork was part of what broke the
    // old always-listening version).
    if (!mounted) return;
    if (!_speech.isListening && _listening) {
      setState(() => _listening = false);
    }
  }

  void _onSpeechError(SpeechRecognitionError error) {
    // Single bounded session: any error just ends listening and lets the
    // user tap the mic again. No auto-retry, no "is this recoverable"
    // guessing - that complexity is exactly what caused the old bugs, and
    // it isn't needed for a tap-once/speak-once/tap-again flow.
    if (!mounted) return;
    setState(() {
      _listening = false;
      _partialText = '';
    });
  }

  Future<void> _toggleListening() async {
    // No longer blocked by _sending - messages now queue (see
    // _pendingQueue/_drainQueue), so it's safe to accept a follow-up while
    // a previous reply is still being processed/spoken. Stopping any
    // in-progress TTS on tap is intentional "barge-in" behavior: starting
    // to ask something new should interrupt CAPHY talking, the way a real
    // assistant lets you cut it off instead of having to wait it out.
    if (!_speechAvailable) return;
    await _tts.stop();
    if (_listening) {
      await _speech.stop();
      if (mounted) setState(() => _listening = false);
      return;
    }
    setState(() {
      _listening = true;
      _partialText = '';
    });
    await _speech.listen(
      onResult: (SpeechRecognitionResult result) {
        if (!mounted) return;
        final normalized = normalizeCaphyMishearings(result.recognizedWords);
        setState(() => _partialText = normalized);
        if (result.finalResult) {
          setState(() => _listening = false);
          if (normalized.trim().isNotEmpty) {
            _send(normalized);
          } else {
            setState(() => _partialText = '');
          }
        }
      },
    );
  }

  @override
  void dispose() {
    _controller.dispose();
    _scrollController.dispose();
    _speech.stop();
    _tts.stop();
    super.dispose();
  }

  Future<void> _send([String? spokenText]) async {
    final text = normalizeCaphyMishearings(
        (spokenText ?? _controller.text).trim());
    if (text.isEmpty) return;

    // Show the user's message immediately, regardless of whether a
    // previous request is still in flight - queueing is invisible to the
    // user except that their message appears right away instead of the
    // input going dead.
    setState(() {
      _messages.add(_ChatMessage(text, true));
      _partialText = '';
    });
    _controller.clear();
    _scrollToEnd();

    _pendingQueue.add(text);
    _drainQueue();   // no-op if already draining; that loop will pick this up
  }

  /// Processes _pendingQueue strictly one at a time, in the order messages
  /// arrived, so replies come back in the same order they were asked -
  /// never running two Api.assistant() calls concurrently (which could
  /// otherwise return out of order, or double up on TTS speaking over
  /// itself).
  Future<void> _drainQueue() async {
    if (_processingQueue) return;
    _processingQueue = true;
    try {
      while (_pendingQueue.isNotEmpty) {
        final text = _pendingQueue.removeAt(0);
        if (mounted) setState(() => _sending = true);

        final result = await Api.assistant(text);

        if (!mounted) return;
        final reply = (result['reply'] as String?)?.trim().isNotEmpty == true
            ? result['reply'] as String
            : "Sorry, I didn't get a reply back.";
        setState(() {
          _messages.add(_ChatMessage(reply, false, type: result['type'] as String?));
        });
        _scrollToEnd();
        // Await the spoken reply before starting the NEXT queued request -
        // otherwise two replies could speak over each other, which would
        // feel far less like a real assistant than a short wait between
        // turns does.
        await _speak(reply);
      }
    } finally {
      _processingQueue = false;
      if (mounted) setState(() => _sending = false);
    }
  }

  void _scrollToEnd() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!_scrollController.hasClients) return;
      _scrollController.animateTo(
        _scrollController.position.maxScrollExtent,
        duration: const Duration(milliseconds: 200),
        curve: Curves.easeOut,
      );
    });
  }

  // Gemini-style bubble rule: the USER's messages are a solid rounded pill
  // (they're short, deserve a clear boundary), but CAPHY's replies are NOT
  // boxed at all - just plain text with a small avatar, flowing down the
  // page like a real conversation/document rather than a chain of boxes.
  // That's the biggest visual difference between "chat app with two colors
  // of rounded rectangles" (what this screen looked like before) and the
  // cleaner, more editorial feel Gemini/ChatGPT-style assistants use.
  Widget _messageTile(_ChatMessage m) {
    if (m.fromUser) {
      return Align(
        alignment: Alignment.centerRight,
        child: Container(
          margin: const EdgeInsets.only(bottom: 14, left: 48),
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 11),
          decoration: BoxDecoration(
            color: cTeal,
            borderRadius: BorderRadius.circular(20),
          ),
          child: Text(m.text,
              style: const TextStyle(
                  color: Colors.black, fontSize: 15, height: 1.3)),
        ),
      );
    }
    final accent = switch (m.type) {
      'do' => cGreen,
      'clarify' => cOrange,
      'error' => cRed,
      _ => cTeal2,
    };
    return Padding(
      padding: const EdgeInsets.only(bottom: 20, right: 12),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Container(
            width: 26,
            height: 26,
            margin: const EdgeInsets.only(top: 2),
            decoration: BoxDecoration(shape: BoxShape.circle, gradient: cGrad),
            child: const Icon(Icons.shield, size: 14, color: Colors.black),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                if (m.type != null && m.type != 'talk')
                  Padding(
                    padding: const EdgeInsets.only(bottom: 4),
                    child: Text(
                        m.type == 'do'
                            ? 'ACTION'
                            : m.type == 'clarify'
                                ? 'NEEDS INFO'
                                : m.type == 'error'
                                    ? 'ERROR'
                                    : 'INFO',
                        style: TextStyle(
                            color: accent,
                            fontSize: 10.5,
                            fontWeight: FontWeight.w800,
                            letterSpacing: 0.6)),
                  ),
                Text(m.text,
                    style: const TextStyle(
                        color: cText, fontSize: 15, height: 1.4)),
              ],
            ),
          ),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: cBg,
      appBar: AppBar(
        backgroundColor: cBg,
        elevation: 0,
        title: const Text('Ask CAPHY'),
      ),
      body: SafeArea(
        child: Column(
          children: [
            Expanded(
              child: ListView.builder(
                controller: _scrollController,
                padding: const EdgeInsets.fromLTRB(16, 12, 16, 12),
                itemCount: _messages.length,
                itemBuilder: (context, i) => _messageTile(_messages[i]),
              ),
            ),
            // ---- one unified bar: typing and voice share the SAME row
            // instead of swapping between two different layouts. The text
            // field always shows what's typed OR the live speech transcript
            // (so "Listening..." never has to live in a separate place),
            // the mic sits inside the field, Send sits beside it - exactly
            // like every other messaging app, and matches the app's own
            // dark navy/purple theme instead of a separate hardcoded palette.
            Padding(
              padding: const EdgeInsets.fromLTRB(14, 6, 14, 14),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.end,
                children: [
                  Expanded(
                    // A soft floating pill (shadow, no hard outline) instead
                    // of a bordered box - the borderless-with-shadow look is
                    // what actually reads as "Gemini-style" rather than a
                    // generic messaging-app input field.
                    child: AnimatedContainer(
                      duration: const Duration(milliseconds: 200),
                      constraints: const BoxConstraints(minHeight: 50),
                      decoration: BoxDecoration(
                        color: cPanel2,
                        borderRadius: BorderRadius.circular(26),
                        boxShadow: [
                          BoxShadow(
                              color: (_listening ? cRed : Colors.black)
                                  .withValues(alpha: _listening ? 0.25 : 0.3),
                              blurRadius: _listening ? 22 : 14,
                              spreadRadius: _listening ? 1 : -2,
                              offset: const Offset(0, 4)),
                        ],
                      ),
                      child: Row(
                        crossAxisAlignment: CrossAxisAlignment.center,
                        children: [
                          const SizedBox(width: 6),
                          _MicButton(
                            listening: _listening,
                            enabled: _speechAvailable,
                            onTap: _toggleListening,
                          ),
                          Expanded(
                            child: _listening
                                ? Padding(
                                    padding:
                                        const EdgeInsets.symmetric(vertical: 14),
                                    child: Row(children: [
                                      const _ListeningWave(),
                                      const SizedBox(width: 10),
                                      Expanded(
                                        child: Text(
                                          _partialText.isEmpty
                                              ? 'Listening…'
                                              : _partialText,
                                          overflow: TextOverflow.ellipsis,
                                          style: TextStyle(
                                              color: _partialText.isEmpty
                                                  ? cMuted
                                                  : cText,
                                              fontSize: 15),
                                        ),
                                      ),
                                    ]),
                                  )
                                : TextField(
                                    controller: _controller,
                                    style: const TextStyle(color: cText),
                                    decoration: InputDecoration(
                                      hintText: _speechAvailable
                                          ? 'Ask CAPHY anything…'
                                          : 'Type a message…',
                                      hintStyle:
                                          const TextStyle(color: cDim),
                                      border: InputBorder.none,
                                      isDense: true,
                                      contentPadding:
                                          const EdgeInsets.symmetric(
                                              vertical: 14),
                                    ),
                                    onSubmitted: (_) => _send(),
                                    textInputAction: TextInputAction.send,
                                  ),
                          ),
                          const SizedBox(width: 4),
                        ],
                      ),
                    ),
                  ),
                  const SizedBox(width: 8),
                  Container(
                    width: 50,
                    height: 50,
                    decoration: BoxDecoration(
                      shape: BoxShape.circle,
                      gradient: cGrad,
                      boxShadow: [
                        BoxShadow(
                            color: cTeal.withValues(alpha: 0.4),
                            blurRadius: 14,
                            offset: const Offset(0, 4)),
                      ],
                    ),
                    child: _sending
                        ? const Padding(
                            padding: EdgeInsets.all(15),
                            child: CircularProgressIndicator(
                                strokeWidth: 2, color: Colors.black),
                          )
                        : IconButton(
                            // Always tappable - typed/spoken messages queue,
                            // see _pendingQueue/_drainQueue.
                            onPressed: () => _send(),
                            icon: const Icon(Icons.arrow_upward_rounded,
                                color: Colors.black, size: 22),
                          ),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

/// Mic button with a soft pulsing glow while listening, instead of a flat
/// icon that just swaps color - small bit of motion that makes "CAPHY is
/// actively listening right now" unambiguous at a glance.
class _MicButton extends StatefulWidget {
  final bool listening;
  final bool enabled;
  final VoidCallback onTap;
  const _MicButton(
      {required this.listening, required this.enabled, required this.onTap});

  @override
  State<_MicButton> createState() => _MicButtonState();
}

class _MicButtonState extends State<_MicButton>
    with SingleTickerProviderStateMixin {
  late final AnimationController _c;

  @override
  void initState() {
    super.initState();
    _c = AnimationController(
        vsync: this, duration: const Duration(milliseconds: 900));
    if (widget.listening) _c.repeat(reverse: true);
  }

  @override
  void didUpdateWidget(covariant _MicButton old) {
    super.didUpdateWidget(old);
    if (widget.listening && !old.listening) {
      _c.repeat(reverse: true);
    } else if (!widget.listening && old.listening) {
      _c.stop();
      _c.value = 0;
    }
  }

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final color = widget.listening
        ? cRed
        : (widget.enabled ? cTeal2 : cDim);
    return AnimatedBuilder(
      animation: _c,
      builder: (context, child) {
        final glow = widget.listening ? 0.15 + (_c.value * 0.25) : 0.0;
        return Container(
          margin: const EdgeInsets.all(3),
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            color: color.withValues(alpha: glow),
          ),
          child: child,
        );
      },
      child: IconButton(
        tooltip: widget.enabled
            ? (widget.listening ? 'Stop listening' : 'Speak')
            : 'Microphone unavailable',
        onPressed: widget.enabled ? widget.onTap : null,
        icon: Icon(widget.listening ? Icons.mic : Icons.mic_none,
            color: color),
      ),
    );
  }
}

/// Three small bars bouncing at slightly offset phases - a lightweight
/// "listening" indicator (same idea as a voice-message waveform) instead
/// of just static text sitting next to the transcript.
class _ListeningWave extends StatefulWidget {
  const _ListeningWave();

  @override
  State<_ListeningWave> createState() => _ListeningWaveState();
}

class _ListeningWaveState extends State<_ListeningWave>
    with SingleTickerProviderStateMixin {
  late final AnimationController _c;

  @override
  void initState() {
    super.initState();
    _c = AnimationController(
        vsync: this, duration: const Duration(milliseconds: 700))
      ..repeat();
  }

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: 20,
      height: 16,
      child: AnimatedBuilder(
        animation: _c,
        builder: (context, _) {
          return Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: List.generate(3, (i) {
              final phase = (_c.value + (i * 0.33)) % 1.0;
              final h = 4 + (10 * (0.5 - (phase - 0.5).abs()) * 2);
              return Container(
                width: 3,
                height: h,
                decoration: BoxDecoration(
                  color: cRed,
                  borderRadius: BorderRadius.circular(2),
                ),
              );
            }),
          );
        },
      ),
    );
  }
}
