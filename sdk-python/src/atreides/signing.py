"""Canonical Ed25519 API requests and wallet grant text."""

import re
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

_BASE64URL = re.compile(r"[A-Za-z0-9_-]+")


def base64url(value: bytes) -> str:
    return urlsafe_b64encode(value).decode("ascii").rstrip("=")


def decode_base64url(text: str, size: int) -> bytes:
    if _BASE64URL.fullmatch(text) is None:
        raise ValueError("Invalid base64url")
    value = urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if len(value) != size or base64url(value) != text:
        raise ValueError("Invalid base64url length or padding")
    return value


@dataclass(frozen=True, slots=True)
class RequestToSign:
    method: str
    target: str
    body: bytes
    timestamp_ms: str
    nonce: str
    key_text: str


def signed_string(request: RequestToSign) -> bytes:
    """Sign the target and body bytes exactly as sent, including query order."""
    return "\n".join(
        (
            "api-req/v1",
            request.method.upper(),
            request.target,
            sha256(request.body).hexdigest(),
            request.timestamp_ms,
            request.nonce,
            request.key_text,
        )
    ).encode("utf-8")


def sign_request(request: RequestToSign, seed: bytes) -> str:
    key = Ed25519PrivateKey.from_private_bytes(seed)
    return base64url(key.sign(signed_string(request)))


def verify_request(request: RequestToSign, signature: str, public_key: bytes) -> bool:
    if len(signature) != 86 or len(public_key) != 32:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(decode_base64url(signature, 64), signed_string(request))
    except (ValueError, InvalidSignature):
        return False
    return True


def grant_message(
    *,
    origin: str,
    key_text: str,
    public_key: str,
    accounts: Sequence[tuple[str, str]],
    ip_allowlist: Sequence[str],
    expires_at: str,
    nonce: str,
    issued_at: str,
) -> str:
    """Build the exact 03 §5.5 text signed by the wallet."""
    # JavaScript sorts strings by UTF-16 code unit. Match that order for grant vectors across languages.
    ordered_accounts = sorted(accounts, key=lambda row: (row[0].encode("utf-16-be"), row[1].encode("utf-16-be")))
    account_text = ", ".join(f"{venue}:{ref}" for venue, ref in ordered_accounts)
    allowlist = " ".join(sorted(ip_allowlist, key=lambda part: part.encode("utf-16-be"))) or "any"
    return "\n".join(
        (
            "api-key-grant/v1",
            f"Origin: {origin}",
            f"Key: {key_text}",
            f"Public key: {public_key}",
            "Scopes: read trade",
            f"Accounts: {account_text}",
            f"IP allowlist: {allowlist}",
            f"Expires: {expires_at}",
            f"Nonce: {nonce}",
            f"Issued: {issued_at}",
        )
    )
