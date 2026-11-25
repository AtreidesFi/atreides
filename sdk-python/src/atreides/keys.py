"""Local key file creation and authenticated binding."""

import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ._http import ApiError, Refusal, RequestKey, api_origin
from .models import Whoami
from .signing import base64url, decode_base64url

_KEY_FILE_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
_NAME = re.compile(r"[A-Za-z0-9 ._-]{1,40}")


@dataclass(frozen=True, slots=True)
class ApiKeyFile:
    key_id: str | None
    private_key: str = field(repr=False)
    public_key: str
    created_at: str
    api_origin: str

    def __post_init__(self) -> None:
        seed = decode_base64url(self.private_key, 32)
        public = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes_raw()
        if base64url(public) != self.public_key:
            raise ValueError("Public key does not match its local seed")
        if _KEY_FILE_TIME.fullmatch(self.created_at) is None:
            raise ValueError("Key file time must be an RFC 3339 UTC timestamp in milliseconds")
        datetime.fromisoformat(self.created_at)
        api_origin(self.api_origin)
        if self.key_id is not None:
            RequestKey(self.key_id, seed)


def generate_key_pair(origin: str, *, now: datetime | None = None) -> ApiKeyFile:
    key = Ed25519PrivateKey.generate()
    at = now if now is not None else datetime.now(UTC)
    if at.utcoffset() is None:
        raise ValueError("Key creation needs a timezone")
    stamp = at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return ApiKeyFile(
        key_id=None,
        private_key=base64url(key.private_bytes_raw()),
        public_key=base64url(key.public_key().public_bytes_raw()),
        created_at=stamp,
        api_origin=api_origin(origin),
    )


def read_key_file(path: str | Path) -> ApiKeyFile:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, encoding="utf-8") as source:
        if os.name == "posix" and os.fstat(source.fileno()).st_mode & 0o077:
            raise PermissionError("The API key file must have owner-only permissions")
        data: object = json.load(source)
    if not isinstance(data, dict) or type(data.get("v")) is not int or data.get("v") != 1:
        raise ValueError("Unsupported API key file version")
    key_id = data.get("keyId")
    private_key = data.get("privateKey")
    public_key = data.get("publicKey")
    created_at = data.get("createdAt")
    origin = data.get("apiOrigin")
    if (
        "keyId" not in data
        or (key_id is not None and not isinstance(key_id, str))
        or not isinstance(private_key, str)
        or not isinstance(public_key, str)
        or not isinstance(created_at, str)
        or not isinstance(origin, str)
    ):
        raise ValueError("Malformed API key file")
    return ApiKeyFile(key_id, private_key, public_key, created_at, origin)


def write_key_file(path: str | Path, key: ApiKeyFile, *, replace_existing: bool = False) -> None:
    target = Path(path)
    encoded = json.dumps(
        {
            "v": 1,
            "keyId": key.key_id,
            "privateKey": key.private_key,
            "publicKey": key.public_key,
            "createdAt": key.created_at,
            "apiOrigin": key.api_origin,
        },
        indent=2,
    )
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(encoded + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if replace_existing:
            os.replace(temporary, target)
        else:
            os.link(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def bound_key(origin: str, key: RequestKey | ApiKeyFile | None) -> RequestKey | None:
    if isinstance(key, ApiKeyFile):
        if key.key_id is None or api_origin(key.api_origin) != api_origin(origin):
            raise ValueError("The key file must be bound to this API origin")
        return RequestKey(key.key_id, decode_base64url(key.private_key, 32))
    return key


def bind_key_file(path: str | Path, key_text: str, *, transport: httpx.BaseTransport | None = None) -> ApiKeyFile:
    from .client import Client

    before = read_key_file(path)
    if before.key_id is not None and before.key_id != key_text:
        raise ValueError("This key file is already bound to another API key")
    candidate = replace(before, key_id=key_text)
    with Client(candidate.api_origin, candidate, transport=transport) as client:
        answer = client.request("whoami")
    if isinstance(answer, Refusal):
        raise ApiError(answer.message, code=answer.code, request_id=answer.request_id)
    if not isinstance(answer.data, Whoami) or answer.data.principal != "api_key" or answer.data.keyText != key_text:
        raise ApiError("The server did not authenticate this key", request_id=answer.request_id)
    if read_key_file(path) != before:
        raise ValueError("The key file changed while its binding was verified")
    if candidate != before:
        write_key_file(path, candidate, replace_existing=True)
    return candidate


def key_request_link(app_origin: str, key: ApiKeyFile, *, name: str, scopes: Sequence[str]) -> str:
    if _NAME.fullmatch(name) is None or tuple(scopes) not in {("read",), ("read", "trade")}:
        raise ValueError("Expected a key name and read or read,trade scopes")
    params = urlencode({"publicKey": key.public_key, "name": name, "scopes": ",".join(scopes)})
    return f"{api_origin(app_origin)}/api-bots?{params}"
