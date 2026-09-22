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
call, using a strict JSON response contract.

Live: wired to POST /api/assistant in web/server.py. Voice control
requires internet/Groq - if Groq is unreachable (no internet, all keys
exhausted/invalid, a deprecated model, provider down), handle_request()
returns a plain "error" response rather than trying a local fallback.
(An earlier revision added an Ollama-based local fallback plus a
keyword-matcher last resort for a fully offline chain; that was removed
2026-08-31 ahead of the thesis defense deadline to simplify what needs to
be tested and explained - CAPHY's other offline capabilities, arming/
disarming/siren via the app's own buttons or the web dashboard, local
detection, and local recording, are untouched by this and still require
zero internet. Only voice/chat control needs Groq.)
"""

import json

from assistant_ai.groq_client import ask_groq, GroqAllKeysFailedError
from assistant_ai.knowledge import CAPHY_FACTS, ASSISTANT_SYSTEM_PROMPT
from assistant_ai.actions import ALLOWED_ACTIONS, ACTION_DESCRIPTIONS, run_action, UnknownActionError

VALID_TYPES = {"talk", "know", "do", "clarify"}


def _build_messages(user_text, user_name=None, live_state=None, history=None):
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

    messages = [
        {"role": "system", "content": ASSISTANT_SYSTEM_PROMPT},
        {"role": "system", "content": "\n".join(context_lines)},
    ]

    # Prior turns from THIS conversation, if the caller sent any (see
    # web/server.py's api_assistant) - without this, every message was
    # answered with zero awareness of what was just said, which is why a
    # follow-up like "did you see anyone?" right after "check the cam" had
    # nothing to attach to. Each turn is still just a plain chat message -
    # the assistant's own past JSON replies aren't replayed verbatim (the
    # model doesn't need to see its own {"type":...} wrapper, just what it
    # actually SAID), so this reads as a normal back-and-forth conversation
    # to the model, one user/assistant message pair at a time.
    for turn in (history or []):
        role = turn.get("role")
        text = turn.get("text", "")
        if role in ("user", "assistant") and text:
            messages.append({"role": role, "content": text})

    messages.append({"role": "user", "content": user_text})
    return messages


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


def handle_request(user_text, user_name=None, live_state=None, action_handlers=None, history=None):
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
    messages = _build_messages(user_text, user_name=user_name, live_state=live_state, history=history)

    try:
        raw_reply = ask_groq(messages)
    except GroqAllKeysFailedError as exc:
        # No internet, all Groq keys exhausted/invalid, or Groq itself
        # down. Voice control needs Groq - this returns a plain, honest
        # error rather than trying a local fallback (removed 2026-08-31,
        # see module docstring). Everything else in CAPHY (detection,
        # recording, siren/arm/disarm via the app's own buttons or the
        # web dashboard) is completely unaffected by this and needs no
        # internet at all - only voice/chat commands do.
        return {
            "type": "error",
            "reply": ("I can't reach the AI service right now - voice control needs "
                      "an internet connection. You can still arm, disarm, or control "
                      "the siren directly from the app's buttons or the dashboard."),
            "error": str(exc),
        }

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
