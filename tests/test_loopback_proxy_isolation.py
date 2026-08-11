from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PACKAGE_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def load_module(name: str, path: Path) -> Any:
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


HOOK = load_module("proxy_test_hook_runtime", PACKAGE_ROOT / "adapters" / "hook_runtime.py")
HEALTH = load_module("proxy_test_health_check", SCRIPTS / "health_check.py")
REPLAY = load_module("proxy_test_replay_offline_outbox", SCRIPTS / "replay_offline_outbox.py")
TOKEN = "avt_01ARZ3NDEKTSV4RRFFQ69G5FAV_" + ("A" * 48)


class CaptureServer(ThreadingHTTPServer):
    requests: list[dict[str, Any]]
    response_status: int


class CaptureHandler(BaseHTTPRequestHandler):
    server: CaptureServer

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        self.server.requests.append(
            {
                "authorization": self.headers.get("Authorization"),
                "path": self.path,
            }
        )
        response = json.dumps({"id": payload.get("id"), "ok": True}).encode("utf-8")
        self.send_response(self.server.response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format: str, *args: object) -> None:
        return


@contextlib.contextmanager
def capture_server(status: int = 200) -> Iterator[tuple[CaptureServer, str]]:
    server = CaptureServer(("127.0.0.1", 0), CaptureHandler)
    server.requests = []
    server.response_status = status
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@contextlib.contextmanager
def hostile_proxy_environment(proxy_url: str) -> Iterator[None]:
    environment = {
        "http_proxy": proxy_url,
        "HTTP_PROXY": proxy_url,
        "https_proxy": proxy_url,
        "HTTPS_PROXY": proxy_url,
        "no_proxy": "",
        "NO_PROXY": "",
    }
    with patch.dict(os.environ, environment, clear=False), patch.object(
        HOOK.urllib.request,
        "proxy_bypass",
        return_value=False,
    ):
        yield


class LoopbackProxyIsolationTest(unittest.TestCase):
    def test_hook_bearer_bypasses_ambient_proxy_for_http_loopback(self) -> None:
        with capture_server() as (target, target_url), capture_server(502) as (proxy, proxy_url), patch.dict(
            os.environ,
            {"ACCEPTORA_PROXY_TEST_TOKEN": TOKEN},
            clear=False,
        ), hostile_proxy_environment(proxy_url):
            response = HOOK._post_gate(
                {
                    "completion_gate_url": f"{target_url}/gate",
                    "token_env": "ACCEPTORA_PROXY_TEST_TOKEN",
                    "retry_attempts": 1,
                },
                {"probe": True},
            )

        self.assertTrue(response["ok"])
        self.assertEqual([f"Bearer {TOKEN}"], [request["authorization"] for request in target.requests])
        self.assertEqual([], proxy.requests)

    def test_health_bearer_bypasses_ambient_proxy_for_http_loopback(self) -> None:
        with capture_server() as (target, target_url), capture_server(502) as (proxy, proxy_url), hostile_proxy_environment(
            proxy_url
        ):
            settings = HEALTH.Settings(
                mcp_url=f"{target_url}/mcp",
                contract_version_url=f"{target_url}/api/contract-version",
                rest_base_url=f"{target_url}/api/v1/integrations",
                openapi_url=f"{target_url}/api/v1/integrations/openapi.json",
                completion_gate_url=f"{target_url}/api/v1/integrations/completion-gate",
                project_id="proj_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                token_env="ACCEPTORA_AGENT_TOKEN",
                token=TOKEN,
                timeout_seconds=2,
                tls_ca_file=None,
            )
            response, _ = HEALTH.Transport(settings).request(
                f"{target_url}/authenticated",
                method="POST",
                token=TOKEN,
                payload={"id": 7},
                request_id=7,
            )

        self.assertTrue(response["ok"])
        self.assertEqual([f"Bearer {TOKEN}"], [request["authorization"] for request in target.requests])
        self.assertEqual([], proxy.requests)

    def test_replay_bearer_bypasses_ambient_proxy_for_http_loopback(self) -> None:
        with capture_server() as (target, target_url), capture_server(502) as (proxy, proxy_url), hostile_proxy_environment(
            proxy_url
        ):
            response, _ = REPLAY._post_json(
                f"{target_url}/mcp",
                {"jsonrpc": "2.0", "id": 9, "method": "probe"},
                TOKEN,
                2,
                request_id=9,
            )

        self.assertTrue(response["ok"])
        self.assertEqual([f"Bearer {TOKEN}"], [request["authorization"] for request in target.requests])
        self.assertEqual([], proxy.requests)

    def test_https_authenticated_openers_retain_ambient_proxy_support(self) -> None:
        for module in (HOOK, REPLAY):
            with self.subTest(module=module.__name__), patch.object(module.urllib.request, "build_opener") as builder:
                module._authenticated_opener("https://acceptora.example/mcp")
                handlers = builder.call_args.args
                self.assertFalse(any(isinstance(handler, module.urllib.request.ProxyHandler) for handler in handlers))

    def test_all_ipv4_loopback_literals_are_accepted_but_public_http_is_rejected(self) -> None:
        self.assertTrue(HOOK._is_http_loopback_url("http://127.23.45.67:8000/gate"))
        self.assertTrue(HEALTH._is_http_loopback_url("http://[::1]:8000/mcp"))
        self.assertTrue(REPLAY._is_http_loopback_url("http://localhost:8000/mcp"))
        self.assertFalse(HOOK._is_http_loopback_url("http://acceptora.example/gate"))


if __name__ == "__main__":
    unittest.main()
