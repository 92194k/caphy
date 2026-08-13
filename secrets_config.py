"""
secrets_config.py — loads API keys that must never be committed to git.

Two sources, in priority order:
  1. Environment variables (CAPHY_METERED_DOMAIN, CAPHY_METERED_API_KEY) -
     preferred for any real deployment, CI, or shared machine.
  2. caphy_keys.json in the repo root - convenient for local dev, already
     listed in .gitignore ("API keys (never commit)") so it never
     actually reaches the repository even though it lives next to the
     code.

This file itself contains no secrets - it only knows how to find them.
Safe to commit.
"""

import os
import json

_KEYS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "caphy_keys.json")

_cache = None


def _load_keys_file():
    global _cache
    if _cache is not None:
        return _cache
    if os.path.exists(_KEYS_FILE):
        try:
            with open(_KEYS_FILE, "r", encoding="utf-8") as f:
                _cache = json.load(f)
                return _cache
        except Exception:
            pass
    _cache = {}
    return _cache


def get_metered_domain():
    """The 'appname' part of https://<domain>/api/v1/turn/credentials -
    e.g. 'caphy.metered.live'."""
    env = os.environ.get("CAPHY_METERED_DOMAIN")
    if env:
        return env
    return _load_keys_file().get("metered_domain", "")


def get_metered_api_key():
    """Metered.ca TURN REST API key. Never log or print this value."""
    env = os.environ.get("CAPHY_METERED_API_KEY")
    if env:
        return env
    return _load_keys_file().get("metered_api_key", "")


def get_groq_api_key():
    """Groq API key - primary cloud AI fallback for voice command
    interpretation when the offline exact-phrase match in voice/intents.json
    doesn't find anything. Free tier: ~14,400 requests/day for
    llama-3.1-8b-instant, no card required. Never log or print this value."""
    env = os.environ.get("CAPHY_GROQ_API_KEY")
    if env:
        return env
    return _load_keys_file().get("groq_api_key", "")


def get_groq_api_keys():
    """List of Groq API keys for automatic failover (voice assistant v2).
    Tried in order; if one is rate-limited/invalid, the caller rotates to
    the next. Falls back to the single-key field if the list isn't set, so
    old code/config keeps working. Never log or print these values.

    Env override: CAPHY_GROQ_API_KEYS as a comma-separated string."""
    env = os.environ.get("CAPHY_GROQ_API_KEYS")
    if env:
        return [k.strip() for k in env.split(",") if k.strip()]
    keys = _load_keys_file().get("groq_api_keys")
    if keys:
        return [k for k in keys if k]
    single = get_groq_api_key()
    return [single] if single else []


def get_gemini_api_key():
    """Google Gemini API key - second cloud AI fallback for voice command
    interpretation, tried after Groq is exhausted/unavailable. Free tier:
    ~1,500 requests/day for Gemini Flash, no card required. Never log or
    print this value."""
    env = os.environ.get("CAPHY_GEMINI_API_KEY")
    if env:
        return env
    return _load_keys_file().get("gemini_api_key", "")


def get_smtp_email():
    """Gmail address CAPHY sends password-reset PIN emails from (SMTP
    'From' address and login username). Never log or print this value
    alongside the app password."""
    env = os.environ.get("CAPHY_SMTP_EMAIL")
    if env:
        return env
    return _load_keys_file().get("smtp_email", "")


def get_smtp_app_password():
    """Gmail App Password (16-char code from
    myaccount.google.com/apppasswords) used to authenticate SMTP sends for
    the forgot-password PIN flow. This is NOT the Gmail account password -
    it only works for SMTP/IMAP and can be revoked independently. Never log
    or print this value."""
    env = os.environ.get("CAPHY_SMTP_APP_PASSWORD")
    if env:
        return env
    return _load_keys_file().get("smtp_app_password", "")


def get_google_oauth_client_secret():
    """
    Client secret for the Web OAuth client (client_type 3 in
    google-services.json, client_id ending in ...27gd8r834sf...) - needed
    for the desktop app's browser-based Google Sign-In flow (see
    web/server.py's /auth/google/start and /auth/google/callback).

    Only Web-type OAuth clients have a secret (Android clients don't),
    and it's required to exchange an authorization code for tokens via
    Google's server-to-server token endpoint. Never log or print this
    value - get it from Google Cloud Console -> APIs & Services ->
    Credentials -> the OAuth 2.0 Client ID -> Client secret.
    """
    env = os.environ.get("CAPHY_GOOGLE_CLIENT_SECRET")
    if env:
        return env
    return _load_keys_file().get("google_oauth_client_secret", "")
