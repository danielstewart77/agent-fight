"""Password hashing.

bcrypt with per-hash salt. `verify_password` is constant-time (bcrypt's own
comparison). bcrypt only considers the first 72 bytes of input, so we reject
anything longer at the boundary rather than silently truncating — a silently
truncated password would let a shorter prefix authenticate.
"""

from __future__ import annotations

import bcrypt

# bcrypt hashes only the first 72 bytes of the password.
MAX_PASSWORD_BYTES = 72


class PasswordTooLong(ValueError):
    """Raised when a password exceeds bcrypt's 72-byte input limit."""


def _check_length(plain: str) -> bytes:
    encoded = plain.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise PasswordTooLong(
            f"password must be at most {MAX_PASSWORD_BYTES} bytes (got {len(encoded)})"
        )
    return encoded


def hash_password(plain: str) -> str:
    """Return a bcrypt hash string safe to store in the DB."""
    return bcrypt.hashpw(_check_length(plain), bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    """Constant-time check of `plain` against a stored bcrypt hash.

    Returns False (rather than raising) for an over-long password or a
    malformed stored hash, so callers can treat it as a plain auth failure.
    """
    try:
        encoded = _check_length(plain)
    except PasswordTooLong:
        return False
    try:
        return bcrypt.checkpw(encoded, hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False
