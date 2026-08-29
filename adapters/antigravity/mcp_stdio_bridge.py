#!/usr/bin/env python3
"""Credential-safe stdio bridge for Antigravity's remote Acceptora MCP server."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
import urllib.error
import urllib.request
from http.client import HTTPMessage
from typing import Any, BinaryIO
from urllib.parse import urlsplit


MAX_MESSAGE_BYTES = 4 * 1024 * 1024
TOKEN_ENV_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")
ACCEPTORA_TOKEN_PATTERN = re.compile(r"^avt_[0-9A-HJKMNP-TV-Z]{26}_[A-Za-z0-9]{48}$")
PROTOCOL_VERSION_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class BridgeError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: BinaryIO,
        code: int,
        message: str,
        headers: HTTPMessage,
        new_url: str,
    ) -> None:
        return None


def _is_loopback(hostname: str) -> bool:
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _validated_server_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (parsed.scheme == "http" and not _is_loopback(parsed.hostname))
    ):
        raise BridgeError("the MCP server URL must be HTTPS or loopback HTTP without credentials, query, or fragment")
    return value


def _opener(server_url: str) -> urllib.request.OpenerDirector:
    handlers: list[Any] = [_NoRedirect()]
    parsed = urlsplit(server_url)
    if parsed.hostname and _is_loopback(parsed.hostname):
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def _json_message(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _parse_json_message(body: bytes, label: str) -> Any:
    if len(body) > MAX_MESSAGE_BYTES:
        raise BridgeError(f"{label} exceeds the 4 MiB limit")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BridgeError(f"{label} is not valid UTF-8 JSON") from error


def _sse_messages(body: bytes) -> list[Any]:
    if len(body) > MAX_MESSAGE_BYTES:
        raise BridgeError("the MCP response exceeds the 4 MiB limit")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BridgeError("the MCP event stream is not valid UTF-8") from error

    messages: list[Any] = []
    data_lines: list[str] = []
    for line in [*text.splitlines(), ""]:
        if line == "":
            if data_lines:
                payload = "\n".join(data_lines)
                if payload != "[DONE]":
                    messages.append(_parse_json_message(payload.encode("utf-8"), "the MCP event payload"))
                data_lines = []
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
    return messages


class StdioHttpBridge:
    def __init__(self, server_url: str, token: str, timeout_seconds: float) -> None:
        self.server_url = _validated_server_url(server_url)
        self.token = token
        self.timeout_seconds = max(0.1, min(timeout_seconds, 120.0))
        self.opener = _opener(self.server_url)
        self.session_id: str | None = None
        self.protocol_version: str | None = None

    def exchange(self, message: Any) -> list[Any]:
        if not isinstance(message, dict):
            raise BridgeError("the MCP stdio message must be a JSON object")
        method = message.get("method")
        if method == "initialize":
            params = message.get("params")
            version = params.get("protocolVersion") if isinstance(params, dict) else None
            if isinstance(version, str) and PROTOCOL_VERSION_PATTERN.fullmatch(version):
                self.protocol_version = version

        headers = {
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "User-Agent": "acceptora-antigravity-bridge/1.0",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version

        request = urllib.request.Request(
            self.server_url,
            data=_json_message(message),
            headers=headers,
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=self.timeout_seconds) as response:
                status = int(response.status)
                returned_session = response.headers.get("Mcp-Session-Id")
                if returned_session:
                    if self.session_id is not None and returned_session != self.session_id:
                        raise BridgeError("the MCP server changed the active session identifier")
                    self.session_id = returned_session
                body = response.read(MAX_MESSAGE_BYTES + 1)
                if len(body) > MAX_MESSAGE_BYTES:
                    raise BridgeError("the MCP response exceeds the 4 MiB limit")
                if status == 202 or not body:
                    return []
                content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        except urllib.error.HTTPError as error:
            try:
                status = error.code
            finally:
                error.close()
            raise BridgeError(f"the MCP server returned HTTP {status}") from error
        except urllib.error.URLError as error:
            raise BridgeError("the MCP server is unavailable") from error
        except TimeoutError as error:
            raise BridgeError("the MCP server request timed out") from error

        if content_type == "text/event-stream":
            return _sse_messages(body)
        if content_type in {"application/json", "application/json-rpc", ""}:
            return [_parse_json_message(body, "the MCP response")]
        raise BridgeError("the MCP server returned an unsupported content type")


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-url", required=True)
    parser.add_argument("--token-env", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = _arguments(argv)
        if TOKEN_ENV_PATTERN.fullmatch(args.token_env) is None:
            raise BridgeError("the MCP credential environment variable name is invalid")
        token = os.environ.get(args.token_env, "")
        if ACCEPTORA_TOKEN_PATTERN.fullmatch(token) is None:
            raise BridgeError("the Acceptora MCP credential is missing or malformed")
        bridge = StdioHttpBridge(args.server_url, token, args.timeout_seconds)

        while True:
            line = sys.stdin.buffer.readline(MAX_MESSAGE_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_MESSAGE_BYTES or not line.endswith(b"\n"):
                raise BridgeError("the MCP stdio message exceeds the 4 MiB line limit")
            message = _parse_json_message(line, "the MCP stdio message")
            for response in bridge.exchange(message):
                sys.stdout.buffer.write(_json_message(response) + b"\n")
                sys.stdout.buffer.flush()
        return 0
    except BridgeError as error:
        sys.stderr.write(f"Acceptora MCP bridge error: {error}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
