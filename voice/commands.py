"""Phase 8 - Voice command logic (no audio here, just interpretation).

Maps recognized text to an intent, and handles the multi-turn confirmation
for sensitive commands (disarm asks 'Are you sure?' before acting).
"""
import re

# bilingual (English + Tagalog) phrases -> intent. Checked in this order.
INTENTS = [
    ("disarm",     ["disarm system", "disarm", "i-disarm", "patayin ang sistema", "patay"]),
    ("arm",        ["arm system", "arm", "i-arm", "buksan ang sistema", "armado"]),
    ("stop_siren", ["stop siren", "stop", "itigil ang sirena", "tigil", "itigil"]),
    ("status",     ["system status", "status", "kalagayan", "kalagayan ng sistema"]),
]
YES = ["yes", "yeah", "yep", "yup", "oo", "opo", "sige", "tama"]
NO = ["no", "nope", "hindi", "huwag", "wag"]
SENSITIVE = {"disarm"}   # this command needs confirmation


def _has(text, phrase):
    # whole-word / phrase match so 'arm' does NOT fire inside 'disarm'
    return re.search(r"(?:^|\s)" + re.escape(phrase) + r"(?:$|\s)", " " + text + " ") is not None


def match_intent(text):
    for intent, phrases in INTENTS:
        for p in phrases:
            if _has(text, p):
                return intent
    return None


def is_yes(text):
    return any(_has(text, w) for w in YES)


def is_no(text):
    return any(_has(text, w) for w in NO)


class Response:
    def __init__(self, speak, action=None, awaiting=False):
        self.speak = speak
        self.action = action
        self.awaiting = awaiting

    def __repr__(self):
        return f"Response(speak={self.speak!r}, action={self.action!r}, awaiting={self.awaiting})"


class CommandInterpreter:
    """Turns recognized text into a spoken response + an action, and handles
    the confirmation dialog for sensitive commands (disarm)."""
    def __init__(self):
        self.pending = None

    def interpret(self, text):
        text = text.lower().strip()
        if not text:
            return None

        if self.pending:                       # we already asked 'Are you sure?'
            if is_yes(text):
                intent = self.pending
                self.pending = None
                return self._do(intent)
            if is_no(text):
                self.pending = None
                return Response("Cancelled.", action=None)
            return Response("Please say yes or no.", action=None, awaiting=True)

        intent = match_intent(text)
        if intent is None:
            return None
        if intent in SENSITIVE:
            self.pending = intent
            return Response("Are you sure?", action=None, awaiting=True)
        return self._do(intent)

    def _do(self, intent):
        return {
            "arm":        Response("System armed.", action="arm"),
            "disarm":     Response("System disarmed.", action="disarm"),
            "stop_siren": Response("Siren stopped.", action="stop_siren"),
            "status":     Response("System status reported.", action="status"),
        }[intent]