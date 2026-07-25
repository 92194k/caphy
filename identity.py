"""
identity.py — CAPHY device identity.

Generates and persists a permanent, unguessable device ID + secret for this
laptop on first run. Every later run reuses the same identity — it is never
regenerated. This is the foundation everything else (accounts, pairing,
Firebase Auth, per-device alerts) attaches to.

Storage location:
    Windows: %APPDATA%\\CAPHY\\device.json
    Mac/Linux: ~/.config/CAPHY/device.json

Usage:
    from identity import get_device_identity
    identity = get_device_identity()
    print(identity["device_id"])
"""

import json
import os
import secrets
import socket
from datetime import datetime, timezone
from pathlib import Path

FILE_VERSION = 1
FILENAME = "device.json"


def _get_config_dir() -> Path:
    """Return the OS-appropriate CAPHY config directory (created if missing)."""
    appdata = os.getenv("APPDATA")
    if appdata:
        config_dir = Path(appdata) / "CAPHY"
    else:
        # Mac/Linux fallback
        config_dir = Path.home() / ".config" / "CAPHY"

    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def _get_device_file() -> Path:
    return _get_config_dir() / FILENAME


def _generate_identity() -> dict:
    """Create a brand-new device identity. Only called once per machine."""
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = "unknown-host"

    return {
        "device_id": "caphy_" + secrets.token_hex(16),
        "device_secret": secrets.token_hex(32),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "hostname": hostname,
        "version": FILE_VERSION,
    }


def get_device_identity() -> dict:
    """
    Load this laptop's permanent CAPHY identity, generating it on first run.

    Returns a dict with: device_id, device_secret, created_at, hostname, version.
    Safe to call repeatedly — after the first run it always returns the same
    device_id/device_secret.
    """
    device_file = _get_device_file()

    if device_file.exists():
        try:
            with open(device_file, "r", encoding="utf-8") as f:
                identity = json.load(f)
            # Basic sanity check — if the file is corrupt/incomplete, regenerate.
            if "device_id" in identity and "device_secret" in identity:
                return identity
        except (json.JSONDecodeError, OSError):
            pass  # fall through and regenerate below

    # No valid identity yet — create one and persist it.
    identity = _generate_identity()
    with open(device_file, "w", encoding="utf-8") as f:
        json.dump(identity, f, indent=2)

    # Lock down permissions where the OS supports it (best-effort, ignored on Windows).
    try:
        os.chmod(device_file, 0o600)
    except (OSError, NotImplementedError):
        pass

    return identity


if __name__ == "__main__":
    # Quick manual check: `python identity.py`
    ident = get_device_identity()
    print(f"Device file: {_get_device_file()}")
    print(f"device_id:   {ident['device_id']}")
    print(f"hostname:    {ident['hostname']}")
    print(f"created_at:  {ident['created_at']}")
    print("device_secret: (hidden — 64 hex chars)")
