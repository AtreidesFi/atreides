"""Local atreides key and API commands."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from time import time_ns

from pydantic import BaseModel

from ._http import ApiError, Refusal
from .aclient import AsyncClient
from .client import Client
from .keys import ApiKeyFile, bind_key_file, generate_key_pair, key_request_link, read_key_file, write_key_file
from .models import Whoami

_API_ORIGIN = "https://api.atreides.finance"
_APP_ORIGIN = "https://app.atreides.finance"


def _configured_key_file() -> str | None:
    return os.environ.get("ATREIDES_KEY_FILE")


def _file(path: Path | None) -> Path:
    if path is not None:
        return path
    configured_file = _configured_key_file()
    if configured_file is not None:
        return Path(configured_file)
    base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "atreides" / "key.json"


def _encode(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError("Unsupported JSON value")


def _out(value: object) -> None:
    print(json.dumps(value, default=_encode, separators=(",", ":")), flush=True)


def _whoami(client: Client, key: ApiKeyFile) -> Whoami:
    answer = client.request("whoami")
    if isinstance(answer, Refusal):
        raise ApiError(answer.message, code=answer.code, request_id=answer.request_id)
    if not isinstance(answer.data, Whoami) or answer.data.principal != "api_key" or answer.data.keyText != key.key_id:
        raise ApiError("The API did not authenticate this key")
    return answer.data


def _keys(args: argparse.Namespace) -> None:
    path = _file(args.file)
    if args.key_command == "create":
        key = generate_key_pair(
            args.origin or os.environ.get("ATREIDES_API_ORIGIN") or _API_ORIGIN
        )
        link = key_request_link(
            os.environ.get("ATREIDES_APP_ORIGIN") or _APP_ORIGIN,
            key,
            name=args.name,
            scopes=args.scopes.split(","),
        )
        if args.file is None and _configured_key_file() is None:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_key_file(path, key)
        print(link)
        return
    if args.key_command == "bind":
        try:
            bind_key_file(path, args.key_text)
        except ApiError as error:
            if error.code == "signature_invalid":
                raise ValueError("The key text does not belong to this key") from error
            raise
        print("Key bound")
        return
    key = read_key_file(path)
    if key.key_id != args.key_text:
        raise ValueError("The key text is not the one bound to this file")
    with Client(key.api_origin, key) as client:
        answer = _whoami(client, key)
    _out(
        {
            "publicKey": key.public_key,
            "scopes": answer.scopes,
            "accounts": answer.accounts,
            "env": answer.env,
            "state": None,
        }
    )


async def _stream(key: ApiKeyFile, channels: str, since: str | None) -> None:
    names = channels.split(",")
    if not all(names) or (since is not None and (not since.isascii() or not since.isdecimal())):
        raise ValueError("Expected a channel list and a decimal stream sequence")
    async with AsyncClient(key.api_origin, key) as client:
        handle = client.stream(
            names,
            since=since,
            on_event=lambda event: _out({"type": "event", "channel": "user", **event}),
            on_reset=lambda latest: _out({"type": "reset", "channel": "user", "latestSeq": latest}),
            on_market=lambda channel, data: _out(
                {"type": "broadcast" if channel == "venues" else "market", "channel": channel, "data": data}
            ),
        )
        await handle.wait()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atreides")
    groups = parser.add_subparsers(dest="command", required=True)
    keys = groups.add_parser("keys")
    operations = keys.add_subparsers(dest="key_command", required=True)
    create = operations.add_parser("create")
    create.add_argument("--scopes", choices=("read", "read,trade"), required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--origin")
    for name in ("bind", "show"):
        operation = operations.add_parser(name)
        operation.add_argument("key_text")
        operation.add_argument("--file", type=Path)
    create.add_argument("--file", type=Path)
    ping = groups.add_parser("ping")
    stream = groups.add_parser("stream")
    stream.add_argument("--since")
    stream.add_argument("--channels", default="user,venues")
    fees = groups.add_parser("fees")
    fees.add_argument("--venue")
    for command in (ping, stream, fees):
        command.add_argument("--file", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "keys":
            _keys(args)
            return 0
        key = read_key_file(_file(args.file))
        if args.command == "stream":
            asyncio.run(_stream(key, args.channels, args.since))
            return 0
        with Client(key.api_origin, key) as client:
            if args.command == "ping":
                before = time_ns() // 1_000_000
                answer = _whoami(client, key)
                midpoint = (before + time_ns() // 1_000_000) // 2
                _out({"skewMs": int(answer.serverTime.root.timestamp() * 1000) - midpoint})
            else:
                reply = client.request("getFees", query=None if args.venue is None else {"venue": args.venue})
                if isinstance(reply, Refusal):
                    raise ApiError(reply.message, code=reply.code, request_id=reply.request_id)
                _out(reply.data)
        return 0
    except KeyboardInterrupt:
        return 0
    except (ApiError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
