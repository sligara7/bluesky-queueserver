"""
Co-hosted HTTP server support for the RE Manager process (U1 unified mode).

When enabled, the manager schedules ``uvicorn.Server(...).serve()`` as a
background asyncio task alongside its 0MQ server. The bluesky-httpserver
FastAPI app is built via ``bluesky_httpserver.app.build_app`` — unchanged
from the split-process deployment. Its internal REManagerAPI client still
speaks 0MQ; in unified mode it just loopbacks to the same process. Phase
U2 will replace the loopback with direct in-process handler calls.

Nothing at module scope pulls in ``uvicorn`` or ``bluesky_httpserver``; the
legacy (HTTP-disabled) path never imports them.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 60610
SHUTDOWN_TIMEOUT_SECONDS = 10.0


@dataclasses.dataclass(frozen=True)
class HttpServerSettings:
    """Parsed ``http_server`` section of the manager configuration."""

    enabled: bool = False
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    config_path: Optional[str] = None

    @classmethod
    def from_config_dict(cls, section: Optional[Dict[str, Any]]) -> "HttpServerSettings":
        if not section:
            return cls()
        enabled = bool(section.get("enabled", False))
        if not enabled:
            return cls(enabled=False)
        host = section.get("host") or DEFAULT_HOST
        port = int(section.get("port") or DEFAULT_PORT)
        if not (1 <= port <= 65535):
            raise ValueError(f"http_server.port must be in [1, 65535] (got {port!r})")
        config_path = section.get("config_path") or None
        return cls(enabled=True, host=host, port=port, config_path=config_path)


def _bind_addr_to_connect_addr(bind_addr: str) -> str:
    """Turn a 0MQ bind address (``tcp://*:60615``) into a loopback connect
    address (``tcp://127.0.0.1:60615``) the FastAPI REManagerAPI client can
    use to reach the same process. Non-TCP addresses pass through."""
    match = re.fullmatch(r"tcp://([^:]+):(\d+)", bind_addr)
    if not match:
        return bind_addr
    host = match.group(1)
    if host in ("*", "0.0.0.0"):
        host = "127.0.0.1"
    return f"tcp://{host}:{match.group(2)}"


class InProcessREManagerAPI:
    """REManagerAPI subclass that short-circuits the 0MQ CONTROL round-trip.

    In unified mode (U1) the httpserver's FastAPI handlers reach the
    manager by msgpack-encoding a request, sending it over 0MQ loopback
    to the same process, and decoding the reply. Phase U2 eliminates
    that hop: every public ``REManagerAPI.<method>()`` call funnels
    through ``send_request(method=..., params=...)``; we override that
    one seam to dispatch directly into ``manager._command_handlers``
    and await the handler coroutine. All ~56 public methods then work
    without reimplementation.

    The 0MQ CONTROL socket stays bound on the manager side so external
    ``qserver``-CLI clients keep working per the backwards-compat
    contract. The INFO/PUB channel is untouched — console-output
    streaming still subscribes via the parent class's monitor machinery.

    ``_inprocess_request_count`` is exposed for integration tests to
    assert the loopback bypass actually fires per HTTP call.
    """

    # Class is defined at module level but we construct it lazily inside
    # CoHostedHttpServer.start to keep bluesky_queueserver_api off the
    # module's import graph when HTTP is disabled.
    _cls_cache: Any = None

    def __new__(cls, *args, **kwargs):
        if cls._cls_cache is None:
            from bluesky_queueserver_api.zmq.aio import REManagerAPI as _RM

            class _InProcessRM(_RM):
                def __init__(self, *, manager, **rm_kwargs):
                    super().__init__(**rm_kwargs)
                    self._manager = manager
                    self._inprocess_request_count = 0

                async def send_request(self, *, method, params=None):
                    self._inprocess_request_count += 1
                    handler = self._manager._command_handlers.get(method)
                    if handler is None:
                        response = {
                            "success": False,
                            "msg": f"Unknown method {method!r}",
                        }
                    else:
                        try:
                            response = await handler(self._manager, params or {})
                        except Exception as ex:  # noqa: BLE001
                            response = {"success": False, "msg": str(ex)}
                    self._check_response(
                        request={"method": method, "params": params},
                        response=response,
                    )
                    return response

            cls._cls_cache = _InProcessRM
        return cls._cls_cache(*args, **kwargs)


class CoHostedHttpServer:
    """Owns the lifecycle of a uvicorn.Server co-running with the manager.

    Start with ``await start()`` after the manager's 0MQ socket is bound
    (so the in-app REManagerAPI client can connect); stop with
    ``await stop()`` before the 0MQ socket closes (so in-flight HTTP→0MQ
    round-trips drain cleanly).
    """

    def __init__(
        self,
        settings: HttpServerSettings,
        *,
        manager: Any,
        manager_zmq_bind_addr: str,
    ) -> None:
        if not settings.enabled:
            raise ValueError(
                "CoHostedHttpServer constructed with disabled settings — "
                "this is a caller bug; guard on settings.enabled"
            )
        self._settings = settings
        self._manager = manager
        self._manager_zmq_connect_addr = _bind_addr_to_connect_addr(manager_zmq_bind_addr)
        self._server: Any = None  # uvicorn.Server
        self._task: Optional[asyncio.Task] = None
        self._rm_client: Any = None  # InProcessREManagerAPI — exposed for tests

    async def start(self) -> None:
        import uvicorn
        from bluesky_httpserver.app import build_app
        from bluesky_httpserver.config import construct_build_app_kwargs, parse_configs

        if self._settings.config_path:
            hs_config = parse_configs(self._settings.config_path)
            build_kwargs = construct_build_app_kwargs(
                hs_config, source_filepath=self._settings.config_path
            )
        else:
            build_kwargs = construct_build_app_kwargs({})

        server_settings = build_kwargs.setdefault("server_settings", {})
        zmq_conf = server_settings.setdefault("qserver_zmq_configuration", {})
        zmq_conf.setdefault("control_address", self._manager_zmq_connect_addr)

        # In-process REManagerAPI — dispatches into manager._command_handlers
        # directly; the 0MQ CONTROL address above is still wired so the
        # underlying client can initialize console / system-info monitors
        # against the INFO/PUB channel, but the CONTROL hot path is bypassed.
        self._rm_client = InProcessREManagerAPI(
            manager=self._manager,
            zmq_control_addr=self._manager_zmq_connect_addr,
            zmq_info_addr=zmq_conf.get("info_address"),
            zmq_encoding=zmq_conf.get("encoding"),
            zmq_public_key=zmq_conf.get("public_key"),
            request_fail_exceptions=False,
            status_expiration_period=0.4,
            console_monitor_max_lines=2000,
        )
        build_kwargs["rm_client"] = self._rm_client

        app = build_app(**build_kwargs)

        config = uvicorn.Config(
            app,
            host=self._settings.host,
            port=self._settings.port,
            log_level="info",
            lifespan="on",
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.ensure_future(self._server.serve())
        logger.info(
            "Co-hosted HTTP server starting on %s:%d (manager 0MQ at %s)",
            self._settings.host,
            self._settings.port,
            self._manager_zmq_connect_addr,
        )

    async def stop(self) -> None:
        if self._server is None or self._task is None:
            return
        self._server.should_exit = True
        try:
            await asyncio.wait_for(self._task, timeout=SHUTDOWN_TIMEOUT_SECONDS)
            logger.info("Co-hosted HTTP server stopped cleanly")
        except asyncio.TimeoutError:
            logger.warning(
                "Co-hosted HTTP server did not exit within %.1fs; cancelling",
                SHUTDOWN_TIMEOUT_SECONDS,
            )
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        finally:
            self._server = None
            self._task = None
