"""
End-to-end smoke test for U1 unified mode.

Starts ``start-re-manager`` as a subprocess with ``--http-port=<random>``
so the manager additionally serves the bluesky-httpserver FastAPI app on
that port. Confirms (a) the 0MQ manager comes up as usual, (b) uvicorn
actually binds the requested TCP port, (c) ``GET /api/status`` returns
200 with the expected manager-status shape, proving an HTTP→0MQ→handler
loopback round-trip inside the same process.

Anonymous HTTP access is enabled via ``QSERVER_HTTP_SERVER_ALLOW_ANONYMOUS_ACCESS``
so the test does not need to mint an API key.
"""

from __future__ import annotations

import socket
import time
from contextlib import contextmanager

import httpx
import pytest

from bluesky_queueserver.manager.http_server import InProcessREManagerAPI
from bluesky_queueserver.manager.tests.common import (
    ReManager,
    condition_manager_idle,
    wait_for_condition,
)


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _poll_http(url: str, *, timeout: float) -> httpx.Response:
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=2.0)
            if response.status_code < 500:
                return response
        except httpx.HTTPError as exc:
            last_exc = exc
        time.sleep(0.2)
    raise TimeoutError(
        f"HTTP endpoint {url} did not return a response within {timeout:.1f}s"
        + (f" (last error: {last_exc})" if last_exc else "")
    )


@contextmanager
def _started_manager(params):
    re = ReManager(params=params)
    failed_to_start = False
    try:
        if not wait_for_condition(time=10, condition=condition_manager_idle):
            failed_to_start = True
            re.kill_manager()
            raise TimeoutError("Timeout: RE Manager failed to start.")
        yield re
    finally:
        if not failed_to_start:
            re.stop_manager()
        else:
            re.kill_manager()


def test_http_port_activates_unified_mode_with_inprocess_dispatch(monkeypatch, tmp_path):
    """--http-port alone enables unified mode; /api/status and /api/ping
    round-trip through HTTP → manager._dispatch_command → handler; and
    stderr confirms the in-process client was injected (not a ZMQ
    fallback). Only status/ping are exercised because anonymous access
    is scoped to read:status — other endpoints would require minting
    an API key."""
    monkeypatch.setenv("QSERVER_HTTP_SERVER_ALLOW_ANONYMOUS_ACCESS", "1")
    http_port = _free_tcp_port()

    stderr_path = tmp_path / "manager_stderr.log"
    with open(stderr_path, "w") as stderr_fp:
        re = ReManager(params=["--http-port", str(http_port)], stderr=stderr_fp)
        failed_to_start = False
        try:
            if not wait_for_condition(time=10, condition=condition_manager_idle):
                failed_to_start = True
                re.kill_manager()
                raise TimeoutError("Timeout: RE Manager failed to start.")

            base = f"http://127.0.0.1:{http_port}/api"
            _poll_http(f"{base}/status", timeout=15.0)

            for path in ("/status", "/ping"):
                response = httpx.get(f"{base}{path}", timeout=5.0)
                assert response.status_code == 200, (path, response.text)
                body = response.json()
                assert body.get("manager_state") == "idle", (path, body)
                assert body.get("worker_environment_exists") is False, (path, body)
        finally:
            if not failed_to_start:
                re.stop_manager()
            else:
                re.kill_manager()

    stderr = stderr_path.read_text()
    assert "Using injected REManagerAPI client" in stderr, (
        "unified mode did not wire the in-process client; stderr tail:\n"
        + "\n".join(stderr.splitlines()[-40:])
    )
    # The ZMQ-fallback log must NOT have fired — its presence would mean
    # httpserver ignored the injected RM and built a fresh client.
    assert "Connecting to RE Manager" not in stderr, (
        "in-process client was injected but httpserver still built a ZMQ client"
    )


def test_no_http_port_leaves_legacy_behavior():
    """Absent any HTTP flag, the manager must not bind an HTTP port — the
    split-process deployment is byte-identical to today."""
    http_port = _free_tcp_port()

    with _started_manager(params=None):
        with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):
            httpx.get(f"http://127.0.0.1:{http_port}/api/status", timeout=1.0)


class _FakeManager:
    """Minimal stand-in for RunEngineManager with a _dispatch_command that
    mirrors the real one (duplicated rather than imported to keep this
    unit test free of manager.py's multiprocessing-heavy import chain)."""

    def __init__(self):
        async def status_handler(manager, params):
            return {"success": True, "msg": "", "manager_state": "idle", "params": params}

        async def failing_handler(manager, params):
            raise RuntimeError("boom")

        self._command_handlers = {"status": status_handler, "boom": failing_handler}

    async def _dispatch_command(self, method, params):
        try:
            handler = self._command_handlers[method]
        except KeyError:
            return {"success": False, "msg": f"Unknown method {method!r}"}
        try:
            return await handler(self, params)
        except Exception as ex:
            return {"success": False, "msg": str(ex)}


@pytest.mark.asyncio
async def test_inprocess_client_delegates_to_dispatch_command():
    manager = _FakeManager()
    rm = InProcessREManagerAPI(
        manager=manager,
        zmq_info_addr="tcp://127.0.0.1:2",
        zmq_encoding="json",
        request_fail_exceptions=False,
    )

    response = await rm.send_request(method="status", params={"k": "v"})
    assert response["success"] is True
    assert response["params"] == {"k": "v"}
    assert rm._inprocess_request_count == 1

    unknown = await rm.send_request(method="does_not_exist", params={})
    assert unknown["success"] is False
    assert "Unknown method" in unknown["msg"]
    assert rm._inprocess_request_count == 2

    failed = await rm.send_request(method="boom", params={})
    assert failed["success"] is False
    assert failed["msg"] == "boom"
    assert rm._inprocess_request_count == 3
