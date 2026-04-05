"""
crypto.py — At-rest encryption for connection credentials.

Uses Fernet (AES-128-CBC + HMAC-SHA256) from the cryptography package.
A Fernet key is derived from the CREDENTIALS_KEY env var using SHA-256.

Behaviour matrix
─────────────────────────────────────────────────────────────────────────────
  CREDENTIALS_KEY set? │ Value looks encrypted? │ Result
  ─────────────────────┼────────────────────────┼──────────────────────────
  No                   │ No  (plaintext)         │ Pass-through (no change)
  No                   │ Yes (Fernet token)      │ Returned as-is (can't decrypt)
  Yes                  │ No  (plaintext)         │ Encrypted on write; decrypts fine
  Yes                  │ Yes (Fernet token)      │ Decrypted on read
─────────────────────────────────────────────────────────────────────────────

Migration
─────────────────────────────────────────────────────────────────────────────
  If CREDENTIALS_KEY is set on first startup after being absent, all stored
  plain-text secrets are automatically encrypted by
  db.migrate_credentials_encryption().  The operation is idempotent —
  values already encrypted are detected by the 'gAAAAA' Fernet prefix and
  are left unchanged.
─────────────────────────────────────────────────────────────────────────────

Secret fields (shared with routers/connections.py)
─────────────────────────────────────────────────────────────────────────────
  SECRET_FIELDS = {"password", "client_secret", "token"}
"""
import base64
import hashlib
import logging
from typing import Optional

log = logging.getLogger(__name__)

# Fields within a connection config dict that contain credentials.
SECRET_FIELDS: frozenset[str] = frozenset({"password", "client_secret", "token"})

_fernet = None  # lazy-initialised singleton


def _get_fernet():
    """Return a Fernet instance keyed from CREDENTIALS_KEY, or None if unset."""
    global _fernet
    if _fernet is not None:
        return _fernet
    # Import here to avoid circular imports at module load time.
    import config
    key_str = config.CREDENTIALS_KEY
    if not key_str:
        return None
    from cryptography.fernet import Fernet
    # Derive a 32-byte Fernet key from the passphrase using SHA-256.
    # CREDENTIALS_KEY should be a strong random value (e.g. a UUID4 or 32 random
    # bytes encoded as hex).  SHA-256 is used as a key-derivation convenience —
    # for human-memorable passphrases use a proper KDF (argon2 / PBKDF2).
    raw_key = hashlib.sha256(key_str.encode()).digest()
    _fernet = Fernet(base64.urlsafe_b64encode(raw_key))
    return _fernet


def _is_encrypted(value: str) -> bool:
    """Fernet tokens are base64url strings starting with 'gAAAAA'."""
    return isinstance(value, str) and value.startswith("gAAAAA")


def encrypt_secret(plaintext: str) -> str:
    """
    Encrypt *plaintext* with Fernet if a key is configured.
    Returns *plaintext* unchanged when no key is set (pass-through mode).
    Values that already look encrypted are returned unchanged.
    """
    if not plaintext:
        return plaintext
    if _is_encrypted(plaintext):
        return plaintext  # already encrypted
    f = _get_fernet()
    if f is None:
        return plaintext
    return f.encrypt(plaintext.encode()).decode()


def decrypt_secret(value: str) -> str:
    """
    Decrypt *value* if it is a Fernet token and a key is configured.
    Returns *value* unchanged when no key is set or the value is plaintext.
    """
    if not value:
        return value
    if not _is_encrypted(value):
        return value
    f = _get_fernet()
    if f is None:
        # Encrypted value but no key — can't decrypt.  Return as-is so the
        # caller receives a clearly wrong value rather than silently connecting
        # with an empty credential.
        log.warning("Encrypted credential found but CREDENTIALS_KEY is not set.")
        return value
    try:
        return f.decrypt(value.encode()).decode()
    except Exception as exc:
        log.warning("Failed to decrypt secret (%s) — returning raw value.", exc)
        return value


def encrypt_config(cfg: dict) -> dict:
    """Return a copy of *cfg* with all SECRET_FIELDS values encrypted."""
    return {
        k: (encrypt_secret(v) if k in SECRET_FIELDS and isinstance(v, str) else v)
        for k, v in cfg.items()
    }


def decrypt_config(cfg: dict) -> dict:
    """Return a copy of *cfg* with all SECRET_FIELDS values decrypted."""
    return {
        k: (decrypt_secret(v) if k in SECRET_FIELDS and isinstance(v, str) else v)
        for k, v in cfg.items()
    }
