"""
groq_client.py - thin Groq chat-completion wrapper with multi-key failover.

Part of the CAPHY Voice Assistant v2 rebuild. This module does ONE job:
send a prompt to Groq and get text back, rotating through every configured
API key if one is rate-limited/invalid, so a single exhausted key never
surfaces as a user-visible failure.

This file does NOT know about intents, actions, or CAPHY's features - that
belongs in the router (assistant_ai/router.py), which imports this module
and is wired to POST /api/assistant in web/server.py. Keeping this file
single-purpose means it can be tested and trusted in isolation.

If every configured key fails (see GroqAllKeysFailedError below), the
router returns a plain error response (see router.py) - voice control
requires Groq/internet; CAPHY's other offline capabilities (detection,
recording, siren/arm/disarm via the app buttons or dashboard) are
unaffected and need no internet at all.
"""

import requests

from secrets_config import get_groq_api_keys

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
# llama-3.1-8b-instant was DECOMMISSIONED by Groq on 2026-08-16 (see
# https://console.groq.com/docs/deprecations) - every single request made
# after that date failed with a non-retryable error on every key (not a
# key problem, a model problem), which is exactly why the voice assistant
# silently fell back to offline mode even with 5 valid Groq keys
# configured and the laptop fully online. Groq's own recommended
# replacement is openai/gpt-oss-20b - fast, cheap, and on the free tier.
# If Groq deprecates this one too in the future, check
# console.groq.com/docs/deprecations and console.groq.com/docs/models for
# the current replacement rather than guessing.
DEFAULT_MODEL = "openai/gpt-oss-20b"
REQUEST_TIMEOUT_SECONDS = 8


class GroqAllKeysFailedError(Exception):
    """Raised when every configured Groq key failed (rate-limited, invalid,
    or Groq itself unreachable). Callers should treat this the same as
    'Groq is unavailable right now' - router.py returns a plain error
    response for voice control in that case (an Ollama-based local
    fallback plus keyword-matcher last resort existed briefly and was
    removed 2026-08-31 to simplify scope before the thesis defense).
    There is no other cloud AI configured (Gemini was considered and
    removed; never implemented)."""
    pass


def _is_retryable_status(status_code):
    # 429 = rate limited, 401/403 = bad/revoked key -> both mean "try the
    # next key". Anything else (e.g. 400 bad request) is a real error that
    # rotating keys won't fix.
    return status_code in (401, 403, 429)


def ask_groq(messages, model=DEFAULT_MODEL, temperature=0.4, max_tokens=512):
    """Send a chat-style prompt to Groq, rotating through all configured
    keys on rate-limit/auth failures.

    messages: list of {"role": "system"|"user"|"assistant", "content": str}
    Returns: the assistant's reply text (str).
    Raises: GroqAllKeysFailedError if every key failed or none are configured.
    """
    keys = get_groq_api_keys()
    if not keys:
        raise GroqAllKeysFailedError("No Groq API keys are configured.")

    last_error = None

    for i, key in enumerate(keys):
        try:
            response = requests.post(
                GROQ_URL,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            # No route to api.groq.com at all (no internet, DNS failure,
            # connection refused/timed out) - this is NOT a per-key
            # problem, every key hits the exact same wall. Retrying the
            # remaining keys here used to mean waiting out
            # REQUEST_TIMEOUT_SECONDS * len(keys) (up to 40s with 5 keys)
            # before the voice assistant ever fell back to the offline
            # keyword matcher - a bad wait for something that will
            # obviously fail again. Fail fast to the caller instead so
            # router.py's plain error response takes over immediately.
            print(f"[CAPHY Voice] Groq unreachable (key {i+1}/{len(keys)}, ...{key[-4:]}): {exc}"
                  f" - network appears down, skipping remaining keys")
            raise GroqAllKeysFailedError(
                f"Groq unreachable (network-level failure on key {i+1}/{len(keys)}): {exc}"
            ) from exc
        except requests.RequestException as exc:
            # Some other request-library failure not covered above -
            # still worth trying the next key in case it's specific to
            # this one request rather than the network as a whole.
            print(f"[CAPHY Voice] Groq request failed (key {i+1}/{len(keys)}, ...{key[-4:]}): {exc}")
            last_error = exc
            continue

        if response.status_code == 200:
            data = response.json()
            return data["choices"][0]["message"]["content"]

        if _is_retryable_status(response.status_code):
            print(f"[CAPHY Voice] Groq key {i+1}/{len(keys)} (...{key[-4:]}) returned "
                  f"HTTP {response.status_code} - trying next key")
            last_error = f"HTTP {response.status_code} from Groq (key ending ...{key[-4:]})"
            continue

        # Non-retryable error (bad request, model not found, deprecated
        # model, etc.) - no point trying other keys, this will fail the
        # same way for all of them. This branch used to raise silently -
        # with zero console output - which is exactly what made a bad/
        # deprecated MODEL name (not a bad key) invisible: every single
        # request would die here on the FIRST key tried, never reaching
        # the retry-loop's print statements at all, and the router would
        # jump straight to the offline fallback with nothing in the
        # server log to explain why.
        print(f"[CAPHY Voice] Groq non-retryable error (key {i+1}/{len(keys)}, ...{key[-4:]}): "
              f"HTTP {response.status_code} - {response.text[:300]}")
        raise GroqAllKeysFailedError(
            f"Groq returned a non-retryable error: HTTP {response.status_code} - {response.text[:200]}"
        )

    raise GroqAllKeysFailedError(
        f"All {len(keys)} Groq key(s) failed. Last error: {last_error}"
    )
