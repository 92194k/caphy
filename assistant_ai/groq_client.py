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
router falls back to assistant_ai/offline_fallback.py's local matcher
rather than surfacing a bare error - Groq is an enhancement for open-ended
talk/know handling, not a hard dependency for direct commands.
"""

import requests

from secrets_config import get_groq_api_keys

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "llama-3.1-8b-instant"
REQUEST_TIMEOUT_SECONDS = 8


class GroqAllKeysFailedError(Exception):
    """Raised when every configured Groq key failed (rate-limited, invalid,
    or Groq itself unreachable). Callers should treat this the same as
    'Groq is unavailable right now' and fall back to Ollama."""
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

    for key in keys:
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
        except requests.RequestException as exc:
            # Network-level failure (timeout, DNS, connection refused) -
            # not key-specific, but still worth trying the next key in
            # case this is a transient blip rather than "Groq is down".
            last_error = exc
            continue

        if response.status_code == 200:
            data = response.json()
            return data["choices"][0]["message"]["content"]

        if _is_retryable_status(response.status_code):
            last_error = f"HTTP {response.status_code} from Groq (key ending ...{key[-4:]})"
            continue

        # Non-retryable error (bad request, model not found, etc.) - no
        # point trying other keys, this will fail the same way for all of them.
        raise GroqAllKeysFailedError(
            f"Groq returned a non-retryable error: HTTP {response.status_code} - {response.text[:200]}"
        )

    raise GroqAllKeysFailedError(
        f"All {len(keys)} Groq key(s) failed. Last error: {last_error}"
    )
