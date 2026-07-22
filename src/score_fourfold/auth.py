from __future__ import annotations

import hashlib
import hmac
import secrets


SCRYPT_N = 16_384
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_SALT_BYTES = 16
SCRYPT_DK_BYTES = 32
MIN_PASSWORD_LENGTH = 16


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Return a fixed-parameter scrypt password hash safe to store in dotenv."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must contain at least {MIN_PASSWORD_LENGTH} characters")
    if "\x00" in password:
        raise ValueError("password must not contain NUL characters")
    actual_salt = salt if salt is not None else secrets.token_bytes(SCRYPT_SALT_BYTES)
    if len(actual_salt) != SCRYPT_SALT_BYTES:
        raise ValueError(f"salt must contain exactly {SCRYPT_SALT_BYTES} bytes")
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=actual_salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DK_BYTES,
    )
    return (
        f"scrypt:{SCRYPT_N}:{SCRYPT_R}:{SCRYPT_P}:"
        f"{actual_salt.hex()}:{derived.hex()}"
    )


def valid_password_hash(encoded: str) -> bool:
    try:
        scheme, n_text, r_text, p_text, salt_hex, derived_hex = encoded.split(":")
        if scheme != "scrypt":
            return False
        if (int(n_text), int(r_text), int(p_text)) != (SCRYPT_N, SCRYPT_R, SCRYPT_P):
            return False
        salt = bytes.fromhex(salt_hex)
        derived = bytes.fromhex(derived_hex)
    except (TypeError, ValueError):
        return False
    return len(salt) == SCRYPT_SALT_BYTES and len(derived) == SCRYPT_DK_BYTES


def verify_password(password: str, encoded: str) -> bool:
    """Verify a password without accepting attacker-controlled scrypt parameters."""
    if not valid_password_hash(encoded):
        return False
    _, _, _, _, salt_hex, derived_hex = encoded.split(":")
    try:
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            dklen=SCRYPT_DK_BYTES,
        )
    except (UnicodeError, ValueError):
        return False
    return hmac.compare_digest(actual, bytes.fromhex(derived_hex))
