"""
postprocessing/crypto.py -- Encryption for sensitive data (HearVision AI)
--------------------------------------------------------------------------
Provides authenticated symmetric encryption (Fernet = AES-128-CBC +
HMAC-SHA256) to protect credentials and other sensitive values at rest.

Why Fernet and not just a hash?
  - A hash (e.g. SHA-256) gives INTEGRITY (detects tampering) but not
    confidentiality: it is irreversible, so it cannot be decrypted back
    into the original value.
  - Fernet gives CONFIDENTIALITY: it encrypts the content, and only
    whoever holds the key can read it back.

Key resolution order:
  1. Environment variable HEARVISION_ENC_KEY (url-safe base64 Fernet key, 44 chars)
  2. Local file ~/.hearvision/enc.key (generated automatically, never committed)

To generate a key for production use:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # paste it into .env as HEARVISION_ENC_KEY=...

Token format: encrypted values are prefixed with "enc:v1:" so encrypted and
plain-text values can be told apart (useful for backward compatibility with
older, unencrypted data).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional


class EncryptionUnavailableError(RuntimeError):
    """Raised when strict-mode encryption is requested and no key is
    available. Deliberately does not inherit from a generic, easily
    silenced exception: callers must catch it explicitly, acknowledging
    that they are deciding what happens when encryption isn't available."""
    pass


_PREFIX = "enc:v1:"
_KEY_FILE = Path(os.path.expanduser("~/.hearvision/enc.key"))

# Cache the Fernet instance so the key isn't reloaded on every call.
_fernet = None
_attempted = False


# --- Key loading / generation -------------------------------------------------

def _resolve_key() -> Optional[bytes]:
    """Fetch the Fernet key from the environment variable or local file;
    generate one if neither exists."""
    env = os.getenv("HEARVISION_ENC_KEY", "").strip()
    if env:
        return env.encode()

    if _KEY_FILE.exists():
        content = _KEY_FILE.read_text(encoding="utf-8").strip()
        if content:
            return content.encode()

    # Generate a new key and persist it with restrictive permissions.
    try:
        from cryptography.fernet import Fernet
        new_key = Fernet.generate_key()
        _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _KEY_FILE.write_text(new_key.decode(), encoding="utf-8")
        try:
            os.chmod(_KEY_FILE, 0o600)
        except OSError:
            pass
        print(f"  Encryption key generated at {_KEY_FILE} "
              f"(back it up -- without it, encrypted data cannot be recovered)")
        return new_key
    except Exception as e:
        print(f"  Warning: could not generate an encryption key: {e}")
        return None


def _get_fernet():
    """Return an initialized Fernet instance, or None if encryption is unavailable."""
    global _fernet, _attempted
    if _fernet is not None:
        return _fernet
    if _attempted:
        return None
    _attempted = True
    try:
        from cryptography.fernet import Fernet
        key = _resolve_key()
        if not key:
            return None
        _fernet = Fernet(key)
        return _fernet
    except ImportError:
        print("  Warning: the 'cryptography' package is not installed -- data will "
              "NOT be encrypted. Install with: pip install cryptography")
        return None
    except Exception as e:
        print(f"  Warning: encryption unavailable ({e}) -- data will NOT be encrypted")
        return None


def is_encryption_available() -> bool:
    """True if a valid key is available and the cryptography package is installed."""
    return _get_fernet() is not None


def is_encrypted(value: Any) -> bool:
    """Whether a value is a token encrypted by this module."""
    return isinstance(value, str) and value.startswith(_PREFIX)


# --- Public API ----------------------------------------------------------------

def encrypt_text(text: str, strict: bool = False) -> str:
    """
    Encrypt a string. Returns a token of the form 'enc:v1:<base64>'.

    strict=False (default, backward compatible): if encryption is
    unavailable, returns the plain text as-is -- only acceptable for data
    that is genuinely fine to lose confidentiality on if the key is missing.

    strict=True: raises EncryptionUnavailableError instead of returning
    plain text when no key is available. Use this ALWAYS for secrets,
    credentials, and other sensitive values -- a configuration mistake
    should never silently downgrade protection to "unencrypted".
    """
    if text is None:
        return text
    f = _get_fernet()
    if f is None:
        if strict:
            raise EncryptionUnavailableError(
                "No encryption key is configured (HEARVISION_ENC_KEY or "
                "~/.hearvision/enc.key). For safety, this value will not be "
                "processed unencrypted."
            )
        return text
    token = f.encrypt(text.encode("utf-8")).decode("ascii")
    return _PREFIX + token


def decrypt_text(value: str) -> str:
    """
    Decrypt a token produced by encrypt_text().
    If the value is not encrypted (legacy data), it is returned as-is.
    """
    if not is_encrypted(value):
        return value
    f = _get_fernet()
    if f is None:
        raise RuntimeError(
            "Encrypted data is present but the decryption key could not be loaded. "
            "Set HEARVISION_ENC_KEY or restore ~/.hearvision/enc.key")
    token = value[len(_PREFIX):].encode("ascii")
    return f.decrypt(token).decode("utf-8")


def encrypt_json(obj: Any, strict: bool = False) -> str:
    """Serialize to JSON and encrypt. Useful for storing dicts/lists."""
    return encrypt_text(json.dumps(obj, ensure_ascii=False, sort_keys=True), strict=strict)


def decrypt_json(value: str) -> Any:
    """Inverse of encrypt_json(). Also accepts plain JSON (legacy data)."""
    text = decrypt_text(value)
    return json.loads(text)


def encrypt_bytes(data: bytes, strict: bool = False) -> bytes:
    """Encrypt raw bytes."""
    f = _get_fernet()
    if f is None:
        if strict:
            raise EncryptionUnavailableError("No encryption key is configured.")
        return data
    return f.encrypt(data)


def decrypt_bytes(data: bytes) -> bytes:
    """Decrypt bytes produced by encrypt_bytes()."""
    f = _get_fernet()
    if f is None:
        raise RuntimeError("Encryption unavailable, cannot decrypt bytes")
    return f.decrypt(data)


# --- Self-test -------------------------------------------------------------

if __name__ == "__main__":
    print("=== HearVision AI -- Encryption self-test ===\n")
    print("Encryption available:", is_encryption_available())
    sample = {"example_field": "sample_value", "count": 3}
    token = encrypt_json(sample)
    print("\nEncrypted sample:\n ", token)
    print("\nDecrypted back:\n ", decrypt_json(token))
