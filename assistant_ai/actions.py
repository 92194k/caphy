"""
actions.py - the closed whitelist of things CAPHY's voice assistant is
allowed to DO, and how each one maps to the system's existing internal
functions.

This is the safety boundary described in the CAPHY Voice Assistant plan:
the AI only ever picks an action NAME from this list (never invents one,
never touches hardware directly). The router calls run_action(name) and
this module is the only thing that actually calls into the real system.

IMPORTANT: this module intentionally does NOT import anything from
web/server.py to avoid coupling/circular imports. Instead, the caller
(server.py, once this is wired in) passes in a small "handlers" dict at
startup mapping each action name to the real function to call - see
build_default_handlers() usage note at the bottom. This keeps
assistant_ai/ testable in isolation, with zero risk to the live app until
someone deliberately wires it in.
"""

# The exact list the AI's prompt will be given. Keep names short, stable,
# and self-explanatory - these are also useful for logging.
ALLOWED_ACTIONS = [
    "arm",
    "disarm",
    "siren_on",
    "siren_off",
    "emergency_on",
    "emergency_off",
    "camera_on",
    "camera_off",
    "camera_pause",
    "camera_resume",
    "night_vision_on",
    "night_vision_off",
    "snapshot",
    "record_start",
    "record_stop",
    "check_status",
    "check_health",
    "check_alerts",
    "dismiss_all_alerts",
]

# Short human descriptions, given to the AI alongside ALLOWED_ACTIONS so it
# understands what each action name actually means (not just the bare word).
ACTION_DESCRIPTIONS = {
    "arm": "Arm the security system.",
    "disarm": "Disarm the security system.",
    "siren_on": "Turn the siren/alarm on manually.",
    "siren_off": "Turn the siren/alarm off.",
    "emergency_on": "Activate emergency/panic mode.",
    "emergency_off": "Deactivate emergency/panic mode.",
    "camera_on": "Turn the camera(s) fully on.",
    "camera_off": "Turn the camera(s) fully off.",
    "camera_pause": "Pause/close the camera without fully turning it off.",
    "camera_resume": "Resume a paused camera.",
    "night_vision_on": "Turn on software night vision for all cameras.",
    "night_vision_off": "Turn off software night vision for all cameras.",
    "snapshot": "Take a snapshot from the current camera.",
    "record_start": "Start recording video from the current camera.",
    "record_stop": "Stop the current video recording.",
    "check_status": "Report current system status (armed/disarmed, camera, night vision, siren, emergency).",
    "check_health": "Report system health (module/camera online status).",
    "check_alerts": "Report recent alerts.",
    "dismiss_all_alerts": "Dismiss all current alerts.",
}


class UnknownActionError(Exception):
    """Raised when the AI (or a bug) produces an action name that isn't in
    ALLOWED_ACTIONS. Callers should treat this as 'ask the user to
    clarify' rather than silently doing nothing or guessing."""
    pass


def run_action(action_name, handlers, context=None):
    """Execute one whitelisted action.

    action_name: must be a key in `handlers` (which itself must only ever
                 contain keys from ALLOWED_ACTIONS - see build note below).
    handlers:    dict[str, callable]. Each callable takes the optional
                 `context` dict and returns a small JSON-safe result dict,
                 e.g. {"ok": True, "armed": True}.
    context:     optional extra info (e.g. which camera index is
                 "current" for snapshot/record), passed through unchanged.

    Raises UnknownActionError if action_name isn't a registered handler -
    this should never happen if the AI was actually constrained to
    ALLOWED_ACTIONS, so treat it as a bug/edge-case to surface, not
    something to swallow silently.
    """
    if action_name not in ALLOWED_ACTIONS:
        raise UnknownActionError(f"'{action_name}' is not an allowed action.")
    handler = handlers.get(action_name)
    if handler is None:
        raise UnknownActionError(f"No handler registered for '{action_name}'.")
    return handler(context or {})


# NOTE for whoever wires this into web/server.py next:
#
# Build a handlers dict there (where the real workers/db/session objects
# already exist), e.g.:
#
#   handlers = {
#       "arm":     lambda ctx: (_set_armed(True),  {"ok": True, "armed": True})[1],
#       "disarm":  lambda ctx: (_set_armed(False), {"ok": True, "armed": False})[1],
#       "siren_on":  lambda ctx: (_do_action("siren_on"),  {"ok": True})[1],
#       "siren_off": lambda ctx: (_do_action("siren_off"), {"ok": True})[1],
#       # ...and so on for every ALLOWED_ACTIONS entry, reusing the exact
#       # same internal functions the existing /api/* routes already call.
#   }
#
# This file deliberately does not build that dict itself, so assistant_ai/
# has no dependency on web/server.py's globals and can be unit-tested on
# its own.
