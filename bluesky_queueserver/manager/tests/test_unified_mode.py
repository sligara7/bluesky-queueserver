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


def test_http_port_activates_unified_mode(monkeypatch):
    """--http-port alone is enough to enable unified mode; /api/status
    round-trips through HTTP → loopback 0MQ → manager handler."""
    monkeypatch.setenv("QSERVER_HTTP_SERVER_ALLOW_ANONYMOUS_ACCESS", "1")
    http_port = _free_tcp_port()

    with _started_manager(["--http-port", str(http_port)]):
        response = _poll_http(f"http://127.0.0.1:{http_port}/api/status", timeout=15.0)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["manager_state"] == "idle"
    assert body["worker_environment_exists"] is False


def test_no_http_port_leaves_legacy_behavior():
    """Absent any HTTP flag, the manager must not bind an HTTP port — the
    split-process deployment is byte-identical to today."""
    http_port = _free_tcp_port()

    with _started_manager(params=None):
        with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):
            httpx.get(f"http://127.0.0.1:{http_port}/api/status", timeout=1.0)


# ----- U2: in-process loopback --------------------------------------------


def test_unified_mode_injects_in_process_client(monkeypatch, tmp_path):
    """Multiple endpoints round-trip through the in-process dispatcher and
    stderr shows the U2 injection log at startup (no 0MQ-client fallback).

    Only ``/api/status`` and ``/api/ping`` are exercised here because
    anonymous access is limited to the ``read:status`` scope — hitting
    ``/api/queue/get`` would require minting an API key, which is out
    of scope for this smoke test. Both endpoints funnel through
    ``send_request(method=...)`` so they exercise the override twice
    with different method names."""
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
            _poll_http(f"{base}/status", timeout=15.0)  # wait for uvicorn

            for path in ("/status", "/ping"):
                response = httpx.get(f"{base}{path}", timeout=5.0)
                assert response.status_code == 200, (path, response.text)
                body = response.json()
                assert isinstance(body, dict) and body, (path, body)
                assert body.get("manager_state") == "idle", (path, body)
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
    # Conversely, the split-process log line must NOT have fired —
    # it would indicate the httpserver fell back to building a fresh
    # ZMQ client instead of using the injected in-process one.
    assert "Connecting to RE Manager" not in stderr, (
        "in-process client was injected but httpserver still built a ZMQ client"
    )


class _FakeManager:
    """Minimal stand-in for the manager that InProcessREManagerAPI needs."""

    def __init__(self):
        self.calls = []

        async def status_handler(manager, params):
            return {"success": True, "msg": "", "manager_state": "idle", "params": params}

        async def failing_handler(manager, params):
            raise RuntimeError("boom")

        self._command_handlers = {
            "status": status_handler,
            "boom": failing_handler,
        }


@pytest.mark.asyncio
async def test_inprocess_client_dispatches_into_command_handlers():
    manager = _FakeManager()
    rm = InProcessREManagerAPI(
        manager=manager,
        zmq_control_addr="tcp://127.0.0.1:1",  # arbitrary; never connected
        zmq_info_addr="tcp://127.0.0.1:2",
        zmq_encoding="json",
        request_fail_exceptions=False,
    )

    response = await rm.send_request(method="status", params={"k": "v"})
    assert response["success"] is True
    assert response["params"] == {"k": "v"}
    assert response["manager_state"] == "idle"
    assert rm._inprocess_request_count == 1

    unknown = await rm.send_request(method="does_not_exist", params={})
    assert unknown["success"] is False
    assert "Unknown method" in unknown["msg"]
    assert rm._inprocess_request_count == 2

    # Handler exceptions are surfaced as failed responses, not raised past
    # send_request — matches _zmq_execute's catch-all.
    failed = await rm.send_request(method="boom", params={})
    assert failed["success"] is False
    assert failed["msg"] == "boom"
    assert rm._inprocess_request_count == 3
