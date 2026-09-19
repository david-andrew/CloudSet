"""Signed, stateless links for confirming, managing, and unsubscribing.

A token is ``<payload>.<signature>`` where the payload is URL-safe base64 of
``purpose:subscription_id:email`` and the signature is an HMAC-SHA256 over the
payload with the server secret. Binding the email into the payload means a token
stops working if a subscription id is ever reused by a different address.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass

PURPOSES = {"confirm", "manage", "unsubscribe"}


@dataclass(frozen=True, slots=True)
class TokenPayload:
    purpose: str
    subscription_id: int
    email: str


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _signature(secret: str, payload: str) -> str:
    digest = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return _b64(digest)[:32]


def make_token(secret: str, purpose: str, subscription_id: int, email: str) -> str:
    if purpose not in PURPOSES:
        raise ValueError(f"Unknown token purpose: {purpose}")
    if not secret:
        raise ValueError("A secret key is required to sign tokens")
    payload = _b64(f"{purpose}:{int(subscription_id)}:{email.strip().lower()}".encode())
    return f"{payload}.{_signature(secret, payload)}"


def read_token(secret: str, token: str, purpose: str | None = None) -> TokenPayload | None:
    """Return the payload when the signature is valid, otherwise ``None``."""
    if not secret or not token or token.count(".") != 1:
        return None
    payload, signature = token.split(".", 1)
    if not hmac.compare_digest(signature, _signature(secret, payload)):
        return None
    try:
        text = _unb64(payload).decode()
        found_purpose, raw_id, email = text.split(":", 2)
        subscription_id = int(raw_id)
    except (ValueError, UnicodeDecodeError):
        return None
    if found_purpose not in PURPOSES or (purpose and found_purpose != purpose):
        return None
    return TokenPayload(found_purpose, subscription_id, email)
