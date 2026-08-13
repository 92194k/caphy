"""
offline_fallback.py - zero-dependency local matcher used ONLY when Groq is
unreachable (no internet, all keys rate-limited/invalid, or Groq itself
down). Keeps CAPHY's voice assistant from going fully silent offline,
matching the project's stated "local-first, cloud is an enhancement, not a
dependency" principle used everywhere else in CAPHY (detection, alerts,
storage sync).

This is intentionally much simpler than the AI path: pure keyword/phrase
overlap against ALLOWED_ACTIONS, using only Python's built-in difflib (no
rapidfuzz/fuzzywuzzy - keeps CAPHY zero-install and 100% offline, same
reasoning requirements.txt already documents for the rest of the project).
It does not attempt "talk" or "know" style conversation - offline mode only
recognizes direct commands ("arm the system", "turn on the siren"). Anything
that doesn't clearly match one action returns a "clarify"-shaped response
telling the user it's running offline and to be more specific.

Never raises - always returns a well-formed dict matching the same shape
assistant_ai.router.handle_request() returns, so callers don't need to
special-case which path answered.
"""

import difflib

from assistant_ai.actions import ALLOWED_ACTIONS, ACTION_DESCRIPTIONS

# A handful of extra trigger words per action, beyond the action name itself
# and its description - covers common ways someone would actually phrase the
# request out loud. Kept short and hand-picked rather than exhaustive; this
# is a fallback path, not the primary matcher.
_KEYWORDS = {
    "arm": ["arm", "arm the system", "turn on security", "activate security", "secure the house"],
    "disarm": ["disarm", "turn off security", "deactivate security", "stand down"],
    "siren_on": ["siren on", "turn on siren", "sound the alarm", "alarm on", "play siren"],
    "siren_off": ["siren off", "turn off siren", "stop the alarm", "alarm off", "silence"],
    "emergency_on": ["emergency", "panic", "emergency mode on", "activate emergency"],
    "emergency_off": ["cancel emergency", "emergency off", "stand down emergency"],
    "camera_on": ["camera on", "turn on camera", "turn on the cameras", "enable camera"],
    "camera_off": ["camera off", "turn off camera", "turn off the cameras", "disable camera"],
    "camera_pause": ["pause camera", "pause the camera"],
    "camera_resume": ["resume camera", "resume the camera", "unpause camera"],
    "night_vision_on": ["night vision on", "turn on night vision", "enable night vision"],
    "night_vision_off": ["night vision off", "turn off night vision", "disable night vision"],
    "snapshot": ["snapshot", "take a picture", "take a photo", "capture image"],
    "record_start": ["start recording", "record video", "begin recording"],
    "record_stop": ["stop recording", "end recording"],
    "check_status": ["status", "system status", "what's the status", "how's the system"],
    "check_health": ["health", "system health", "is everything okay", "check health"],
    "check_alerts": ["alerts", "check alerts", "any alerts", "recent alerts"],
    "dismiss_all_alerts": ["dismiss alerts", "clear alerts", "dismiss all alerts", "clear all alerts"],
}

_MATCH_THRESHOLD = 0.6  # below this, treat as unrecognized rather than guess

# Opposite-action pairs where naive substring matching is actively
# dangerous ("disarm" contains "arm"; "camera off" shares every word with
# "camera on" except one). Checked BEFORE the generic scoring loop so the
# more specific/negated phrase always wins over its opposite.
_NEGATION_GUARDS = {
    "disarm": "arm",
    "siren_off": "siren_on",
    "emergency_off": "emergency_on",
    "camera_off": "camera_on",
    "camera_resume": "camera_pause",
    "night_vision_off": "night_vision_on",
    "record_stop": "record_start",
}


def _best_action(user_text):
    """Returns (action_name, score) for the best-matching action, or
    (None, 0.0) if nothing clears the threshold.

    Whole-word matching only (word-boundary substring, not raw `in`), so
    "arm" doesn't spuriously match inside "disarm" or "alarm". Negated
    actions (disarm, siren_off, ...) are checked first so their trigger
    words win over the un-negated action they'd otherwise tie with.
    """
    text = f" {user_text.strip().lower()} "
    if not text.strip():
        return None, 0.0

    def _phrase_score(phrase):
        phrase = phrase.lower()
        ratio = difflib.SequenceMatcher(None, text.strip(), phrase).ratio()
        # word-boundary containment (spaces padded on both sides) instead of
        # raw substring - "arm" must appear as its own word, not inside
        # "disarm" or "alarm"
        if f" {phrase} " in text:
            ratio = max(ratio, 0.85)
        return ratio

    # Check negated actions first - if a negation phrase matches well
    # enough on its own, it must beat its positive counterpart outright
    # rather than being averaged/overridden by shared words.
    best_name, best_score = None, 0.0
    for name in ALLOWED_ACTIONS:
        phrases = [name.replace("_", " "), ACTION_DESCRIPTIONS.get(name, "")] + _KEYWORDS.get(name, [])
        score = max(_phrase_score(p) for p in phrases if p)
        if score > best_score:
            best_name, best_score = name, score

    # If the winner is the "positive" half of a negation pair but the
    # negated phrasing is also present in the text, prefer the negation -
    # covers phrasings the scoring loop alone might not rank correctly.
    for neg_name, pos_name in _NEGATION_GUARDS.items():
        if best_name == pos_name:
            neg_phrases = [neg_name.replace("_", " ")] + _KEYWORDS.get(neg_name, [])
            if any(f" {p.lower()} " in text for p in neg_phrases):
                return neg_name, max(best_score, 0.85)

    return best_name, best_score


def handle_offline(user_text, action_handlers=None):
    """Same return shape as assistant_ai.router.handle_request(), used as
    the fallback when ask_groq() raises GroqAllKeysFailedError.

    Only ever produces "do" (a confident action match) or "clarify" (no
    confident match) - offline mode makes no attempt at open-ended "talk"
    or "know" answers, since that requires the language model.
    """
    action_name, score = _best_action(user_text)

    if action_name is None or score < _MATCH_THRESHOLD:
        return {
            "type": "clarify",
            "reply": ("I'm running offline right now, so I can only handle direct "
                      "commands like \"arm the system\" or \"turn on the siren\". "
                      "Could you rephrase that?"),
        }

    result = {"type": "do", "action": action_name,
              "reply": f"Okay, {ACTION_DESCRIPTIONS.get(action_name, action_name)}"}

    if action_handlers and action_name in action_handlers:
        try:
            result["action_result"] = action_handlers[action_name]({})
        except Exception as exc:
            result["action_result"] = {"ok": False, "error": str(exc)}
    else:
        result["action_result"] = {"ok": False, "error": "Action not wired up."}

    return result
