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
