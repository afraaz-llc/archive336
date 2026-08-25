"""Fernet symmetric encryption for sensitive at-rest data.

Used to encrypt OAuth tokens before they hit the database. The Fernet key
itself lives in ``ARCHIVE336_FERNET_KEY`` in the env (file mode 600), so a
database dump alone can't decrypt the tokens.

Generate a fresh key with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
from __future__ import annotations

import os
from typing import Optional

from cryptography.fernet import Fernet


_fernet: Optional[Fernet] = None


def _load() -> Fernet:
    global _fernet
    if _fernet is not None:
        return _fernet
    key = os.environ.get("ARCHIVE336_FERNET_KEY")
    if not key:
        raise RuntimeError(
            "ARCHIVE336_FERNET_KEY missing — generate one with "
            "`python -c 'from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())'` and add it to /opt/aether/.env"
        )
    _fernet = Fernet(key.encode("ascii"))
    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypt a string. Returns the base64 ciphertext as a string."""
    return _load().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    """Decrypt a string previously produced by ``encrypt``."""
    return _load().decrypt(ciphertext.encode("ascii")).decode("utf-8")
