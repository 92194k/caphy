"""
router.py - the single entry point that turns a user's spoken/typed
sentence into one of: talk / know / do / clarify.

This replaces the old assistant/engine.py short-circuit design. The
difference that matters: previously an offline fuzzy-matcher ran FIRST and
only fell through to AI on a weak match, which is what caused "it's still
using the pre-list commands" - a mediocre offline match kept winning
before AI ever got a turn.

This version does the opposite: the AI is always asked first and is the
one doing the talk/know/do/clarify classification itself, in a single
call, using a strict JSON response contract. There is no separate offline
intent-matching step for the online path at all.

Live: wired to POST /api/assistant in web/server.py. If Groq is
unreachable (no internet, keys exhausted, provider down), handle_request()
falls back to assistant_ai/offline_fallback.py - a local keyword matcher
for direct commands - rather than returning a bare error, so voice control
degrades gracefully instead of going silent offline.
"""

import json

from assistant_ai.groq_client import ask_groq, GroqAllKeysFailedError
from assistant_ai.knowledge import CAPHY_FACTS, ASSISTANT_SYSTEM_PROMPT
from assistant_ai.actions import ALLOWED_ACTIONS, ACTION_DESCRIPTIONS, run_action, UnknownActionError
from assistant_ai.offline_fallback import handle_offline

VALID_TYPES = {"talk", "know", "do", "clarify"}


def _build_messages(user_text, user_name=None, live_state=None):
    action_list_text = "\n".join(
        f"- {name}: {ACTION_DESCRIPTIONS[name]}" for name in ALLOWED_ACTIONS
    )

    context_lines = [
        "CAPHY FACTS (only use these for 'know' answers):",
        CAPHY_FACTS,
        "",
        "ALLOWED ACTIONS (only use these for 'do' answers):",
        action_list_text,
    ]
    if user_name:
        context_lines += ["", f"The user's name is: {user_name}"]
    if live_state:
        context_lines += ["", "CURRENT LIVE SYSTEM STATE (use this to answer status questions accurately):",
                           json.dumps(live_state)]

    return [
        {"role": "system", "content": ASSISTANT_SYSTEM_PROMPT},
        {"role": "system", "content": "\n".join(context_lines)},
        {"role": "user", "content": user_text},
    ]


def _parse_ai_response(raw_text):
    """The AI is instructed to return ONLY a JSON object. Models sometimes
    wrap it in stray text/markdown fences anyway, so extract the first
    {...} block defensively rather than trusting raw_text is pure JSON."""
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"No JSON object found in AI response: {raw_text!r}")

    parsed = json.loads(text[start:end + 1])

    if parsed.get("type") not in VALID_TYPES:
        raise ValueError(f"AI returned an unrecognized type: {parsed!r}")

    return parsed


def handle_request(user_text, user_name=None, live_state=None, action_handlers=None):
    """Classify user_text and, for 'do' responses, execute the action.

    Returns a dict always shaped like:
      {"type": "talk"|"know"|"do"|"clarify"|"error",
       "reply": "<text to speak/show to the user>",
       "action": "<action name, only present for type == 'do'>",
       "action_result": {...}  # only present for type == 'do'}

    action_handlers: passed straight through to assistant_ai.actions.run_action
                      - see actions.py's wiring note. If a 'do' response
                      comes back but no handlers were supplied (e.g. this
                      is being called before that wiring exists yet), the
                      action is reported but not executed, and that is
                      reflected in action_result so callers can't
                      mistake "not wired up" for "silently succeeded".
    """
    messages = _build_messages(user_text, user_name=user_name, live_state=live_state)

    try:
        raw_reply = ask_groq(messages)
    except GroqAllKeysFailedError:
        # No internet, all Groq keys exhausted/invalid, or Groq itself down.
        # Voice must not go fully silent offline - CAPHY's core detection,
        # alerts, and storage already all work with zero internet, so the
        # assistant falls back to a local keyword matcher covering direct
        # commands (arm, disarm, siren, etc.) instead of returning a bare
        # error. "talk"/"know" conversation still needs the AI and isn't
        # available offline - only "do" actions and a "clarify" prompt are.
        return handle_offline(user_text, action_handlers=action_handlers)

    try:
        parsed = _parse_ai_response(raw_reply)
    except ValueError as exc:
        return {
            "type": "error",
            "reply": "Sorry, I didn't quite catch that - could you say it again?",
            "error": str(exc),
        }

    result = {"type": parsed["type"], "reply": parsed.get("reply", "")}

    if parsed["type"] == "do":
        action_name = parsed.get("action")
        result["action"] = action_name
        if action_name not in ALLOWED_ACTIONS:
            result["type"] = "clarify"
            result["reply"] = "I'm not sure exactly what you'd like me to do - could you rephrase that?"
            result.pop("action", None)
        elif action_handlers:
            try:
                result["action_result"] = run_action(action_name, action_handlers)
            except UnknownActionError as exc:
                result["action_result"] = {"ok": False, "error": str(exc)}
        else:
            result["action_result"] = {"ok": False, "error": "No action handlers wired in yet."}

    return result
