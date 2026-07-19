"""CAPHY voice command logic (no audio here, just interpretation).

All phrases live in voice/intents.json - the single source of truth shared by
the desktop voice loop (main.py) and the web/phone voice API (web/server.py).
Add or change phrases there, never in this file.

Sensitive commands (disarm) ask "Are you sure?" and wait for yes/no before
acting. That multi-turn flow is preserved from the original version.
"""
import json
import os
import re

INTENTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "intents.json")

_cfg = None


def config():
    """Load intents.json once and cache it."""
    global _cfg
    if _cfg is None:
        with open(INTENTS_PATH, encoding="utf-8") as f:
            _cfg = json.load(f)
    return _cfg


def _has(text, phrase):
    """Whole-phrase match, so 'arm' does NOT fire inside 'disarm'."""
    return re.search(r"(?:^|\s)" + re.escape(phrase) + r"(?:$|\s)",
                     " " + text + " ") is not None


def all_phrases(lang):
    """Every phrase for one language - used to build the Vosk grammar."""
    cfg = config()
    out = []
    for intent in cfg["intents"]:
        out += intent["phrases"].get(lang, [])
    out += cfg["confirm"]["yes"].get(lang, [])
    out += cfg["confirm"]["no"].get(lang, [])
    return out


def match_intent(text):
    """Return the intent dict whose phrase appears in text, or None.

    Longest phrases are checked first so 'disable emergency mode' wins over
    'emergency mode'.
    """
    text = (text or "").lower().strip()
    if not text:
        return None
    candidates = []
    for intent in config()["intents"]:
        for lang in ("en", "tl"):
            for phrase in intent["phrases"].get(lang, []):
                candidates.append((len(phrase), phrase, intent["id"]))
    by_id = {i["id"]: i for i in config()["intents"]}
    for _, phrase, iid in sorted(candidates, key=lambda c: -c[0]):
        if _has(text, phrase):
            return by_id[iid]
    return None


def detect_lang(text):
    """Which language was this phrase spoken in? Defaults to English."""
    text = (text or "").lower().strip()
    cfg = config()
    for intent in cfg["intents"]:
        for phrase in intent["phrases"].get("tl", []):
            if _has(text, phrase):
                return "tl"
    for w in cfg["confirm"]["yes"]["tl"] + cfg["confirm"]["no"]["tl"]:
        if _has(text, w):
            return "tl"
    return "en"


def is_yes(text):
    c = config()["confirm"]["yes"]
    return any(_has((text or "").lower(), w) for w in c["en"] + c["tl"])


def is_no(text):
    c = config()["confirm"]["no"]
    return any(_has((text or "").lower(), w) for w in c["en"] + c["tl"])


def response_text(intent, lang, data=None):
    """The reply string for an intent, with {placeholders} filled in.

    Reporting intents (status, threat level, alerts) are marked "template" in
    intents.json and need a data dict from the caller.
    """
    cfg = config()
    template = intent["response"][lang]
    if not intent.get("template"):
        return template

    words = cfg["status_words"][lang]
    data = data or {}

    if intent["id"] == "check_status":
        threat = data.get("threat_level")
        return template.format(
            armed=words["armed_true"] if data.get("armed") else words["armed_false"],
            camera=words["camera_true"] if data.get("camera_on") else words["camera_false"],
            night_vision=words["nv_true"] if data.get("night_vision") else words["nv_false"],
            threat=(f"Threat level: {threat}." if threat else words["no_threat"]))

    if intent["id"] == "threat_level":
        return template.format(tier=data.get("tier", "unknown"))

    if intent["id"] == "alert_status":
        alerts = data.get("alerts", [])
        latest = alerts[0].get("timestamp", "none") if alerts else "none"
        return template.format(count=len(alerts), latest=latest)

    return template


class Response:
    def __init__(self, speak, action=None, awaiting=False, lang="en", intent=None):
        self.speak = speak
        self.action = action        # intent id, e.g. "camera_off"
        self.awaiting = awaiting    # True while waiting for a yes/no
        self.lang = lang
        self.intent = intent        # full intent dict, for template responses

    def __repr__(self):
        return (f"Response(speak={self.speak!r}, action={self.action!r}, "
                f"awaiting={self.awaiting}, lang={self.lang!r})")


class CommandInterpreter:
    """Turns recognized text into a spoken reply + an action id, and handles
    the confirmation dialog for sensitive commands."""

    def __init__(self):
        self.pending = None         # intent dict awaiting yes/no
        self.pending_lang = "en"

    def interpret(self, text):
        text = (text or "").lower().strip()
        if not text:
            return None
        cfg = config()

        # ---- we already asked "Are you sure?" ----
        if self.pending:
            lang = self.pending_lang
            if is_yes(text):
                intent = self.pending
                self.pending = None
                return Response(response_text(intent, lang), action=intent["id"],
                                lang=lang, intent=intent)
            if is_no(text):
                self.pending = None
                return Response(cfg["confirm"]["cancelled"][lang], lang=lang)
            return Response(cfg["confirm"]["say_yes_no"][lang], awaiting=True, lang=lang)

        intent = match_intent(text)
        if intent is None:
            return None
        lang = detect_lang(text)

        if intent.get("sensitive"):
            self.pending = intent
            self.pending_lang = lang
            return Response(cfg["confirm"]["ask"][lang], awaiting=True, lang=lang)

        return Response(response_text(intent, lang), action=intent["id"],
                        lang=lang, intent=intent)
